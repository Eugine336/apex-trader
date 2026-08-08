"""Backward-compatibility shim — real implementation in adaptive.optimizer."""
from adaptive.optimizer import (  # noqa: F401
    AdaptiveOptimizer,
    MLAdapter,
    OptimizationReport,
    TradeAdjustments,
)

__all__ = ["AdaptiveOptimizer", "MLAdapter", "OptimizationReport", "TradeAdjustments"]
