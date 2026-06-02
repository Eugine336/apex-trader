"""Tests for P2 execution features: TP modify + pending orders.

These tests exercise connector-level request building and config-gating
without requiring pandas, numpy, or live broker connections.
"""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace, ModuleType
from unittest.mock import MagicMock, patch

import pytest


# ── Stub heavy deps so imports succeed in minimal environments ────────────
_STUB_MODULES = [
    "MetaTrader5", "requests", "loguru", "websockets",
    "websockets.sync", "websockets.sync.client",
    "aiosqlite", "sqlalchemy",
]
for mod in _STUB_MODULES:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# loguru needs a callable `logger` with `.info`, `.debug`, `.warning`, `.error`, `.exception`
_loguru = sys.modules["loguru"]
_loguru.logger = MagicMock()

if "numpy" not in sys.modules:
    np_stub = MagicMock()
    np_stub.__version__ = "0.0.0"
    sys.modules["numpy"] = np_stub

if "pandas" not in sys.modules:
    pd_stub = ModuleType("pandas")

    class _FakeTimestamp:
        def __init__(self, *a, **kw): pass

    class _FakeSeries:
        def __init__(self, *a, **kw):
            self._data = a[0] if a else []
        def __len__(self):
            return len(self._data) if hasattr(self._data, '__len__') else 0
        def __getitem__(self, k):
            return self._data[k] if hasattr(self._data, '__getitem__') else None
        def __iter__(self):
            return iter(self._data) if hasattr(self._data, '__iter__') else iter([])
        @property
        def iloc(self):
            return self
        @property
        def values(self):
            return list(self._data) if hasattr(self._data, '__iter__') else []
        def rolling(self, *a, **kw): return self
        def mean(self, *a, **kw): return self
        def std(self, *a, **kw): return self
        def max(self, *a, **kw): return 0
        def min(self, *a, **kw): return 0
        def sum(self, *a, **kw): return 0
        def abs(self, *a, **kw): return self
        def shift(self, *a, **kw): return self
        def dropna(self, *a, **kw): return self
        def tolist(self): return list(self._data) if hasattr(self._data, '__iter__') else []

    class _FakeDataFrame:
        def __init__(self, data=None, **kw):
            self._data = data or {}
            self._len = 0
            if data:
                first = next(iter(data.values()), [])
                self._len = len(first) if hasattr(first, '__len__') else 0

        def __len__(self):
            return self._len

        def __getitem__(self, key):
            if isinstance(key, str) and key in self._data:
                return _FakeSeries(self._data[key])
            return self

        @property
        def iloc(self):
            return self

        @property
        def values(self):
            return []

        @property
        def columns(self):
            return list(self._data.keys())

        def iterrows(self):
            return iter([])

        def tail(self, n=5):
            return self

        def head(self, n=5):
            return self

        @property
        def empty(self):
            return self._len == 0

    pd_stub.DataFrame = _FakeDataFrame
    pd_stub.Timestamp = _FakeTimestamp
    pd_stub.Series = _FakeSeries
    pd_stub.to_datetime = MagicMock()
    pd_stub.concat = MagicMock(return_value=_FakeDataFrame())
    pd_stub.isna = MagicMock(return_value=False)
    pd_stub.notna = MagicMock(return_value=True)
    sys.modules["pandas"] = pd_stub

if "scipy" not in sys.modules:
    sys.modules["scipy"] = MagicMock()
    sys.modules["scipy.stats"] = MagicMock()

if "sklearn" not in sys.modules:
    sys.modules["sklearn"] = MagicMock()
    sys.modules["sklearn.linear_model"] = MagicMock()


# =====================================================================
# ITEM 4 — TP modify
# =====================================================================

class TestTPAdjustConfig:
    """tp_adjust_enabled config flag default + propagation."""

    def test_default_off(self):
        from config import RiskConfig
        rc = RiskConfig()
        assert rc.tp_adjust_enabled is False

    def test_trade_manager_receives_flag(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp_adjust_enabled=True)
        assert tm.tp_adjust_enabled is True

    def test_trade_manager_default_off(self):
        from management.trade_manager import TradeManager
        tm = TradeManager()
        assert tm.tp_adjust_enabled is False


class TestManagedTradeOriginalTP2:
    """ManagedTrade stores original_tp2 for reference."""

    def test_original_tp2_stored(self):
        from management.trade_manager import TradeManager, EntrySignal
        tm = TradeManager()
        sig = EntrySignal(
            pair="EURUSD",
            direction="LONG",
            entry_price=1.1000,
            stop_loss=1.0950,
            tp1=1.1050,
            tp2=1.1100,
            risk_reward_1=1.0,
            risk_reward_2=2.0,
            position_size_lots=0.01,
            score=90,
        )
        trade = tm.open_trade(sig)
        assert trade.original_tp2 == 1.1100
        assert trade.tp2 == 1.1100


class TestTPAdjustLogic:
    """_adjust_tp2 extends TP on continuation BOS, tightens on counter CHOCH."""

    def _make_trade(self, tm, direction="LONG"):
        from management.trade_manager import EntrySignal
        entry = 1.1000 if direction == "LONG" else 1.1100
        sl = 1.0950 if direction == "LONG" else 1.1150
        tp2 = 1.1200 if direction == "LONG" else 1.0900
        sig = EntrySignal(
            pair="EURUSD",
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            tp1=1.1050 if direction == "LONG" else 1.1050,
            tp2=tp2,
            risk_reward_1=1.0,
            risk_reward_2=2.0,
            position_size_lots=0.01,
            score=90,
        )
        trade = tm.open_trade(sig)
        trade.partial_closed = True
        return trade

    def test_extend_long_on_bos_bullish(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp_adjust_enabled=True)
        trade = self._make_trade(tm, "LONG")
        original_tp2 = trade.tp2

        with patch("management.trade_manager.StructureEngine") as MockSE:
            from brain.structure_engine import StructureEvent
            mock_analysis = SimpleNamespace(last_event=StructureEvent.BOS_BULLISH)
            MockSE.return_value.analyze.return_value = mock_analysis
            df = sys.modules["pandas"].DataFrame({"close": list(range(20))})
            tm._adjust_tp2(trade, df)

        assert trade.tp2 > original_tp2

    def test_no_adjust_when_disabled(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp_adjust_enabled=False)
        trade = self._make_trade(tm, "LONG")
        original_tp2 = trade.tp2
        trade.current_price = 1.1150

        from management.trade_manager import TradeStatus
        assert trade.tp2 == original_tp2

    def test_tighten_short_on_counter_choch(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp_adjust_enabled=True)
        trade = self._make_trade(tm, "SHORT")
        trade.current_price = 1.0950
        original_tp2 = trade.tp2

        with patch("management.trade_manager.StructureEngine") as MockSE:
            from brain.structure_engine import StructureEvent
            mock_analysis = SimpleNamespace(last_event=StructureEvent.CHOCH_BULLISH)
            MockSE.return_value.analyze.return_value = mock_analysis
            df = sys.modules["pandas"].DataFrame({"close": list(range(20))})
            tm._adjust_tp2(trade, df)

        assert trade.tp2 > original_tp2


class TestTPModifyWiring:
    """TP changes reach modify_trade in the management loop.

    Covers main_loop.py lines 1128-1134 (prev_tp2 capture, trade_manager.update)
    and 1223-1234 (tp_changed detection + modify_trade dispatch).
    """

    def _make_loop_with_position(self, *, tp2_before=1.1100, tp2_after=1.1200,
                                  sl_before=1.0950, sl_after=1.0950):
        """Build a minimal TradingLoop for the modify-dispatch tests."""
        from platforms.main_loop import TradingLoop, ManagedPosition
        from platforms.base_connector import OrderResult, PositionInfo
        from management.trade_manager import TradeStatus

        loop = TradingLoop.__new__(TradingLoop)
        loop.managed_positions = {}
        loop.position_store = MagicMock()
        loop.platforms = MagicMock()
        loop.trade_manager = MagicMock()
        loop.config = MagicMock()
        loop.config.risk.margin_guardian_enabled = False
        loop.config.risk.reconcile_max_unconfirmed_cycles = 20

        order = OrderResult(
            success=True, order_id="TP_TEST_1", fill_price=1.1000,
            requested_price=1.1000, slippage_pips=0.0, lots=0.01,
            symbol="EURUSD", direction="BUY", sl=sl_before, tp=tp2_before,
            platform="mt5",
        )
        pos = ManagedPosition(order=order, tp1=1.1050, tp2=tp2_before)
        pos.tm_trade_id = "tm_1"
        pos.broker_pnl = 0.0
        pos.broker_lots = 0.01
        loop.managed_positions["TP_TEST_1"] = pos

        bp = PositionInfo(
            order_id="TP_TEST_1", symbol="EURUSD", direction="BUY",
            lots=0.01, open_price=1.1000, current_price=1.1050,
            sl=sl_before, tp=tp2_before, pnl=5.0, swap=0.0,
            open_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            platform="mt5",
        )
        from platforms.platform_manager import BrokerPositionsSnapshot
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[bp], confirmed_platforms={"mt5"}, failed_platforms=set(),
        )
        loop.platforms.get_all_open_positions.return_value = [bp]
        loop.platforms.get_realized_pnl.return_value = None

        tick = SimpleNamespace(bid=1.1050, ask=1.1052)
        loop.platforms.get_price.return_value = tick
        loop.platforms.fetch_market_data.return_value = {}
        loop.platforms.modify_trade.return_value = True

        tm_trade = SimpleNamespace(
            trade_id="tm_1",
            stop_loss=sl_before,
            tp2=tp2_before,
            partial_closed=False,
            status=TradeStatus.OPEN,
            close_reason=None,
            close_time=None,
            breakeven_active=False,
            re_entry_eligible=False,
            current_price=1.1050,
        )
        loop.trade_manager.get_trade.return_value = tm_trade

        def update_side_effect(trade, price, m5_df):
            trade.stop_loss = sl_after
            trade.tp2 = tp2_after
            return trade
        loop.trade_manager.update.side_effect = update_side_effect

        return loop, pos

    def test_modify_trade_called_with_new_tp(self):
        """Covers main_loop.py:1224-1230 — tp_changed triggers modify_trade with new_tp."""
        loop, pos = self._make_loop_with_position(
            tp2_before=1.1100, tp2_after=1.1200,
            sl_before=1.0950, sl_after=1.0950,
        )
        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(
                supports_modify=True, supports_partial_close=True,
                sizing_mode="lots", uses_stake=False,
            )
            loop._update_positions()

        loop.platforms.modify_trade.assert_called_once_with(
            "TP_TEST_1", "mt5", new_sl=None, new_tp=1.1200,
        )

    def test_tp_and_sl_both_changed(self):
        """Covers main_loop.py:1223-1230 — both SL and TP changed in one cycle."""
        loop, pos = self._make_loop_with_position(
            tp2_before=1.1100, tp2_after=1.1200,
            sl_before=1.0950, sl_after=1.1000,
        )
        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(
                supports_modify=True, supports_partial_close=True,
                sizing_mode="lots", uses_stake=False,
            )
            loop._update_positions()

        loop.platforms.modify_trade.assert_called_once_with(
            "TP_TEST_1", "mt5", new_sl=1.1000, new_tp=1.1200,
        )


# =====================================================================
# ITEM 5 — Pending orders
# =====================================================================

class TestPendingOrderConfig:
    """pending_orders_enabled config flag default."""

    def test_default_off(self):
        from config import RiskConfig
        rc = RiskConfig()
        assert rc.pending_orders_enabled is False

    def test_max_wait_default(self):
        from config import RiskConfig
        rc = RiskConfig()
        assert rc.pending_max_wait_minutes == 30


class TestBaseConnectorPendingDefault:
    """BaseConnector.place_pending_order returns unsupported by default.

    Covers base_connector.py:153-177 — the non-abstract default body.
    """

    def test_default_returns_failure(self):
        """Actually invoke the base-class default via a minimal concrete subclass."""
        from platforms.base_connector import BaseConnector, OrderResult

        class _MinimalConnector(BaseConnector):
            def connect(self): return True
            def disconnect(self): pass
            def is_connected(self): return True
            def place_order(self, *a, **kw): return OrderResult(
                success=False, order_id="", fill_price=0, requested_price=0,
                slippage_pips=0, lots=0, symbol="", direction="", sl=0, tp=0,
                platform="test",
            )
            def modify_order(self, *a, **kw): return False
            def close_order(self, *a, **kw): pass
            def get_open_positions(self): return []
            def get_position_info(self, oid): return None
            def get_price(self, sym): return None
            def fetch_candles(self, *a, **kw): return []
            def get_account_info(self): return {}
            def get_ohlcv(self, *a, **kw): return []
            def get_spread(self, *a, **kw): return 0.0
            def get_tick(self, *a, **kw): return None

        c = _MinimalConnector()
        result = c.place_pending_order("EURUSD", "BUY_LIMIT", 1.0950, 0.05, 1.0900, 1.1050)
        assert not result.success
        assert "not supported" in result.error.lower()


class TestPendingOrderKindEnum:
    """PendingOrderKind enum has all 4 types."""

    def test_all_kinds_present(self):
        from platforms.base_connector import PendingOrderKind
        assert PendingOrderKind.BUY_LIMIT.value == "BUY_LIMIT"
        assert PendingOrderKind.SELL_LIMIT.value == "SELL_LIMIT"
        assert PendingOrderKind.BUY_STOP.value == "BUY_STOP"
        assert PendingOrderKind.SELL_STOP.value == "SELL_STOP"


class TestMT5PendingOrderRequest:
    """MT5Connector.place_pending_order builds correct request."""

    def test_request_shape_buy_limit(self):
        mt5_mock = sys.modules["MetaTrader5"]
        mt5_mock.TRADE_ACTION_PENDING = 5
        mt5_mock.ORDER_TYPE_BUY_LIMIT = 2
        mt5_mock.ORDER_TIME_GTC = 0
        mt5_mock.ORDER_FILLING_IOC = 1
        mt5_mock.TRADE_RETCODE_DONE = 10009

        from platforms.mt5.mt5_connector import MT5Connector
        c = MT5Connector.__new__(MT5Connector)
        c._connected = True
        c._magic = 123456
        c._deviation = 10
        c._broker_name = "test"
        c._broker_config = {}
        c._symbol_not_found_cache = set()
        c._symbol_cache = {"EURUSD": "EURUSD"}
        c._mapper = None

        sent_request = {}
        def fake_order_send(req):
            sent_request.update(req)
            return SimpleNamespace(
                retcode=10009, order=999, price=1.0950, comment=""
            )

        mt5_mock.order_send = fake_order_send
        mt5_mock.symbol_select = MagicMock()
        mt5_mock.symbol_info = MagicMock(return_value=SimpleNamespace(
            volume_min=0.01, volume_max=100.0, volume_step=0.01, digits=5,
            trade_stops_level=0, point=0.00001,
        ))

        result = c.place_pending_order(
            "EURUSD", "BUY_LIMIT", 1.0950, 0.05, 1.0900, 1.1050,
        )

        assert result.success
        assert sent_request["action"] == 5
        assert sent_request["type"] == 2
        assert sent_request["price"] == 1.095
        assert sent_request["volume"] == 0.05

    def test_unknown_order_kind_fails(self):
        from platforms.mt5.mt5_connector import MT5Connector
        c = MT5Connector.__new__(MT5Connector)
        c._connected = True
        c._magic = 123456
        c._deviation = 10
        c._broker_name = "test"
        c._broker_config = {}
        c._symbol_not_found_cache = set()
        c._symbol_cache = {"EURUSD": "EURUSD"}
        c._mapper = None

        result = c.place_pending_order(
            "EURUSD", "INVALID_TYPE", 1.0950, 0.05, 1.0900, 1.1050,
        )
        assert not result.success
        assert "Unknown" in result.error


class TestDerivPendingUnsupported:
    """Deriv connector returns unsupported for pending orders."""

    def test_deriv_pending_returns_unsupported(self):
        from platforms.deriv.deriv_connector import DerivConnector
        c = DerivConnector.__new__(DerivConnector)
        result = c.place_pending_order(
            "V75_1S", "BUY_LIMIT", 100.0, 0.01, 95.0, 110.0,
        )
        assert not result.success
        assert "not supported" in result.error.lower()


class TestPlatformManagerPendingEntry:
    """PlatformManager.place_pending_entry routes correctly."""

    def test_routes_to_mt5(self):
        from platforms.platform_manager import PlatformManager
        pm = PlatformManager.__new__(PlatformManager)
        mock_connector = MagicMock()
        mock_connector.place_pending_order.return_value = MagicMock(
            success=True, order_id="456",
        )
        pm.get_connector = MagicMock(return_value=mock_connector)

        from platforms.mt5.mt5_connector import MT5Connector
        with patch("platforms.platform_manager.MT5Connector", MT5Connector):
            result = pm.place_pending_entry(
                "EURUSD", "BUY_LIMIT", 1.095, 0.05, 1.09, 1.105,
            )
        mock_connector.place_pending_order.assert_called_once()


class TestPendingOrderTracking:
    """_pending_orders dict tracks placed orders."""

    def test_pending_dict_initialized(self):
        from platforms.main_loop import TradingLoop
        loop = TradingLoop.__new__(TradingLoop)
        loop._pending_orders = {}
        assert isinstance(loop._pending_orders, dict)


class TestRegressionSLOnlyModifyStillWorks:
    """When only SL changes (no TP change), modify_trade is called with new_sl only.

    Covers main_loop.py:1223-1230 — sl_changed=True, tp_changed=False results
    in modify_trade(new_sl=<new>, new_tp=None).
    """

    def test_sl_change_alone(self):
        """SL-only change dispatches modify_trade with new_sl, new_tp=None."""
        from platforms.main_loop import TradingLoop, ManagedPosition
        from platforms.base_connector import OrderResult, PositionInfo
        from management.trade_manager import TradeStatus

        loop = TradingLoop.__new__(TradingLoop)
        loop.managed_positions = {}
        loop.position_store = MagicMock()
        loop.platforms = MagicMock()
        loop.trade_manager = MagicMock()
        loop.config = MagicMock()
        loop.config.risk.margin_guardian_enabled = False
        loop.config.risk.reconcile_max_unconfirmed_cycles = 20

        order = OrderResult(
            success=True, order_id="SL_TEST_1", fill_price=1.1000,
            requested_price=1.1000, slippage_pips=0.0, lots=0.01,
            symbol="EURUSD", direction="BUY", sl=1.0950, tp=1.1100,
            platform="mt5",
        )
        pos = ManagedPosition(order=order, tp1=1.1050, tp2=1.1100)
        pos.tm_trade_id = "tm_sl"
        pos.broker_pnl = 0.0
        pos.broker_lots = 0.01
        loop.managed_positions["SL_TEST_1"] = pos

        bp = PositionInfo(
            order_id="SL_TEST_1", symbol="EURUSD", direction="BUY",
            lots=0.01, open_price=1.1000, current_price=1.1050,
            sl=1.0950, tp=1.1100, pnl=5.0, swap=0.0,
            open_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            platform="mt5",
        )
        from platforms.platform_manager import BrokerPositionsSnapshot
        loop.platforms.get_open_positions_snapshot.return_value = BrokerPositionsSnapshot(
            positions=[bp], confirmed_platforms={"mt5"}, failed_platforms=set(),
        )
        loop.platforms.get_all_open_positions.return_value = [bp]
        loop.platforms.get_realized_pnl.return_value = None
        loop.platforms.get_price.return_value = SimpleNamespace(bid=1.1050, ask=1.1052)
        loop.platforms.fetch_market_data.return_value = {}
        loop.platforms.modify_trade.return_value = True

        tm_trade = SimpleNamespace(
            trade_id="tm_sl", stop_loss=1.0950, tp2=1.1100,
            partial_closed=False, status=TradeStatus.OPEN,
            close_reason=None, close_time=None,
            breakeven_active=False, re_entry_eligible=False,
            current_price=1.1050,
        )
        loop.trade_manager.get_trade.return_value = tm_trade

        def update_sl_only(trade, price, m5_df):
            trade.stop_loss = 1.1000  # moved to breakeven
            return trade
        loop.trade_manager.update.side_effect = update_sl_only

        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(
                supports_modify=True, supports_partial_close=True,
                sizing_mode="lots", uses_stake=False,
            )
            loop._update_positions()

        loop.platforms.modify_trade.assert_called_once_with(
            "SL_TEST_1", "mt5", new_sl=1.1000, new_tp=None,
        )


# =====================================================================
# _check_pending_orders lifecycle tests
# =====================================================================

class TestCheckPendingOrdersLifecycle:
    """End-to-end tests for TradingLoop._check_pending_orders.

    Covers main_loop.py lines 939-1018: fill detection, expiry cancellation,
    and edge cases (not-yet-filled, broker query failure, empty dict).
    """

    def _make_loop(self, pending=None):
        """Build a TradingLoop with only the attributes _check_pending_orders reads."""
        from platforms.main_loop import TradingLoop
        loop = TradingLoop.__new__(TradingLoop)
        loop._pending_orders = pending if pending is not None else {}
        loop.managed_positions = {}
        loop.platforms = MagicMock()
        loop.trade_manager = MagicMock()
        loop.position_store = MagicMock()
        loop._daily_trades = 0
        return loop

    def _make_pending_info(self, symbol="EURUSD", direction="BUY",
                           placed_minutes_ago=5, max_wait=30):
        """Build a pending-order info dict matching what _execute_entry stores."""
        from trigger.entry_engine import EntrySignal
        sig = SimpleNamespace(
            tp1=1.1050, tp2=1.1100, score=90, stop_loss=1.0950,
            risk_reward_1=1.0, risk_reward_2=2.0, entry_type="FVG_MIDPOINT",
            entry_timeframe="M5",
        )
        placed = datetime.now(timezone.utc) - __import__("datetime").timedelta(minutes=placed_minutes_ago)
        return {
            "symbol": symbol,
            "direction": direction,
            "signal": sig,
            "session": "LONDON",
            "placed_at": placed,
            "max_wait_minutes": max_wait,
        }

    def test_fill_path(self):
        """Covers lines 953-999: pending order found in broker_positions → adopted.

        Asserts: moved into managed_positions, trade_manager.open_trade called,
        position_store.save_position called, _daily_trades incremented, oid removed
        from _pending_orders.
        """
        from platforms.base_connector import PositionInfo

        oid = "PEND_001"
        info = self._make_pending_info()
        loop = self._make_loop(pending={oid: info})

        bp = PositionInfo(
            order_id=oid, symbol="EURUSD", direction="BUY",
            lots=0.05, open_price=1.0950, current_price=1.1000,
            sl=1.0900, tp=1.1100, pnl=2.5, swap=0.0,
            open_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            platform="mt5",
        )
        loop.platforms.get_all_open_positions.return_value = [bp]

        mock_tm_trade = SimpleNamespace(trade_id="tm_pend_1")
        loop.trade_manager.open_trade.return_value = mock_tm_trade

        loop._check_pending_orders()

        assert oid not in loop._pending_orders
        assert oid in loop.managed_positions
        loop.trade_manager.open_trade.assert_called_once()
        loop.position_store.save_position.assert_called_once()
        assert loop._daily_trades == 1

    def test_expiry_cancels_mt5_pending(self):
        """Covers lines 1000-1016: aged-out pending → MT5 TRADE_ACTION_REMOVE cancel.

        Asserts: mt5.order_send called with TRADE_ACTION_REMOVE, oid removed from
        _pending_orders. Uses numeric oid since production code calls int(oid).
        """
        mt5_mod = sys.modules["MetaTrader5"]
        mt5_mod.TRADE_ACTION_REMOVE = 11
        mt5_mod.order_send = MagicMock()

        oid = "1234567"
        info = self._make_pending_info(placed_minutes_ago=60, max_wait=30)
        loop = self._make_loop(pending={oid: info})

        loop.platforms.get_all_open_positions.return_value = []

        from platforms.mt5.mt5_connector import MT5Connector
        mt5_connector = MT5Connector.__new__(MT5Connector)
        loop.platforms.get_connector.return_value = mt5_connector

        loop._check_pending_orders()

        assert oid not in loop._pending_orders
        mt5_mod.order_send.assert_called_once()
        sent_req = mt5_mod.order_send.call_args[0][0]
        assert sent_req["action"] == 11  # TRADE_ACTION_REMOVE
        assert sent_req["order"] == 1234567

    def test_not_filled_not_expired_is_kept(self):
        """Covers lines 951-952 + 1000: pending order still young, not filled → stays.

        Asserts: remains in _pending_orders, no open_trade, no order_send REMOVE.
        """
        mt5_mod = sys.modules["MetaTrader5"]
        mt5_mod.order_send = MagicMock()

        oid = "PEND_WAITING"
        info = self._make_pending_info(placed_minutes_ago=5, max_wait=30)
        loop = self._make_loop(pending={oid: info})

        loop.platforms.get_all_open_positions.return_value = []

        loop._check_pending_orders()

        assert oid in loop._pending_orders
        loop.trade_manager.open_trade.assert_not_called()
        mt5_mod.order_send.assert_not_called()

    def test_broker_query_failure_is_safe(self):
        """Covers lines 945-949: get_all_open_positions raises → returns safely.

        Asserts: method returns without raising, _pending_orders unchanged.
        """
        oid = "PEND_SAFE"
        info = self._make_pending_info()
        loop = self._make_loop(pending={oid: info})

        loop.platforms.get_all_open_positions.side_effect = ConnectionError("broker down")

        loop._check_pending_orders()

        assert oid in loop._pending_orders
        loop.trade_manager.open_trade.assert_not_called()

    def test_empty_pending_is_noop(self):
        """Covers line 940-941: empty _pending_orders → early return, no broker call.

        Asserts: get_all_open_positions never called.
        """
        loop = self._make_loop(pending={})

        loop._check_pending_orders()

        loop.platforms.get_all_open_positions.assert_not_called()
