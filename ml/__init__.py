"""
APEX TRADER — ML Package (backward-compatibility shim)
All modules have moved to ``adaptive/``.  This package re-exports
every public name so existing ``from ml import …`` statements
continue to work.
"""

from adaptive import (  # noqa: F401
    AdaptiveOptimizer,
    EVEstimate,
    EVEstimator,
    MLAdapter,
    OptimizationReport,
    PairLearner,
    PairProfile,
    PerformanceProfile,
    RegimeLearner,
    RegimeStrategy,
    ScoreOptimizer,
    ScoringWeights,
    SessionLearner,
    SessionProfile,
    TradeAdjustments,
    TradeAnalyzer,
)

__all__ = [
    "TradeAnalyzer", "PerformanceProfile",
    "ScoreOptimizer", "ScoringWeights",
    "RegimeLearner", "RegimeStrategy",
    "PairLearner", "PairProfile",
    "SessionLearner", "SessionProfile",
    "AdaptiveOptimizer", "MLAdapter", "OptimizationReport", "TradeAdjustments",
    "EVEstimator", "EVEstimate",
]
