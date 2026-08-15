"""
APEX TRADER — Management Package
The Hands. Protects every winner and cuts every loser.
"""

from management.partial_close import PartialCloseCalculator
from management.trailing_stop import StructureTrailingStop
# V-012 — DEPRECATED LEGACY: TradeManager (and its EntrySignal / ManagedTrade /
# TradeStatus dataclasses) is the mechanical management layer (fixed stop-loss /
# TP1 / breakeven / trailing / TP2 adjustment / M5 structure exit / stall exit).
# Live management is Brain-driven (thesis-based) and the event-driven bootstrap
# runs with ``trade_manager = None``. This export is retained ONLY for backtests /
# legacy tooling; constructing TradeManager emits a DeprecationWarning. Do not
# wire it into the live path.
from management.trade_manager import (
    EntrySignal,
    ManagedTrade,
    TradeManager,
    TradeStatus,
)
from management.re_entry import ReEntryManager, ReEntryOpportunity

__all__ = [
    # ── DEPRECATED LEGACY (V-012) — backtests / legacy tooling only ──
    "TradeManager",
    "ManagedTrade",
    "EntrySignal",
    "TradeStatus",
    # ── Active helpers ──
    "PartialCloseCalculator",
    "StructureTrailingStop",
    "ReEntryManager",
    "ReEntryOpportunity",
]
