"""
APEX TRADER — ML Package
The Memory. Learns from every trade, adapts weights,
profiles pairs, sessions, and regimes. Gets smarter every day.
"""

from ml.trade_analyzer import PerformanceProfile, TradeAnalyzer
from ml.score_optimizer import ScoreOptimizer, ScoringWeights
from ml.regime_learner import RegimeLearner, RegimeStrategy
from ml.pair_learner import PairLearner, PairProfile
from ml.session_learner import SessionLearner, SessionProfile
from ml.ml_adapter import MLAdapter, OptimizationReport, TradeAdjustments

__all__ = [
    "TradeAnalyzer", "PerformanceProfile",
    "ScoreOptimizer", "ScoringWeights",
    "RegimeLearner", "RegimeStrategy",
    "PairLearner", "PairProfile",
    "SessionLearner", "SessionProfile",
    "MLAdapter", "OptimizationReport", "TradeAdjustments",
]
