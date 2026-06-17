"""
APEX TRADER — Adaptive Optimizer
The master learner. Coordinates all learning modules,
runs optimisation cycles, and feeds adjustments back
into the trading system. Gets smarter every day.

Every trade teaches me something. I remember what works
on each pair, each session, each regime. I adapt. I evolve.
What worked last month might not work this month — I know
the difference.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from adaptive.trade_analyzer import PerformanceProfile, TradeAnalyzer
from adaptive.score_optimizer import ScoreOptimizer, ScoringWeights
from adaptive.regime_learner import RegimeLearner, RegimeStrategy
from adaptive.pair_learner import PairLearner, PairProfile
from adaptive.session_learner import SessionLearner, SessionProfile
from adaptive.tunable import TuningGuardMixin


@dataclass
class TradeAdjustments:
    score_threshold_adjustment: int = 0
    position_size_multiplier: float = 1.0
    tp_multiplier: float = 1.0
    sl_buffer_adjustment: float = 0.0
    should_trade: bool = True
    confidence: float = 0.0
    reason: str = "defaults"


@dataclass
class OptimizationReport:
    new_scoring_weights: ScoringWeights = field(default_factory=ScoringWeights)
    regime_strategies: dict[str, RegimeStrategy] = field(default_factory=dict)
    pair_profiles: dict[str, PairProfile] = field(default_factory=dict)
    session_profiles: dict[str, SessionProfile] = field(default_factory=dict)
    overall_performance: Optional[PerformanceProfile] = None
    recommendations: list[str] = field(default_factory=list)
    trades_analyzed: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AdaptiveOptimizer(TuningGuardMixin):
    """
    APEX TRADER — Adaptive Optimizer
    Coordinates all learning modules, runs optimisation cycles,
    and feeds adjustments back into the trading system.
    """

    RETRAIN_TRADE_INTERVAL = 50
    RETRAIN_DAY_INTERVAL = 7

    # Recency: the learners adapt to the CURRENT regime by training on trades
    # within this rolling window — but only if enough remain (≥ min trades),
    # otherwise the full history is used so a young account never starves the
    # learners. 0 days disables windowing (full history, legacy behaviour).
    RECENCY_WINDOW_DAYS = 90
    RECENCY_MIN_TRADES = 50

    # Losing-pattern hard-block thresholds. A cached multi-dimensional
    # combination (pair×session, pair×regime, …) only becomes a live entry
    # block when it has at least this many samples AND a win rate at or below
    # the ceiling — deliberately stricter than the analyzer's display threshold
    # (win_rate < 0.45, n ≥ 10) so we never block on a marginal/under-sampled
    # pattern.
    LOSING_PATTERN_MIN_SAMPLES = 20
    LOSING_PATTERN_MAX_WIN_RATE = 0.35

    def __init__(self) -> None:
        self.analyzer = TradeAnalyzer()
        self.optimizer = ScoreOptimizer()
        self.regime_learner = RegimeLearner()
        self.pair_learner = PairLearner()
        self.session_learner = SessionLearner()

        # Tunable per instance (kept as attributes so ops can adjust/disable).
        self.recency_window_days = self.RECENCY_WINDOW_DAYS
        self.recency_min_trades = self.RECENCY_MIN_TRADES

        # Losing-pattern gate: tunable per instance; the cached patterns are
        # refreshed on every optimisation pass.
        self.losing_pattern_min_samples = self.LOSING_PATTERN_MIN_SAMPLES
        self.losing_pattern_max_win_rate = self.LOSING_PATTERN_MAX_WIN_RATE
        self._losing_patterns: list[dict] = []

        self._last_train_time: Optional[datetime] = None
        self._trades_since_train: int = 0
        self._last_recommendations: list[str] = []

    def run_optimization(self, trades: list[dict]) -> OptimizationReport:
        # When the Tuner Agent is sole authority it drives the sub-learners
        # individually; a direct run_optimization() is a bypass — return an
        # inert report instead of re-tuning behind the agent's back.
        if self._tuning_blocked("run_optimization"):
            return OptimizationReport(trades_analyzed=len(trades))
        n = len(trades)
        logger.info(f"Adaptive optimisation cycle — {n} trades")

        # Train the learners on RECENT trades so they reflect the current regime
        # (the full history still drives the all-time performance report below).
        learner_trades = self._recent_trades(
            trades, self.recency_window_days, self.recency_min_trades,
        )
        if len(learner_trades) < n:
            logger.info(
                "Recency window — learners train on last {}d: {}/{} trades",
                self.recency_window_days, len(learner_trades), n,
            )

        overall = self.analyzer.analyze_all(trades)
        weights = self.optimizer.optimize(learner_trades)
        regimes = self.regime_learner.learn(learner_trades)
        pairs = self.pair_learner.learn(learner_trades)
        sessions = self.session_learner.learn(learner_trades)

        # Cache statistically-confident losing combinations for the live
        # defensive entry gate (is_losing_pattern). Built from the recency
        # window so the block reflects the CURRENT regime, like the learners.
        self._losing_patterns = self.analyzer.get_losing_patterns(learner_trades)

        recommendations = self._generate_recommendations(
            trades, overall, regimes, pairs, sessions
        )

        self._last_train_time = datetime.now(timezone.utc)
        self._trades_since_train = 0

        report = OptimizationReport(
            new_scoring_weights=weights,
            regime_strategies=regimes,
            pair_profiles=pairs,
            session_profiles=sessions,
            overall_performance=overall,
            recommendations=recommendations,
            trades_analyzed=n,
        )
        logger.info(f"Optimisation complete — {len(recommendations)} recommendations")
        return report

    @staticmethod
    def _parse_trade_ts(t: dict) -> Optional[datetime]:
        """Best-effort UTC timestamp for a trade dict (timestamp/entry/close)."""
        raw = t.get("timestamp") or t.get("entry_time") or t.get("close_time")
        if not raw:
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _recent_trades(
        cls,
        trades: list[dict],
        window_days: int,
        min_trades: int,
        now: Optional[datetime] = None,
    ) -> list[dict]:
        """Return trades within `window_days` — but only if at least `min_trades`
        remain; otherwise return the full set so the learners never starve. This
        is what lets the learners adapt to the current regime instead of being
        anchored by stale, months-old trades. Disabled when window_days <= 0."""
        if window_days <= 0 or len(trades) <= min_trades:
            return trades
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=window_days)
        recent = [
            t for t in trades
            if (ts := cls._parse_trade_ts(t)) is not None and ts >= cutoff
        ]
        return recent if len(recent) >= min_trades else trades

    def get_trade_adjustments(
        self, pair: str, regime: str, session: str
    ) -> TradeAdjustments:
        regime_strat = self.regime_learner.get_strategy(regime)
        pair_mult = self.pair_learner.get_pair_multiplier(pair)
        session_agg = self.session_learner.get_session_aggression(session)

        can_trade_regime, regime_reason = self.regime_learner.should_trade_regime(regime)
        if not can_trade_regime:
            return TradeAdjustments(
                should_trade=False, confidence=regime_strat.confidence, reason=regime_reason
            )
        if pair_mult == 0.0:
            return TradeAdjustments(
                should_trade=False, confidence=0.8, reason=f"{pair} — AVOID recommendation"
            )

        threshold_adj = regime_strat.optimal_score_threshold - 85
        tp_mult = regime_strat.optimal_tp_multiplier
        sl_adj = regime_strat.optimal_sl_buffer_pips - 2.0
        size_mult = pair_mult

        if session_agg == "AGGRESSIVE":
            size_mult = min(size_mult * 1.1, 1.2)
        elif session_agg == "CAUTIOUS":
            size_mult *= 0.85
        elif session_agg == "AVOID":
            return TradeAdjustments(
                should_trade=False,
                confidence=0.6,
                reason=f"Session {session} has poor historical performance — avoiding",
            )

        confidence = (regime_strat.confidence + min(1.0, pair_mult)) / 2.0
        parts = [f"regime={regime}", f"pair_mult={pair_mult:.1f}", f"session={session_agg}"]
        reason = " | ".join(parts)

        return TradeAdjustments(
            score_threshold_adjustment=threshold_adj,
            position_size_multiplier=round(size_mult, 3),
            tp_multiplier=round(tp_mult, 2),
            sl_buffer_adjustment=round(sl_adj, 2),
            should_trade=True,
            confidence=round(confidence, 4),
            reason=reason,
        )

    def get_recommendations(self) -> list[str]:
        return self._last_recommendations[:]

    def is_losing_pattern(
        self, pair: str, regime: str, session: str, entry_type: str = ""
    ) -> tuple[bool, str]:
        """Defensive entry check: does this setup match a statistically-confident
        losing multi-dimensional pattern from our own trade history?

        Only blocks when a cached pattern (refreshed each optimisation pass via
        ``get_losing_patterns``) has at least ``losing_pattern_min_samples`` trades
        AND a win rate at or below ``losing_pattern_max_win_rate``. Neutral (never
        blocks) until enough history accumulates. Returns ``(is_loser, reason)``.
        """
        setup = {
            "pair": str(pair),
            "regime": str(regime),
            "session": str(session),
            "entry_type": str(entry_type),
        }
        for pat in self._losing_patterns:
            if pat.get("sample_size", 0) < self.losing_pattern_min_samples:
                continue
            if pat.get("win_rate", 1.0) > self.losing_pattern_max_win_rate:
                continue
            dims = pat.get("dimensions", {})
            if not dims:
                continue
            if all(setup.get(k, "") == str(v) for k, v in dims.items()):
                label = ", ".join(f"{k}={v}" for k, v in dims.items())
                return True, (
                    f"{label} — {pat['win_rate']:.0%} win rate over "
                    f"{pat['sample_size']} trades"
                )
        return False, ""

    def should_retrain(
        self, last_train_time: Optional[datetime] = None, new_trades_since: int = 0
    ) -> bool:
        ltt = last_train_time or self._last_train_time
        nts = new_trades_since or self._trades_since_train

        if ltt is None:
            return True
        if nts >= self.RETRAIN_TRADE_INTERVAL:
            return True
        elapsed = (datetime.now(timezone.utc) - ltt).days
        return elapsed >= self.RETRAIN_DAY_INTERVAL

    def register_new_trade(self, exit_cause: Optional[str] = None) -> None:
        self._trades_since_train += 1
        # P7: the normalised exit cause is an additive learning signal. The
        # retrain trigger itself is count-based, so we only surface the cause
        # here (kwarg is optional — existing callers are unaffected).
        if exit_cause:
            logger.debug("[ml] trade registered — exit_cause={}", exit_cause)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _generate_recommendations(
        self,
        trades: list[dict],
        overall: PerformanceProfile,
        regimes: dict[str, RegimeStrategy],
        pairs: dict[str, PairProfile],
        sessions: dict[str, SessionProfile],
    ) -> list[str]:
        recs: list[str] = []

        if overall.total_trades > 0:
            recs.append(
                f"Overall: {overall.win_rate:.0%} win rate, "
                f"PF={overall.profit_factor:.2f}, "
                f"expectancy={overall.expectancy:.2f}"
            )

        effectiveness = self.optimizer.get_factor_effectiveness(trades)
        for factor, stats in effectiveness.items():
            if stats["sample_present"] >= 10 and stats["lift"] > 0.10:
                recs.append(
                    f"Increase weight on {factor} — "
                    f"{stats['win_rate_when_present']:.0%} when present vs "
                    f"{stats['win_rate_when_absent']:.0%} when absent"
                )
            elif stats["sample_present"] >= 10 and stats["lift"] < -0.10:
                recs.append(
                    f"Decrease weight on {factor} — "
                    f"actually hurts ({stats['lift']:+.0%} lift)"
                )

        for regime, strat in regimes.items():
            if strat.sample_size >= 30 and strat.win_rate < 0.45:
                recs.append(
                    f"Reduce trading in {regime} — only {strat.win_rate:.0%} "
                    f"win rate ({strat.sample_size} trades)"
                )
            elif strat.sample_size >= 30 and strat.win_rate >= 0.80:
                recs.append(
                    f"{regime} is your edge — {strat.win_rate:.0%} win rate, "
                    f"lean in"
                )

        for pair, prof in pairs.items():
            if prof.total_trades >= 20 and prof.recommendation == "AVOID":
                recs.append(
                    f"Avoid {pair} — {prof.win_rate:.0%} win rate "
                    f"({prof.total_trades} trades)"
                )

        for session, prof in sessions.items():
            if prof.total_trades >= 15 and prof.recommendation == "AGGRESSIVE":
                recs.append(
                    f"{session} is your goldmine — "
                    f"{prof.win_rate:.0%} win rate, increase aggression"
                )
            elif prof.total_trades >= 15 and prof.recommendation == "AVOID":
                recs.append(
                    f"Avoid {session} — "
                    f"{prof.win_rate:.0%} win rate, stay out"
                )

        self._last_recommendations = recs
        return recs


MLAdapter = AdaptiveOptimizer
