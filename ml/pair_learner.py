"""Backward-compatibility shim — real implementation in adaptive.pair_learner."""
from adaptive.pair_learner import PairLearner, PairProfile  # noqa: F401

__all__ = ["PairLearner", "PairProfile"]
