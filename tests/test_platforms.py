"""
APEX TRADER — Platform Integration Tests
Tests connectors, routing, and the trading loop with mock platforms.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

import pandas as pd

from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    OrderResult,
    PositionInfo,
    TickData,
)
from platforms.mt5.mt5_connector import MT5Connector
from platforms.deriv.deriv_connector import DerivConnector, _GRANULARITY_MAP
from platforms.platform_manager import PlatformManager
from platforms.main_loop import ManagedPosition, TradingLoop


# ═══════════════════════════════════════════════════════════════════════════
# BaseConnector interface
# ═══════════════════════════════════════════════════════════════════════════

class TestBaseConnector:

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseConnector()

    def test_all_abstract_methods_exist(self):
        methods = [
            "connect", "disconnect", "is_connected",
            "get_account_info", "get_price", "get_ohlcv",
            "place_order", "modify_order", "close_order",
            "get_open_positions", "get_position_info",
            "get_spread", "get_tick",
        ]
        for method in methods:
            assert hasattr(BaseConnector, method), f"Missing: {method}"

    def test_concrete_helpers_on_base(self):
        assert hasattr(BaseConnector, "symbol_map")
        assert hasattr(BaseConnector, "timeframe_map")


# ═══════════════════════════════════════════════════════════════════════════
# MT5 Connector
# ═══════════════════════════════════════════════════════════════════════════

class TestMT5Connector:

    def test_init_defaults(self):
        c = MT5Connector()
        assert c._login == 0
        assert c._deviation == 20
        assert c._magic == 202500
        assert c._connected is False

    def test_init_with_credentials(self):
        c = MT5Connector(login=12345, password="pw", server="Demo")
        assert c._login == 12345
        assert c._password == "pw"
        assert c._server == "Demo"

    @patch("platforms.mt5.mt5_connector.sys_platform")
    def test_connect_non_windows_returns_false(self, mock_platform):
        mock_platform.system.return_value = "Linux"
        c = MT5Connector()
        assert c.connect() is False

    def test_symbol_map_default(self):
        c = MT5Connector()
        assert c.symbol_map("EURUSD") == "EURUSD"

    def test_not_connected_raises(self):
        c = MT5Connector()
        with pytest.raises(ConnectionError):
            c.get_account_info()

    def test_not_connected_order_raises(self):
        c = MT5Connector()
        with pytest.raises(ConnectionError):
            c.place_order("EURUSD", "BUY", 0.1, 1.0, 1.1)

    def test_not_connected_positions_raises(self):
        c = MT5Connector()
        with pytest.raises(ConnectionError):
            c.get_open_positions()


# ═══════════════════════════════════════════════════════════════════════════
# Deriv Connector
# ═══════════════════════════════════════════════════════════════════════════

class TestDerivConnector:

    def test_init_defaults(self):
        c = DerivConnector()
        assert c._api_token == ""
        assert c._app_id == ""
        assert c._connected is False

    def test_init_with_credentials(self):
        c = DerivConnector(api_token="tok", app_id="999")
        assert c._api_token == "tok"
        assert c._app_id == "999"

    def test_connect_no_app_id_returns_false(self):
        c = DerivConnector()
        assert c.connect() is False

    def test_symbol_map_synthetics(self):
        c = DerivConnector()
        assert c.symbol_map("V75_1S") == "1HZ75V"
        assert c.symbol_map("BOOM300") == "BOOM300N"
        assert c.symbol_map("CRASH1000") == "CRASH1000N"
        assert c.symbol_map("STPIDX") == "stpRNG"

    def test_symbol_map_forex(self):
        c = DerivConnector()
        assert c.symbol_map("EURUSD") == "frxEURUSD"
        assert c.symbol_map("XAUUSD") == "frxXAUUSD"

    def test_symbol_map_unknown_passthrough(self):
        c = DerivConnector()
        assert c.symbol_map("UNKNOWN123") == "UNKNOWN123"

    def test_timeframe_map(self):
        c = DerivConnector()
        assert c.timeframe_map("M1") == 60
        assert c.timeframe_map("M5") == 300
        assert c.timeframe_map("H1") == 3600
        assert c.timeframe_map("H4") == 14400
        assert c.timeframe_map("D1") == 86400

    def test_timeframe_map_unknown_default(self):
        c = DerivConnector()
        assert c.timeframe_map("W1") == 3600

    def test_not_connected_raises(self):
        c = DerivConnector()
        with pytest.raises(ConnectionError):
            c.get_account_info()

    def test_granularity_map_complete(self):
        assert set(_GRANULARITY_MAP.keys()) == {"M1", "M5", "M15", "H1", "H4", "D1"}

    def test_symbol_map_coverage(self):
        synthetics = ["V10_1S", "V25_1S", "V50_1S", "V75_1S", "V100_1S"]
        c = DerivConnector()
        for s in synthetics:
            assert c.symbol_map(s) != s, f"{s} should map to a Deriv-specific name"


# ═══════════════════════════════════════════════════════════════════════════
# PlatformManager
# ═══════════════════════════════════════════════════════════════════════════

class TestPlatformManager:

    def _make_manager(self):
        mgr = PlatformManager.__new__(PlatformManager)
        mgr.config = MagicMock()
        mgr.config.enabled_pairs = ["EURUSD", "V75_1S"]
        mgr.mt5 = MagicMock(spec=MT5Connector)
        mgr.deriv = MagicMock(spec=DerivConnector)
        mgr._mt5_connected = True
        mgr._deriv_connected = True
        return mgr

    def test_get_connector_deriv_synthetic(self):
        mgr = self._make_manager()
        connector = mgr.get_connector("V75_1S")
        assert connector is mgr.deriv

    def test_get_connector_mt5_forex(self):
        mgr = self._make_manager()
        connector = mgr.get_connector("GER40")
        assert connector is mgr.mt5

    def test_get_connector_both_prefers_mt5(self):
        mgr = self._make_manager()
        connector = mgr.get_connector("EURUSD")
        assert connector is mgr.mt5

    def test_get_connector_both_falls_to_deriv(self):
        mgr = self._make_manager()
        mgr._mt5_connected = False
        connector = mgr.get_connector("EURUSD")
        assert connector is mgr.deriv

    def test_get_connector_no_connection_raises(self):
        mgr = self._make_manager()
        mgr._mt5_connected = False
        mgr._deriv_connected = False
        with pytest.raises(ConnectionError):
            mgr.get_connector("EURUSD")

    def test_get_platform_name(self):
        mgr = self._make_manager()
        assert mgr.get_platform_name("V75_1S") == "deriv"
        assert mgr.get_platform_name("EURUSD") == "mt5"

    def test_any_connected(self):
        mgr = self._make_manager()
        assert mgr.any_connected is True
        mgr._mt5_connected = False
        mgr._deriv_connected = False
        assert mgr.any_connected is False

    def test_get_all_open_positions_merges(self):
        mgr = self._make_manager()
        mt5_pos = PositionInfo(
            order_id="1", symbol="EURUSD", direction="BUY", lots=0.1,
            open_price=1.1, current_price=1.11, sl=1.09, tp=1.12,
            pnl=10, swap=0, open_time=datetime.now(timezone.utc), platform="mt5",
        )
        deriv_pos = PositionInfo(
            order_id="2", symbol="V75_1S", direction="SELL", lots=0.5,
            open_price=500, current_price=490, sl=510, tp=480,
            pnl=5, swap=0, open_time=datetime.now(timezone.utc), platform="deriv",
        )
        mgr.mt5.get_open_positions.return_value = [mt5_pos]
        mgr.deriv.get_open_positions.return_value = [deriv_pos]
        positions = mgr.get_all_open_positions()
        assert len(positions) == 2

    def test_execute_entry_routes_correctly(self):
        mgr = self._make_manager()
        mock_order = OrderResult(
            success=True, order_id="123", fill_price=1.105,
            requested_price=1.1, slippage_pips=0.5, lots=0.1,
            symbol="EURUSD", direction="BUY", sl=1.09, tp=1.12, platform="mt5",
        )
        mgr.mt5.place_order.return_value = mock_order
        result = mgr.execute_entry("EURUSD", "BUY", 0.1, 1.09, 1.12)
        assert result.success is True
        assert result.platform == "mt5"
        mgr.mt5.place_order.assert_called_once()

    def test_get_account_summary(self):
        mgr = self._make_manager()
        mgr.mt5.get_account_info.return_value = AccountInfo(
            balance=10000, equity=10050, margin=500, free_margin=9550,
            margin_level=2010, currency="USD", leverage=100, platform="mt5",
        )
        mgr.deriv.get_account_info.return_value = AccountInfo(
            balance=5000, equity=5000, margin=0, free_margin=5000,
            margin_level=0, currency="USD", leverage=1, platform="deriv",
        )
        summary = mgr.get_account_summary()
        assert "mt5" in summary
        assert "deriv" in summary
        assert mgr.get_total_balance() == 15000

    def test_modify_trade_routes(self):
        mgr = self._make_manager()
        mgr.mt5.modify_order.return_value = True
        assert mgr.modify_trade("123", "mt5", new_sl=1.08) is True
        mgr.mt5.modify_order.assert_called_once_with("123", 1.08, None)

    def test_close_trade_routes(self):
        mgr = self._make_manager()
        mgr.deriv.close_order.return_value = CloseResult(
            success=True, order_id="99", close_price=500,
            lots_closed=0.5, pnl=10, platform="deriv",
        )
        result = mgr.close_trade("99", "deriv")
        assert result.success is True


# ═══════════════════════════════════════════════════════════════════════════
# ManagedPosition
# ═══════════════════════════════════════════════════════════════════════════

class TestManagedPosition:

    def _make_order(self) -> OrderResult:
        return OrderResult(
            success=True, order_id="42", fill_price=1.105,
            requested_price=1.1, slippage_pips=0.5, lots=0.2,
            symbol="GBPUSD", direction="BUY", sl=1.09, tp=1.12, platform="mt5",
        )

    def test_init_from_order(self):
        order = self._make_order()
        pos = ManagedPosition(order, tp1=1.12, tp2=1.15, score=92, regime="TRENDING")
        assert pos.order_id == "42"
        assert pos.symbol == "GBPUSD"
        assert pos.direction == "BUY"
        assert pos.lots == 0.2
        assert pos.tp1 == 1.12
        assert pos.tp2 == 1.15
        assert pos.score == 92
        assert pos.tp1_hit is False
        assert pos.at_breakeven is False
        assert pos.trailing is False

    def test_open_time_set(self):
        pos = ManagedPosition(self._make_order(), tp1=1.1, tp2=1.2)
        assert pos.open_time.tzinfo is not None


# ═══════════════════════════════════════════════════════════════════════════
# TradingLoop
# ═══════════════════════════════════════════════════════════════════════════

class TestTradingLoop:

    def test_init_creates_all_components(self):
        loop = TradingLoop()
        assert loop.scanner is not None
        assert loop.ranker is not None
        assert loop.scheduler is not None
        assert loop.orchestrator is not None
        assert loop.drawdown is not None
        assert loop.correlation is not None
        assert loop.execution_monitor is not None
        assert loop.platforms is not None
        assert loop.running is False
        assert loop.managed_positions == {}

    def test_run_once_returns_cycle(self):
        loop = TradingLoop()
        loop.platforms = MagicMock()
        loop.platforms.any_connected = True
        loop.platforms.fetch_all_market_data.return_value = {}
        loop.platforms.get_price = MagicMock(side_effect=Exception("no data"))
        cycle = loop.run_once()
        assert "timestamp" in cycle
        assert "scanned" in cycle
        assert isinstance(cycle["entries_attempted"], int)

    def test_stop_sets_running_false(self):
        loop = TradingLoop()
        loop.platforms = MagicMock()
        loop.running = True
        loop.stop()
        assert loop.running is False

    def test_daily_reset(self):
        loop = TradingLoop()
        loop._daily_trades = 15
        loop._last_reset_day = "2020-01-01"
        loop._check_daily_reset()
        assert loop._daily_trades == 0

    def test_check_sl_buy(self):
        loop = TradingLoop()
        order = OrderResult(
            success=True, order_id="1", fill_price=1.1,
            requested_price=1.1, slippage_pips=0, lots=0.1,
            symbol="EURUSD", direction="BUY", sl=1.09, tp=1.12, platform="mt5",
        )
        pos = ManagedPosition(order, tp1=1.12, tp2=1.15)
        assert loop._check_sl(pos, 1.089, True) is True
        assert loop._check_sl(pos, 1.095, True) is False

    def test_check_sl_sell(self):
        loop = TradingLoop()
        order = OrderResult(
            success=True, order_id="1", fill_price=1.1,
            requested_price=1.1, slippage_pips=0, lots=0.1,
            symbol="EURUSD", direction="SELL", sl=1.11, tp=1.08, platform="mt5",
        )
        pos = ManagedPosition(order, tp1=1.08, tp2=1.06)
        assert loop._check_sl(pos, 1.115, False) is True
        assert loop._check_sl(pos, 1.105, False) is False

    def test_check_tp1(self):
        loop = TradingLoop()
        order = OrderResult(
            success=True, order_id="1", fill_price=1.1,
            requested_price=1.1, slippage_pips=0, lots=0.1,
            symbol="EURUSD", direction="BUY", sl=1.09, tp=1.12, platform="mt5",
        )
        pos = ManagedPosition(order, tp1=1.12, tp2=1.15)
        assert loop._check_tp1(pos, 1.125, True) is True
        assert loop._check_tp1(pos, 1.115, True) is False

    def test_check_tp2(self):
        loop = TradingLoop()
        order = OrderResult(
            success=True, order_id="1", fill_price=1.1,
            requested_price=1.1, slippage_pips=0, lots=0.1,
            symbol="EURUSD", direction="SELL", sl=1.11, tp=1.06, platform="mt5",
        )
        pos = ManagedPosition(order, tp1=1.08, tp2=1.06)
        assert loop._check_tp2(pos, 1.055, False) is True
        assert loop._check_tp2(pos, 1.065, False) is False
