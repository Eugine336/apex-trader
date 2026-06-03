"""Shared position primitives for the trading-loop package.

Leaf module — imports nothing from platforms.main_loop or the mixins,
breaking the circular-import dependency introduced by the M1 mixin split.
"""

import threading
from datetime import datetime, timezone

from platforms.base_connector import OrderResult


class ManagedPosition:
    """Tracks a live position through its lifecycle."""

    __slots__ = (
        "order_id",
        "platform",
        "symbol",
        "direction",
        "lots",
        "entry_price",
        "sl",
        "tp1",
        "tp2",
        "score",
        "regime",
        "session",
        "entry_type",
        "open_time",
        "tp1_hit",
        "at_breakeven",
        "trailing",
        "last_update",
        "re_entry_eligible",
        "tm_trade_id",
        "stake_usd",
        "multiplier",
        "broker_pnl",
        "broker_lots",
        "scale_in_count",
        "idempotency_key",
        "revalidation_pending",
        "unconfirmed_cycles",
        "confluences",
        "initial_risk_dollars",
    )

    def __init__(
        self,
        order: OrderResult,
        tp1: float,
        tp2: float,
        score: int = 0,
        regime: str = "",
        session: str = "",
        entry_type: str = "",
        stake_usd: float = 0.0,
        multiplier: int = 100,
        idempotency_key: str = "",
        confluences: list = None,
    ):
        self.order_id = order.order_id
        self.platform = order.platform
        self.symbol = order.symbol
        self.direction = order.direction
        self.lots = order.lots
        self.entry_price = order.fill_price
        self.sl = order.sl
        self.tp1 = tp1
        self.tp2 = tp2
        self.score = score
        self.regime = regime
        self.session = session
        self.entry_type = entry_type
        self.open_time = datetime.now(timezone.utc)
        self.tp1_hit = False
        self.at_breakeven = False
        self.trailing = False
        self.last_update = datetime.now(timezone.utc)
        self.re_entry_eligible = False
        self.tm_trade_id = ""
        self.stake_usd = stake_usd  # Deriv only; 0.0 for MT5
        self.multiplier = multiplier  # Deriv contract multiplier; 100 default
        self.broker_pnl = 0.0  # Live P&L from broker (source of truth)
        self.broker_lots = 0.0  # Live lots from broker (detects partial fills)
        self.scale_in_count = 0
        self.idempotency_key = idempotency_key
        self.revalidation_pending = False
        self.unconfirmed_cycles = 0
        self.confluences = list(confluences) if confluences else []
        self.initial_risk_dollars: float | None = None


class _LockedPositions:
    """Thread-safe dict wrapper for managed positions.

    Prevents 'dictionary changed size during iteration' when the
    dashboard/API thread reads while the trading-loop thread mutates.
    Iteration helpers return snapshot lists; every operation acquires
    an RLock so compound operations from a single thread cannot deadlock.
    """

    def __init__(self) -> None:
        self._data: dict[str, ManagedPosition] = {}
        self.lock = threading.RLock()

    def __getitem__(self, key: str) -> ManagedPosition:
        with self.lock:
            return self._data[key]

    def __setitem__(self, key: str, value: ManagedPosition) -> None:
        with self.lock:
            self._data[key] = value

    def __delitem__(self, key: str) -> None:
        with self.lock:
            del self._data[key]

    def __contains__(self, key: object) -> bool:
        with self.lock:
            return key in self._data

    def __len__(self) -> int:
        with self.lock:
            return len(self._data)

    def __bool__(self) -> bool:
        with self.lock:
            return bool(self._data)

    def __iter__(self):
        with self.lock:
            return iter(list(self._data))

    def get(self, key: str, default=None):
        with self.lock:
            return self._data.get(key, default)

    def pop(self, key: str, *args):
        with self.lock:
            return self._data.pop(key, *args)

    def keys(self):
        with self.lock:
            return list(self._data.keys())

    def values(self):
        with self.lock:
            return list(self._data.values())

    def items(self):
        with self.lock:
            return list(self._data.items())

    def clear(self) -> None:
        with self.lock:
            self._data.clear()

    def snapshot(self) -> dict:
        """Return a shallow copy for safe cross-thread iteration."""
        with self.lock:
            return dict(self._data)

    def __eq__(self, other: object) -> bool:
        with self.lock:
            if isinstance(other, _LockedPositions):
                return self._data == other._data
            if isinstance(other, dict):
                return self._data == other
            return NotImplemented
