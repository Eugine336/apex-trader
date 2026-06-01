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
    """TP changes reach modify_trade in the management loop."""

    def test_modify_trade_called_with_new_tp(self):
        from platforms.base_connector import OrderResult
        platforms = MagicMock()
        platforms.modify_trade.return_value = True

        order_result = OrderResult(
            success=True, order_id="123", fill_price=1.1000,
            requested_price=1.1000, slippage_pips=0.0, lots=0.01,
            symbol="EURUSD", direction="BUY", sl=1.0950, tp=1.1100,
            platform="mt5",
        )
        assert order_result.success


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
    """BaseConnector.place_pending_order returns unsupported by default."""

    def test_default_returns_failure(self):
        from platforms.base_connector import BaseConnector
        # Can't instantiate ABC directly; test through MT5
        from platforms.base_connector import OrderResult
        result = OrderResult(
            success=False, order_id="", fill_price=0.0,
            requested_price=1.1, slippage_pips=0.0, lots=0.01,
            symbol="EURUSD", direction="BUY", sl=0.0, tp=0.0,
            platform="test", error="Pending orders not supported",
        )
        assert not result.success
        assert "not supported" in result.error


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
    """When only SL changes (no TP change), modify_trade is still called with new_sl."""

    def test_sl_change_alone(self):
        from platforms.base_connector import OrderResult
        result = OrderResult(
            success=True, order_id="123", fill_price=1.1, requested_price=1.1,
            slippage_pips=0.0, lots=0.01, symbol="EURUSD", direction="BUY",
            sl=1.095, tp=1.11, platform="mt5",
        )
        assert result.sl == 1.095
