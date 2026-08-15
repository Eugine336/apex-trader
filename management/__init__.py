"""
APEX TRADER — Management Package
The Hands. Protects every winner and cuts every loser.

.. deprecated::
    ``TradeManager`` (and the ``EntrySignal`` / ``ManagedTrade`` / ``TradeStatus``
    dataclasses it drives) is a retired legacy component — superseded by the
    event-driven ``execution/`` management path (e.g. PositionWorker) and kept
    only for the legacy loop, shadow/backtest replay and tests. Constructing a
    ``TradeManager`` emits a ``DeprecationWarning``; do not use it in new code.
"""

from management.partial_close import PartialCloseCalculator
from management.trailing_stop import StructureTrailingStop
# Deprecated (retired legacy): TradeManager and its dataclasses — construction
# emits a DeprecationWarning. See management/trade_manager.py.
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
