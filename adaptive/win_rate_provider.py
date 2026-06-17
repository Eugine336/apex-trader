"""
APEX TRADER — Adaptive Win-Rate Provider

The opportunity ranker exposes a ``win_rate_provider`` hook: a callable
``(direction, timeframe_class) -> Optional[float]`` that overrides the modelled
win-probability formula with a calibrated, observed rate.  Until now the hook
was never supplied on the live path, so the ranker — and the orchestrator
sizing that consumes its EV — ran on the hardcoded ``base_win_rate=0.40``
constant even though PairLearner and EVEstimator already hold real per-pair /
regime / session win rates.

This adapter closes that gap.  It READS (never mutates) the learned components
and produces a per-pair win probability with a documented fallback chain and
Bayesian shrinkage toward the prior so a thin sample never yields an extreme
rate:

    1. PairLearner per-pair observed win rate
    2. EVEstimator pair → regime → session win rate
    3. cold-start prior (the same 0.40 the ranker used as a constant)

It is built *per pair* (bound to the pair / regime / session) because the
ranker hook is only handed ``(direction, timeframe_class)``.  The returned
callable is pre-resolved and never raises — a degenerate sample or a missing
component falls back gracefully rather than breaking the sizing path.

Pure read-only adapter: no broker/network access, no side effects on the
learners.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from loguru import logger

# Defaults mirror the ranker's constant prior so cold-start behaviour matches
# the legacy ``base_win_rate`` and only diverges once real history exists.
DEFAULT_PRIOR = 0.40
DEFAULT_PRIOR_STRENGTH = 10
DEFAULT_MIN_TRADES = 10
DEFAULT_CLAMP_LOW = 0.15
DEFAULT_CLAMP_HIGH = 0.85

# Source labels recorded as provenance on the opportunity.
SOURCE_PAIR_LEARNER = "pair_learner"
SOURCE_EV_ESTIMATOR = "ev_estimator"
SOURCE_COLD_START = "cold_start"

# The ranker callable: (direction, timeframe_class) -> Optional[float].
WinRateProvider = Callable[[str, str], Optional[float]]


@dataclass(frozen=True)
class WinRateResult:
    """The resolved win probability for a pair plus its audit provenance."""

    win_rate: float
    source: str          # SOURCE_PAIR_LEARNER | SOURCE_EV_ESTIMATOR | SOURCE_COLD_START
    sample_size: int     # trades behind the observed rate (0 for cold start)


class AdaptiveWinRateProvider:
    """Build per-pair win-rate callables for the opportunity ranker.

    Wraps the (optional) PairLearner and EVEstimator the rest of the system
    already maintains.  Any missing component is simply skipped in the fallback
    chain, so the adapter is safe to construct with only one source — or none,
    in which case every pair resolves to the cold-start prior.
    """

    def __init__(
        self,
        *,
        pair_learner=None,
        ev_estimator=None,
        prior: float = DEFAULT_PRIOR,
        prior_strength: int = DEFAULT_PRIOR_STRENGTH,
        min_trades: int = DEFAULT_MIN_TRADES,
        clamp: tuple[float, float] = (DEFAULT_CLAMP_LOW, DEFAULT_CLAMP_HIGH),
    ) -> None:
        self._pair_learner = pair_learner
        self._ev_estimator = ev_estimator
        self._prior = float(prior)
        self._prior_strength = max(0, int(prior_strength))
        self._min_trades = max(1, int(min_trades))
        low, high = clamp
        self._clamp_low = float(low)
        self._clamp_high = float(high)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def for_pair(
        self,
        pair: str,
        *,
        regime: str = "",
        session: str = "",
        trade_history: Optional[list[dict]] = None,
    ) -> tuple[WinRateProvider, WinRateResult]:
        """Return a ``(callable, result)`` pair for the given instrument.

        The callable matches the ranker's ``win_rate_provider`` signature and
        returns the same pre-resolved win probability for every
        ``(direction, timeframe_class)`` — the learned rates are per-pair, not
        per-direction/horizon.  ``result`` carries the provenance so the caller
        can stamp it onto the opportunity for the decision journal.
        """
        result = self.resolve(
            pair, regime=regime, session=session, trade_history=trade_history,
        )

        def _provider(direction: str, timeframe_class: str) -> Optional[float]:
            return result.win_rate

        return _provider, result

    def resolve(
        self,
        pair: str,
        *,
        regime: str = "",
        session: str = "",
        trade_history: Optional[list[dict]] = None,
    ) -> WinRateResult:
        """Resolve the win probability for a pair via the fallback chain.

        Never raises: any component error degrades to the next source and,
        ultimately, the cold-start prior.
        """
        try:
            pl = self._from_pair_learner(pair)
            if pl is not None:
                return pl

            ev = self._from_ev_estimator(pair, regime, session, trade_history)
            if ev is not None:
                return ev
        except Exception as exc:  # never let a bad read break sizing
            logger.warning("[win-rate] {} resolve failed, using prior: {}", pair, exc)

        return WinRateResult(
            win_rate=self._clamp(self._prior),
            source=SOURCE_COLD_START,
            sample_size=0,
        )

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def _from_pair_learner(self, pair: str) -> Optional[WinRateResult]:
        learner = self._pair_learner
        if learner is None:
            return None
        getter = getattr(learner, "get_profile", None)
        if getter is None:
            return None
        profile = getter(pair)
        if profile is None:
            return None
        n = int(getattr(profile, "total_trades", 0) or 0)
        if n <= 0:
            return None
        observed = float(getattr(profile, "win_rate", 0.0) or 0.0)
        win_rate = self._shrink_and_clamp(observed, n)
        logger.debug(
            "[win-rate] {} pair_learner wr={:.3f} n={} → {:.3f}",
            pair, observed, n, win_rate,
        )
        return WinRateResult(win_rate=win_rate, source=SOURCE_PAIR_LEARNER, sample_size=n)

    def _from_ev_estimator(
        self,
        pair: str,
        regime: str,
        session: str,
        trade_history: Optional[list[dict]],
    ) -> Optional[WinRateResult]:
        estimator = self._ev_estimator
        if estimator is None or not trade_history:
            return None
        est = estimator.estimate(
            pair=pair,
            regime=regime or "",
            session=session or "",
            trade_history=trade_history,
        )
        # EVEstimator returns source="default"/confidence="insufficient" when it
        # lacks enough trades at every level — that is not a usable observation.
        source = getattr(est, "source", "default")
        n = int(getattr(est, "sample_size", 0) or 0)
        if source == "default" or n <= 0:
            return None
        observed = float(getattr(est, "win_rate", 0.0) or 0.0)
        win_rate = self._shrink_and_clamp(observed, n)
        logger.debug(
            "[win-rate] {} ev_estimator({}) wr={:.3f} n={} → {:.3f}",
            pair, source, observed, n, win_rate,
        )
        return WinRateResult(win_rate=win_rate, source=SOURCE_EV_ESTIMATOR, sample_size=n)

    # ------------------------------------------------------------------
    # Math
    # ------------------------------------------------------------------

    def _shrink_and_clamp(self, observed: float, n: int) -> float:
        """Bayesian-shrink a thin sample toward the prior, then clamp.

        At/above ``min_trades`` the observed rate is used directly (only
        clamped).  Below it the rate is blended with the prior so a 1-3 trade
        sample cannot drive an extreme size:

            blended = (n * observed + prior_strength * prior) / (n + prior_strength)
        """
        if n >= self._min_trades:
            return self._clamp(observed)
        denom = n + self._prior_strength
        if denom <= 0:
            return self._clamp(self._prior)
        blended = (n * observed + self._prior_strength * self._prior) / denom
        return self._clamp(blended)

    def _clamp(self, value: float) -> float:
        return max(self._clamp_low, min(self._clamp_high, float(value)))
