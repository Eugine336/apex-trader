"""
APEX TRADER — Tunable Adapters

Thin wrappers that make the existing learners / tuners satisfy the
:class:`~adaptive.tunable.Tunable` protocol so the central ``TunerAgent`` can
schedule, order, validate, audit, and roll them back. The adapters add NO new
tuning logic — each ``tune`` call delegates to the exact method the live system
already calls (``ScoreOptimizer.optimize``, ``RegimeLearner.learn``,
``GateTuner.calibrate``, ``Calibrator.calibrate``, ``SignalLedger.run_grading_cycle``
…). They only centralise *when* it runs and *whether the result is sane*.

Each component's training data is supplied by an injected provider callable so
the adapters stay decoupled from the main loop's internals and are trivially
unit-testable. A component that cannot be reached (provider returns nothing)
yields a harmless skipped result, never an error.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, replace
from typing import Callable, Optional

from loguru import logger

from adaptive.tunable import TuneContext, TuneFrequency, TuneResult


# Provider callables (all optional / injected by the wiring layer).
TradesProvider = Callable[[], list]
OutcomesProvider = Callable[[], list]
PricesProvider = Callable[[], dict]
IntProvider = Callable[[], int]


class _BaseTunable:
    """Common scheduling + snapshot/rollback plumbing for every adapter."""

    def __init__(
        self,
        *,
        name: str,
        frequency: TuneFrequency,
        dependencies: Optional[list[str]] = None,
        min_trades: int = 0,
        min_interval: float = 0.0,
    ) -> None:
        self._name = name
        self._frequency = frequency
        self._dependencies = list(dependencies or [])
        self._min_trades = int(min_trades)
        self._min_interval = float(min_interval)
        self._snapshot: Optional[dict] = None
        self._last_tune_ts: Optional[float] = None
        self._trades_at_last_tune: int = 0

    # ── Protocol metadata ────────────────────────────────────────────────

    @property
    def tunable_name(self) -> str:
        return self._name

    @property
    def frequency(self) -> TuneFrequency:
        return self._frequency

    @property
    def dependencies(self) -> list[str]:
        return list(self._dependencies)

    @property
    def min_trades_required(self) -> int:
        return self._min_trades

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    # ── Scheduling ───────────────────────────────────────────────────────

    def should_tune(self, ctx: TuneContext) -> bool:
        if ctx.force:
            return True
        if self._frequency == TuneFrequency.PER_SCAN_CYCLE:
            return True
        # Data floor — never tune on an empty / thin history.
        if ctx.total_trades < self._min_trades:
            return False
        # Interval floor — wall-clock, tracked per tunable.
        if self._min_interval > 0 and self._last_tune_ts is not None:
            if (time.time() - self._last_tune_ts) < self._min_interval:
                return False
        # Batch cadence — re-tune every `min_trades` closed trades.
        if self._frequency == TuneFrequency.ON_TRADE_BATCH and self._min_trades > 0:
            if self._last_tune_ts is not None:
                if (ctx.total_trades - self._trades_at_last_tune) < self._min_trades:
                    return False
        return True

    def _mark_tuned(self, ctx: TuneContext) -> None:
        self._last_tune_ts = time.time()
        self._trades_at_last_tune = ctx.total_trades

    # ── Snapshot / rollback ──────────────────────────────────────────────

    def get_current_params(self) -> dict:
        try:
            return self._read_params()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[{}] read params failed: {}", self._name, exc)
            return {}

    def rollback(self) -> bool:
        if self._snapshot is None:
            return False
        try:
            self._apply_params(self._snapshot)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[{}] rollback failed: {}", self._name, exc)
            return False

    def validate_params(self, params: dict) -> tuple[bool, str]:
        return True, "ok"

    # ── Subclass hooks ───────────────────────────────────────────────────

    def _read_params(self) -> dict:
        raise NotImplementedError

    def _apply_params(self, params: dict) -> None:
        raise NotImplementedError

    def tune(self, ctx: TuneContext) -> TuneResult:
        raise NotImplementedError

    # Convenience for subclasses to build a result and snapshot.
    def _begin(self) -> dict:
        before = self.get_current_params()
        self._snapshot = before
        return before


# ───────────────────────────── ML learners ─────────────────────────────


class ScoreOptimizerTunable(_BaseTunable):
    """Wraps ``ScoreOptimizer.optimize`` — the 12-factor confluence weights."""

    def __init__(
        self,
        optimizer,
        trades_provider: TradesProvider,
        *,
        on_update: Optional[Callable[[object], None]] = None,
        min_trades: int = 50,
        min_interval: float = 6 * 3600,
    ) -> None:
        super().__init__(
            name="score_optimizer",
            frequency=TuneFrequency.ON_TRADE_BATCH,
            dependencies=[],
            min_trades=min_trades,
            min_interval=min_interval,
        )
        self._optimizer = optimizer
        self._trades_provider = trades_provider
        self._on_update = on_update

    def _read_params(self) -> dict:
        base = dict(self._optimizer.current_weights.as_dict())
        # In per-class mode, expose each class profile under a reserved key so
        # snapshot/rollback restores them too. Validation ignores this key.
        if getattr(self._optimizer, "per_class", False):
            base["classes"] = {
                cls: dict(w.as_dict())
                for cls, w in (getattr(self._optimizer, "class_weights", {}) or {}).items()
            }
        return base

    def _apply_params(self, params: dict) -> None:
        from adaptive.score_optimizer import ScoringWeights

        classes = params.get("classes")
        flat = {k: v for k, v in params.items() if k != "classes"}
        kwargs = {f"{k}_weight": int(v) for k, v in flat.items()}
        weights = ScoringWeights(**{
            k: v for k, v in kwargs.items()
            if k in ScoringWeights.__dataclass_fields__
        })
        self._optimizer.current_weights = weights
        if classes is not None and getattr(self._optimizer, "per_class", False):
            restored: dict = {}
            for cls, d in classes.items():
                ckw = {f"{k}_weight": int(v) for k, v in d.items()}
                restored[cls] = ScoringWeights(**{
                    k: v for k, v in ckw.items()
                    if k in ScoringWeights.__dataclass_fields__
                })
            self._optimizer.class_weights = restored
        self._optimizer.save_weights()

    def validate_params(self, params: dict) -> tuple[bool, str]:
        from adaptive.score_optimizer import (
            ADAPTIVE_WEIGHT_ENVELOPE_PCT as ENV,
            FACTOR_KEYS,
            ScoreOptimizer,
            ScoringWeights,
        )

        base = ScoringWeights().as_dict()
        for k in FACTOR_KEYS:
            v = params.get(k)
            if v is None:
                return False, f"missing factor '{k}'"
            if v < ScoreOptimizer.MIN_WEIGHT:
                return False, f"{k}={v} below MIN_WEIGHT {ScoreOptimizer.MIN_WEIGHT}"
            lo = round(base[k] * (1.0 - ENV))
            hi = round(base[k] * (1.0 + ENV))
            if not (lo <= v <= hi):
                return False, f"{k}={v} outside envelope [{lo},{hi}]"
        return True, "ok"

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        trades = list(self._trades_provider() or [])
        if not trades:
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=before,
                reason="no trades available",
            )
        self._optimizer.optimize(trades)
        after = self._read_params()
        changed = after != before
        if changed and self._on_update is not None:
            try:
                self._on_update(self._optimizer.current_weights)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[score_optimizer] on_update failed: {}", exc)
        self._mark_tuned(ctx)
        return TuneResult(
            tunable_name=self._name, success=True, changed=changed,
            params_before=before, params_after=after,
            reason=f"optimized on {len(trades)} trades"
            + ("" if changed else " (no change)"),
        )


class _LearnerTunable(_BaseTunable):
    """Shared base for the per-dimension learners (regime / pair / session).

    Each wraps a component exposing ``learn(trades) -> dict[str, dataclass]``
    plus private ``_strategies``/``_profiles`` state and a ``_save`` method.
    """

    def __init__(self, learner, trades_provider, *, name, min_trades, min_interval,
                 state_attr, save_attr, record_cls_provider):
        super().__init__(
            name=name,
            frequency=TuneFrequency.ON_TRADE_BATCH,
            dependencies=[],
            min_trades=min_trades,
            min_interval=min_interval,
        )
        self._learner = learner
        self._trades_provider = trades_provider
        self._state_attr = state_attr
        self._save_attr = save_attr
        self._record_cls_provider = record_cls_provider

    def _state(self) -> dict:
        return getattr(self._learner, self._state_attr, {}) or {}

    def _read_params(self) -> dict:
        out: dict = {}
        for key, rec in self._state().items():
            try:
                out[key] = asdict(rec)
            except Exception:  # noqa: BLE001
                out[key] = {}
        return out

    def _apply_params(self, params: dict) -> None:
        record_cls = self._record_cls_provider()
        rebuilt = {}
        for key, val in (params or {}).items():
            fields = record_cls.__dataclass_fields__
            clean = {k: v for k, v in (val or {}).items() if k in fields}
            rebuilt[key] = record_cls(**clean)
        setattr(self._learner, self._state_attr, rebuilt)
        save = getattr(self._learner, self._save_attr, None)
        if callable(save):
            save()

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        trades = list(self._trades_provider() or [])
        if not trades:
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=before,
                reason="no trades available",
            )
        self._learner.learn(trades)
        after = self._read_params()
        changed = after != before
        self._mark_tuned(ctx)
        return TuneResult(
            tunable_name=self._name, success=True, changed=changed,
            params_before=before, params_after=after,
            reason=f"learned on {len(trades)} trades"
            + ("" if changed else " (no change)"),
        )


class RegimeLearnerTunable(_LearnerTunable):
    """Wraps ``RegimeLearner.learn`` — per-regime TP/SL/threshold strategy."""

    def __init__(self, learner, trades_provider, *, min_trades=30, min_interval=6 * 3600):
        from adaptive.regime_learner import RegimeStrategy

        super().__init__(
            learner, trades_provider,
            name="regime_learner", min_trades=min_trades, min_interval=min_interval,
            state_attr="_strategies", save_attr="_save",
            record_cls_provider=lambda: RegimeStrategy,
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        for regime, v in (params or {}).items():
            tp = float(v.get("optimal_tp_multiplier", 1.0))
            sl = float(v.get("optimal_sl_buffer_pips", 2.0))
            thr = float(v.get("optimal_score_threshold", 85))
            partial = float(v.get("optimal_partial_close_ratio", 0.5))
            wr = float(v.get("win_rate", 0.0))
            if not (0.5 <= tp <= 2.0):
                return False, f"{regime} tp_multiplier {tp} out of [0.5,2.0]"
            if sl < 0:
                return False, f"{regime} sl_buffer {sl} < 0"
            if not (40 <= thr <= 100):
                return False, f"{regime} score_threshold {thr} out of [40,100]"
            if not (0.0 <= partial <= 1.0):
                return False, f"{regime} partial_close {partial} out of [0,1]"
            if not (0.0 <= wr <= 1.0):
                return False, f"{regime} win_rate {wr} out of [0,1]"
        return True, "ok"


class PairLearnerTunable(_LearnerTunable):
    """Wraps ``PairLearner.learn`` — per-pair size multiplier + win rate."""

    _VALID_RECS = {"INSUFFICIENT_DATA", "AVOID", "REDUCE_SIZE", "TRADE"}
    # Reserved key under which the continuous-multiplier (sigmoid) params are
    # surfaced. Namespaced so it can never collide with a real pair symbol.
    _SIGMOID_KEY = "__sigmoid_config__"

    def __init__(self, learner, trades_provider, *, min_trades=10, min_interval=4 * 3600):
        from adaptive.pair_learner import PairProfile

        super().__init__(
            learner, trades_provider,
            name="pair_learner", min_trades=min_trades, min_interval=min_interval,
            state_attr="_profiles", save_attr="_save",
            record_cls_provider=lambda: PairProfile,
        )

    def _sigmoid_params(self) -> dict:
        """Current continuous-multiplier shape params (informational + rollback
        symmetry). Static during ``learn`` — present in both before/after so the
        agent's change-detection still keys off the per-pair profiles only."""
        learner = self._learner
        return {
            "continuous_pair_multiplier": bool(getattr(learner, "continuous_enabled", False)),
            "midpoint": float(getattr(learner, "continuous_midpoint", 0.5)),
            "steepness": float(getattr(learner, "continuous_steepness", 10.0)),
            "floor": float(getattr(learner, "continuous_floor", 0.3)),
            "ceiling": float(getattr(learner, "continuous_ceiling", 1.2)),
            "prior": float(getattr(learner, "continuous_prior", 0.8)),
            "shrinkage_full_weight": int(getattr(learner, "shrinkage_full_weight", 30)),
            "absolute_floor": float(getattr(learner, "continuous_absolute_floor", 0.1)),
            "cold_start_multiplier": float(getattr(learner, "cold_start_multiplier", 0.8)),
            "entry_management_split_enabled": bool(
                getattr(learner, "entry_management_split_enabled", False)
            ),
            "entry_accuracy_blend_weight": float(
                getattr(learner, "entry_accuracy_blend_weight", 0.3)
            ),
        }

    def _read_params(self) -> dict:
        out = super()._read_params()
        out[self._SIGMOID_KEY] = self._sigmoid_params()
        return out

    def _apply_params(self, params: dict) -> None:
        # The sigmoid params live on config (not mutated by learn) — strip the
        # reserved key so rollback only rebuilds the per-pair profiles.
        clean = {k: v for k, v in (params or {}).items() if k != self._SIGMOID_KEY}
        super()._apply_params(clean)

    def validate_params(self, params: dict) -> tuple[bool, str]:
        sig = (params or {}).get(self._SIGMOID_KEY)
        if sig is not None:
            ok, why = self._validate_sigmoid(sig)
            if not ok:
                return False, why
        for pair, v in (params or {}).items():
            if pair == self._SIGMOID_KEY:
                continue
            wr = float(v.get("win_rate", 0.0))
            n = int(v.get("total_trades", 0))
            rec = str(v.get("recommendation", "INSUFFICIENT_DATA"))
            if not (0.0 <= wr <= 1.0):
                return False, f"{pair} win_rate {wr} out of [0,1]"
            if n < 0:
                return False, f"{pair} total_trades {n} < 0"
            if rec not in self._VALID_RECS:
                return False, f"{pair} unknown recommendation '{rec}'"
        return True, "ok"

    @staticmethod
    def _validate_sigmoid(sig: dict) -> tuple[bool, str]:
        floor = float(sig.get("floor", 0.3))
        ceiling = float(sig.get("ceiling", 1.2))
        steepness = float(sig.get("steepness", 10.0))
        midpoint = float(sig.get("midpoint", 0.5))
        blend = float(sig.get("entry_accuracy_blend_weight", 0.3))
        sfw = int(sig.get("shrinkage_full_weight", 30))
        if floor > ceiling:
            return False, f"sigmoid floor {floor} > ceiling {ceiling}"
        if steepness <= 0:
            return False, f"sigmoid steepness {steepness} <= 0"
        if not (0.0 <= midpoint <= 1.0):
            return False, f"sigmoid midpoint {midpoint} out of [0,1]"
        if not (0.0 <= blend <= 1.0):
            return False, f"entry_accuracy_blend_weight {blend} out of [0,1]"
        if sfw <= 0:
            return False, f"shrinkage_full_weight {sfw} <= 0"
        return True, "ok"


class SessionLearnerTunable(_LearnerTunable):
    """Wraps ``SessionLearner.learn`` — per-session aggression level."""

    _VALID_RECS = {"AGGRESSIVE", "NORMAL", "CAUTIOUS", "AVOID"}

    def __init__(self, learner, trades_provider, *, min_trades=20, min_interval=6 * 3600):
        from adaptive.session_learner import SessionProfile

        super().__init__(
            learner, trades_provider,
            name="session_learner", min_trades=min_trades, min_interval=min_interval,
            state_attr="_profiles", save_attr="_save",
            record_cls_provider=lambda: SessionProfile,
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        for session, v in (params or {}).items():
            wr = float(v.get("win_rate", 0.0))
            rec = str(v.get("recommendation", "NORMAL"))
            if not (0.0 <= wr <= 1.0):
                return False, f"{session} win_rate {wr} out of [0,1]"
            if rec not in self._VALID_RECS:
                return False, f"{session} unknown recommendation '{rec}'"
        return True, "ok"


# ───────────────────────────── EV / gates ──────────────────────────────


class EVEstimatorTunable(_BaseTunable):
    """Refreshes the trade-history snapshot the (stateless) EVEstimator reads.

    EVEstimator computes EV on demand from a supplied ``trade_history``; the
    live system keeps that snapshot fresh by feeding it the latest trades. This
    adapter centralises that refresh and records its sample size. It depends on
    ``pair_learner`` so EV reflects the freshest per-pair learning first.
    """

    def __init__(
        self,
        estimator,
        refresh: Optional[IntProvider] = None,
        *,
        min_trades: int = 10,
        min_interval: float = 0.0,
    ) -> None:
        super().__init__(
            name="ev_estimator",
            frequency=TuneFrequency.ON_TRADE_CLOSE,
            dependencies=["pair_learner"],
            min_trades=min_trades,
            min_interval=min_interval,
        )
        self._estimator = estimator
        self._refresh = refresh
        self._last_size = 0

    def _read_params(self) -> dict:
        return {
            "min_trades_for_gate": int(getattr(self._estimator, "min_trades_for_gate", 0)),
            "history_size": int(self._last_size),
        }

    def _apply_params(self, params: dict) -> None:
        # Refreshing a read-only snapshot is monotonic — nothing to undo.
        return None

    def rollback(self) -> bool:
        # No mutable learner state to revert.
        return True

    def validate_params(self, params: dict) -> tuple[bool, str]:
        if int(params.get("history_size", 0)) < 0:
            return False, "history_size < 0"
        return True, "ok"

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        if self._refresh is None:
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=before,
                reason="no refresh hook",
            )
        n = int(self._refresh() or 0)
        changed = n != self._last_size
        self._last_size = n
        self._mark_tuned(ctx)
        return TuneResult(
            tunable_name=self._name, success=True, changed=changed,
            params_before=before, params_after=self._read_params(),
            reason=f"history refreshed (n={n})",
        )


class GateTunerTunable(_BaseTunable):
    """Wraps ``GateTuner.calibrate`` — bounded quality-gate threshold offsets."""

    def __init__(
        self,
        tuner,
        outcomes_provider: OutcomesProvider,
        *,
        min_trades: int = 30,
        min_interval: float = 6 * 3600,
    ) -> None:
        super().__init__(
            name="gate_tuner",
            frequency=TuneFrequency.PERIODIC,
            dependencies=["ev_estimator"],
            min_trades=min_trades,
            min_interval=min_interval,
        )
        self._tuner = tuner
        self._outcomes_provider = outcomes_provider

    def _read_params(self) -> dict:
        return dict(self._tuner.all_offsets())

    def _apply_params(self, params: dict) -> None:
        tunable_families = getattr(type(self._tuner), "TUNABLE", {})
        offsets = {k: float(v) for k, v in (params or {}).items() if k in tunable_families}
        self._tuner._offsets = offsets
        save = getattr(self._tuner, "_save", None)
        if callable(save):
            save()

    def validate_params(self, params: dict) -> tuple[bool, str]:
        tunable_families = getattr(type(self._tuner), "TUNABLE", {})
        for family, off in (params or {}).items():
            spec = tunable_families.get(family)
            if spec is None:
                return False, f"non-tunable family '{family}' in offsets"
            lo, hi = spec[0], spec[1]
            if not (lo <= float(off) <= hi):
                return False, f"{family} offset {off} outside [{lo},{hi}]"
        return True, "ok"

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        outcomes = list(self._outcomes_provider() or [])
        changes = self._tuner.calibrate(outcomes)
        after = self._read_params()
        changed = bool(changes)
        self._mark_tuned(ctx)
        return TuneResult(
            tunable_name=self._name, success=True, changed=changed,
            params_before=before, params_after=after,
            reason=(
                f"{len(changes)} gate(s) adjusted" if changed
                else f"no change ({len(outcomes)} outcome rows)"
            ),
        )


# ───────────────────────────── Planner ─────────────────────────────────


class PlannerCalibratorTunable(_BaseTunable):
    """Wraps the planner ``Calibrator.calibrate`` — 4 PlannerConfig params."""

    _FIELDS = (
        "prefer_structure_sl_within_atr",
        "limit_order_zone_distance_atr",
        "default_be_trigger_r",
        "min_confidence_to_enter",
    )

    def __init__(
        self,
        calibrator,
        completed_provider: TradesProvider,
        *,
        planner=None,
        planner_enabled: bool = True,
        on_update: Optional[Callable[[object], None]] = None,
        min_trades: int = 50,
        min_interval: float = 4 * 3600,
    ) -> None:
        super().__init__(
            name="planner_calibrator",
            frequency=TuneFrequency.ON_TRADE_BATCH,
            dependencies=["regime_learner"],
            min_trades=min_trades,
            min_interval=min_interval,
        )
        self._calibrator = calibrator
        self._completed_provider = completed_provider
        self._planner = planner
        self._planner_enabled = planner_enabled
        self._on_update = on_update

    def _read_params(self) -> dict:
        cfg = self._calibrator.config
        return {f: getattr(cfg, f, None) for f in self._FIELDS}

    def _apply_params(self, params: dict) -> None:
        clean = {f: params[f] for f in self._FIELDS if params.get(f) is not None}
        new_cfg = replace(self._calibrator.config, **clean)
        self._calibrator.config = new_cfg
        if self._planner is not None and hasattr(self._planner, "update_config"):
            new_cfg.enabled = self._planner_enabled
            self._planner.update_config(new_cfg)

    def validate_params(self, params: dict) -> tuple[bool, str]:
        mc = params.get("min_confidence_to_enter")
        if mc is not None and not (0.0 <= float(mc) <= 1.0):
            return False, f"min_confidence_to_enter {mc} out of [0,1]"
        be = params.get("default_be_trigger_r")
        if be is not None and float(be) <= 0:
            return False, f"default_be_trigger_r {be} <= 0"
        zd = params.get("limit_order_zone_distance_atr")
        if zd is not None and float(zd) < 0:
            return False, f"limit_order_zone_distance_atr {zd} < 0"
        ps = params.get("prefer_structure_sl_within_atr")
        if ps is not None and float(ps) < 0:
            return False, f"prefer_structure_sl_within_atr {ps} < 0"
        return True, "ok"

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        completed = list(self._completed_provider() or [])
        if not self._calibrator.should_calibrate(len(completed)):
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=before,
                reason=f"not due ({len(completed)} completed)",
            )
        new_cfg = self._calibrator.calibrate(completed)
        if self._planner is not None and hasattr(self._planner, "update_config"):
            new_cfg.enabled = self._planner_enabled
            self._planner.update_config(new_cfg)
            try:
                new_cfg.save()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[planner_calibrator] config save failed: {}", exc)
        if self._on_update is not None:
            try:
                self._on_update(new_cfg)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[planner_calibrator] on_update failed: {}", exc)
        after = self._read_params()
        changed = after != before
        self._mark_tuned(ctx)
        return TuneResult(
            tunable_name=self._name, success=True, changed=changed,
            params_before=before, params_after=after,
            reason=f"calibrated on {len(completed)} completed plans"
            + ("" if changed else " (no change)"),
        )


# ───────────────────────────── Signal grading ──────────────────────────


class SignalLedgerTunable(_BaseTunable):
    """Wraps ``SignalLedger.run_grading_cycle`` — per-scan signal grading.

    Pure observation: grading is append-only, so there is nothing to validate
    or roll back. Returns a skipped result on cycles that graded nothing, so
    the audit log only records real grading work.
    """

    def __init__(self, ledger, prices_provider: PricesProvider) -> None:
        super().__init__(
            name="signal_ledger",
            frequency=TuneFrequency.PER_SCAN_CYCLE,
            dependencies=[],
            min_trades=0,
            min_interval=0.0,
        )
        self._ledger = ledger
        self._prices_provider = prices_provider

    def _read_params(self) -> dict:
        return {}

    def _apply_params(self, params: dict) -> None:
        return None

    def rollback(self) -> bool:
        return True

    def tune(self, ctx: TuneContext) -> TuneResult:
        prices = ctx.current_prices or {}
        if not prices and self._prices_provider is not None:
            try:
                prices = self._prices_provider() or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("[signal_ledger] prices provider failed: {}", exc)
                prices = {}
        if not prices:
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                reason="no prices this cycle",
            )
        stats = self._ledger.run_grading_cycle(prices) or {}
        graded = int(stats.get("graded", 0) or 0)
        finalized = int(stats.get("finalized", stats.get("finalised", 0)) or 0)
        did_work = bool(graded or finalized)
        return TuneResult(
            tunable_name=self._name,
            success=True,
            skipped=not did_work,
            changed=did_work,
            reason=f"graded={graded} finalized={finalized}",
        )


# ───────────────────────── Post-close MFE/MAE ──────────────────────────


class PostCloseTrackerTunable(_BaseTunable):
    """Wraps ``PostCloseTracker.process_pending_checks`` — per-scan forward
    MFE/MAE sampling on recently-closed trades.

    Pure observation (append-only price checks); nothing to validate or roll
    back. Returns a skipped result on cycles with no pending checks so the
    audit log only records real work. The data source (live platform manager)
    is supplied by an injected provider so this adapter stays decoupled.
    """

    def __init__(self, tracker, data_source_provider: Optional[Callable[[], object]] = None) -> None:
        super().__init__(
            name="post_close_tracker",
            frequency=TuneFrequency.PER_SCAN_CYCLE,
            dependencies=[],
            min_trades=0,
            min_interval=0.0,
        )
        self._tracker = tracker
        self._data_source_provider = data_source_provider

    def _read_params(self) -> dict:
        return {
            "enabled": bool(getattr(self._tracker, "enabled", False)),
            "pending_count": int(getattr(self._tracker, "pending_count", 0) or 0),
        }

    def _apply_params(self, params: dict) -> None:
        return None

    def rollback(self) -> bool:
        return True

    def tune(self, ctx: TuneContext) -> TuneResult:
        if not getattr(self._tracker, "enabled", False):
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                reason="tracker disabled",
            )
        pending = int(getattr(self._tracker, "pending_count", 0) or 0)
        if not pending:
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                reason="no pending checks",
            )
        data_source = None
        if self._data_source_provider is not None:
            try:
                data_source = self._data_source_provider()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[post_close_tracker] data source provider failed: {}", exc)
                data_source = None
        if data_source is None:
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                reason="no data source this cycle",
            )
        self._tracker.process_pending_checks(data_source)
        remaining = int(getattr(self._tracker, "pending_count", 0) or 0)
        return TuneResult(
            tunable_name=self._name, success=True, changed=(remaining != pending),
            reason=f"processed checks (pending {pending} -> {remaining})",
        )


# ───────────────────────── Vote calibration ────────────────────────────


class VoteCalibratorTunable(_BaseTunable):
    """Wraps ``VoteCalibrator.recalibrate`` — per-module consensus vote weights.

    Periodic: recomputes each module's weight multiplier from its graded
    accuracy (read via EmitterFeedback). Depends on ``signal_ledger`` so the
    multipliers are built from freshly-graded signals. The agent snapshots the
    published multiplier map and rolls it back if a recompute produces values
    outside the configured [floor, ceiling] band.
    """

    def __init__(self, calibrator, *, min_interval: float = 3600.0) -> None:
        super().__init__(
            name="vote_calibrator",
            frequency=TuneFrequency.PERIODIC,
            dependencies=["signal_ledger"],
            min_trades=0,
            min_interval=float(min_interval),
        )
        self._calibrator = calibrator

    def _read_params(self) -> dict:
        try:
            return self._calibrator.get_state()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[vote_calibrator] get_state failed: {}", exc)
            return {}

    def _apply_params(self, params: dict) -> None:
        self._calibrator.restore_multipliers((params or {}).get("multipliers", {}))

    def validate_params(self, params: dict) -> tuple[bool, str]:
        mults = (params or {}).get("multipliers", {})
        if not isinstance(mults, dict):
            return False, "multipliers not a dict"
        floor = float(params.get("floor", 0.0)) if isinstance(params, dict) else 0.0
        ceiling = float(params.get("ceiling", float("inf"))) if isinstance(params, dict) else float("inf")
        for module, w in mults.items():
            try:
                wv = float(w)
            except (TypeError, ValueError):
                return False, f"multiplier for {module} not numeric: {w!r}"
            if not math.isfinite(wv):
                return False, f"multiplier for {module} not finite: {wv}"
            # Allow a small epsilon past the band for floating-point safety.
            if wv < floor - 1e-9 or wv > ceiling + 1e-9:
                return False, f"multiplier for {module} ({wv}) outside [{floor}, {ceiling}]"
        return True, "ok"

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        cal = self._calibrator.recalibrate()
        after = self._read_params()
        if getattr(cal, "skipped", False):
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=after,
                reason=getattr(cal, "reason", "skipped"),
            )
        changed = after.get("multipliers") != before.get("multipliers")
        self._mark_tuned(ctx)
        return TuneResult(
            tunable_name=self._name, success=True, changed=changed,
            params_before=before, params_after=after,
            reason=(
                f"recalibrated {getattr(cal, 'qualifying_modules', 0)} module(s)"
                + ("" if changed else " (no change)")
            ),
        )


# ─────────────────────── Counterfactual attribution ────────────────────


class CounterfactualTunable(_BaseTunable):
    """Wraps ``CounterfactualEngine.maybe_recompute`` — periodic leave-one-out
    module attribution.

    Pure analysis: the engine only reads closed-trade snapshots and caches a
    ranked module table, so there is nothing to validate or roll back. Runs on
    the trade-close batch cadence (every ``attribution_interval`` trades, gated
    by ``min_trades_for_attribution``); a cycle that does not recompute records
    a harmless skip. ``attribution_lookback`` and ``attribution_interval`` are
    exposed as the tunable knobs.
    """

    def __init__(
        self, engine, *, min_trades: int = 50, min_interval: float = 0.0,
    ) -> None:
        super().__init__(
            name="counterfactual",
            frequency=TuneFrequency.ON_TRADE_BATCH,
            dependencies=[],
            min_trades=int(min_trades),
            min_interval=float(min_interval),
        )
        self._engine = engine

    def _read_params(self) -> dict:
        return {
            "enabled": bool(getattr(self._engine, "enabled", False)),
            "lookback": int(getattr(self._engine, "lookback", 0) or 0),
            "interval": int(getattr(self._engine, "interval", 0) or 0),
            "min_trades_for_attribution": int(
                getattr(self._engine, "min_trades_for_attribution", 0) or 0
            ),
        }

    def _apply_params(self, params: dict) -> None:
        if not params:
            return
        self._engine.set_params(
            attribution_lookback=params.get("lookback"),
            attribution_interval=params.get("interval"),
        )

    def rollback(self) -> bool:
        if self._snapshot is None:
            return True
        try:
            self._apply_params(self._snapshot)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[counterfactual] rollback failed: {}", exc)
            return False

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        if not getattr(self._engine, "enabled", False):
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=before,
                reason="engine disabled",
            )
        payload = self._engine.maybe_recompute(ctx.total_trades)
        self._mark_tuned(ctx)
        if not payload:
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=before,
                reason="not due / insufficient trades",
            )
        after = self._read_params()
        analyzed = int(payload.get("trades_analyzed", 0) or 0)
        modules = int(payload.get("module_count", 0) or 0)
        return TuneResult(
            tunable_name=self._name, success=True, changed=True,
            params_before=before, params_after=after,
            reason=f"attributed {modules} module(s) over {analyzed} trades",
        )


# ───────────────────────── Module governor ─────────────────────────────


class ModuleGovernorTunable(_BaseTunable):
    """Wraps ``ModuleGovernor.evaluate_transitions`` — L3 shadow mode.

    Periodic: re-evaluates every governed module against its graded accuracy
    (read via EmitterFeedback) and moves it between ACTIVE / SHADOW / DISABLED.
    Depends on ``signal_ledger`` so transitions are decided from freshly-graded
    signals. The agent snapshots the published mode map and rolls it back if a
    pass produces an unknown mode value.
    """

    def __init__(self, governor, *, min_interval: float = 3600.0) -> None:
        super().__init__(
            name="module_governor",
            frequency=TuneFrequency.PERIODIC,
            dependencies=["signal_ledger"],
            min_trades=0,
            min_interval=float(min_interval),
        )
        self._governor = governor

    def _read_params(self) -> dict:
        try:
            return self._governor.get_state()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[module_governor] get_state failed: {}", exc)
            return {}

    def _apply_params(self, params: dict) -> None:
        self._governor.restore_modes((params or {}).get("modes", {}))

    def validate_params(self, params: dict) -> tuple[bool, str]:
        modes = (params or {}).get("modes", {})
        if not isinstance(modes, dict):
            return False, "modes not a dict"
        valid = {"ACTIVE", "SHADOW", "DISABLED"}
        for module, mode in modes.items():
            if str(mode).upper() not in valid:
                return False, f"invalid mode for {module}: {mode!r}"
        return True, "ok"

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        transitions = self._governor.evaluate_transitions() or []
        after = self._read_params()
        changed = bool(transitions) or after.get("modes") != before.get("modes")
        self._mark_tuned(ctx)
        if not transitions:
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=after,
                reason="no module transitions",
            )
        return TuneResult(
            tunable_name=self._name, success=True, changed=changed,
            params_before=before, params_after=after,
            reason="; ".join(
                f"{t.module} {t.old_mode}->{t.new_mode}" for t in transitions
            ),
        )


# ───────────────────── L5 — evolution / discovery ──────────────────────


class ParameterEvolverTunable(_BaseTunable):
    """Wraps ``ParameterEvolver.run_cycle`` — L5a parameter evolution.

    Periodic: advances any shadow candidates with newly-closed trades, resolves
    those with enough evidence (promote / reject / extend), and — cooldown and
    concurrency permitting — runs a fresh replay tournament to seed new
    candidates.  The evolver only *recommends*; an injected callback (wired by
    the caller) is what actually applies an approved value, so there is nothing
    here to validate or roll back.  Depends on ``counterfactual`` so it explores
    over the freshest closed-trade snapshots.
    """

    def __init__(self, evolver, *, min_trades: int = 50, min_interval: float = 0.0) -> None:
        super().__init__(
            name="parameter_evolver",
            frequency=TuneFrequency.PERIODIC,
            dependencies=["counterfactual"],
            min_trades=int(min_trades),
            min_interval=float(min_interval),
        )
        self._evolver = evolver

    def _read_params(self) -> dict:
        try:
            st = self._evolver.get_state()
            return {
                "enabled": bool(st.get("enabled", False)),
                "active_shadows": int(st.get("active_shadow_count", 0) or 0),
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[parameter_evolver] read params failed: {}", exc)
            return {}

    def _apply_params(self, params: dict) -> None:  # nothing to apply / roll back
        return None

    def rollback(self) -> bool:
        return True

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        if not getattr(self._evolver, "enabled", False):
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=before, reason="evolver disabled",
            )
        summary = self._evolver.run_cycle()
        self._mark_tuned(ctx)
        decisions = summary.get("decisions", []) if isinstance(summary, dict) else []
        after = self._read_params()
        changed = bool(decisions)
        if not decisions and not summary.get("ran_tournament"):
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=after, reason="no shadow progress",
            )
        promoted = [d for d in decisions if d.get("decision") in ("promoted", "recommended")]
        reason = (
            f"{len(promoted)} promotion(s), {len(decisions)} decision(s); "
            f"{summary.get('active_shadows', 0)} active shadow(s)"
        )
        return TuneResult(
            tunable_name=self._name, success=True, changed=changed,
            params_before=before, params_after=after, reason=reason,
        )


class _AnalysisRecomputeTunable(_BaseTunable):
    """Shared base for the pure-analysis L5 engines (interaction / discovery).

    Each wraps a component exposing ``maybe_recompute(total_trades) -> dict|None``
    plus an ``enabled`` flag.  Nothing to validate or roll back — they only read
    closed-trade snapshots and cache a ranked table for the dashboard.
    """

    def __init__(self, engine, *, name: str, min_trades: int) -> None:
        super().__init__(
            name=name,
            frequency=TuneFrequency.ON_TRADE_BATCH,
            dependencies=[],
            min_trades=int(min_trades),
            min_interval=0.0,
        )
        self._engine = engine

    def _read_params(self) -> dict:
        return {
            "enabled": bool(getattr(self._engine, "enabled", False)),
            "lookback": int(getattr(self._engine, "lookback", 0) or 0),
            "interval": int(getattr(self._engine, "interval", 0) or 0),
        }

    def _apply_params(self, params: dict) -> None:
        return None

    def rollback(self) -> bool:
        return True

    def _result_summary(self, payload: dict) -> str:
        raise NotImplementedError

    def tune(self, ctx: TuneContext) -> TuneResult:
        before = self._begin()
        if not getattr(self._engine, "enabled", False):
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=before, reason="engine disabled",
            )
        payload = self._engine.maybe_recompute(ctx.total_trades)
        self._mark_tuned(ctx)
        if not payload:
            return TuneResult(
                tunable_name=self._name, success=True, skipped=True,
                params_before=before, params_after=before,
                reason="not due / insufficient trades",
            )
        return TuneResult(
            tunable_name=self._name, success=True, changed=True,
            params_before=before, params_after=self._read_params(),
            reason=self._result_summary(payload),
        )


class ModuleInteractionTunable(_AnalysisRecomputeTunable):
    """Wraps ``ModuleInteractionEngine.maybe_recompute`` — L5b leave-K-out."""

    def __init__(self, engine, *, min_trades: int = 50) -> None:
        super().__init__(engine, name="module_interaction", min_trades=min_trades)

    def _result_summary(self, payload: dict) -> str:
        pairs = payload.get("pairs", []) or []
        analyzed = int(payload.get("trades_analyzed", 0) or 0)
        toxic = sum(1 for p in pairs if p.get("classification") == "TOXIC")
        return f"{len(pairs)} pair(s) ({toxic} toxic) over {analyzed} trades"


class SignalDiscoveryTunable(_AnalysisRecomputeTunable):
    """Wraps ``SignalDiscoveryEngine.maybe_recompute`` — L5c rule mining."""

    def __init__(self, engine, *, min_trades: int = 100) -> None:
        super().__init__(engine, name="signal_discovery", min_trades=min_trades)

    def _result_summary(self, payload: dict) -> str:
        analyzed = int(payload.get("trades_analyzed", 0) or 0)
        rules = int(payload.get("rule_count", 0) or 0)
        qual = int(payload.get("qualifying_count", 0) or 0)
        return f"{rules} rule(s), {qual} qualifying over {analyzed} trades"


# ───────────────────────── Consumers / observers ───────────────────────


class ConsumerTunable:
    """Read-only registry entry for a component that CONSUMES tuned params or
    only reports state — it is never auto-tuned by the agent.

    Components like the RiskEngine, PositionSizer, Orchestrator, Governor and
    TradeManager don't learn; they read parameters other tunables produce. The
    RL stack is dormant until a checkpoint exists. Registering them here makes
    the agent the single place that sees the WHOLE system: their current config
    shows up in ``get_system_tuning_status`` and the startup validation, and a
    forced run records a harmless skip rather than mutating anything.

    Frequency is ON_DEMAND so the agent's trade-close / scan-cycle / periodic
    triggers never pick it up — only ``force_tune_all`` reaches it, and even
    then it skips.
    """

    def __init__(
        self,
        name: str,
        params_provider: Optional[Callable[[], dict]] = None,
        *,
        dormant: bool = False,
        note: str = "",
    ) -> None:
        self._name = name
        self._params_provider = params_provider
        self._dormant = bool(dormant)
        self._note = note or ("dormant — not active" if dormant else "consumer — not auto-tuned")

    @property
    def tunable_name(self) -> str:
        return self._name

    @property
    def frequency(self) -> TuneFrequency:
        return TuneFrequency.ON_DEMAND

    @property
    def dependencies(self) -> list[str]:
        return []

    @property
    def min_trades_required(self) -> int:
        return 0

    @property
    def min_interval_seconds(self) -> float:
        return 0.0

    def should_tune(self, ctx: TuneContext) -> bool:
        return False

    def get_current_params(self) -> dict:
        base = {"role": "dormant" if self._dormant else "consumer", "note": self._note}
        if self._params_provider is None:
            return base
        try:
            params = self._params_provider() or {}
            if isinstance(params, dict):
                base.update(params)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[{}] params provider failed: {}", self._name, exc)
        return base

    def tune(self, ctx: TuneContext) -> TuneResult:
        return TuneResult(
            tunable_name=self._name, success=True, skipped=True,
            params_before=self.get_current_params(),
            params_after=self.get_current_params(),
            reason=self._note,
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        return True, "ok"

    def rollback(self) -> bool:
        return True


__all__ = [
    "ScoreOptimizerTunable",
    "RegimeLearnerTunable",
    "PairLearnerTunable",
    "SessionLearnerTunable",
    "EVEstimatorTunable",
    "GateTunerTunable",
    "PlannerCalibratorTunable",
    "SignalLedgerTunable",
    "PostCloseTrackerTunable",
    "VoteCalibratorTunable",
    "CounterfactualTunable",
    "ModuleGovernorTunable",
    "ParameterEvolverTunable",
    "ModuleInteractionTunable",
    "SignalDiscoveryTunable",
    "ConsumerTunable",
]
