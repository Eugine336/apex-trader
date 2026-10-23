"""Gold market evidence instruments for Apex's existing single CognitiveBrain.

This package observes XAUUSD. The detector families do not choose a trade or
send orders; their facts enter CognitionLoop as one neutral evidence record.
"""
from gold_desk.models import Candle, EventWindow, MarketSnapshot, Venue
from gold_desk.risk import GoldExecutionGate, RiskState, GateDecision
from gold_desk.symbols import GoldOnlyGuard
from gold_desk.strategies import PatternDetector, default_pattern_detectors

__all__ = [
    "Candle", "EventWindow", "MarketSnapshot", "Venue", "PatternDetector",
    "GoldExecutionGate", "RiskState", "GateDecision", "GoldOnlyGuard",
    "default_pattern_detectors",
]
