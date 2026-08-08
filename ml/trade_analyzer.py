"""Backward-compatibility shim — real implementation in adaptive.trade_analyzer."""
from adaptive.trade_analyzer import PerformanceProfile, TradeAnalyzer  # noqa: F401

__all__ = ["PerformanceProfile", "TradeAnalyzer"]
