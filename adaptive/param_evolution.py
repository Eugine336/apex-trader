"""
APEX TRADER — Parameter Evolution Engine (L5a)

The TunerAgent nudges parameters by *following gradients* on recent performance.
It improves what is already there, but it never *explores* — it never asks
"what if ``min_net_score`` were 1.0 instead of 1.5?".  This engine asks that
question and answers it with evidence rather than a guess.

It is built on the **same decision-replay foundation as the L4 Counterfactual
engine** (``replay_consensus`` over the exact vote panel + consensus config that
opened each trade).  For a candidate parameter value it re-runs the entry
decision on every recent closed trade and measures whether the realised book
would have been better.

Honest scope — what this data can and cannot prove:
  * The counterfactual store only holds snapshots of trades that were *opened*.
    Replaying a candidate against them faithfully measures the effect of a
    *tightening* change (a stricter gate drops marginal trades that were taken;
    we know their realised R).  A *loosening* change would only ever ADD trades
    that were never taken — there is no snapshot for those — so historical
    replay cannot validate it.  The forward **shadow-validation** phase is what
    closes that gap: it re-scores trades that close *after* a candidate enters
    shadow (genuine out-of-sample), crediting a candidate the losses it would
    have avoided and debiting the winners it would have skipped.
  * Therefore replay-based evolution is scoped to the consensus / ranker
    thresholds that ``replay_consensus`` actually consumes.  Scoring weights and
    gate offsets are NOT in the snapshot, so we do not pretend to replay them.

Safety: a candidate is never adopted off historical replay alone.  It must
(1) improve in BOTH halves of a walk-forward split, (2) survive an outlier
robustness check, (3) then prove itself over ``shadow_validation_trades`` live
closes, and only then is a promotion *recommended*.  Application is delegated to
an injected callback (the wiring layer) so this module never mutates live config
directly and always defers to the TunerAgent as the sole tuning authority.

Leaf-ish module — stdlib + loguru + the pure ``adaptive.counterfactual`` replay
helpers only.  Exception-safe throughout; a fault here never blocks trading.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from adaptive.counterfactual import TradeAttribution, _votes_from_dicts, replay_consensus

# The pure ``decide`` math logs every call at INFO; a single tournament does
# tens of thousands of replays, so we silence just those two consensus loggers
# for the duration of a bulk-replay loop (never any other logger), restoring
# them in a finally so an exception can never leave logging disabled.
_REPLAY_LOGGERS = ("brain.directional_consensus", "brain.opportunity_ranker")


@contextlib.contextmanager
def _quiet_replay():
    for name in _REPLAY_LOGGERS:
        logger.disable(name)
    try:
        yield
    finally:
        for name in _REPLAY_LOGGERS:
            logger.enable(name)

from runtime_paths import data_dir as _data_dir  # noqa: E402

_DB_DIR = _data_dir()
_DB_PATH = _DB_DIR / "param_evolution.db"

# Where each evolvable parameter lives in the replay snapshot: the ``decide``
# thresholds dict, or the ``rank_opportunities`` kwargs dict.
LOC_THRESHOLD = "threshold"
LOC_RANKER = "ranker"

# Shadow-candidate lifecycle states.
SHADOW = "SHADOW"
PROMOTED = "PROMOTED"
REJECTED = "REJECTED"
EXTENDED = "EXTENDED"


@dataclass(frozen=True)
class EvolvableParam:
    """One numeric knob the engine may explore, with its safe search bounds.

    ``location`` selects which replay dict the value is substituted into so the
    replay uses the identical math the live entry path used.
    """

    name: str
    location: str          # LOC_THRESHOLD | LOC_RANKER
    lo: float
    hi: float
    is_int: bool = False

    def clamp(self, value: float) -> float:
        v = max(self.lo, min(self.hi, float(value)))
        return float(round(v)) if self.is_int else v


# The evolvable search space — exactly the knobs ``replay_consensus`` consumes.
DEFAULT_EVOLVABLE_PARAMS: tuple[EvolvableParam, ...] = (
    EvolvableParam("min_net_score", LOC_THRESHOLD, 0.5, 4.0),
    EvolvableParam("min_agreement", LOC_THRESHOLD, 0.30, 0.90),
    EvolvableParam("min_contributors", LOC_THRESHOLD, 1, 5, is_int=True),
    EvolvableParam("min_expected_value", LOC_RANKER, 0.0, 1.5),
    EvolvableParam("min_cluster_confidence", LOC_RANKER, 0.0, 1.0),
    EvolvableParam("min_cluster_contributors", LOC_RANKER, 1, 5, is_int=True),
)


@dataclass
class ParameterCandidate:
    """A proposed value for one parameter, plus how it was generated."""

    param_name: str
    location: str
    current_value: float
    proposed_value: float
    generation_method: str = ""    # perturbation | grid | best_neighbor

    def key(self) -> str:
        return f"{self.param_name}={self.proposed_value:g}"


@dataclass
class ReplayEvaluation:
    """Result of replaying one candidate over a window of closed trades."""

    candidate: ParameterCandidate
    trades_analyzed: int = 0
    retained_trades: int = 0
    dropped_trades: int = 0
    actual_total_r: float = 0.0
    candidate_total_r: float = 0.0
    improvement_r: float = 0.0           # candidate_total_r − actual_total_r
    improvement_per_trade: float = 0.0
    walk_forward_ok: bool = False
    robust: bool = False
    train_improvement_r: float = 0.0
    test_improvement_r: float = 0.0

    def to_dict(self) -> dict:
        return {
            "param_name": self.candidate.param_name,
            "location": self.candidate.location,
            "current_value": self.candidate.current_value,
            "proposed_value": self.candidate.proposed_value,
            "generation_method": self.candidate.generation_method,
            "trades_analyzed": self.trades_analyzed,
            "retained_trades": self.retained_trades,
            "dropped_trades": self.dropped_trades,
            "actual_total_r": round(self.actual_total_r, 4),
            "candidate_total_r": round(self.candidate_total_r, 4),
            "improvement_r": round(self.improvement_r, 4),
            "improvement_per_trade": round(self.improvement_per_trade, 4),
            "walk_forward_ok": bool(self.walk_forward_ok),
            "robust": bool(self.robust),
            "train_improvement_r": round(self.train_improvement_r, 4),
            "test_improvement_r": round(self.test_improvement_r, 4),
        }


# ── Pure replay scoring ────────────────────────────────────────────────────


def _apply_candidate(
    attribution: TradeAttribution, candidate: ParameterCandidate,
) -> tuple[dict, dict]:
    """Return (thresholds, ranker_kwargs) with the candidate value substituted."""
    thresholds = dict(attribution.thresholds or {})
    ranker_kwargs = dict(attribution.ranker_kwargs or {})
    if candidate.location == LOC_THRESHOLD:
        thresholds[candidate.param_name] = candidate.proposed_value
    else:
        ranker_kwargs[candidate.param_name] = candidate.proposed_value
    return thresholds, ranker_kwargs


def candidate_retains_trade(
    attribution: TradeAttribution, candidate: ParameterCandidate,
) -> bool:
    """True if, under the candidate value, the trade still opens in the SAME
    direction (so its realised R still counts).  Quiet, never raises."""
    if attribution.direction not in ("LONG", "SHORT"):
        return False
    thresholds, ranker_kwargs = _apply_candidate(attribution, candidate)
    votes = _votes_from_dicts(attribution.votes)
    try:
        cf_dir, _ = replay_consensus(votes, thresholds, ranker_kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[param-evolution] replay failed: {}", exc)
        return True  # fail-safe: assume unchanged rather than fabricate a drop
    return cf_dir == attribution.direction


def evaluate_candidate(
    candidate: ParameterCandidate,
    trades: list[TradeAttribution],
    *,
    walk_forward_split: float = 0.7,
) -> ReplayEvaluation:
    """Replay a candidate over ``trades`` and grade it (walk-forward + robust).

    Single replay pass per trade: each graded trade is replayed once, then the
    overall / train / test improvements and the robustness check are all derived
    from that one pass (no re-replaying).  ``trades`` may arrive newest-first (as
    the counterfactual store returns them); we sort by open time so the
    walk-forward split is train = older 70%, test = newer 30%.
    """
    ev = ReplayEvaluation(candidate=candidate)
    graded = [
        t for t in trades
        if t.pnl_r is not None and t.direction in ("LONG", "SHORT")
    ]
    if not graded:
        return ev
    ordered = sorted(graded, key=lambda t: t.timestamp_open)

    # One replay per trade: (realised R, retained?) in chronological order.
    samples: list[tuple[float, bool]] = []
    with _quiet_replay():
        for t in ordered:
            samples.append((float(t.pnl_r), candidate_retains_trade(t, candidate)))

    dropped_r: list[float] = []
    for r, retained in samples:
        ev.trades_analyzed += 1
        ev.actual_total_r += r
        if retained:
            ev.retained_trades += 1
            ev.candidate_total_r += r
        else:
            ev.dropped_trades += 1
            dropped_r.append(r)

    ev.improvement_r = ev.candidate_total_r - ev.actual_total_r
    if ev.trades_analyzed:
        ev.improvement_per_trade = ev.improvement_r / ev.trades_analyzed

    # Walk-forward: candidate must not destroy value in EITHER period.
    split = max(1, int(len(samples) * float(walk_forward_split)))
    ev.train_improvement_r = _improvement_of(samples[:split])
    ev.test_improvement_r = _improvement_of(samples[split:])
    # No test rows (tiny sample) → fall back to requiring train improvement.
    has_test = split < len(samples)
    ev.walk_forward_ok = ev.train_improvement_r > 0 and (
        ev.test_improvement_r > 0 or not has_test
    )

    # Robustness: the gain must not hinge on 1-2 outlier drops.  Removing the
    # two most negative dropped trades (the biggest avoided losers) must still
    # leave a positive improvement.
    if dropped_r:
        worst_two = sorted(dropped_r)[:2]   # most-negative R the candidate avoided
        adjusted = ev.improvement_r + sum(worst_two)  # add back (they were avoided losses)
        ev.robust = adjusted > 0
    else:
        ev.robust = ev.improvement_r > 0
    return ev


def _improvement_of(samples: list[tuple[float, bool]]) -> float:
    """Candidate-minus-actual total R over a window of (R, retained) samples.

    A dropped trade contributes 0 to the candidate total but its R to the
    actual total, so the difference is exactly ``-sum(R of dropped trades)``.
    """
    return -sum(r for r, retained in samples if not retained)


def generate_candidates(
    param: EvolvableParam,
    current_value: float,
    *,
    n_candidates: int = 10,
    best_neighbors: Optional[list[float]] = None,
) -> list[ParameterCandidate]:
    """Build candidate values for one parameter via grid + neighbourhood search.

    Deterministic (no RNG) so a tournament is reproducible and unit-testable:
    an evenly-spaced grid across the bounds, plus near-neighbours of the current
    value and of any ``best_neighbors`` hints (e.g. previously promising values).
    """
    cur = param.clamp(current_value)
    values: set[float] = set()

    # Even grid across the legal range.
    n = max(2, int(n_candidates))
    step = (param.hi - param.lo) / (n - 1)
    for i in range(n):
        values.add(param.clamp(param.lo + i * step))

    # Local neighbourhood around the current value (fine-grained).
    span = param.hi - param.lo
    for delta in (-0.10, -0.05, 0.05, 0.10):
        values.add(param.clamp(cur + delta * span))

    # Neighbourhood around any supplied best-performing hints.
    for hint in best_neighbors or []:
        h = param.clamp(hint)
        for delta in (-0.05, 0.0, 0.05):
            values.add(param.clamp(h + delta * span))

    out: list[ParameterCandidate] = []
    for v in sorted(values):
        if abs(v - cur) < (1e-9 if not param.is_int else 0.5):
            continue  # skip the no-op (== current value)
        method = "best_neighbor" if best_neighbors else "grid"
        out.append(ParameterCandidate(
            param_name=param.name, location=param.location,
            current_value=cur, proposed_value=v, generation_method=method,
        ))
    return out


# ── Persistence ──────────────────────────────────────────────────────────

_CREATE_SHADOWS = """
CREATE TABLE IF NOT EXISTS shadow_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    param_name      TEXT NOT NULL,
    location        TEXT NOT NULL,
    current_value   REAL NOT NULL,
    proposed_value  REAL NOT NULL,
    generation_method TEXT,
    created_at      REAL NOT NULL,
    state           TEXT NOT NULL,
    shadow_trades   INTEGER NOT NULL DEFAULT 0,
    candidate_r     REAL NOT NULL DEFAULT 0.0,
    actual_r        REAL NOT NULL DEFAULT 0.0,
    replay          TEXT,
    seen_trade_ids  TEXT
)
"""

_CREATE_PROMOTIONS = """
CREATE TABLE IF NOT EXISTS promotions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at      REAL NOT NULL,
    param_name      TEXT NOT NULL,
    location        TEXT NOT NULL,
    old_value       REAL NOT NULL,
    new_value       REAL NOT NULL,
    decision        TEXT NOT NULL,
    evidence        TEXT
)
"""


@dataclass
class ShadowCandidate:
    """A candidate proving itself over real closes after the replay tournament."""

    param_name: str
    location: str
    current_value: float
    proposed_value: float
    generation_method: str
    created_at: float = field(default_factory=time.time)
    state: str = SHADOW
    shadow_trades: int = 0
    candidate_r: float = 0.0
    actual_r: float = 0.0
    replay: dict = field(default_factory=dict)
    seen_trade_ids: list[str] = field(default_factory=list)
    row_id: Optional[int] = None

    def as_candidate(self) -> ParameterCandidate:
        return ParameterCandidate(
            param_name=self.param_name, location=self.location,
            current_value=self.current_value, proposed_value=self.proposed_value,
            generation_method=self.generation_method,
        )

    @property
    def improvement_r(self) -> float:
        return self.candidate_r - self.actual_r

    @property
    def improvement_per_trade(self) -> float:
        return self.improvement_r / self.shadow_trades if self.shadow_trades else 0.0

    def to_dict(self) -> dict:
        return {
            "param_name": self.param_name,
            "location": self.location,
            "current_value": round(self.current_value, 4),
            "proposed_value": round(self.proposed_value, 4),
            "generation_method": self.generation_method,
            "created_at": self.created_at,
            "state": self.state,
            "shadow_trades": self.shadow_trades,
            "candidate_r": round(self.candidate_r, 4),
            "actual_r": round(self.actual_r, 4),
            "improvement_r": round(self.improvement_r, 4),
            "improvement_per_trade": round(self.improvement_per_trade, 4),
            "replay": self.replay,
        }


class ParameterEvolver:
    """Explores the consensus/ranker threshold space, validates by replay +
    shadow, and recommends promotions — never mutating live config itself.

    Construction is exception-safe; every public method is too.  Pass a
    ``promote_callback(param_name, location, value) -> bool`` to let the wiring
    layer actually apply an approved value (gated by the caller); leave it None
    to run in pure recommendation mode.
    """

    def __init__(
        self,
        counterfactual_engine,
        *,
        enabled: bool = False,
        db_path: Optional[Path | str] = None,
        params: Optional[tuple[EvolvableParam, ...]] = None,
        current_values_provider: Optional[Callable[[], dict]] = None,
        promote_callback: Optional[Callable[[str, str, float], bool]] = None,
        candidates_per_param: int = 10,
        replay_lookback: int = 500,
        shadow_validation_trades: int = 50,
        significance_threshold: float = 0.05,
        walk_forward_split: float = 0.7,
        evolution_cooldown_hours: float = 48.0,
        max_concurrent_shadows: int = 3,
        rollback_window: int = 100,
        min_replay_trades: int = 50,
    ) -> None:
        self.enabled = bool(enabled)
        self._engine = counterfactual_engine
        self._params = tuple(params or DEFAULT_EVOLVABLE_PARAMS)
        self._current_values_provider = current_values_provider
        self._promote_callback = promote_callback
        self._candidates_per_param = max(2, int(candidates_per_param))
        self._replay_lookback = max(1, int(replay_lookback))
        self._shadow_trades_target = max(1, int(shadow_validation_trades))
        self._significance = float(significance_threshold)
        self._walk_forward_split = min(0.95, max(0.5, float(walk_forward_split)))
        self._cooldown_seconds = max(0.0, float(evolution_cooldown_hours) * 3600.0)
        self._max_shadows = max(1, int(max_concurrent_shadows))
        self._rollback_window = max(1, int(rollback_window))
        self._min_replay_trades = max(1, int(min_replay_trades))

        self._lock = threading.RLock()
        self._last_promotion_ts: float = 0.0
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[param-evolution] could not create db dir: {}", exc)
        self._connect()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_SHADOWS)
            self._conn.execute(_CREATE_PROMOTIONS)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[param-evolution] DB init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[param-evolution] conn close failed")
                self._conn = None

    # ── Current values ────────────────────────────────────────────────────

    def _current_values(self) -> dict:
        if self._current_values_provider is None:
            return {}
        try:
            vals = self._current_values_provider() or {}
            return dict(vals) if isinstance(vals, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[param-evolution] current values provider failed: {}", exc)
            return {}

    def _current_value(self, param: EvolvableParam, current_values: dict) -> float:
        if param.name in current_values:
            try:
                return param.clamp(float(current_values[param.name]))
            except (TypeError, ValueError):
                pass
        # Sensible default at the midpoint when the live value is unknown.
        return param.clamp((param.lo + param.hi) / 2.0)

    # ── Tournament ────────────────────────────────────────────────────────

    def run_tournament(self) -> list[ReplayEvaluation]:
        """Generate + replay candidates for every parameter and seed the best
        survivors into shadow validation.  Returns all evaluations (ranked)."""
        if not self.enabled or self._engine is None:
            return []
        try:
            trades = self._engine.get_closed_attributions(self._replay_lookback)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[param-evolution] could not read attributions: {}", exc)
            return []
        graded = [t for t in trades if t.pnl_r is not None]
        if len(graded) < self._min_replay_trades:
            return []

        current_values = self._current_values()
        evaluations: list[ReplayEvaluation] = []
        for param in self._params:
            cur = self._current_value(param, current_values)
            for cand in generate_candidates(
                param, cur, n_candidates=self._candidates_per_param,
            ):
                ev = evaluate_candidate(
                    cand, graded,
                    walk_forward_split=self._walk_forward_split,
                )
                evaluations.append(ev)

        # A candidate only deserves shadow validation if it improved the book
        # meaningfully AND survived both overfitting guards.
        qualifying = [
            ev for ev in evaluations
            if ev.walk_forward_ok and ev.robust
            and ev.improvement_per_trade >= self._significance
        ]
        qualifying.sort(key=lambda e: e.improvement_r, reverse=True)
        self._seed_shadows(qualifying)
        evaluations.sort(key=lambda e: e.improvement_r, reverse=True)
        if qualifying:
            logger.info(
                "[param-evolution] tournament: {} candidate(s) qualified for "
                "shadow validation (top: {} -> {:g}, +{:.3f}R/trade)",
                len(qualifying), qualifying[0].candidate.param_name,
                qualifying[0].candidate.proposed_value,
                qualifying[0].improvement_per_trade,
            )
        return evaluations

    def _seed_shadows(self, qualifying: list[ReplayEvaluation]) -> None:
        active = self.get_active_shadows()
        active_keys = {(s.param_name, round(s.proposed_value, 6)) for s in active}
        # Do not exceed the concurrency cap, and never duplicate a live shadow
        # or one already shadowed for the same parameter.
        active_params = {s.param_name for s in active}
        slots = self._max_shadows - len(active)
        for ev in qualifying:
            if slots <= 0:
                break
            cand = ev.candidate
            key = (cand.param_name, round(cand.proposed_value, 6))
            if key in active_keys or cand.param_name in active_params:
                continue
            self._insert_shadow(ShadowCandidate(
                param_name=cand.param_name, location=cand.location,
                current_value=cand.current_value, proposed_value=cand.proposed_value,
                generation_method=cand.generation_method, replay=ev.to_dict(),
            ))
            active_params.add(cand.param_name)
            slots -= 1

    # ── Shadow validation ─────────────────────────────────────────────────

    def evaluate_shadows(self) -> list[dict]:
        """Update every active shadow with newly-closed trades and resolve any
        that have gathered enough evidence.  Returns the decisions made."""
        if not self.enabled or self._engine is None:
            return []
        try:
            recent = self._engine.get_closed_attributions(self._replay_lookback)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[param-evolution] shadow read failed: {}", exc)
            return []
        decisions: list[dict] = []
        for shadow in self.get_active_shadows():
            updated = self._update_shadow(shadow, recent)
            if not updated:
                continue
            decision = self._resolve_shadow(shadow)
            if decision:
                decisions.append(decision)
        return decisions

    def _update_shadow(
        self, shadow: ShadowCandidate, recent: list[TradeAttribution],
    ) -> bool:
        """Fold trades that closed since the shadow last saw them into its
        running candidate-vs-actual R.  Returns True if anything changed."""
        seen = set(shadow.seen_trade_ids)
        cand = shadow.as_candidate()
        changed = False
        # Oldest-first so the running tally is chronological (out-of-sample).
        with _quiet_replay():
            for t in sorted(recent, key=lambda a: a.timestamp_open):
                if t.trade_id in seen or t.pnl_r is None:
                    continue
                if t.direction not in ("LONG", "SHORT"):
                    continue
                r = float(t.pnl_r)
                shadow.shadow_trades += 1
                shadow.actual_r += r
                if candidate_retains_trade(t, cand):
                    shadow.candidate_r += r
                shadow.seen_trade_ids.append(t.trade_id)
                seen.add(t.trade_id)
                changed = True
        if changed:
            self._save_shadow(shadow)
        return changed

    def _resolve_shadow(self, shadow: ShadowCandidate) -> Optional[dict]:
        """Promote / reject / extend a shadow once it has enough closes."""
        if shadow.shadow_trades < self._shadow_trades_target:
            return None
        per_trade = shadow.improvement_per_trade
        if per_trade >= self._significance:
            return self._promote(shadow)
        if per_trade <= -self._significance:
            return self._reject(shadow, f"underperformed ({per_trade:+.3f}R/trade)")
        # Inconclusive: extend the shadow window once, then force a decision.
        if shadow.state != EXTENDED and shadow.shadow_trades < self._shadow_trades_target * 2:
            shadow.state = EXTENDED
            self._save_shadow(shadow)
            return None
        return self._reject(shadow, f"inconclusive after extension ({per_trade:+.3f}R/trade)")

    def _promote(self, shadow: ShadowCandidate) -> dict:
        """Recommend (and, if a callback is wired, apply) a proven candidate.

        Rate-limited to one promotion per cooldown window so the live book is
        never reshaped by several simultaneous changes.
        """
        now = time.time()
        if self._cooldown_seconds and (now - self._last_promotion_ts) < self._cooldown_seconds:
            return {
                "param_name": shadow.param_name, "decision": "deferred",
                "reason": "cooldown active", "proposed_value": shadow.proposed_value,
            }
        applied = False
        if self._promote_callback is not None:
            try:
                applied = bool(self._promote_callback(
                    shadow.param_name, shadow.location, shadow.proposed_value,
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[param-evolution] promote_callback failed: {}", exc)
                applied = False
        shadow.state = PROMOTED
        self._save_shadow(shadow)
        self._last_promotion_ts = now
        evidence = {
            "shadow": shadow.to_dict(),
            "applied": applied,
            "shadow_trades": shadow.shadow_trades,
        }
        self._record_promotion(
            shadow, "promoted" if applied else "recommended", evidence,
        )
        logger.info(
            "[param-evolution] {} {} -> {:g} ({} over {} shadow trades, "
            "+{:.3f}R/trade)",
            "PROMOTED" if applied else "RECOMMENDED",
            shadow.param_name, shadow.proposed_value,
            shadow.location, shadow.shadow_trades, shadow.improvement_per_trade,
        )
        return {
            "param_name": shadow.param_name,
            "location": shadow.location,
            "old_value": shadow.current_value,
            "new_value": shadow.proposed_value,
            "decision": "promoted" if applied else "recommended",
            "applied": applied,
            "improvement_per_trade": shadow.improvement_per_trade,
            "shadow_trades": shadow.shadow_trades,
        }

    def _reject(self, shadow: ShadowCandidate, reason: str) -> dict:
        shadow.state = REJECTED
        self._save_shadow(shadow)
        self._record_promotion(shadow, "rejected", {"reason": reason, "shadow": shadow.to_dict()})
        logger.info(
            "[param-evolution] REJECTED {} -> {:g}: {}",
            shadow.param_name, shadow.proposed_value, reason,
        )
        return {
            "param_name": shadow.param_name,
            "proposed_value": shadow.proposed_value,
            "decision": "rejected",
            "reason": reason,
            "shadow_trades": shadow.shadow_trades,
        }

    # ── Combined periodic entry point ─────────────────────────────────────

    def run_cycle(self, *, force_tournament: bool = False) -> dict:
        """One periodic step: advance shadows, then (cooldown permitting) run a
        fresh tournament to seed new candidates.  Safe to call every tick."""
        if not self.enabled:
            return {"enabled": False}
        decisions = self.evaluate_shadows()
        ran_tournament = False
        n_qualified = 0
        active = self.get_active_shadows()
        cooldown_ok = (
            not self._cooldown_seconds
            or (time.time() - self._last_promotion_ts) >= self._cooldown_seconds
        )
        if force_tournament or (len(active) < self._max_shadows and cooldown_ok):
            evals = self.run_tournament()
            ran_tournament = True
            n_qualified = sum(1 for e in evals if e.walk_forward_ok and e.robust)
        return {
            "enabled": True,
            "decisions": decisions,
            "ran_tournament": ran_tournament,
            "qualified": n_qualified,
            "active_shadows": len(self.get_active_shadows()),
        }

    # ── SQLite read/write ─────────────────────────────────────────────────

    def _insert_shadow(self, shadow: ShadowCandidate) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                cur = self._conn.execute(
                    """INSERT INTO shadow_candidates
                       (param_name, location, current_value, proposed_value,
                        generation_method, created_at, state, shadow_trades,
                        candidate_r, actual_r, replay, seen_trade_ids)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        shadow.param_name, shadow.location, shadow.current_value,
                        shadow.proposed_value, shadow.generation_method,
                        shadow.created_at, shadow.state, shadow.shadow_trades,
                        shadow.candidate_r, shadow.actual_r,
                        json.dumps(shadow.replay, default=str),
                        json.dumps(shadow.seen_trade_ids, default=str),
                    ),
                )
                shadow.row_id = cur.lastrowid
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[param-evolution] insert shadow failed: {}", exc)

    def _save_shadow(self, shadow: ShadowCandidate) -> None:
        if self._conn is None or shadow.row_id is None:
            return
        with self._lock:
            try:
                self._conn.execute(
                    """UPDATE shadow_candidates
                       SET state=?, shadow_trades=?, candidate_r=?, actual_r=?,
                           seen_trade_ids=?
                       WHERE id=?""",
                    (
                        shadow.state, shadow.shadow_trades, shadow.candidate_r,
                        shadow.actual_r, json.dumps(shadow.seen_trade_ids, default=str),
                        shadow.row_id,
                    ),
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[param-evolution] save shadow failed: {}", exc)

    def _row_to_shadow(self, row: dict) -> ShadowCandidate:
        return ShadowCandidate(
            param_name=str(row.get("param_name", "")),
            location=str(row.get("location", "")),
            current_value=float(row.get("current_value") or 0.0),
            proposed_value=float(row.get("proposed_value") or 0.0),
            generation_method=str(row.get("generation_method") or ""),
            created_at=float(row.get("created_at") or 0.0),
            state=str(row.get("state") or SHADOW),
            shadow_trades=int(row.get("shadow_trades") or 0),
            candidate_r=float(row.get("candidate_r") or 0.0),
            actual_r=float(row.get("actual_r") or 0.0),
            replay=_loads_dict(row.get("replay")),
            seen_trade_ids=_loads_list(row.get("seen_trade_ids")),
            row_id=int(row.get("id")) if row.get("id") is not None else None,
        )

    def get_active_shadows(self) -> list[ShadowCandidate]:
        if self._conn is None:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT * FROM shadow_candidates WHERE state IN (?, ?) "
                    "ORDER BY created_at",
                    (SHADOW, EXTENDED),
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[param-evolution] active shadows read failed: {}", exc)
                return []
        return [self._row_to_shadow(r) for r in rows]

    def _record_promotion(
        self, shadow: ShadowCandidate, decision: str, evidence: dict,
    ) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO promotions
                       (decided_at, param_name, location, old_value, new_value,
                        decision, evidence)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        time.time(), shadow.param_name, shadow.location,
                        shadow.current_value, shadow.proposed_value, decision,
                        json.dumps(evidence, default=str),
                    ),
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[param-evolution] record promotion failed: {}", exc)

    def get_promotion_history(self, limit: int = 50) -> list[dict]:
        if self._conn is None:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT * FROM promotions ORDER BY id DESC LIMIT ?", (int(limit),),
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[param-evolution] promotion history read failed: {}", exc)
                return []
        for r in rows:
            r["evidence"] = _loads_dict(r.get("evidence"))
        return rows

    # ── Dashboard surface ─────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Read-only snapshot for the dashboard / ops."""
        shadows = [s.to_dict() for s in self.get_active_shadows()]
        return {
            "enabled": bool(self.enabled),
            "evolvable_params": [p.name for p in self._params],
            "candidates_per_param": self._candidates_per_param,
            "replay_lookback": self._replay_lookback,
            "shadow_validation_trades": self._shadow_trades_target,
            "significance_threshold": self._significance,
            "max_concurrent_shadows": self._max_shadows,
            "active_shadows": shadows,
            "active_shadow_count": len(shadows),
            "recent_promotions": self.get_promotion_history(20),
            "last_promotion_ts": self._last_promotion_ts or None,
        }


# ── Helpers ────────────────────────────────────────────────────────────────


def _loads_dict(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


def _loads_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (TypeError, ValueError):
        return []


__all__ = [
    "LOC_THRESHOLD",
    "LOC_RANKER",
    "EvolvableParam",
    "ParameterCandidate",
    "ReplayEvaluation",
    "ShadowCandidate",
    "ParameterEvolver",
    "DEFAULT_EVOLVABLE_PARAMS",
    "generate_candidates",
    "evaluate_candidate",
    "candidate_retains_trade",
]
