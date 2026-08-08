"""Backward-compatibility shim — real implementation in adaptive.score_optimizer."""
from adaptive.score_optimizer import FACTOR_KEYS, ScoreOptimizer, ScoringWeights  # noqa: F401

__all__ = ["FACTOR_KEYS", "ScoreOptimizer", "ScoringWeights"]
