"""APEX TRADER — Entry Plane (Phase 7).

Tick-triggered entry with M1 confirmation and multi-gate validation.

Replaces the scanner-timer-driven entry path with an event-driven model:

    WorldModel (candle-close)
        → ZoneWatcher   (extracts active entry zones per symbol)
        → TickEntryDetector (detects zone touch on live ticks)
        → M1CandleConfirmer (waits for M1 close confirmation)
        → EntryGate     (runs all pre-entry validation checks)
        → EntryOrchestrator (submits OPEN intent to execution plane)
"""

from entry.models import (
    EntryZone,
    EntryConfig,
    GateResult,
    PendingEntry,
)
from entry.zone_watcher import ZoneWatcher
from entry.tick_entry_detector import TickEntryDetector
from entry.m1_confirmation import M1CandleConfirmer
from entry.entry_gate import EntryGate
from entry.entry_orchestrator import EntryOrchestrator
from entry.zone_order_staging import ZoneOrderStager, StagedOrder

__all__ = [
    "EntryZone",
    "EntryConfig",
    "GateResult",
    "PendingEntry",
    "ZoneWatcher",
    "TickEntryDetector",
    "M1CandleConfirmer",
    "EntryGate",
    "EntryOrchestrator",
    "ZoneOrderStager",
    "StagedOrder",
]
