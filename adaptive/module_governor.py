"""
APEX TRADER — Module Governor (L3: Shadow Mode + auto-reactivation)

A voting module (structure, momentum, order blocks, liquidity, …) used to have
only two fates: it ran and influenced every decision, or — once the TunerAgent
3-strike-disabled the component behind it — it was fully OFF.  There was no
middle ground in which a struggling module could keep being *measured* without
being allowed to do damage.

This governor adds that middle ground.  Each module sits in one of three modes:

* ``ACTIVE``   — votes normally; its calibrated weight is used as-is.
* ``SHADOW``   — still runs and is still graded by the SignalLedger, but its
  vote weight is forced to 0.0 so it cannot influence a live decision.  We keep
  watching it; if its accuracy recovers it returns to ACTIVE, and if it stays
  poor it is fully disabled.
* ``DISABLED`` — statistically harmful; suppressed entirely (also weight 0.0).
  Re-entry is manual, or automatic after ``auto_retry_days`` if configured.

The transitions are driven purely by graded accuracy READ FROM the read-only
:class:`~adaptive.emitter_feedback.EmitterFeedbackService` — the governor never
grades signals or computes its own accuracy.  It only owns the *policy*: which
mode a module belongs in given its track record, and the audit trail of every
move.

Safety properties:

* **Inert unless enabled.**  When ``module_governor_enabled`` is off,
  :meth:`is_suppressed` always returns ``False`` and nothing is ever shadowed —
  byte-for-byte the legacy behaviour.
* **Sample floors.**  A module is never shadowed/disabled until it has enough
  graded signals; a low-data module is left ACTIVE (we do not punish silence).
* **Reads, never writes, the ledger.**  Accuracy comes from EmitterFeedback;
  this module never touches how signals are recorded or graded.
* **Hot path is lock-free.**  Mode lookups (:meth:`is_suppressed`) read an
  atomically-published dict, so the scanner pays no DB / lock cost per vote.
* **Guarded core op.**  :meth:`evaluate_transitions` defers to a central
  ``TunerAgent`` when one is sole authority (via :class:`TuningGuardMixin`).

Storage mirrors the proven sqlite3-in-WAL pattern used across the adaptive
layer: exception-safe, survives restarts.

Leaf module — standard library + loguru + adaptive.tunable only.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from adaptive.tunable import TuningGuardMixin

# The nine brain modules that cast directional votes (matches the emitter names
# the SignalLedger records and the keys in ConsensusConfig.weights). "consensus"
# is deliberately excluded — it is the derived verdict, not an organ to govern.
DEFAULT_GOVERNED_MODULES: tuple[str, ...] = (
    "structure",
    "currency_strength",
    "wyckoff",
    "volume",
    "order_block",
    "fvg",
    "liquidity",
    "momentum",
    "vwap",
)

_DB_DIR = Path(__file__).parent.parent / "data"
_DB_PATH = _DB_DIR / "module_governor.db"


class ModuleMode(str, Enum):
    """The mode a governed module sits in (str-valued for trivial JSON/SQL)."""

    ACTIVE = "ACTIVE"
    SHADOW = "SHADOW"
    DISABLED = "DISABLED"


_CREATE_STATE = """
CREATE TABLE IF NOT EXISTS module_governor_state (
    module                TEXT PRIMARY KEY,
    mode                  TEXT NOT NULL DEFAULT 'ACTIVE',
    last_transition_time  REAL NOT NULL DEFAULT 0.0,
    reason                TEXT,
    shadow_start_time     REAL,
    shadow_baseline_signals INTEGER NOT NULL DEFAULT 0,
    disabled_time         REAL,
    accuracy_at_transition REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_TRANSITIONS = """
CREATE TABLE IF NOT EXISTS module_governor_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     REAL NOT NULL,
    module        TEXT NOT NULL,
    old_mode      TEXT NOT NULL,
    new_mode      TEXT NOT NULL,
    reason        TEXT,
    accuracy      REAL NOT NULL DEFAULT 0.0,
    sample_size   INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_TRANS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_mg_trans_ts "
    "ON module_governor_transitions (timestamp)"
)


@dataclass
class ModuleStateRecord:
    """Persistent per-module governance state."""

    module: str
    mode: ModuleMode = ModuleMode.ACTIVE
    last_transition_time: float = 0.0
    reason: str = ""
    shadow_start_time: Optional[float] = None
    shadow_baseline_signals: int = 0
    disabled_time: Optional[float] = None
    accuracy_at_transition: float = 0.0

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "mode": self.mode.value if isinstance(self.mode, ModuleMode) else str(self.mode),
            "last_transition_time": self.last_transition_time,
            "reason": self.reason,
            "shadow_start_time": self.shadow_start_time,
            "shadow_baseline_signals": int(self.shadow_baseline_signals),
            "disabled_time": self.disabled_time,
            "accuracy_at_transition": round(float(self.accuracy_at_transition), 4),
        }


@dataclass
class GovernorTransition:
    """One recorded state change — a row in the transition audit log."""

    module: str
    old_mode: str
    new_mode: str
    reason: str = ""
    accuracy: float = 0.0
    sample_size: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "old_mode": self.old_mode,
            "new_mode": self.new_mode,
            "reason": self.reason,
            "accuracy": round(float(self.accuracy), 4),
            "sample_size": int(self.sample_size),
            "timestamp": self.timestamp,
        }


class ModuleGovernor(TuningGuardMixin):
    """Three-state module governor with auto shadow / reactivate / disable.

    Reads accuracy via an injected, read-only EmitterFeedback service and
    publishes a ``{module: ModuleMode}`` map the scanner consults to suppress a
    shadowed/disabled module's vote.  Thread-safe: the published mode map is an
    immutable dict swapped atomically, so per-vote reads need no lock.
    """

    def __init__(
        self,
        config,
        emitter_feedback=None,
        *,
        db_path: Optional[Path | str] = None,
        modules: Optional[tuple[str, ...] | list[str]] = None,
    ) -> None:
        self._config = config
        self._emitter_feedback = emitter_feedback
        self._modules = tuple(modules) if modules else DEFAULT_GOVERNED_MODULES
        # Atomically-published mode cache for lock-free hot reads.
        self._modes: Dict[str, str] = {m: ModuleMode.ACTIVE.value for m in self._modules}
        # Full state records (mode + bookkeeping), guarded by _lock for writes.
        self._state: Dict[str, ModuleStateRecord] = {}
        self._lock = threading.Lock()
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[module-governor] could not create db dir: {}", exc)
        self._connect()
        self._load_state()

    # ── Lifecycle / persistence ──────────────────────────────────────────

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_STATE)
            self._conn.execute(_CREATE_TRANSITIONS)
            self._conn.execute(_CREATE_TRANS_IDX)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[module-governor] DB connect/init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def _load_state(self) -> None:
        """Hydrate in-memory state + the published mode cache from SQLite."""
        records: Dict[str, ModuleStateRecord] = {}
        if self._conn is not None:
            try:
                cur = self._conn.execute("SELECT * FROM module_governor_state")
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    r = dict(zip(cols, row))
                    mod = str(r.get("module") or "")
                    if not mod:
                        continue
                    records[mod] = ModuleStateRecord(
                        module=mod,
                        mode=_coerce_mode(r.get("mode")),
                        last_transition_time=float(r.get("last_transition_time") or 0.0),
                        reason=str(r.get("reason") or ""),
                        shadow_start_time=_opt_float(r.get("shadow_start_time")),
                        shadow_baseline_signals=int(r.get("shadow_baseline_signals") or 0),
                        disabled_time=_opt_float(r.get("disabled_time")),
                        accuracy_at_transition=float(r.get("accuracy_at_transition") or 0.0),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[module-governor] load_state failed: {}", exc)
        # Ensure every governed module has a record (default ACTIVE).
        for m in self._modules:
            records.setdefault(m, ModuleStateRecord(module=m))
        with self._lock:
            self._state = records
            self._modes = {m: rec.mode.value for m, rec in records.items()}

    def _persist(self, rec: ModuleStateRecord) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO module_governor_state
                   (module, mode, last_transition_time, reason, shadow_start_time,
                    shadow_baseline_signals, disabled_time, accuracy_at_transition)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    rec.module,
                    rec.mode.value if isinstance(rec.mode, ModuleMode) else str(rec.mode),
                    float(rec.last_transition_time or 0.0),
                    rec.reason or "",
                    rec.shadow_start_time,
                    int(rec.shadow_baseline_signals or 0),
                    rec.disabled_time,
                    float(rec.accuracy_at_transition or 0.0),
                ),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[module-governor] persist failed: {}", exc)

    def _record_transition(self, t: GovernorTransition) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """INSERT INTO module_governor_transitions
                   (timestamp, module, old_mode, new_mode, reason, accuracy, sample_size)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    float(t.timestamp),
                    t.module, t.old_mode, t.new_mode, t.reason or "",
                    float(t.accuracy), int(t.sample_size),
                ),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[module-governor] record_transition failed: {}", exc)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[module-governor] conn close failed")
                self._conn = None

    # ── Configuration / wiring ────────────────────────────────────────────

    def set_emitter_feedback(self, emitter_feedback) -> None:
        """Inject (or replace, with ``None``) the read-only feedback source."""
        self._emitter_feedback = emitter_feedback

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._config, "module_governor_enabled", False))

    # ── Hot path: mode lookup (thread-safe, lock-free) ─────────────────────

    def mode_for(self, module: str) -> ModuleMode:
        """Current mode for ``module`` (ACTIVE when unknown / disabled feature)."""
        if not self.enabled:
            return ModuleMode.ACTIVE
        return _coerce_mode(self._modes.get(module, ModuleMode.ACTIVE.value))

    def is_shadowed(self, module: str) -> bool:
        """True when the module is in SHADOW mode (running but vote-suppressed)."""
        return self.mode_for(module) == ModuleMode.SHADOW

    def is_disabled(self, module: str) -> bool:
        """True when the module has been fully DISABLED."""
        return self.mode_for(module) == ModuleMode.DISABLED

    def is_suppressed(self, module: str) -> bool:
        """True when the module must NOT influence a live decision.

        SHADOW and DISABLED both suppress the vote (weight forced to 0.0); only
        ACTIVE modules contribute. Always ``False`` when the feature is off, so
        the consensus path is unchanged. This is the single hook the scanner
        calls per vote.
        """
        m = self.mode_for(module)
        return m in (ModuleMode.SHADOW, ModuleMode.DISABLED)

    # ── Manual override ────────────────────────────────────────────────────

    def force_mode(self, module: str, mode: ModuleMode | str, reason: str = "manual") -> bool:
        """Manually move a module into a mode (e.g. DISABLED → SHADOW retry).

        Records the transition like an automatic one. Returns False if the
        module is unknown. Always safe to call.
        """
        target = _coerce_mode(mode)
        with self._lock:
            rec = self._state.get(module)
            if rec is None:
                return False
            old = rec.mode
            if old == target:
                return True
            self._apply_mode(rec, target, reason=reason, accuracy=rec.accuracy_at_transition,
                             sample_size=0, baseline_signals=0)
        return True

    # ── Core operation: evaluate transitions (guarded) ─────────────────────

    def evaluate_transitions(self) -> List[GovernorTransition]:
        """Re-evaluate every governed module against its graded accuracy and
        move it between ACTIVE / SHADOW / DISABLED as policy dictates.

        Driven periodically by the TunerAgent (and from the trade-close hook
        when the agent is off). Guarded: a direct call while the agent is sole
        authority is blocked so nothing bypasses the central tuner. Never
        raises — any failure leaves the published mode map intact.
        """
        if self._tuning_blocked("evaluate_transitions"):
            return []
        return self._evaluate_unguarded()

    def _evaluate_unguarded(self) -> List[GovernorTransition]:
        if not self.enabled:
            return []
        if self._emitter_feedback is None:
            return []
        transitions: List[GovernorTransition] = []
        for module in self._modules:
            try:
                acc, n = self._accuracy_and_count(module)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[module-governor] accuracy fetch failed for {}: {}", module, exc)
                continue
            t = self._evaluate_one(module, acc, n)
            if t is not None:
                transitions.append(t)
        if transitions:
            logger.info(
                "[module-governor] {} transition(s): {}",
                len(transitions),
                ", ".join(f"{t.module} {t.old_mode}->{t.new_mode}" for t in transitions),
            )
        return transitions

    def _evaluate_one(
        self, module: str, accuracy: float, sample_size: int,
    ) -> Optional[GovernorTransition]:
        """Apply the transition policy for a single module (caller-safe)."""
        cfg = self._config
        shadow_threshold = float(getattr(cfg, "shadow_threshold", 0.35))
        shadow_lookback = int(getattr(cfg, "shadow_lookback", 50))
        reactivation_threshold = float(getattr(cfg, "reactivation_threshold", 0.50))
        reactivation_min = int(getattr(cfg, "reactivation_min_signals", 30))
        disable_threshold = float(getattr(cfg, "disable_threshold", 0.25))
        disable_min = int(getattr(cfg, "disable_min_signals", 50))
        auto_retry_days = int(getattr(cfg, "auto_retry_days", 0))

        with self._lock:
            rec = self._state.get(module)
            if rec is None:
                rec = ModuleStateRecord(module=module)
                self._state[module] = rec
            mode = rec.mode

            if mode == ModuleMode.ACTIVE:
                # Demote to SHADOW once a full trailing window is poor.
                if sample_size >= shadow_lookback and accuracy < shadow_threshold:
                    reason = (
                        f"accuracy {accuracy:.2f} < {shadow_threshold:.2f} "
                        f"over {sample_size} signals"
                    )
                    return self._apply_mode(
                        rec, ModuleMode.SHADOW, reason=reason, accuracy=accuracy,
                        sample_size=sample_size, baseline_signals=sample_size,
                    )
                return None

            if mode == ModuleMode.SHADOW:
                # Only count signals accrued DURING the shadow period.
                shadow_n = max(0, sample_size - int(rec.shadow_baseline_signals))
                # Recovered → reactivate.
                if shadow_n >= reactivation_min and accuracy >= reactivation_threshold:
                    reason = (
                        f"recovered to {accuracy:.2f} >= {reactivation_threshold:.2f} "
                        f"over {shadow_n} shadow signals"
                    )
                    return self._apply_mode(
                        rec, ModuleMode.ACTIVE, reason=reason, accuracy=accuracy,
                        sample_size=shadow_n, baseline_signals=0,
                    )
                # Still harmful after enough shadow signals → disable.
                if shadow_n >= disable_min and accuracy < disable_threshold:
                    reason = (
                        f"stayed at {accuracy:.2f} < {disable_threshold:.2f} "
                        f"over {shadow_n} shadow signals"
                    )
                    return self._apply_mode(
                        rec, ModuleMode.DISABLED, reason=reason, accuracy=accuracy,
                        sample_size=shadow_n, baseline_signals=0,
                    )
                return None

            if mode == ModuleMode.DISABLED:
                # Optional automatic retry: after N days, re-enter SHADOW so the
                # module gets another supervised chance. 0 = manual only.
                if auto_retry_days > 0 and rec.disabled_time:
                    elapsed_days = (time.time() - float(rec.disabled_time)) / 86400.0
                    if elapsed_days >= auto_retry_days:
                        reason = f"auto-retry after {elapsed_days:.1f}d disabled"
                        return self._apply_mode(
                            rec, ModuleMode.SHADOW, reason=reason, accuracy=accuracy,
                            sample_size=sample_size, baseline_signals=sample_size,
                        )
                return None
        return None

    def _apply_mode(
        self,
        rec: ModuleStateRecord,
        new_mode: ModuleMode,
        *,
        reason: str,
        accuracy: float,
        sample_size: int,
        baseline_signals: int,
    ) -> GovernorTransition:
        """Mutate + persist a module's mode and record the transition.

        Caller MUST hold ``self._lock``.
        """
        old_mode = rec.mode
        now = time.time()
        rec.mode = new_mode
        rec.last_transition_time = now
        rec.reason = reason
        rec.accuracy_at_transition = float(accuracy)
        if new_mode == ModuleMode.SHADOW:
            rec.shadow_start_time = now
            rec.shadow_baseline_signals = int(baseline_signals)
            rec.disabled_time = None
        elif new_mode == ModuleMode.DISABLED:
            rec.disabled_time = now
            rec.shadow_start_time = None
            rec.shadow_baseline_signals = 0
        else:  # ACTIVE
            rec.shadow_start_time = None
            rec.shadow_baseline_signals = 0
            rec.disabled_time = None
        # Publish a fresh immutable mode map (atomic swap for lock-free reads).
        new_modes = dict(self._modes)
        new_modes[rec.module] = new_mode.value
        self._modes = new_modes
        self._persist(rec)
        t = GovernorTransition(
            module=rec.module,
            old_mode=old_mode.value if isinstance(old_mode, ModuleMode) else str(old_mode),
            new_mode=new_mode.value,
            reason=reason,
            accuracy=float(accuracy),
            sample_size=int(sample_size),
            timestamp=now,
        )
        self._record_transition(t)
        return t

    def _accuracy_and_count(self, module: str) -> tuple[float, int]:
        """Read a module's graded accuracy + sample size from EmitterFeedback.

        The governor consumes the read-only feedback service; it never grades
        signals or computes accuracy itself. Returns ``(0.0, 0)`` when no
        feedback is wired or the module has no graded signals yet.
        """
        fb = self._emitter_feedback
        if fb is None:
            return 0.0, 0
        lookback = int(getattr(self._config, "feedback_lookback", 500))
        from adaptive.emitter_feedback import EmitterFeedbackRequest

        resp = fb.request_feedback(
            EmitterFeedbackRequest(emitter=module, lookback=lookback)
        )
        accuracy = float(getattr(resp, "accuracy_all", 0.0) or 0.0)
        n = int(getattr(resp, "total_signals", 0) or 0)
        return accuracy, n

    # ── Snapshot / restore (used by the TunerAgent adapter for rollback) ──

    def get_state(self) -> dict:
        """JSON-serialisable snapshot of every module's published mode."""
        with self._lock:
            return {"modes": dict(self._modes)}

    def restore_modes(self, modes: Optional[dict]) -> None:
        """Re-publish a previously-snapshotted mode map (rollback).

        Updates both the published cache and the persisted state records so the
        rollback survives a restart. Unknown modes are ignored.
        """
        if not modes:
            return
        with self._lock:
            new_modes = dict(self._modes)
            for module, mode in modes.items():
                if module not in self._state:
                    continue
                target = _coerce_mode(mode)
                rec = self._state[module]
                if rec.mode == target:
                    continue
                rec.mode = target
                rec.last_transition_time = time.time()
                rec.reason = "rollback"
                new_modes[module] = target.value
                self._persist(rec)
            self._modes = new_modes

    # ── Reading / dashboard ────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Per-module status + counts for the dashboard / ops."""
        with self._lock:
            records = [rec.to_dict() for rec in self._state.values()]
        now = time.time()
        for r in records:
            ts = float(r.get("last_transition_time") or 0.0)
            r["seconds_in_mode"] = round(max(0.0, now - ts), 1) if ts else 0.0
        records.sort(key=lambda r: (r["mode"] != ModuleMode.ACTIVE.value, r["module"]))
        counts = {m.value: 0 for m in ModuleMode}
        for r in records:
            counts[r["mode"]] = counts.get(r["mode"], 0) + 1
        return {
            "enabled": self.enabled,
            "modules": records,
            "counts": counts,
            "module_count": len(records),
        }

    def get_transitions(self, limit: int = 100) -> List[dict]:
        """Recent transition audit rows, newest-first."""
        if self._conn is None:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT timestamp, module, old_mode, new_mode, reason, accuracy, "
                    "sample_size FROM module_governor_transitions "
                    "ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[module-governor] get_transitions failed: {}", exc)
                return []


# ── Helpers ────────────────────────────────────────────────────────────────

def _coerce_mode(value) -> ModuleMode:
    if isinstance(value, ModuleMode):
        return value
    try:
        return ModuleMode(str(value).upper())
    except (ValueError, AttributeError):
        return ModuleMode.ACTIVE


def _opt_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ModuleGovernor",
    "ModuleMode",
    "ModuleStateRecord",
    "GovernorTransition",
    "DEFAULT_GOVERNED_MODULES",
]
