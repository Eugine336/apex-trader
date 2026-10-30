"""Gold market evidence instruments for Apex's existing single CognitiveBrain.

This package observes XAUUSD and returns facts, not trading decisions. The
existing Apex risk and execution pipeline remains responsible for governance.
"""
from gold_desk.models import Candle, EventWindow, MarketSnapshot, PatternObservation, Venue
from gold_desk.symbols import SymbolPolicy
from gold_desk.strategies import PatternDetector, default_pattern_detectors

__all__ = [
    "Candle", "EventWindow", "MarketSnapshot", "PatternObservation", "Venue",
    "PatternDetector", "SymbolPolicy", "default_pattern_detectors",
]
