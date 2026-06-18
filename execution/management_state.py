"""APEX TRADER — Persistent position management state for event-driven mode.

Without a TradingLoop/TradeManager, position management state (breakeven,
trailing, partial close status, price extremes) is lost between evaluation
cycles.  This store persists that state in-memory keyed by position ticket.

Thread-safe: all mutations are guarded by an RLock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ManagementState:
    """Per-position management state that persists across eval cycles."""

    ticket: str
    original_stop_loss: float = 0.0
    original_tp2: float = 0.0
    stop_loss: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    remaining_size_lots: float = 0.0
    pnl_pips: float = 0.0
    pnl_dollars: float = 0.0
    pip_size: float = 0.0001
    pip_value_per_lot: float = 10.0
    candles_since_entry: int = 0
    entry_timeframe: str = "M5"
    highest_price_since_entry: float = 0.0
    lowest_price_since_entry: float = float("inf")
    tp1_hit: bool = False
    at_breakeven: bool = False
    trailing: bool = False
    partial_closed: bool = False
    tp3: Optional[float] = None
    tp3_hit: bool = False
    status: str = "OPEN"
    plan_be_trigger_r: Optional[float] = None
    plan_trail_activation_r: Optional[float] = None
    plan_trail_strategy: Optional[str] = None
    plan_partial_ratio: Optional[float] = None
    strategic_structure_integrity: Optional[float] = None
    strategic_tf_alignment: Optional[float] = None
    strategic_assessment_time: Optional[datetime] = None
    last_eval_time: Optional[datetime] = None


class ManagementStateStore:
    """Thread-safe store for position management state."""

    def __init__(self) -> None:
        self._states: dict[str, ManagementState] = {}
        self._lock = threading.RLock()

    def get(self, ticket: str) -> Optional[ManagementState]:
        with self._lock:
            return self._states.get(ticket)

    def get_or_create(self, ticket: str, **defaults) -> ManagementState:
        with self._lock:
            if ticket not in self._states:
                self._states[ticket] = ManagementState(ticket=ticket, **defaults)
            return self._states[ticket]

    def update(self, ticket: str, **updates) -> None:
        with self._lock:
            state = self._states.get(ticket)
            if state is not None:
                for k, v in updates.items():
                    if hasattr(state, k):
                        setattr(state, k, v)

    def remove(self, ticket: str) -> None:
        with self._lock:
            self._states.pop(ticket, None)

    def cleanup(self, active_tickets: set[str]) -> int:
        """Remove states for positions no longer open. Returns count removed."""
        with self._lock:
            stale = [t for t in self._states if t not in active_tickets]
            for t in stale:
                del self._states[t]
            return len(stale)

    def all_tickets(self) -> set[str]:
        with self._lock:
            return set(self._states.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)
