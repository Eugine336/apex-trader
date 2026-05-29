"""
APEX TRADER — Trigger Package
The sniper's trigger finger. Entry engine, pattern detection,
and pre-entry validation in one lethal package.
"""

from trigger.entry_engine import EntryEngine, EntrySignal, EntryRejection
from trigger.entry_patterns import EntryPatternDetector, PatternMatch
from trigger.entry_validator import EntryValidator, ValidationResult

__all__ = [
    "EntryEngine",
    "EntrySignal",
    "EntryRejection",
    "EntryPatternDetector",
    "PatternMatch",
    "EntryValidator",
    "ValidationResult",
]
