"""
APEX TRADER — Tuner Agent

The single place where ALL auto-tuning is coordinated. Every component that
wants to be tuned registers here; the agent resolves dependency order, decides
who is due, executes them in order, validates the result against each
component's own safety bounds, rolls back on failure, and writes every action
to a persistent audit log.

Why this exists: tuning used to be scattered — AdaptiveOptimizer on its own
50-trade / 7-day trigger, GateTuner on an independent 6h timer, the planner
Calibrator on its own threshold, signal grading per scan cycle. Nothing
ordered them (EVEstimator should read fresh PairLearner output; the gate tuner
tunes the gate the EV estimate feeds), nothing validated them centrally, and
nothing left an audit trail. The agent fixes all of that without changing any
tuner's internal logic — it only owns the scheduling, validation, and audit.

Safety first:
  * A tunable that raises is caught, rolled back, and logged — never crashes
    the caller. The trading loop is never blocked by a tuner fault.
  * After ``max_consecutive_failures`` a tunable is disabled with a loud
    warning instead of being retried forever.
  * If the agent itself faults, existing params are left untouched (fail-open).

Leaf-ish module — stdlib + loguru + adaptive.tunable only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from adaptive.tunable import TuneContext, TuneFrequency, TuneResult, Tunable

# Per-user writeable state — the tuner audit DB resolves under the owning user's
# data tree (APEX_DATA_DIR) rather than a bare cwd-relative ``data/`` path.
from runtime_paths import data_dir as _data_dir


# Canonical names of every component that should be visible to the agent when
# it owns tuning — the 19 from the learning-layer audit. Used by
# get_system_tuning_status() to flag any expected component that did not
# register (e.g. because its own config flag is off, or wiring is missing).
EXPECTED_TUNABLES: tuple[str, ...] = (
    "adaptive_optimizer",   # 1  — coordinator (run_optimization guarded)
    "score_optimizer",      # 2  — confluence scoring weights
    "regime_learner",       # 3  — per-regime TP/SL/threshold
    "pair_learner",         # 4  — per-pair sizing
    "session_learner",      # 5  — per-session aggression
    "trade_analyzer",       # 6  — losing-pattern block list
    "ev_estimator",         # 7  — EV gate history
    "gate_tuner",           # 8  — quality-gate offsets
    "win_rate_provider",    # 9  — ranker win-probability source
    "signal_ledger",        # 10 — universal signal grading
    "emitter_feedback",     # 11 — per-emitter accuracy (read-side)
    "post_close_tracker",   # 12 — MFE/MAE post-close checks
    "planner_calibrator",   # 13 — PlannerConfig calibration
    "vote_calibrator",      # 13b — consensus vote weights from emitter accuracy
    "risk_engine",          # 14 — sizing chain (consumer)
    "position_sizer",       # 15 — risk% -> lots (consumer)
    "orchestrator",         # 16 — bounded size multiplier (consumer)
    "portfolio_governor",   # 17 — exposure limits (consumer)
    "trade_manager",        # 18 — SL/TP/partial/trail (consumer)
    "rl_stack",             # 19 — RL augmentation/veto (dormant)
)


_CREATE_AUDIT = """
CREATE TABLE IF NOT EXISTS tuner_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    tunable_name TEXT NOT NULL,
    success INTEGER NOT NULL,
    params_before TEXT,
    params_after TEXT,
    reason TEXT,
    duration_ms REAL,
    error TEXT,
    rollback_performed INTEGER,
    skipped INTEGER,
    changed INTEGER
)
"""
_CREATE_AUDIT_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_tuner_audit_name "
    "ON tuner_audit(tunable_name, timestamp)"
)


class _TunableState:
    """Per-tunable bookkeeping the agent maintains (never the tunable itself)."""

    __slots__ = (
        "last_tune_time", "trades_at_last_tune", "tune_count",
        "last_success", "consecutive_failures", "disabled",
    )

    def __init__(self) -> None:
        self.last_tune_time: Optional[float] = None
        self.trades_at_last_tune: int = 0
        self.tune_count: int = 0
        self.last_success: Optional[bool] = None
        self.consecutive_failures: int = 0
        self.disabled: bool = False


class TunerAgent:
    """Central coordinator for all registered :class:`Tunable` components."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        audit_db_path: str | None = None,
        max_tune_duration_seconds: float = 30.0,
        max_consecutive_failures: int = 3,
        log_all_skips: bool = False,
    ) -> None:
        self.enabled = bool(enabled)
        self._max_duration = float(max_tune_duration_seconds)
        self._max_failures = max(1, int(max_consecutive_failures))
        self._log_all_skips = bool(log_all_skips)

        self._tunables: dict[str, Tunable] = {}
        self._state: dict[str, _TunableState] = {}
        self._lock = threading.RLock()

        # Sole-authority enforcement. When enabled, the agent is the only place
        # tuning may happen; a component's own tune entry point checks
        # ``is_authorizing`` (set only while the agent is driving that very
        # tune) and otherwise records a bypass attempt. Thread-local so a
        # concurrent caller on another thread can never see the agent's own
        # authorisation window.
        self._authorizing = threading.local()
        self._bypass_log: list[dict] = []
        self._max_bypass_log = 200

        self._db_path = Path(audit_db_path) if audit_db_path else (_data_dir() / "tuner_audit.db")
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ── Registration ────────────────────────────────────────────────────

    def register(self, tunable: Tunable) -> None:
        """Register a tunable. Re-registering the same name replaces it."""
        name = tunable.tunable_name
        if not name:
            raise ValueError("Tunable.tunable_name must be a non-empty string")
        with self._lock:
            self._tunables[name] = tunable
            self._state.setdefault(name, _TunableState())
        logger.debug("[tuner-agent] registered tunable '{}'", name)

    def unregister(self, name: str) -> bool:
        with self._lock:
            existed = self._tunables.pop(name, None) is not None
            self._state.pop(name, None)
        return existed

    @property
    def registered_names(self) -> list[str]:
        with self._lock:
            return list(self._tunables.keys())

    def reset_failure_count(self, name: str) -> bool:
        """Re-enable a tunable that was auto-disabled after repeated failures.

        Also the release path for a Governance ``freeze_tunable`` containment."""
        with self._lock:
            st = self._state.get(name)
            if st is None:
                return False
            st.consecutive_failures = 0
            st.disabled = False
        logger.info("[tuner-agent] '{}' failure count reset — re-enabled", name)
        return True

    def freeze_tunable(self, name: str, *, reason: str = "") -> bool:
        """Disable a tunable on a Governance containment order.

        Governance (Department ⑧) decides *that* a runaway tunable must stop;
        the agent is the enforcement arm that *executes* it — flipping the same
        ``disabled`` flag the failure-tripwire uses (honoured in ``_run_batch``,
        so a frozen tunable is skipped on every cycle) and writing an audit row.
        Reverse with :meth:`reset_failure_count`.  Returns False for an unknown
        tunable.  Never raises.
        """
        with self._lock:
            st = self._state.get(name)
            if st is None:
                logger.warning("[tuner-agent] freeze_tunable unknown tunable '{}'", name)
                return False
            already = st.disabled
            st.disabled = True
        if not already:
            logger.warning(
                "[tuner-agent] '{}' FROZEN by governance containment — {}",
                name, reason or "no reason given",
            )
            self._write_audit(TuneResult(
                tunable_name=str(name),
                success=False,
                reason=f"frozen by governance: {reason}" if reason else "frozen by governance",
                error="governance_containment",
            ))
        return True

    # ── Sole-authority enforcement ──────────────────────────────────────

    @property
    def is_sole_authority(self) -> bool:
        """True when the agent owns ALL tuning — direct self-tune is blocked."""
        return self.enabled

    def is_authorizing(self) -> bool:
        """True only while the agent is itself driving a tune on this thread.

        A guarded component calls this to tell an agent-driven delegated tune
        (allowed) apart from a rogue direct call (blocked).
        """
        return getattr(self._authorizing, "depth", 0) > 0

    def _enter_authorization(self) -> None:
        self._authorizing.depth = getattr(self._authorizing, "depth", 0) + 1

    def _exit_authorization(self) -> None:
        self._authorizing.depth = max(0, getattr(self._authorizing, "depth", 0) - 1)

    def log_bypass_attempt(self, component: str, caller: str) -> None:
        """Record (and loudly log) a component trying to self-tune behind the
        agent's back. Called by ``TuningGuardMixin`` when it blocks a call."""
        entry = {
            "timestamp": time.time(),
            "component": str(component),
            "caller": str(caller),
        }
        with self._lock:
            self._bypass_log.append(entry)
            if len(self._bypass_log) > self._max_bypass_log:
                self._bypass_log = self._bypass_log[-self._max_bypass_log:]
        logger.warning(
            "[tuner-agent] TUNING BYPASS BLOCKED — {}.{}() called directly while "
            "the agent is sole authority; ignored. Route tuning through the agent.",
            component, caller,
        )
        # Persist as an audit row so bypasses show up in the trail too.
        self._write_audit(TuneResult(
            tunable_name=str(component),
            success=False,
            reason=f"bypass blocked: direct {component}.{caller}()",
            error="tuning_bypass_attempt",
        ))

    def get_bypass_attempts(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._bypass_log[-int(limit):])

    # ── Dependency resolution (topological sort) ────────────────────────

    def _resolve_order(self, names: list[str]) -> list[str]:
        """Topologically sort ``names`` by declared dependencies.

        Only dependencies that are themselves in ``names`` constrain ordering;
        a dependency that is registered but not in this batch, or not
        registered at all, is treated as already-satisfied (it simply does not
        impose an edge). Raises ``ValueError`` on a circular dependency.
        """
        in_set = set(names)
        # Build adjacency (dep -> dependents) limited to the batch.
        deps: dict[str, set[str]] = {n: set() for n in names}
        for n in names:
            tunable = self._tunables.get(n)
            if tunable is None:
                continue
            for d in getattr(tunable, "dependencies", []) or []:
                if d in in_set:
                    deps[n].add(d)

        ordered: list[str] = []
        # states: 0 = unvisited, 1 = visiting (on stack), 2 = done
        state: dict[str, int] = {n: 0 for n in names}

        def _visit(node: str, stack: list[str]) -> None:
            s = state[node]
            if s == 2:
                return
            if s == 1:
                cycle = " -> ".join(stack + [node])
                raise ValueError(f"Circular tunable dependency detected: {cycle}")
            state[node] = 1
            for dep in sorted(deps[node]):
                _visit(dep, stack + [node])
            state[node] = 2
            ordered.append(node)

        for n in sorted(names):
            if state[n] != 2:
                _visit(n, [])
        return ordered

    # ── Trigger entry points ────────────────────────────────────────────

    def on_trade_close(self, ctx: TuneContext) -> list[TuneResult]:
        """Run tunables that react to closed trades (ON_TRADE_CLOSE / ON_TRADE_BATCH)."""
        return self._run_for_frequencies(
            (TuneFrequency.ON_TRADE_CLOSE, TuneFrequency.ON_TRADE_BATCH),
            ctx,
            label="trade_close",
        )

    def on_scan_cycle(self, ctx: TuneContext) -> list[TuneResult]:
        """Run per-scan-cycle tunables (e.g. signal grading)."""
        return self._run_for_frequencies(
            (TuneFrequency.PER_SCAN_CYCLE,), ctx, label="scan_cycle",
        )

    def on_periodic_tick(self, ctx: TuneContext) -> list[TuneResult]:
        """Run time-based tunables (e.g. the 6h gate tuner)."""
        return self._run_for_frequencies(
            (TuneFrequency.PERIODIC,), ctx, label="periodic",
        )

    def force_tune_all(self, ctx: TuneContext) -> list[TuneResult]:
        """Force-retune every registered tunable, in dependency order."""
        forced = TuneContext(
            total_trades=ctx.total_trades,
            trades_since_last_tune=ctx.trades_since_last_tune,
            seconds_since_last_tune=ctx.seconds_since_last_tune,
            current_prices=ctx.current_prices,
            force=True,
        )
        with self._lock:
            names = list(self._tunables.keys())
        return self._run_batch(names, forced, label="force_all")

    def force_tune(self, name: str, ctx: TuneContext) -> Optional[TuneResult]:
        """Force-retune a single tunable by name (bypasses should_tune)."""
        with self._lock:
            if name not in self._tunables:
                logger.warning("[tuner-agent] force_tune unknown tunable '{}'", name)
                return None
        forced = TuneContext(
            total_trades=ctx.total_trades,
            trades_since_last_tune=ctx.trades_since_last_tune,
            seconds_since_last_tune=ctx.seconds_since_last_tune,
            current_prices=ctx.current_prices,
            force=True,
        )
        results = self._run_batch([name], forced, label="force_one")
        return results[0] if results else None

    # ── Core execution ──────────────────────────────────────────────────

    def _run_for_frequencies(
        self, freqs: tuple[TuneFrequency, ...], ctx: TuneContext, *, label: str,
    ) -> list[TuneResult]:
        if not self.enabled:
            return []
        with self._lock:
            names = [
                n for n, t in self._tunables.items()
                if getattr(t, "frequency", None) in freqs
            ]
        if not names:
            return []
        return self._run_batch(names, ctx, label=label)

    def _run_batch(
        self, names: list[str], ctx: TuneContext, *, label: str,
    ) -> list[TuneResult]:
        """Resolve order, then execute each candidate honouring dependencies."""
        try:
            order = self._resolve_order(names)
        except ValueError as exc:
            # A cyclic graph is a programming error — surface it loudly but
            # never crash the trading loop.
            logger.error("[tuner-agent] dependency resolution failed ({}): {}", label, exc)
            return []

        results: list[TuneResult] = []
        succeeded: set[str] = set()
        blocked: set[str] = set()    # skipped because a dependency failed/was skipped

        for name in order:
            tunable = self._tunables.get(name)
            if tunable is None:
                continue
            st = self._state[name]

            if st.disabled:
                if self._log_all_skips:
                    logger.debug("[tuner-agent] '{}' is disabled — skipping", name)
                continue

            # Dependency gate: if any in-batch dependency did not succeed, skip.
            unmet = [
                d for d in (getattr(tunable, "dependencies", []) or [])
                if d in order and d not in succeeded
            ]
            if unmet:
                blocked.add(name)
                res = TuneResult(
                    tunable_name=name, success=False, skipped=True,
                    reason=f"dependency not satisfied: {', '.join(sorted(unmet))}",
                )
                results.append(res)
                if self._should_audit(res):
                    self._write_audit(res)
                logger.debug(
                    "[tuner-agent] '{}' skipped — unmet dependencies: {}",
                    name, unmet,
                )
                continue

            # should_tune gate (unless forced).
            if not ctx.force:
                try:
                    due = bool(tunable.should_tune(ctx))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[tuner-agent] '{}' should_tune raised: {}", name, exc)
                    due = False
                if not due:
                    if self._log_all_skips:
                        logger.debug("[tuner-agent] '{}' not due — skipping", name)
                    continue

            res = self._execute_one(tunable, ctx)
            results.append(res)
            if res.success and not res.skipped:
                succeeded.add(name)

        return results

    def _execute_one(self, tunable: Tunable, ctx: TuneContext) -> TuneResult:
        """Snapshot → tune → validate → (rollback on failure) → audit."""
        name = tunable.tunable_name
        st = self._state[name]
        before = self._safe_params(tunable)
        start = time.monotonic()
        result: TuneResult
        try:
            # Open the authorisation window so the component's own guard lets
            # this agent-driven (delegated) call through, then close it again.
            self._enter_authorization()
            try:
                result = tunable.tune(ctx)
            finally:
                self._exit_authorization()
            if not isinstance(result, TuneResult):
                # A tunable that returns the wrong type is treated as a fault.
                raise TypeError(
                    f"tune() must return TuneResult, got {type(result).__name__}"
                )
            result.tunable_name = name
            if not result.params_before:
                result.params_before = before
            if not result.params_after:
                result.params_after = self._safe_params(tunable)

            # Validate the post-tune params — invalid output is rolled back.
            if result.success and not result.skipped:
                ok, why = self._safe_validate(tunable, result.params_after)
                if not ok:
                    rolled = self._safe_rollback(tunable)
                    result.success = False
                    result.rollback_performed = rolled
                    result.error = f"validation failed: {why}"
                    result.reason = (result.reason + " | " if result.reason else "") + (
                        f"rolled back ({why})" if rolled else f"validation failed ({why})"
                    )
                    logger.warning(
                        "[tuner-agent] '{}' produced invalid params — {} (rollback={})",
                        name, why, rolled,
                    )
        except Exception as exc:  # noqa: BLE001
            rolled = self._safe_rollback(tunable)
            result = TuneResult(
                tunable_name=name,
                success=False,
                params_before=before,
                params_after=self._safe_params(tunable),
                reason="exception during tune",
                error=str(exc),
                rollback_performed=rolled,
            )
            logger.warning(
                "[tuner-agent] '{}' tune raised: {} (rollback={})", name, exc, rolled,
            )

        result.duration_ms = (time.monotonic() - start) * 1000.0
        if result.duration_ms > self._max_duration * 1000.0:
            # Cannot safely hard-kill arbitrary sync code mid-flight (a thread
            # cannot be interrupted without risking a half-written DB); we flag
            # the overrun loudly so ops can investigate / split the work.
            logger.warning(
                "[tuner-agent] '{}' exceeded max duration ({:.0f}ms > {:.0f}ms)",
                name, result.duration_ms, self._max_duration * 1000.0,
            )

        # Update bookkeeping + failure tracking.
        with self._lock:
            st.last_tune_time = time.time()
            st.trades_at_last_tune = ctx.total_trades
            st.last_success = result.success
            if not result.skipped:
                st.tune_count += 1
                if result.success:
                    st.consecutive_failures = 0
                else:
                    st.consecutive_failures += 1
                    if st.consecutive_failures >= self._max_failures:
                        st.disabled = True
                        logger.error(
                            "[tuner-agent] '{}' DISABLED after {} consecutive "
                            "failures — call reset_failure_count to re-enable",
                            name, st.consecutive_failures,
                        )

        self._write_audit_if_relevant(result)
        if result.success and result.changed:
            logger.info(
                "[tuner-agent] '{}' tuned — {} ({:.0f}ms)",
                name, result.reason or "params updated", result.duration_ms,
            )
        return result

    def _should_audit(self, result: TuneResult) -> bool:
        """Keep the audit DB bounded: skip pure no-ops (e.g. per-scan grading
        cycles that graded nothing) unless verbose logging is on. Always record
        failures, rollbacks, and real changes."""
        if self._log_all_skips:
            return True
        if result.error or result.rollback_performed or result.changed:
            return True
        if result.skipped:
            return False
        return True

    def _write_audit_if_relevant(self, result: TuneResult) -> None:
        if self._should_audit(result):
            self._write_audit(result)

    # ── Safe wrappers (never raise) ─────────────────────────────────────

    @staticmethod
    def _safe_params(tunable: Tunable) -> dict:
        try:
            params = tunable.get_current_params()
            return dict(params) if isinstance(params, dict) else {"value": params}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] get_current_params failed: {}", exc)
            return {}

    @staticmethod
    def _safe_validate(tunable: Tunable, params: dict) -> tuple[bool, str]:
        try:
            ok, why = tunable.validate_params(params)
            return bool(ok), str(why)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] validate_params raised: {}", exc)
            # A validator that itself faults must not silently pass bad params.
            return False, f"validator error: {exc}"

    @staticmethod
    def _safe_rollback(tunable: Tunable) -> bool:
        try:
            return bool(tunable.rollback())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tuner-agent] rollback raised: {}", exc)
            return False

    # ── Audit log (SQLite) ──────────────────────────────────────────────

    def _init_db(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tuner-agent] could not create audit dir: {}", exc)
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_AUDIT)
            self._conn.execute(_CREATE_AUDIT_IDX)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tuner-agent] audit DB init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def _write_audit(self, result: TuneResult) -> None:
        if self._conn is None:
            return
        row = result.to_row()
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO tuner_audit
                       (timestamp, tunable_name, success, params_before,
                        params_after, reason, duration_ms, error,
                        rollback_performed, skipped, changed)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        time.time(),
                        row["tunable_name"], row["success"], row["params_before"],
                        row["params_after"], row["reason"], row["duration_ms"],
                        row["error"], row["rollback_performed"], row["skipped"],
                        row["changed"],
                    ),
                )
                # Keep the audit table bounded — only the most recent 500 rows
                # are retained (other adaptive DBs trim similarly). Prevents
                # unbounded growth over a long-running session.
                self._conn.execute(
                    """DELETE FROM tuner_audit WHERE id NOT IN
                       (SELECT id FROM tuner_audit ORDER BY id DESC LIMIT 500)"""
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[tuner-agent] audit write failed: {}", exc)

    def get_audit_log(
        self, tunable_name: Optional[str] = None, limit: int = 100,
    ) -> list[dict]:
        """Return recent audit rows (newest first), optionally filtered by name."""
        if self._conn is None:
            return []
        with self._lock:
            try:
                if tunable_name:
                    cur = self._conn.execute(
                        "SELECT * FROM tuner_audit WHERE tunable_name=? "
                        "ORDER BY id DESC LIMIT ?",
                        (tunable_name, int(limit)),
                    )
                else:
                    cur = self._conn.execute(
                        "SELECT * FROM tuner_audit ORDER BY id DESC LIMIT ?",
                        (int(limit),),
                    )
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[tuner-agent] audit read failed: {}", exc)
                return []
        for r in rows:
            for key in ("params_before", "params_after"):
                r[key] = _loads(r.get(key))
        return rows

    def get_last_tune_time(self, tunable_name: str) -> Optional[float]:
        with self._lock:
            st = self._state.get(tunable_name)
            return st.last_tune_time if st else None

    def get_tuner_status(self) -> dict:
        """Summary of every registered tunable for the dashboard / ops."""
        out: dict[str, dict] = {}
        with self._lock:
            items = list(self._tunables.items())
            states = {n: self._state.get(n) for n in self._tunables}
        for name, tunable in items:
            st = states.get(name) or _TunableState()
            out[name] = {
                "frequency": getattr(getattr(tunable, "frequency", None), "value", ""),
                "dependencies": list(getattr(tunable, "dependencies", []) or []),
                "min_trades_required": int(getattr(tunable, "min_trades_required", 0) or 0),
                "min_interval_seconds": float(getattr(tunable, "min_interval_seconds", 0) or 0),
                "last_tune_time": st.last_tune_time,
                "tune_count": st.tune_count,
                "last_success": st.last_success,
                "consecutive_failures": st.consecutive_failures,
                "disabled": st.disabled,
                "current_params": self._safe_params(tunable),
            }
        return out

    def get_system_tuning_status(self) -> dict:
        """Complete system tuning state — every registered tunable plus the
        expected components that did NOT register, recent bypass attempts, and
        the agent's authority flags. This is the one place ops / the dashboard
        can see who is under the agent and who is missing."""
        registered = self.get_tuner_status()
        expected = list(EXPECTED_TUNABLES)
        missing = [n for n in expected if n not in registered]
        return {
            "agent_enabled": bool(self.enabled),
            "is_sole_authority": bool(self.is_sole_authority),
            "expected_count": len(expected),
            "registered_count": len(registered),
            "registered_tunables": registered,
            "unregistered_expected": missing,
            "bypass_attempts": self.get_bypass_attempts(50),
        }

    def validate_registry(self) -> dict:
        """Startup check: warn about any expected component that is missing and
        about any registered tunable whose current params fail their own
        validator. Returns a summary dict (also logged). Never raises."""
        with self._lock:
            items = list(self._tunables.items())
        registered_names = {n for n, _ in items}
        missing = [n for n in EXPECTED_TUNABLES if n not in registered_names]
        invalid: list[str] = []
        for name, tunable in items:
            params = self._safe_params(tunable)
            ok, why = self._safe_validate(tunable, params)
            if not ok:
                invalid.append(f"{name}: {why}")
        if missing:
            logger.warning(
                "[tuner-agent] {} expected tunable(s) NOT registered: {}",
                len(missing), ", ".join(missing),
            )
        if invalid:
            logger.warning(
                "[tuner-agent] {} tunable(s) report invalid params at startup: {}",
                len(invalid), "; ".join(invalid),
            )
        logger.info(
            "[tuner-agent] registry validated — {}/{} expected components under "
            "the agent",
            len(registered_names), len(EXPECTED_TUNABLES),
        )
        return {
            "registered": sorted(registered_names),
            "missing": missing,
            "invalid": invalid,
        }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[tuner-agent] audit conn close failed")
                self._conn = None


def _loads(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except Exception:  # noqa: BLE001
        return {}


__all__ = ["TunerAgent", "EXPECTED_TUNABLES"]
