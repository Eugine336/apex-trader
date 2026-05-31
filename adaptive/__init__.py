"""
APEX TRADER — Adaptive Optimizer Package
Statistical heuristic optimization — learns from every trade,
adapts weights, profiles pairs, sessions, and regimes.
"""

from adaptive.trade_analyzer import PerformanceProfile, TradeAnalyzer
from adaptive.score_optimizer import ScoreOptimizer, ScoringWeights
from adaptive.regime_learner import RegimeLearner, RegimeStrategy
from adaptive.pair_learner import PairLearner, PairProfile
from adaptive.session_learner import SessionLearner, SessionProfile
from adaptive.optimizer import AdaptiveOptimizer, MLAdapter, OptimizationReport, TradeAdjustments
from adaptive.ev_estimator import EVEstimator, EVEstimate

__all__ = [
    "TradeAnalyzer", "PerformanceProfile",
    "ScoreOptimizer", "ScoringWeights",
    "RegimeLearner", "RegimeStrategy",
    "PairLearner", "PairProfile",
    "SessionLearner", "SessionProfile",
    "AdaptiveOptimizer", "MLAdapter", "OptimizationReport", "TradeAdjustments",
    "EVEstimator", "EVEstimate",
]
