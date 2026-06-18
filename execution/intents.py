"""APEX TRADER — Intent dataclasses (Phase 4).

An Intent is a position management decision that has NOT yet been sent to the
broker.  Workers emit Intents; the Intent Aggregator (Phase 5) deduplicates
and resolves conflicts; the Action Executor (Phase 6) sends them to the broker.

Frozen so they can be safely queued across threads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from typing import Optional


class IntentType(IntEnum):
    """Action type ordered by priority — higher value = more urgent.

    When the Intent Aggregator (Phase 5) sees conflicting intents for the
    same position in a single tick window, the highest-priority type wins.
    CLOSE supersedes everything.
    """

    MODIFY_TP = 10
    MODIFY_SL = 20
    PARTIAL_CLOSE = 30
    CLOSE = 40


@dataclass(frozen=True)
class Intent:
    """A single position management decision.

    Immutable after creation so it can be safely shared across threads
    without copying.  The ``source`` field records which check/module
    generated this intent for observability and debugging.
    """

    intent_type: IntentType
    symbol: str
    position_ticket: str
    timestamp: datetime
    source: str
    reason: str

    new_sl: Optional[float] = None
    new_tp: Optional[float] = None
    close_fraction: Optional[float] = None
    priority: int = 0

    @staticmethod
    def close(
        *,
        symbol: str,
        ticket: str,
        source: str,
        reason: str,
        timestamp: Optional[datetime] = None,
    ) -> Intent:
        return Intent(
            intent_type=IntentType.CLOSE,
            symbol=symbol,
            position_ticket=ticket,
            timestamp=timestamp or datetime.now(timezone.utc),
            source=source,
            reason=reason,
            priority=IntentType.CLOSE,
        )

    @staticmethod
    def modify_sl(
        *,
        symbol: str,
        ticket: str,
        new_sl: float,
        source: str,
        reason: str,
        timestamp: Optional[datetime] = None,
    ) -> Intent:
        return Intent(
            intent_type=IntentType.MODIFY_SL,
            symbol=symbol,
            position_ticket=ticket,
            timestamp=timestamp or datetime.now(timezone.utc),
            source=source,
            reason=reason,
            new_sl=new_sl,
            priority=IntentType.MODIFY_SL,
        )

    @staticmethod
    def modify_tp(
        *,
        symbol: str,
        ticket: str,
        new_tp: float,
        source: str,
        reason: str,
        timestamp: Optional[datetime] = None,
    ) -> Intent:
        return Intent(
            intent_type=IntentType.MODIFY_TP,
            symbol=symbol,
            position_ticket=ticket,
            timestamp=timestamp or datetime.now(timezone.utc),
            source=source,
            reason=reason,
            new_tp=new_tp,
            priority=IntentType.MODIFY_TP,
        )

    @staticmethod
    def partial_close(
        *,
        symbol: str,
        ticket: str,
        fraction: float,
        source: str,
        reason: str,
        timestamp: Optional[datetime] = None,
    ) -> Intent:
        return Intent(
            intent_type=IntentType.PARTIAL_CLOSE,
            symbol=symbol,
            position_ticket=ticket,
            timestamp=timestamp or datetime.now(timezone.utc),
            source=source,
            reason=reason,
            close_fraction=fraction,
            priority=IntentType.PARTIAL_CLOSE,
        )
