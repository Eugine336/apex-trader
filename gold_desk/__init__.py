"""Gold market evidence instruments for Apex's single CognitiveBrain.

The Gold desk observes XAUUSD and reports factual conditions. It contains no
independent trade decision engine, strategy ranker, or broker execution API.
"""
from gold_desk.models import Candle, EventWindow, MarketSnapshot, PatternObservation, Venue
from gold_desk.symbols import GoldOnlyGuard
from gold_desk.strategies import PatternDetector, default_pattern_detectors

__all__ = [
    "Candle", "EventWindow", "MarketSnapshot", "PatternObservation", "Venue",
    "GoldOnlyGuard", "PatternDetector", "default_pattern_detectors",
]
