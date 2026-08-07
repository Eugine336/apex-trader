"""Backward-compatibility shim — real implementation in adaptive.regime_learner."""
from adaptive.regime_learner import RegimeLearner, RegimeStrategy  # noqa: F401

__all__ = ["RegimeLearner", "RegimeStrategy"]
