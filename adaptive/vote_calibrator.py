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

        cal = self._compute_calibration(accuracy_data)
        with self._lock:
            self._multipliers = dict(cal.multipliers)
            self._last_calibration = cal
        if not cal.skipped:
            logger.info(
                "[vote-calibrator] recalibrated ({} method, {} qualifying): {}",
                cal.method, cal.qualifying_modules,
                ", ".join(
                    f"{m}×{w:.2f}" for m, w in sorted(cal.multipliers.items())
                ) or "none",
            )
        return cal

    # ── Core math (pure, deterministic) ───────────────────────────────────

    def _compute_calibration(self, accuracy_data: Dict[str, dict]) -> VoteCalibration:
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

        return VoteCalibration(
            multipliers=mults,
            accuracies=shrunk,
            raw_accuracies=raw_acc,
            sample_sizes=sample_sizes,
            method=method,
            qualifying_modules=len(qualifying),
            computed_at=now,
            skipped=False,
            reason="ok",
        )


__all__ = ["VoteCalibrator", "VoteCalibration", "DEFAULT_VOTE_MODULES"]
