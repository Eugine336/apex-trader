"""Loop adapter — a SimulatedPlatformManager backed by SimulatedBroker.

This is the thin decoupling layer that lets the REAL
:class:`platforms.main_loop.TradingLoop` be replayed against historical data.
``TradingLoop.__init__`` accepts an injected ``platform_manager``; passing one
of these routes every data fetch and order through a single
:class:`~backtest.broker.SimulatedBroker` instead of a live MT5/Deriv account.

It deliberately does NOT call :class:`platforms.platform_manager.PlatformManager`'s
``__init__`` (which constructs real broker connectors); it duck-types the subset
of that class's surface the trading loop calls. The default
:class:`~backtest.runner.BacktestRunner` path does not require this adapter — it
drives the broker and adaptive layers directly — but this bridge is provided so
the full live loop can be exercised when desired.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from platforms.base_connector import (
    AccountInfo,
    CloseResult,
    DealCloseInfo,
    OrderResult,
    PositionInfo,
    TickData,
)
from platforms.platform_manager import BrokerPositionsSnapshot
from backtest.broker import SimulatedBroker

_PLATFORM = "backtest"


class SimulatedPlatformManager:
    """Duck-typed PlatformManager routing everything to one SimulatedBroker."""

    def __init__(self, broker: SimulatedBroker, config=None) -> None:
        self.broker = broker
        self.config = config
        self.candle_cache = None  # fetch_market_data serves directly from broker
        self.system_warnings: list[dict] = []

    # ── Connection lifecycle ─────────────────────────────────────────────

    def connect_all(self) -> dict[str, bool]:
        self.broker.connect()
        return {_PLATFORM: True}

    def disconnect_all(self) -> None:
        self.broker.disconnect()

    def check_connections(self) -> dict[str, bool]:
        return {_PLATFORM: self.broker.is_connected()}

    def reconnect_platform(self, platform: str) -> bool:
        return True

    def should_attempt_reconnect(self, platform: str) -> bool:
        return False

    def any_connected(self) -> bool:
        return self.broker.is_connected()

    # ── Routing / identity ───────────────────────────────────────────────

    def get_connector(self, symbol: str):
        return self.broker

    def get_platform_name(self, symbol: str) -> str:
        return _PLATFORM

    def get_broker_name(self, symbol: str) -> str:
        return _PLATFORM

    def get_account_id(self, symbol: str) -> str:
        return _PLATFORM

    def get_typical_spreads(self, symbol: str) -> dict[str, float]:
        return {_PLATFORM: self.broker.get_spread(symbol)}

    def find_order_by_idem_key(self, idem_key: str):
        return None

    # ── Orders ───────────────────────────────────────────────────────────

    def execute_entry(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        stake_usd: Optional[float] = None,
        idempotency_key: str = "",
    ) -> OrderResult:
        return self.broker.place_order(
            symbol, direction, lots, sl, tp, comment,
            idempotency_key=idempotency_key,
        )

    def place_pending_entry(
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
        return self.broker.place_pending_order(
            symbol, order_kind, entry_price, lots, sl, tp, comment,
            idempotency_key=idempotency_key,
        )

    def modify_trade(
        self, order_id: str, platform: str,
        new_sl: Optional[float] = None, new_tp: Optional[float] = None,
    ) -> bool:
        return self.broker.modify_order(order_id, new_sl, new_tp)

    def close_trade(
        self, order_id: str, platform: str, lots: Optional[float] = None,
    ) -> CloseResult:
        return self.broker.close_order(order_id, lots)

    def get_realized_pnl(self, order_id: str, platform: str) -> Optional[float]:
        return self.broker.get_realized_pnl(order_id)

    def get_deal_close_info(self, order_id: str, platform: str) -> Optional[DealCloseInfo]:
        return self.broker.get_deal_close_info(order_id)

    # ── Positions / account ──────────────────────────────────────────────

    def get_all_open_positions(self) -> list[PositionInfo]:
        return self.broker.get_open_positions()

    def get_open_positions_snapshot(self) -> BrokerPositionsSnapshot:
        return BrokerPositionsSnapshot(
            positions=self.broker.get_open_positions(),
            confirmed_platforms={_PLATFORM},
            failed_platforms=set(),
        )

    def get_account_summary(self) -> dict[str, AccountInfo]:
        return {_PLATFORM: self.broker.get_account_info()}

    def get_total_balance(self) -> float:
        return self.broker.get_account_info().balance

    def get_platform_balance(self, symbol: str) -> float:
        return self.broker.get_account_info().balance

    def get_total_equity(self) -> float:
        return self.broker.get_account_info().equity

    # ── Market data ──────────────────────────────────────────────────────

    def fetch_market_data(
        self, symbol: str, timeframes: Optional[list[str]] = None, count: int = 200,
    ) -> dict[str, pd.DataFrame]:
        if timeframes is None:
            timeframes = ["H4", "H1", "M15", "M5"]
        out: dict[str, pd.DataFrame] = {}
        for tf in timeframes:
            df = self.broker.get_ohlcv(symbol, tf, count)
            if df is not None and not df.empty:
                out[tf] = df
        return out

    def fetch_all_market_data(
        self, symbols: list[str], timeframes: Optional[list[str]] = None, count: int = 200,
    ) -> dict[str, dict[str, pd.DataFrame]]:
        return {s: self.fetch_market_data(s, timeframes, count) for s in symbols}

    def get_price(self, symbol: str) -> TickData:
        return self.broker.get_price(symbol)

    def get_spread(self, symbol: str) -> float:
        return self.broker.get_spread(symbol)
