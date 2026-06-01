"""
APEX TRADER — Platform Connector Interface
Every platform speaks the same language to the brain.
MT5 or Deriv — the interface is identical.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import pandas as pd


class OrderDirection(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class AccountInfo:
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float
    currency: str
    leverage: int
    platform: str


@dataclass
class TickData:
    bid: float
    ask: float
    spread: float
    time: datetime


@dataclass
class OrderResult:
    success: bool
    order_id: str
    fill_price: float
    requested_price: float
    slippage_pips: float
    lots: float
    symbol: str
    direction: str
    sl: float
    tp: float
    platform: str
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    error: str = ""


@dataclass
class CloseResult:
    success: bool
    order_id: str
    close_price: float
    lots_closed: float
    pnl: float
    platform: str
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    error: str = ""


@dataclass
class PositionInfo:
    order_id: str
    symbol: str
    direction: str
    lots: float
    open_price: float
    current_price: float
    sl: float
    tp: float
    pnl: float
    swap: float
    open_time: datetime
    platform: str


TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")


class BaseConnector(ABC):
    """Abstract interface every platform connector must implement."""

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to the platform. Returns True on success."""

    @abstractmethod
    def disconnect(self) -> None:
        """Clean disconnect from the platform."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if the connection is alive."""

    @abstractmethod
    def get_account_info(self) -> AccountInfo:
        """Retrieve current account balance, equity, margin."""

    @abstractmethod
    def get_price(self, symbol: str) -> TickData:
        """Get the current bid/ask/spread for a symbol."""

    @abstractmethod
    def get_ohlcv(
        self, symbol: str, timeframe: str, count: int = 200
    ) -> pd.DataFrame:
        """
        Fetch historical candles.
        Returns DataFrame with columns: time, open, high, low, close, volume.
        """

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
    ) -> OrderResult:
        """Place a market order. Returns fill details."""

    @abstractmethod
    def modify_order(
        self,
        order_id: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        """Modify SL and/or TP on an open position."""

    @abstractmethod
    def close_order(
        self, order_id: str, lots: Optional[float] = None
    ) -> CloseResult:
        """Close a position fully or partially (if lots specified)."""

    @abstractmethod
    def get_open_positions(self) -> list[PositionInfo]:
        """Return all currently open positions."""

    @abstractmethod
    def get_position_info(self, order_id: str) -> Optional[PositionInfo]:
        """Return details for a single open position."""

    @abstractmethod
    def get_spread(self, symbol: str) -> float:
        """Current spread in pips for a symbol."""

    @abstractmethod
    def get_tick(self, symbol: str) -> TickData:
        """Latest tick data (bid, ask, time)."""

    def get_realized_pnl(self, order_id: str) -> Optional[float]:
        """Return broker-reported realized P&L for a closed ticket.
        Returns None when unavailable (Deriv, unsupported)."""
        return None

    # ── Concrete helpers (shared by all connectors) ──────────────────────

    def symbol_map(self, apex_symbol: str) -> str:
        """
        Map APEX internal symbol to a platform-specific name.
        Override in subclass if the broker uses suffixes like 'm' or '.raw'.
        """
        return apex_symbol

    def timeframe_map(self, tf: str) -> Any:
        """
        Map string timeframe ('M1', 'H4') to platform-specific constant.
        Override in subclass.
        """
        return tf
