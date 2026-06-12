"""
APEX TRADER — Platform Integration Tests
Tests connectors, routing, and the trading loop with mock platforms.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest


from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    OrderResult,
    PositionInfo,
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
        assert c._access_token == ""
        assert c._client_id == ""
        assert c._account_type == "demo"
        assert c._connected is False

    def test_init_with_credentials(self):
        c = DerivConnector(client_id="cid", access_token="tok", account_type="real")
        assert c._client_id == "cid"
        assert c._access_token == "tok"
        assert c._account_type == "real"

    def test_connect_no_access_token_returns_false(self):
        c = DerivConnector()
        assert c.connect() is False

    def test_symbol_map_synthetics(self):
        c = DerivConnector()
        assert c.symbol_map("V75_1S") == "1HZ75V"
        assert c.symbol_map("BOOM500") == "BOOM500"
        assert c.symbol_map("CRASH1000") == "CRASH1000"
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
        mgr.mt5_connectors = [mgr.mt5]
        mgr._mt5_connected_flags = [True]
        mgr._mt5_was_connected = [False]
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

    def test_get_platform_balance_mt5_symbol(self):
        mgr = self._make_manager()
        mgr.mt5.get_account_info.return_value = AccountInfo(
            balance=200, equity=200, margin=0, free_margin=200,
            margin_level=0, currency="USD", leverage=1000, platform="mt5",
        )

        assert mgr.get_platform_balance("EURUSD") == 200.0
        mgr.mt5.get_account_info.assert_called_once()
        mgr.deriv.get_account_info.assert_not_called()

    def test_get_platform_balance_deriv_symbol(self):
        mgr = self._make_manager()
        mgr.deriv.get_account_info.return_value = AccountInfo(
            balance=10100.02, equity=10100.02, margin=0, free_margin=10100.02,
            margin_level=0, currency="USD", leverage=1, platform="deriv",
        )

        assert mgr.get_platform_balance("V75_1S") == 10100.02
        mgr.deriv.get_account_info.assert_called_once()
        mgr.mt5.get_account_info.assert_not_called()

    def test_get_platform_balance_returns_zero_on_routing_error(self):
        mgr = self._make_manager()
        mgr._mt5_connected = False
        mgr._deriv_connected = False
        assert mgr.get_platform_balance("EURUSD") == 0.0

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

    def test_deriv_reconnect_log_throttle_positions(self):
        mgr = self._make_manager()
        mgr._deriv_reconnect_warned = False
        mgr.deriv.get_open_positions.side_effect = ConnectionError(
            "Deriv is reconnecting — request blocked"
        )
        mgr.get_all_open_positions()
        assert mgr._deriv_reconnect_warned is True
        mgr.get_all_open_positions()
        assert mgr._deriv_reconnect_warned is True

    def test_deriv_reconnect_log_throttle_account(self):
        mgr = self._make_manager()
        mgr._deriv_reconnect_warned = False
        mgr.deriv.get_account_info.side_effect = ConnectionError(
            "Deriv is reconnecting — request blocked"
        )
        mgr.get_account_summary()
        assert mgr._deriv_reconnect_warned is True
        mgr.get_account_summary()
        assert mgr._deriv_reconnect_warned is True

    def test_deriv_reconnect_flag_clears_on_recovery(self):
        mgr = self._make_manager()
        mgr._deriv_reconnect_warned = True
        mgr.deriv.get_open_positions.return_value = []
        mgr.get_all_open_positions()
        assert mgr._deriv_reconnect_warned is False


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

    def test_trade_manager_sl_buy(self):
        loop = TradingLoop()
        tm = loop.trade_manager
        from management.trade_manager import EntrySignal as TMSig
        sig = TMSig(
            pair="EURUSD", direction="LONG", entry_price=1.1,
            stop_loss=1.09, tp1=1.12, tp2=1.15,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.1, score=90,
        )
        trade = tm.open_trade(sig)
        tm.update(trade, 1.089)
        from management.trade_manager import TradeStatus
        assert trade.status == TradeStatus.STOPPED

    def test_trade_manager_sl_sell(self):
        loop = TradingLoop()
        tm = loop.trade_manager
        from management.trade_manager import EntrySignal as TMSig
        sig = TMSig(
            pair="EURUSD", direction="SHORT", entry_price=1.1,
            stop_loss=1.11, tp1=1.08, tp2=1.06,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.1, score=90,
        )
        trade = tm.open_trade(sig)
        tm.update(trade, 1.115)
        from management.trade_manager import TradeStatus
        assert trade.status == TradeStatus.STOPPED

    def test_trade_manager_tp1(self):
        loop = TradingLoop()
        tm = loop.trade_manager
        from management.trade_manager import EntrySignal as TMSig
        sig = TMSig(
            pair="EURUSD", direction="LONG", entry_price=1.1,
            stop_loss=1.09, tp1=1.12, tp2=1.15,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.1, score=90,
        )
        trade = tm.open_trade(sig)
        tm.update(trade, 1.125)
        assert trade.partial_closed is True

    def test_trade_manager_tp2(self):
        loop = TradingLoop()
        tm = loop.trade_manager
        from management.trade_manager import EntrySignal as TMSig
        sig = TMSig(
            pair="EURUSD", direction="SHORT", entry_price=1.1,
            stop_loss=1.11, tp1=1.08, tp2=1.06,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.1, score=90,
        )
        trade = tm.open_trade(sig)
        tm.update(trade, 1.075)
        tm.update(trade, 1.055)
        from management.trade_manager import TradeStatus
        assert trade.status == TradeStatus.CLOSED
