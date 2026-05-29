"""
APEX TRADER — Management Package
The Hands. Protects every winner and cuts every loser.
"""

from management.partial_close import PartialCloseCalculator
from management.trailing_stop import StructureTrailingStop
from management.trade_manager import (
    EntrySignal,
    ManagedTrade,
    TradeManager,
    TradeStatus,
)
from management.re_entry import ReEntryManager, ReEntryOpportunity

__all__ = [
    "TradeManager",
    "ManagedTrade",
    "EntrySignal",
    "TradeStatus",
    "PartialCloseCalculator",
    "StructureTrailingStop",
    "ReEntryManager",
    "ReEntryOpportunity",
]
