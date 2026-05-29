"""
APEX TRADER — ML Adapter
The master learner. Coordinates all learning modules,
runs optimisation cycles, and feeds adjustments back
into the trading system. Gets smarter every day.

Every trade teaches me something. I remember what works
on each pair, each session, each regime. I adapt. I evolve.
What worked last month might not work this month — I know
the difference.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from ml.trade_analyzer import PerformanceProfile, TradeAnalyzer
from ml.score_optimizer import ScoreOptimizer, ScoringWeights
from ml.regime_learner import RegimeLearner, RegimeStrategy
from ml.pair_learner import PairLearner, PairProfile
from ml.session_learner import SessionLearner, SessionProfile


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


class MLAdapter:
    """
    APEX TRADER — ML Adapter
    The master learner. Coordinates all learning modules,
    runs optimisation cycles, and feeds adjustments back
    into the trading system. Gets smarter every day.
    """

    RETRAIN_TRADE_INTERVAL = 50
    RETRAIN_DAY_INTERVAL = 7

    def __init__(self) -> None:
        self.analyzer = TradeAnalyzer()
        self.optimizer = ScoreOptimizer()
        self.regime_learner = RegimeLearner()
        self.pair_learner = PairLearner()
        self.session_learner = SessionLearner()

        self._last_train_time: Optional[datetime] = None
        self._trades_since_train: int = 0

    def run_optimization(self, trades: list[dict]) -> OptimizationReport:
        n = len(trades)
        logger.info(f"ML optimisation cycle — {n} trades")

        overall = self.analyzer.analyze_all(trades)
        weights = self.optimizer.optimize(trades)
        regimes = self.regime_learner.learn(trades)
        pairs = self.pair_learner.learn(trades)
        sessions = self.session_learner.learn(trades)

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

    def register_new_trade(self) -> None:
        self._trades_since_train += 1

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    _last_recommendations: list[str] = []

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
