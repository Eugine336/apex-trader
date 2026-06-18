"""
Regression tests for:
  A) MT5 modify_order min-stop-distance clamp (root cause of recurring INVALID_STOPS)
  B) Exit-handler failure logging + position retention on close/modify failures
"""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace, ModuleType
from unittest.mock import MagicMock, patch, call

# ── Stub heavy deps before any app imports ──────────────────────────────
_STUB_MODULES = [
    "MetaTrader5", "requests", "loguru", "websockets",
    "websockets.sync", "websockets.sync.client",
    "aiosqlite", "sqlalchemy",
    "torch", "torch.nn", "torch.nn.functional",
    "torch.optim", "torch.distributions",
]
for mod in _STUB_MODULES:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

_loguru = sys.modules["loguru"]
_loguru.logger = MagicMock()

_np = ModuleType("numpy")
_np.__version__ = "1.26.0"
_np.ndarray = list
_np.float64 = float
_np.array = lambda x, **kw: list(x) if hasattr(x, "__iter__") else [x]
_np.zeros = lambda n: [0] * n
_np.mean = lambda x: sum(x) / len(x) if x else 0.0
_np.std = lambda x, **kw: 0.0
_np.nan = float("nan")
_np.isnan = lambda x: x != x
_np.isscalar = lambda x: isinstance(x, (int, float, complex))
if "numpy" not in sys.modules:
    sys.modules["numpy"] = _np

_pd = ModuleType("pandas")

class _FakeTS:
    @staticmethod
    def now(tz=None):
        return _FakeTS()
    def isoformat(self):
        return "2025-01-01T00:00:00+00:00"

_pd.Timestamp = _FakeTS
_pd.Series = MagicMock
_pd.DataFrame = MagicMock
if "pandas" not in sys.modules:
    sys.modules["pandas"] = _pd

import pytest

_fake_mt5 = sys.modules["MetaTrader5"]
_fake_mt5.ORDER_TYPE_BUY = 0
_fake_mt5.ORDER_TYPE_SELL = 1
_fake_mt5.TRADE_ACTION_SLTP = 6
_fake_mt5.TRADE_RETCODE_DONE = 10009
_fake_mt5.last_error.return_value = (0, "ok")

from platforms.mt5.mt5_connector import MT5Connector  # noqa: E402
from platforms.base_connector import CloseResult  # noqa: E402

# Reference the mt5 module object that the connector actually imported
import platforms.mt5.mt5_connector as _connector_mod
_mt5 = _connector_mod.mt5


# ═══════════════════════════════════════════════════════════════════════════
# A) MT5 modify_order clamp tests
# ═══════════════════════════════════════════════════════════════════════════

def _make_connector():
    c = MT5Connector.__new__(MT5Connector)
    c._connected = True
    c._magic = 202500
    c._deviation = 20
    c._symbol_cache = {}
    c._not_found_warned = set()
    c._broker_name = "test"
    c._mapper = None
    c._reject_on_minlot_inflation = False
    return c


def _position_ns(ticket=99, symbol="EURUSD", type_=0, sl=1.09, tp=1.12,
                 price_open=1.105, volume=0.1):
    return SimpleNamespace(
        ticket=ticket, symbol=symbol, type=type_,
        sl=sl, tp=tp, price_open=price_open, volume=volume,
    )


class TestModifyOrderClamp:
    """Verify that modify_order clamps SL/TP to the broker min-stop distance."""

    def test_sl_inside_min_band_gets_clamped_buy(self):
        """BUY: SL too close to bid → pushed out to min_distance below bid."""
        c = _make_connector()
        pos = _position_ns(type_=0, sl=1.09)  # BUY
        tick = SimpleNamespace(bid=1.10500, ask=1.10520)
        constraints = {"stops_level": 50, "point": 0.00001, "digits": 5}
        send_result = SimpleNamespace(retcode=10009, comment="ok")

        with patch.object(c, "_get_symbol_constraints", return_value=constraints), \
             patch.object(_mt5, "positions_get", return_value=[pos]), \
             patch.object(_mt5, "symbol_info_tick", return_value=tick), \
             patch.object(_mt5, "order_send", return_value=send_result) as mock_send:
            result = c.modify_order("99", new_sl=1.10480)  # 0.00020 away, < 0.00050

        assert result is True
        sent = mock_send.call_args[0][0]
        # SL should be clamped to bid - 0.00050 = 1.10500 - 0.00050 = 1.10450
        assert abs(sent["sl"] - 1.10450) < 1e-5

    def test_sl_outside_min_band_passes_through(self):
        """SL already far enough → no clamping."""
        c = _make_connector()
        pos = _position_ns(type_=0, sl=1.09)
        tick = SimpleNamespace(bid=1.10500, ask=1.10520)
        constraints = {"stops_level": 50, "point": 0.00001, "digits": 5}
        send_result = SimpleNamespace(retcode=10009, comment="ok")

        with patch.object(c, "_get_symbol_constraints", return_value=constraints), \
             patch.object(_mt5, "positions_get", return_value=[pos]), \
             patch.object(_mt5, "symbol_info_tick", return_value=tick), \
             patch.object(_mt5, "order_send", return_value=send_result) as mock_send:
            result = c.modify_order("99", new_sl=1.09500)

        assert result is True
        sent = mock_send.call_args[0][0]
        assert abs(sent["sl"] - 1.09500) < 1e-5

    def test_sl_inside_min_band_sell(self):
        """SELL: SL too close to ask → pushed out to min_distance above ask."""
        c = _make_connector()
        pos = _position_ns(type_=1, sl=1.12)  # SELL
        tick = SimpleNamespace(bid=1.10480, ask=1.10500)
        constraints = {"stops_level": 50, "point": 0.00001, "digits": 5}
        send_result = SimpleNamespace(retcode=10009, comment="ok")

        with patch.object(c, "_get_symbol_constraints", return_value=constraints), \
             patch.object(_mt5, "positions_get", return_value=[pos]), \
             patch.object(_mt5, "symbol_info_tick", return_value=tick), \
             patch.object(_mt5, "order_send", return_value=send_result) as mock_send:
            result = c.modify_order("99", new_sl=1.10520)

        assert result is True
        sent = mock_send.call_args[0][0]
        assert abs(sent["sl"] - 1.10550) < 1e-5

    def test_tp_inside_min_band_gets_clamped(self):
        """TP too close → pushed out to min_distance."""
        c = _make_connector()
        pos = _position_ns(type_=0, tp=1.12)  # BUY
        tick = SimpleNamespace(bid=1.10500, ask=1.10520)
        constraints = {"stops_level": 50, "point": 0.00001, "digits": 5}
        send_result = SimpleNamespace(retcode=10009, comment="ok")

        with patch.object(c, "_get_symbol_constraints", return_value=constraints), \
             patch.object(_mt5, "positions_get", return_value=[pos]), \
             patch.object(_mt5, "symbol_info_tick", return_value=tick), \
             patch.object(_mt5, "order_send", return_value=send_result) as mock_send:
            result = c.modify_order("99", new_tp=1.10530)

        assert result is True
        sent = mock_send.call_args[0][0]
        assert abs(sent["tp"] - 1.10550) < 1e-5

    def test_clamp_warning_logged(self):
        """Clamp emits a logger.warning."""
        c = _make_connector()
        pos = _position_ns(type_=0, sl=1.09)
        tick = SimpleNamespace(bid=1.10500, ask=1.10520)
        constraints = {"stops_level": 50, "point": 0.00001, "digits": 5}
        send_result = SimpleNamespace(retcode=10009, comment="ok")

        with patch.object(c, "_get_symbol_constraints", return_value=constraints), \
             patch.object(_mt5, "positions_get", return_value=[pos]), \
             patch.object(_mt5, "symbol_info_tick", return_value=tick), \
             patch.object(_mt5, "order_send", return_value=send_result), \
             patch("platforms.mt5.mt5_connector.logger") as mock_logger:
            c.modify_order("99", new_sl=1.10480)

        warning_calls = [
            c for c in mock_logger.warning.call_args_list
            if "SL too close on modify" in str(c)
        ]
        assert len(warning_calls) >= 1

    def test_no_tick_returns_false(self):
        """If tick fetch fails, modify returns False immediately."""
        c = _make_connector()
        pos = _position_ns()

        with patch.object(_mt5, "positions_get", return_value=[pos]), \
             patch.object(_mt5, "symbol_info_tick", return_value=None):
            result = c.modify_order("99", new_sl=1.09)
        assert result is False

    def test_zero_stops_level_spread_floor_clamps(self):
        """stops_level=0 (e.g. BTCUSD) still clamps via the spread floor — this is
        the INVALID_STOPS fix: a too-tight SL is widened instead of rejected."""
        c = _make_connector()
        pos = _position_ns(type_=0, sl=1.09)  # BUY, wide existing stop
        tick = SimpleNamespace(bid=1.10500, ask=1.10520)  # spread 0.00020 → floor 0.00030
        constraints = {"stops_level": 0, "point": 0.00001, "digits": 5}
        send_result = SimpleNamespace(retcode=10009, comment="ok")

        with patch.object(c, "_get_symbol_constraints", return_value=constraints), \
             patch.object(_mt5, "positions_get", return_value=[pos]), \
             patch.object(_mt5, "symbol_info_tick", return_value=tick), \
             patch.object(_mt5, "order_send", return_value=send_result) as mock_send:
            result = c.modify_order("99", new_sl=1.10499)  # 0.00001 from bid → too close

        assert result is True
        sent = mock_send.call_args[0][0]
        # clamped to bid - 1.5*spread = 1.10500 - 0.00030 = 1.10470
        assert abs(sent["sl"] - 1.10470) < 1e-5

    def test_clamp_never_loosens_existing_sl(self):
        """If the spread-floor clamp would push SL looser than the current stop,
        keep the existing stop — the min-distance clamp must never raise risk."""
        c = _make_connector()
        pos = _position_ns(type_=0, sl=1.10495)  # BUY, tight existing stop near price
        tick = SimpleNamespace(bid=1.10500, ask=1.10560)  # spread 0.00060 → floor 0.00090
        constraints = {"stops_level": 0, "point": 0.00001, "digits": 5}
        send_result = SimpleNamespace(retcode=10009, comment="ok")

        with patch.object(c, "_get_symbol_constraints", return_value=constraints), \
             patch.object(_mt5, "positions_get", return_value=[pos]), \
             patch.object(_mt5, "symbol_info_tick", return_value=tick), \
             patch.object(_mt5, "order_send", return_value=send_result) as mock_send:
            # request a tighter SL; floor would clamp to 1.10410 (looser than 1.10495)
            result = c.modify_order("99", new_sl=1.10498)

        assert result is True
        sent = mock_send.call_args[0][0]
        assert abs(sent["sl"] - 1.10495) < 1e-5  # existing stop kept, not loosened


# ═══════════════════════════════════════════════════════════════════════════
# B) Exit-handler failure logging + position retention
# ═══════════════════════════════════════════════════════════════════════════

def _make_managed_position(**kw):
    defaults = dict(
        symbol="EURUSD", direction="BUY", entry_price=1.105,
        sl=1.09, sl_original=1.09, tp=1.12, lots=0.1,
        platform="mt5", score=85, tm_trade_id="tm-1",
        open_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_fail_close():
    return CloseResult(
        success=False, order_id="oid-1", close_price=0.0,
        lots_closed=0.0, pnl=0.0, platform="mt5", error="broker rejected",
    )


def _make_mixin():
    mixin = MagicMock()
    mixin.managed_positions = {}
    mixin.position_store = MagicMock()
    mixin._position_scores = {}
    mixin._position_last_h1_close = {}
    mixin._news_exit_protected = set()
    mixin._record_closed_trade = MagicMock()
    return mixin


class TestNewsExitCloseFailure:
    """_check_news_exit mode=close: on failure, position retained + error logged."""

    def test_news_close_failure_retains_position(self):
        from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin

        mixin = _make_mixin()
        pos = _make_managed_position()
        mixin.managed_positions = {"oid-1": pos}
        mixin.platforms = MagicMock()
        mixin.platforms.close_trade.return_value = _make_fail_close()
        mixin.news_guard = MagicMock()
        event = SimpleNamespace(
            minutes_until=5, affected_pairs=["EURUSD"],
            impact="HIGH", name="NFP",
        )
        mixin.news_guard.check.return_value = SimpleNamespace(upcoming_events=[event])
        mixin.config = SimpleNamespace(
            risk=SimpleNamespace(
                news_exit_enabled=True,
                news_exit_minutes_before=15,
                news_exit_mode="close",
            ),
        )

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            ExitChecksMixin._check_news_exit(mixin, datetime(2025, 1, 1, tzinfo=timezone.utc))

        assert "oid-1" in mixin.managed_positions
        mixin.position_store.remove_position.assert_not_called()
        error_calls = [c for c in mock_logger.error.call_args_list if "NEWS EXIT close FAILED" in str(c)]
        assert len(error_calls) >= 1


class TestSessionCloseFailure:
    """_check_session_close: on close failure, position retained + error logged."""

    def test_session_close_failure_retains_position(self):
        from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin

        mixin = _make_mixin()
        pos = _make_managed_position(symbol="US100")
        mixin.managed_positions = {"oid-1": pos}
        mixin.platforms = MagicMock()
        mixin.platforms.close_trade.return_value = CloseResult(
            success=False, order_id="oid-1", close_price=0.0,
            lots_closed=0.0, pnl=0.0, platform="mt5", error="timeout",
        )
        mixin.trade_manager = MagicMock()
        mixin.trade_manager.get_trade.return_value = SimpleNamespace(pnl_pips=5.0)
        mixin.config = SimpleNamespace(
            risk=SimpleNamespace(
                session_close_enabled=True,
                index_close_buffer_minutes=30,
                dead_zone_management=False,
            ),
        )

        # 20:50 UTC → US100 closes at 21:00 → 10 min until close → inside buffer
        now = datetime(2025, 1, 1, 20, 50, tzinfo=timezone.utc)
        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            ExitChecksMixin._check_session_close(mixin, now)

        assert "oid-1" in mixin.managed_positions
        mixin.position_store.remove_position.assert_not_called()
        error_calls = [c for c in mock_logger.error.call_args_list if "SESSION CLOSE close FAILED" in str(c)]
        assert len(error_calls) >= 1


class TestDynamicSlTightenFailure:
    """_apply_dynamic_sl_tightening: on modify failure, SL not updated + warning logged."""

    def test_tighten_failure_keeps_old_sl(self):
        from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin

        mixin = _make_mixin()
        pos = _make_managed_position(sl=1.09, entry_price=1.105)
        mixin.managed_positions = {"oid-1": pos}
        mixin.platforms = MagicMock()
        mixin.platforms.modify_trade.return_value = False
        # entry=1.105, sl_original=1.09 → risk=0.015; need price at 2R+ → 1.105+0.030=1.135
        mixin.platforms.get_price.return_value = SimpleNamespace(bid=1.136, ask=1.13620)

        tm_trade = SimpleNamespace(
            stop_loss=1.09, breakeven_active=True,
            pip_size=0.0001, pnl_pips=310.0, partial_closed=False,
        )
        mixin.trade_manager = MagicMock()
        mixin.trade_manager.get_trade.return_value = tm_trade

        mixin.config = SimpleNamespace(
            risk=SimpleNamespace(
                dynamic_sl_tightening_enabled=True,
                dynamic_sl_tighten_at_r=2.0,
                dynamic_sl_tighten_ratio=0.5,
            ),
        )

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            ExitChecksMixin._apply_dynamic_sl_tightening(mixin, "oid-1", pos)

        assert pos.sl == 1.09  # unchanged
        mixin.position_store.update_position.assert_not_called()
        warn_calls = [c for c in mock_logger.warning.call_args_list if "DYNAMIC SL TIGHTEN FAILED" in str(c)]
        assert len(warn_calls) >= 1


class TestSpreadDeteriorationFailure:
    """_check_spread_deterioration: on modify failure, error logged, SL not updated."""

    def test_spread_be_failure_keeps_old_sl(self):
        from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin

        mixin = _make_mixin()
        pos = _make_managed_position(sl=1.09)
        mixin.managed_positions = {"oid-1": pos}
        mixin.platforms = MagicMock()
        mixin.platforms.modify_trade.return_value = False
        mixin.platforms.get_price.return_value = SimpleNamespace(
            bid=1.11, ask=1.115, spread=50.0,
        )

        tm_trade = SimpleNamespace(
            stop_loss=1.09, breakeven_active=False,
            pip_size=0.0001,
        )
        mixin.trade_manager = MagicMock()
        mixin.trade_manager.get_trade.return_value = tm_trade
        mixin.execution_monitor = MagicMock()
        mixin.execution_monitor.get_typical_spread.return_value = 10.0

        mixin.config = SimpleNamespace(
            risk=SimpleNamespace(
                spread_monitor_enabled=True,
                spread_deterioration_multiplier=3.0,
            ),
        )

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger, \
             patch("management.partial_close.PartialCloseCalculator") as mock_pc:
            mock_pc.calculate_breakeven_level.return_value = 1.1052
            ExitChecksMixin._check_spread_deterioration(mixin)

        assert pos.sl == 1.09  # unchanged
        mixin.position_store.update_position.assert_not_called()
        error_calls = [c for c in mock_logger.error.call_args_list if "SPREAD DETERIORATION BE FAILED" in str(c)]
        assert len(error_calls) >= 1
