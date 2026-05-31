"""Backward-compatibility shim — real implementation in adaptive.ev_estimator."""
from adaptive.ev_estimator import EVEstimate, EVEstimator  # noqa: F401

__all__ = ["EVEstimate", "EVEstimator"]
