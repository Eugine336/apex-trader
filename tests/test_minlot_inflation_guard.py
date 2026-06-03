"""Tests for the MT5 min-lot inflation guard (PR-B).

Validates that when broker volume_min floors the risk-sized lot upward,
the connector:
  - ALWAYS logs a [MINLOT_INFLATION] warning (regardless of flag)
  - When reject_on_minlot_inflation=True, returns a failed OrderResult
  - When reject_on_minlot_inflation=False (default), proceeds normally
  - When no inflation occurs, behaves identically for both flag states

Uses the same dep-stubbing pattern as test_p2_execution_features.py.
"""
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import MagicMock

# ── Stub heavy deps so imports succeed in minimal environments ────────────
_STUB_MODULES = [
    "MetaTrader5", "requests", "loguru", "websockets",
    "websockets.sync", "websockets.sync.client",
    "aiosqlite", "sqlalchemy",
]
for mod in _STUB_MODULES:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

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

    class _FakeDataFrame:
        def __init__(self, *a, **kw): pass
        def __len__(self): return 0

    pd_stub.Timestamp = _FakeTimestamp
    pd_stub.Series = _FakeSeries
    pd_stub.DataFrame = _FakeDataFrame
    sys.modules["pandas"] = pd_stub


def _make_connector(reject_on_minlot_inflation: bool = False):
    """Build a minimally-initialised MT5Connector with _connected=True."""
    from platforms.mt5.mt5_connector import MT5Connector
    c = MT5Connector.__new__(MT5Connector)
    c._connected = True
    c._magic = 202500
    c._deviation = 20
    c._broker_name = "test"
    c._symbol_cache = {"XTIUSD": "XTIUSD", "EURUSD": "EURUSD"}
    c._not_found_warned = set()
    c._reject_on_minlot_inflation = reject_on_minlot_inflation
    c._mapper = None
    return c


def _setup_mt5_mock_market(volume_min: float = 0.01):
    """Configure the MT5 mock for a market-order path."""
    mt5_mock = sys.modules["MetaTrader5"]
    mt5_mock.TRADE_ACTION_DEAL = 1
    mt5_mock.ORDER_TYPE_BUY = 0
    mt5_mock.ORDER_TYPE_SELL = 1
    mt5_mock.ORDER_TIME_GTC = 0
    mt5_mock.ORDER_FILLING_IOC = 1
    mt5_mock.TRADE_RETCODE_DONE = 10009

    mt5_mock.symbol_info_tick = MagicMock(return_value=SimpleNamespace(
        ask=1.1000, bid=1.0998,
    ))
    mt5_mock.symbol_select = MagicMock()
    mt5_mock.symbol_info = MagicMock(return_value=SimpleNamespace(
        volume_min=volume_min, volume_max=100.0, volume_step=0.01,
        trade_stops_level=0, digits=5, point=0.00001,
    ))

    sent = {}
    def fake_order_send(req):
        sent.update(req)
        return SimpleNamespace(
            retcode=10009, order=1001, price=1.1000, comment="",
        )
    mt5_mock.order_send = fake_order_send
    return mt5_mock, sent


def _setup_mt5_mock_pending(volume_min: float = 0.01):
    """Configure the MT5 mock for a pending-order path."""
    mt5_mock = sys.modules["MetaTrader5"]
    mt5_mock.TRADE_ACTION_PENDING = 5
    mt5_mock.ORDER_TYPE_SELL_LIMIT = 3
    mt5_mock.ORDER_TIME_GTC = 0
    mt5_mock.ORDER_FILLING_IOC = 1
    mt5_mock.TRADE_RETCODE_DONE = 10009

    mt5_mock.symbol_select = MagicMock()
    mt5_mock.symbol_info = MagicMock(return_value=SimpleNamespace(
        volume_min=volume_min, volume_max=100.0, volume_step=0.01,
        digits=5,
    ))

    sent = {}
    def fake_order_send(req):
        sent.update(req)
        return SimpleNamespace(
            retcode=10009, order=2001, price=98.245, comment="",
        )
    mt5_mock.order_send = fake_order_send
    return mt5_mock, sent


# ═══════════════════════════════════════════════════════════════════════════
# Market order tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMarketOrderMinlotInflation:

    def test_inflation_flag_off_proceeds_with_warning(self):
        """Flag OFF + inflation → warning logged, order placed at vol_min."""
        _setup_mt5_mock_market(volume_min=0.5)
        c = _make_connector(reject_on_minlot_inflation=False)
        result = c.place_order("XTIUSD", "SELL", 0.01, 99.0, 97.0)
        assert result.success is True
        assert result.lots == 0.5
        logger = sys.modules["loguru"].logger
        logger.warning.assert_called()
        warning_args = logger.warning.call_args_list[-1]
        assert "[MINLOT_INFLATION]" in str(warning_args)

    def test_inflation_flag_on_rejects(self):
        """Flag ON + inflation → order rejected, _fail_order returned."""
        _setup_mt5_mock_market(volume_min=0.5)
        c = _make_connector(reject_on_minlot_inflation=True)
        result = c.place_order("XTIUSD", "SELL", 0.01, 99.0, 97.0)
        assert result.success is False
        assert "min-lot inflation" in result.error
        mt5_mock = sys.modules["MetaTrader5"]
        mt5_mock.order_send.assert_not_called()

    def test_no_inflation_flag_off_unchanged(self):
        """No inflation + flag OFF → normal execution."""
        _setup_mt5_mock_market(volume_min=0.01)
        c = _make_connector(reject_on_minlot_inflation=False)
        result = c.place_order("EURUSD", "BUY", 0.05, 1.09, 1.11)
        assert result.success is True
        assert result.lots == 0.05

    def test_no_inflation_flag_on_unchanged(self):
        """No inflation + flag ON → normal execution (guard doesn't fire)."""
        _setup_mt5_mock_market(volume_min=0.01)
        c = _make_connector(reject_on_minlot_inflation=True)
        result = c.place_order("EURUSD", "BUY", 0.05, 1.09, 1.11)
        assert result.success is True
        assert result.lots == 0.05


# ═══════════════════════════════════════════════════════════════════════════
# Pending order tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPendingOrderMinlotInflation:

    def test_inflation_flag_off_proceeds_with_warning(self):
        """Flag OFF + inflation → warning logged, pending placed at vol_min."""
        _setup_mt5_mock_pending(volume_min=0.5)
        c = _make_connector(reject_on_minlot_inflation=False)
        result = c.place_pending_order(
            "XTIUSD", "SELL_LIMIT", 98.245, 0.01, 99.0, 97.0,
        )
        assert result.success is True
        assert result.lots == 0.5
        logger = sys.modules["loguru"].logger
        logger.warning.assert_called()
        warning_args = logger.warning.call_args_list[-1]
        assert "[MINLOT_INFLATION]" in str(warning_args)

    def test_inflation_flag_on_rejects(self):
        """Flag ON + inflation → pending rejected, no order_send."""
        _setup_mt5_mock_pending(volume_min=0.5)
        c = _make_connector(reject_on_minlot_inflation=True)
        result = c.place_pending_order(
            "XTIUSD", "SELL_LIMIT", 98.245, 0.01, 99.0, 97.0,
        )
        assert result.success is False
        assert "min-lot inflation" in result.error

    def test_no_inflation_flag_off_unchanged(self):
        """No inflation + flag OFF → normal pending execution."""
        _setup_mt5_mock_pending(volume_min=0.01)
        c = _make_connector(reject_on_minlot_inflation=False)
        result = c.place_pending_order(
            "EURUSD", "SELL_LIMIT", 1.095, 0.05, 1.10, 1.08,
        )
        assert result.success is True
        assert result.lots == 0.05

    def test_no_inflation_flag_on_unchanged(self):
        """No inflation + flag ON → normal pending (guard doesn't fire)."""
        _setup_mt5_mock_pending(volume_min=0.01)
        c = _make_connector(reject_on_minlot_inflation=True)
        result = c.place_pending_order(
            "EURUSD", "SELL_LIMIT", 1.095, 0.05, 1.10, 1.08,
        )
        assert result.success is True
        assert result.lots == 0.05


# ═══════════════════════════════════════════════════════════════════════════
# Config flag default
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigDefault:

    def test_config_flag_defaults_false(self):
        from config import RiskConfig
        cfg = RiskConfig()
        assert cfg.reject_on_minlot_inflation is False

    def test_connector_default_is_false(self):
        from platforms.mt5.mt5_connector import MT5Connector
        c = MT5Connector()
        assert c._reject_on_minlot_inflation is False
