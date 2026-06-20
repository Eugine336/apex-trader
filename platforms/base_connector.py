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


class PendingOrderKind(Enum):
    BUY_LIMIT = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"


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
class DealCloseInfo:
    """Broker-reported close details for a closed ticket."""
    pnl: float
    exit_reason: str
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0
    raw_reason_code: Optional[int] = None
    raw_comment: Optional[str] = None
    close_price: Optional[float] = None
    close_time: Optional[datetime] = None


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
        idempotency_key: str = "",
    ) -> OrderResult:
        """Place a market order. Returns fill details."""

    def place_pending_order(
        self,
        symbol: str,
        order_kind: str,
        entry_price: float,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        idempotency_key: str = "",
    ) -> OrderResult:
        """Place a pending (limit/stop) order. Override in subclass if supported."""
        return OrderResult(
            success=False,
            order_id="",
            fill_price=0.0,
            requested_price=entry_price,
            slippage_pips=0.0,
            lots=lots,
            symbol=symbol,
            direction=order_kind.split("_")[0],
            sl=sl,
            tp=tp,
            platform="unknown",
            error="Pending orders not supported on this platform",
        )

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

    def get_deal_close_info(self, order_id: str) -> Optional[DealCloseInfo]:
        """Return broker-reported close details including exit reason.
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
