"""
APEX TRADER — Trigger Package
The sniper's trigger finger. Entry engine, pattern detection,
and pre-entry validation in one lethal package.
"""

from trigger.entry_engine import EntryEngine, EntrySignal, EntryRejection
from trigger.entry_patterns import EntryPatternDetector, PatternMatch

__all__ = [
    "EntryEngine",
    "EntrySignal",
    "EntryRejection",
    "EntryPatternDetector",
    "PatternMatch",
]
