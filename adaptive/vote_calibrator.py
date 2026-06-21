"""
APEX TRADER — Vote Calibrator

Every brain module casts a directional vote each scan cycle, and the
:func:`directional_consensus.decide` math weights each vote by a *static*
``ConsensusConfig.weights`` entry — momentum and structure carry whatever the
config says, regardless of how accurate each module has actually been.

The :class:`~adaptive.signal_ledger.SignalLedger` already grades EVERY signal
(traded or blocked) on whether price moved the predicted way, and
:class:`~adaptive.emitter_feedback.EmitterFeedbackService` exposes the resulting
per-emitter accuracy.  This calibrator closes that loop: it reads each module's
track record and turns it into a *weight multiplier* centred on 1.0 — a module
that is reliably right votes louder, one that is mostly noise votes softer.

Key safety properties (this is the last place the system should be allowed to
distort itself):

* **Centred on 1.0.**  A module of average accuracy gets multiplier ≈ 1.0, so a
  panel of equally-accurate modules produces NO change — calibration only ever
  re-balances *relative* to the panel mean.
* **Bayesian shrinkage.**  A module with few graded signals is pulled toward the
  panel mean, so a 3-signal fluke never dominates a 200-signal track record.
* **Floor + ceiling.**  No module can be silenced (floor) or allowed to
  dominate (ceiling); the multiplier is clamped to a configured band.
* **Read-only over the ledger.**  It CONSUMES EmitterFeedback; it never writes
  to the ledger or changes how signals are graded.

Recalibration is periodic (driven by the TunerAgent), not per-signal: the
scanner reads a cached, atomically-published multiplier map.  Inert unless
``VoteCalibratorConfig.vote_calibration_enabled`` is on — when off,
``calibrated_weight`` returns the base weight unchanged.

Leaf module — standard library + loguru only.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from loguru import logger

from adaptive.tunable import TuningGuardMixin

# The nine brain modules that cast directional votes (matches the emitter names
# the SignalLedger records and the keys in ConsensusConfig.weights). "consensus"
# is excluded — it is a derived verdict, not an independent module to re-weight.
DEFAULT_VOTE_MODULES: tuple[str, ...] = (
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

_VALID_METHODS = ("softmax", "proportional", "log_odds")
_EPS = 1e-6


@dataclass
class VoteCalibration:
    """The result of one calibration pass — the published multiplier map plus
    the inputs that produced it, for audit / dashboard visibility."""

    multipliers: Dict[str, float] = field(default_factory=dict)
    accuracies: Dict[str, float] = field(default_factory=dict)   # shrunk accuracy used
    raw_accuracies: Dict[str, float] = field(default_factory=dict)
    sample_sizes: Dict[str, int] = field(default_factory=dict)
    method: str = "softmax"
    qualifying_modules: int = 0
    computed_at: float = 0.0
    skipped: bool = False
    reason: str = ""
    # Whether the marginal-R (counterfactual) signal was blended into the
    # multipliers this pass, and the per-module marginal R per trade used.
    counterfactual_used: bool = False
    marginal_r: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "multipliers": dict(self.multipliers),
            "accuracies": dict(self.accuracies),
            "raw_accuracies": dict(self.raw_accuracies),
            "sample_sizes": dict(self.sample_sizes),
            "method": self.method,
            "qualifying_modules": self.qualifying_modules,
            "computed_at": self.computed_at,
            "skipped": self.skipped,
            "reason": self.reason,
            "counterfactual_used": bool(self.counterfactual_used),
            "marginal_r": dict(self.marginal_r),
        }


def _logit(p: float) -> float:
    p = min(max(p, _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


class VoteCalibrator(TuningGuardMixin):
    """Re-weights module votes by their graded track record.

    Reads accuracy via an injected :class:`EmitterFeedbackService` (read-only)
    and publishes a ``{module: weight_multiplier}`` map the scanner multiplies
    into the static consensus weights.  Thread-safe: the published map is an
    immutable dict swapped atomically, so per-vote reads need no lock.
    """

    def __init__(
        self,
        config,
        emitter_feedback=None,
        *,
        modules: Optional[tuple[str, ...] | list[str]] = None,
    ) -> None:
        self._config = config
        self._emitter_feedback = emitter_feedback
        # Optional read-only CounterfactualEngine — supplies each module's
        # marginal R (per attributed trade) so a module that is accurate yet
        # harmful by marginal R can be damped. Read-only: never recomputes.
        self._counterfactual = None
        # Optional Learning→Governance recommendation gateway. When wired, a new
        # multiplier map is published as a WEIGHT_UPDATE recommendation that must
        # be approved before it takes effect (auto-approved until Phase 7, so
        # behaviour is unchanged). When None, the map publishes directly.
        self._recommendation_gateway = None
        # Optional read-only OutcomeFeedback — attached as supporting evidence on
        # the WEIGHT_UPDATE recommendation (per-module accuracy snapshot).
        self._outcome_feedback = None
        self._modules = tuple(modules) if modules else DEFAULT_VOTE_MODULES
        # Published, immutable multiplier map (never mutated in place — swapped
        # atomically on recalibrate so concurrent scanner reads are safe).
        self._multipliers: Dict[str, float] = {}
        self._last_calibration: Optional[VoteCalibration] = None
        # Only guards the recompute / snapshot bookkeeping, not the hot reads.
        self._lock = threading.Lock()

    # ── Configuration / wiring ────────────────────────────────────────────

    def set_emitter_feedback(self, emitter_feedback) -> None:
        """Inject (or replace) the read-only feedback source."""
        self._emitter_feedback = emitter_feedback

    def set_counterfactual(self, counterfactual) -> None:
        """Inject (or replace, with ``None``) the read-only counterfactual engine.

        Supplies the marginal-R signal blended into the weight when
        ``use_counterfactual_weight`` is on. Read-only: only the cached
        attribution table is consulted; the engine is never recomputed here.
        """
        self._counterfactual = counterfactual

    def set_recommendation_gateway(self, gateway) -> None:
        """Inject (or clear) the Learning→Governance recommendation gateway.

        When wired, a freshly-computed multiplier map is submitted as a
        WEIGHT_UPDATE recommendation and only published if approved. Auto-approved
        until Governance (Phase 7) flips ``governance_required`` on, so wiring
        this is behaviour-neutral.
        """
        self._recommendation_gateway = gateway

    def set_outcome_feedback(self, outcome_feedback) -> None:
        """Inject (or clear) the read-only OutcomeFeedback source used as
        supporting evidence (per-module accuracy) on weight recommendations."""
        self._outcome_feedback = outcome_feedback

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._config, "vote_calibration_enabled", False))

    # ── Hot path: weight lookup (thread-safe, lock-free) ──────────────────

    def multiplier_for(self, module: str) -> float:
        """Return the cached weight multiplier for ``module`` (1.0 default).

        Always safe to call from any thread — reads the atomically-published
        map and never recomputes. Returns 1.0 (neutral) when calibration is
        disabled, the module is unknown, or no calibration has run yet.
        """
        if not self.enabled:
            return 1.0
        return float(self._multipliers.get(module, 1.0))

    def calibrated_weight(self, module: str, base_weight: float) -> float:
        """Scale a base consensus weight by the module's calibrated multiplier.

        When calibration is disabled this returns ``base_weight`` unchanged, so
        callers can wire it in unconditionally with zero behaviour change.
        """
        try:
            base = float(base_weight)
        except (TypeError, ValueError):
            return base_weight
        if not self.enabled:
            return base
        return base * self.multiplier_for(module)

    def get_weight_multipliers(self) -> Dict[str, float]:
        """A copy of the current published multiplier map."""
        return dict(self._multipliers)

    @property
    def last_calibration(self) -> Optional[VoteCalibration]:
        return self._last_calibration

    # ── Snapshot / restore (used by the TunerAgent adapter for rollback) ──

    def get_state(self) -> dict:
        """JSON-serialisable snapshot of the published map + hyperparameters."""
        cfg = self._config
        return {
            "multipliers": dict(self._multipliers),
            "method": str(getattr(cfg, "vote_weight_method", "softmax")),
            "temperature": float(getattr(cfg, "vote_weight_temperature", 1.0)),
            "floor": float(getattr(cfg, "vote_weight_floor", 0.1)),
            "ceiling": float(getattr(cfg, "vote_weight_ceiling", 3.0)),
            "shrinkage": float(getattr(cfg, "vote_calibration_shrinkage", 0.5)),
            "min_signals": int(getattr(cfg, "vote_calibration_min_signals", 20)),
            "use_counterfactual_weight": bool(
                getattr(cfg, "use_counterfactual_weight", False)
            ) and self._counterfactual is not None,
            "counterfactual_weight_blend": float(
                getattr(cfg, "counterfactual_weight_blend", 0.3)
            ),
        }

    def restore_multipliers(self, multipliers: Optional[dict]) -> None:
        """Re-publish a previously-snapshotted multiplier map (rollback)."""
        with self._lock:
            self._multipliers = {
                str(k): float(v)
                for k, v in (multipliers or {}).items()
                if isinstance(v, (int, float)) and math.isfinite(float(v))
            }

    # ── Recalibration (periodic, agent-driven) ────────────────────────────

    def recalibrate(self, lookback: Optional[int] = None) -> VoteCalibration:
        """Recompute and publish multipliers from the latest graded accuracy.

        Periodic — driven by the TunerAgent, never per-signal. Guarded: a direct
        call while the agent is sole authority is blocked (returns the last
        calibration unchanged) so nothing can bypass the central tuner. Never
        raises — any failure leaves the previously-published map intact.
        """
        if self._tuning_blocked("recalibrate"):
            return self._last_calibration or VoteCalibration(
                multipliers=dict(self._multipliers), skipped=True,
                reason="tuning blocked (agent is sole authority)",
            )
        return self._recalibrate_unguarded(lookback)

    def _recalibrate_unguarded(self, lookback: Optional[int] = None) -> VoteCalibration:
        cfg = self._config
        if not self.enabled:
            cal = VoteCalibration(
                multipliers=dict(self._multipliers), skipped=True,
                reason="calibration disabled",
                computed_at=time.time(),
            )
            self._last_calibration = cal
            return cal
        if self._emitter_feedback is None:
            cal = VoteCalibration(
                multipliers=dict(self._multipliers), skipped=True,
                reason="no emitter feedback wired", computed_at=time.time(),
            )
            self._last_calibration = cal
            return cal

        lb = int(lookback) if lookback else int(
            getattr(cfg, "vote_calibration_lookback", 100)
        )
        try:
            summaries = self._emitter_feedback.get_all_emitter_summaries(lookback=lb)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[vote-calibrator] feedback fetch failed: {}", exc)
            cal = VoteCalibration(
                multipliers=dict(self._multipliers), skipped=True,
                reason=f"feedback fetch failed: {exc}", computed_at=time.time(),
            )
            self._last_calibration = cal
            return cal

        accuracy_data: Dict[str, dict] = {}
        for module in self._modules:
            resp = summaries.get(module)
            if resp is None:
                continue
            accuracy_data[module] = {
                "accuracy": float(getattr(resp, "accuracy_all", 0.0) or 0.0),
                "n": int(getattr(resp, "total_signals", 0) or 0),
            }

        cal = self._compute_calibration(accuracy_data, self._cf_marginal_r_map())
        # Route the new multiplier map through the Learning→Governance gateway:
        # publish only when the WEIGHT_UPDATE recommendation is approved. With no
        # gateway wired (or governance not required) this is always approved, so
        # the map publishes exactly as before — behaviour-neutral.
        if cal.skipped or self._publish_approved(cal):
            with self._lock:
                self._multipliers = dict(cal.multipliers)
                self._last_calibration = cal
        else:
            # Rejected by Governance — keep the previously-published map and
            # record that the new calibration did not take effect.
            cal.skipped = True
            cal.reason = (cal.reason + " | " if cal.reason else "") + \
                "rejected by governance"
            cal.multipliers = dict(self._multipliers)
            self._last_calibration = cal
            logger.info(
                "[vote-calibrator] new calibration rejected by governance — "
                "retaining previous multiplier map",
            )
            return cal
        if not cal.skipped:
            logger.info(
                "[vote-calibrator] recalibrated ({} method, {} qualifying): {}",
                cal.method, cal.qualifying_modules,
                ", ".join(
                    f"{m}×{w:.2f}" for m, w in sorted(cal.multipliers.items())
                ) or "none",
            )
        return cal

    def _publish_approved(self, cal: "VoteCalibration") -> bool:
        """Submit the new multiplier map to the recommendation gateway.

        Returns True (publish) when no gateway is wired or the WEIGHT_UPDATE
        recommendation is approved. Never raises — any fault publishes (the
        gateway is an authorisation overlay, not a tuning authority).
        """
        gateway = self._recommendation_gateway
        if gateway is None:
            return True
        try:
            from adaptive.recommendations import (
                LearningRecommendation,
                RecommendationType,
            )
            rec = LearningRecommendation(
                source="vote_calibrator",
                recommendation_type=RecommendationType.WEIGHT_UPDATE,
                payload={"multipliers": dict(cal.multipliers)},
                confidence=float(cal.qualifying_modules) / max(len(self._modules), 1),
                evidence={
                    "method": cal.method,
                    "qualifying_modules": cal.qualifying_modules,
                    "sample_sizes": dict(cal.sample_sizes),
                    "counterfactual_used": bool(cal.counterfactual_used),
                    "module_accuracy": self._outcome_feedback_evidence(),
                },
            )
            return bool(gateway.submit(rec).approved)
        except Exception as exc:  # noqa: BLE001 — authorisation overlay must never break tuning
            logger.debug("[vote-calibrator] gateway submit failed: {}", exc)
            return True

    def _outcome_feedback_evidence(self) -> dict:
        """Per-module accuracy snapshot from OutcomeFeedback (evidence only).

        Empty when unwired or on any fault — never raises.
        """
        of = self._outcome_feedback
        if of is None:
            return {}
        try:
            getter = getattr(of, "get_module_accuracy", None) or getattr(
                of, "module_accuracy", None
            )
            if getter is None:
                return {}
            data = getter() if callable(getter) else getter
            return dict(data) if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[vote-calibrator] outcome-feedback evidence failed: {}", exc)
            return {}

    # ── Counterfactual signal (read-only) ─────────────────────────────────

    def _cf_marginal_r_map(self) -> Dict[str, dict]:
        """Per-module marginal R per attributed trade from the counterfactual
        cache. Empty when disabled / unwired / no data. Never raises."""
        if not bool(getattr(self._config, "use_counterfactual_weight", False)):
            return {}
        cf = self._counterfactual
        if cf is None:
            return {}
        try:
            cached = cf.get_cached_attributions() or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[vote-calibrator] cf cache read failed: {}", exc)
            return {}
        out: Dict[str, dict] = {}
        for m in cached.get("modules", []) or []:
            try:
                module = str(m.get("module") or "")
                if not module:
                    continue
                trades = int(m.get("trades_involved", 0) or 0)
                marginal_r = float(m.get("marginal_r", 0.0) or 0.0)
                out[module] = {
                    "mr_per_trade": marginal_r / trades if trades > 0 else 0.0,
                    "trades": trades,
                }
            except Exception as exc:  # noqa: BLE001
                logger.debug("[vote-calibrator] cf row parse failed: {}", exc)
        return out

    # ── Core math (pure, deterministic) ───────────────────────────────────

    def _compute_calibration(
        self, accuracy_data: Dict[str, dict], cf_data: Optional[Dict[str, dict]] = None,
    ) -> VoteCalibration:
        """Turn per-module accuracy + sample size into bounded, mean-1.0
        multipliers. Pure function of the inputs and the current config.

        Modules below ``min_signals`` graded samples are held at multiplier 1.0
        (not enough evidence). With fewer than two qualifying modules there is
        nothing to differentiate, so every module stays at 1.0.
        """
        cfg = self._config
        method = str(getattr(cfg, "vote_weight_method", "softmax"))
        if method not in _VALID_METHODS:
            method = "softmax"
        temperature = float(getattr(cfg, "vote_weight_temperature", 1.0))
        if temperature <= 0:
            temperature = 1.0
        floor = float(getattr(cfg, "vote_weight_floor", 0.1))
        ceiling = float(getattr(cfg, "vote_weight_ceiling", 3.0))
        shrinkage = float(getattr(cfg, "vote_calibration_shrinkage", 0.5))
        min_signals = int(getattr(cfg, "vote_calibration_min_signals", 20))

        sample_sizes = {m: int(d.get("n", 0)) for m, d in accuracy_data.items()}
        raw_acc = {m: float(d.get("accuracy", 0.0)) for m, d in accuracy_data.items()}

        qualifying = {
            m: raw_acc[m]
            for m, n in sample_sizes.items()
            if n >= min_signals
        }
        now = time.time()
        if len(qualifying) < 2:
            # Nothing to differentiate — everyone stays neutral.
            mults = {m: 1.0 for m in accuracy_data}
            return VoteCalibration(
                multipliers=mults,
                accuracies={},
                raw_accuracies=raw_acc,
                sample_sizes=sample_sizes,
                method=method,
                qualifying_modules=len(qualifying),
                computed_at=now,
                skipped=True,
                reason=f"only {len(qualifying)} module(s) with >= {min_signals} signals",
            )

        mean_acc = sum(qualifying.values()) / len(qualifying)

        # Bayesian shrinkage toward the panel mean — a thin sample is pulled
        # harder. pseudo = shrinkage * min_signals pseudo-observations at the mean.
        pseudo = max(0.0, shrinkage) * float(min_signals)
        shrunk: Dict[str, float] = {}
        for m, acc in qualifying.items():
            n = sample_sizes[m]
            denom = n + pseudo
            shrunk[m] = (n * acc + pseudo * mean_acc) / denom if denom > 0 else acc

        # Map shrunk accuracy → raw multiplier per method, then normalise so the
        # mean multiplier is exactly 1.0 (neutral panel ⇒ no change).
        raw_mult: Dict[str, float] = {}
        for m, sa in shrunk.items():
            if method == "proportional":
                raw_mult[m] = max(sa, 0.0)
            elif method == "log_odds":
                raw_mult[m] = math.exp(_logit(sa) / temperature)
            else:  # softmax
                raw_mult[m] = math.exp(sa / temperature)

        mean_raw = sum(raw_mult.values()) / len(raw_mult)
        mults: Dict[str, float] = {}
        if mean_raw <= 0:
            mults = {m: 1.0 for m in accuracy_data}
        else:
            for m in accuracy_data:
                if m in raw_mult:
                    val = raw_mult[m] / mean_raw
                    mults[m] = float(min(max(val, floor), ceiling))
                else:
                    # Below the sample floor — stay neutral.
                    mults[m] = 1.0

        # ── Blend the marginal-R (counterfactual) signal, if available ────────
        cf_used, cf_map = self._blend_counterfactual(mults, cf_data, floor, ceiling)

        return VoteCalibration(
            multipliers=mults,
            accuracies=shrunk,
            raw_accuracies=raw_acc,
            sample_sizes=sample_sizes,
            method=method,
            qualifying_modules=len(qualifying),
            computed_at=now,
            skipped=False,
            reason="ok" if not cf_used else "ok (counterfactual blended)",
            counterfactual_used=cf_used,
            marginal_r=cf_map,
        )

    def _blend_counterfactual(
        self,
        mults: Dict[str, float],
        cf_data: Optional[Dict[str, dict]],
        floor: float,
        ceiling: float,
    ) -> tuple[bool, Dict[str, float]]:
        """Blend marginal R per trade into ``mults`` in place (mutates ``mults``).

        The continuous complement to the Module Governor's discrete shadow: a
        module that is accurate yet harmful by marginal R is damped below 1.0.
        Combined as a weighted geometric mean and re-centred on 1.0. No-op (and
        ``mults`` unchanged) when the flag is off, there is no engine wired, or
        fewer than two modules clear the trust floor. Returns
        ``(blended?, {module: marginal_r_per_trade})``.
        """
        cfg = self._config
        if not bool(getattr(cfg, "use_counterfactual_weight", False)) or not cf_data:
            return False, {}
        blend = float(getattr(cfg, "counterfactual_weight_blend", 0.3))
        if blend <= 0:
            return False, {}
        min_trades = int(getattr(cfg, "counterfactual_weight_min_trades", 100))
        temperature = float(getattr(cfg, "vote_weight_temperature", 1.0))
        if temperature <= 0:
            temperature = 1.0

        # Only modules in the panel with a trusted (enough-trades) marginal R.
        trusted = {
            m: float(d.get("mr_per_trade", 0.0))
            for m, d in cf_data.items()
            if m in mults and int(d.get("trades", 0)) >= min_trades
        }
        if len(trusted) < 2:
            return False, {m: float(d.get("mr_per_trade", 0.0)) for m, d in cf_data.items()}

        # Centre marginal R on the trusted-panel mean, then map to a mean-1.0
        # multiplier (better-than-peers → louder, worse → softer).
        mean_mr = sum(trusted.values()) / len(trusted)
        cf_raw = {m: math.exp((mr - mean_mr) / temperature) for m, mr in trusted.items()}
        mean_cf = sum(cf_raw.values()) / len(cf_raw)
        if mean_cf <= 0:
            return False, {m: mr for m, mr in trusted.items()}
        cf_mult = {m: v / mean_cf for m, v in cf_raw.items()}

        # Weighted geometric blend (only trusted modules move).
        for m, cfm in cf_mult.items():
            base = max(mults.get(m, 1.0), _EPS)
            mults[m] = base ** (1.0 - blend) * max(cfm, _EPS) ** blend

        # Re-centre the whole panel on mean 1.0 and re-clamp.
        mean_final = sum(mults.values()) / len(mults) if mults else 1.0
        if mean_final > 0:
            for m in mults:
                mults[m] = float(min(max(mults[m] / mean_final, floor), ceiling))
        return True, {m: mr for m, mr in trusted.items()}


__all__ = ["VoteCalibrator", "VoteCalibration", "DEFAULT_VOTE_MODULES"]
