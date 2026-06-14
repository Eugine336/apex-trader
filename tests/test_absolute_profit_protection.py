"""
Tests for absolute / early profit protection.

`_apply_absolute_profit_protection` locks a modest open profit to breakeven
the moment it crosses an *absolute* floor (account currency OR pips),
independent of TP1 / R-multiple. This is the protection adopted/orphan trades
(unknown original risk → R-gates never fire) previously lacked, letting a
+$X position round-trip into a loss.
"""
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

# ── Stub heavy/optional deps before any app imports (sandbox + CI parity) ──
_STUB_MODULES = [
    "loguru", "MetaTrader5", "requests", "websockets",
    "websockets.sync", "websockets.sync.client",
    "aiosqlite", "sqlalchemy",
    "torch", "torch.nn", "torch.nn.functional",
    "torch.optim", "torch.distributions",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
sys.modules["loguru"].logger = MagicMock()

if "numpy" not in sys.modules:
    _np = ModuleType("numpy")
    _np.__version__ = "1.26.0"
    _np.ndarray = list
    _np.float64 = float
    _np.array = lambda x, **kw: list(x) if hasattr(x, "__iter__") else [x]
    _np.zeros = lambda n: [0] * n
    _np.mean = lambda x: sum(x) / len(x) if len(x) else 0.0
    _np.std = lambda x, **kw: 0.0
    _np.nan = float("nan")
    _np.isnan = lambda x: x != x
    _np.isscalar = lambda x: isinstance(x, (int, float, complex))
    sys.modules["numpy"] = _np

if "pandas" not in sys.modules:
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
    sys.modules["pandas"] = _pd

from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin  # noqa: E402

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _risk(**kw):
    defaults = dict(
        absolute_be_protection_enabled=True,
        absolute_be_floor_usd=15.0,
        absolute_be_floor_pips=12.0,
        absolute_be_buffer_pips=1.0,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _mixin(**risk_kw):
    m = MagicMock()
    m.config = SimpleNamespace(risk=_risk(**risk_kw))
    m._absolute_be_protected = set()
    m.position_store = MagicMock()
    m.platforms = MagicMock()
    m.platforms.modify_trade.return_value = True
    return m


def _pos(**kw):
    defaults = dict(
        symbol="EURUSD", direction="BUY", entry_price=1.10000,
        sl=1.09800, lots=0.5, platform="mt5",
        at_breakeven=False, broker_pnl=0.0, tm_trade_id="tm-1",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _tm(**kw):
    defaults = dict(
        breakeven_active=False, stop_loss=1.09800,
        pnl_pips=0.0, pnl_dollars=0.0,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _call(mixin, pos, tm_trade, current_price, supports_modify=True):
    with patch("platforms.trading_loop.exit_checks_mixin.get_pip_size", return_value=0.0001), \
         patch(
             "platforms.trading_loop.exit_checks_mixin.build_context_for_symbol",
             return_value=SimpleNamespace(supports_modify=supports_modify),
         ):
        ExitChecksMixin._apply_absolute_profit_protection(
            mixin, "oid-1", pos, tm_trade, current_price, NOW,
        )


class TestUsdFloorTriggers:
    def test_usd_floor_moves_sl_to_breakeven_buy(self):
        m = _mixin()
        pos = _pos(broker_pnl=18.0)
        tm = _tm(pnl_pips=20.0, pnl_dollars=18.0)
        _call(m, pos, tm, current_price=1.10200)

        m.platforms.modify_trade.assert_called_once()
        new_sl = m.platforms.modify_trade.call_args.kwargs["new_sl"]
        assert abs(new_sl - 1.10010) < 1e-6  # entry + 1 pip buffer
        assert abs(pos.sl - 1.10010) < 1e-6
        assert pos.at_breakeven is True
        assert tm.breakeven_active is True
        assert "oid-1" in m._absolute_be_protected
        m.position_store.update_position.assert_called_once()


class TestPipsFloorTriggers:
    def test_pips_floor_triggers_without_broker_pnl(self):
        # USD leg disabled; only the pip floor can fire.
        m = _mixin(absolute_be_floor_usd=0.0, absolute_be_floor_pips=12.0)
        pos = _pos(broker_pnl=0.0)
        tm = _tm(pnl_pips=0.0, pnl_dollars=0.0)
        _call(m, pos, tm, current_price=1.10200)  # +20 pips

        m.platforms.modify_trade.assert_called_once()
        assert pos.at_breakeven is True
        assert "oid-1" in m._absolute_be_protected


class TestBelowFloorNoop:
    def test_below_both_floors_does_nothing(self):
        m = _mixin()
        pos = _pos(broker_pnl=3.0)
        tm = _tm(pnl_pips=5.0, pnl_dollars=3.0)
        _call(m, pos, tm, current_price=1.10005)  # +5 pips, $3

        m.platforms.modify_trade.assert_not_called()
        assert pos.at_breakeven is False
        assert "oid-1" not in m._absolute_be_protected


class TestIdempotent:
    def test_already_at_breakeven_noop(self):
        m = _mixin()
        pos = _pos(broker_pnl=50.0, at_breakeven=True)
        tm = _tm(pnl_pips=40.0, pnl_dollars=50.0)
        _call(m, pos, tm, current_price=1.10400)
        m.platforms.modify_trade.assert_not_called()

    def test_already_in_protected_set_noop(self):
        m = _mixin()
        m._absolute_be_protected.add("oid-1")
        pos = _pos(broker_pnl=50.0)
        tm = _tm(pnl_pips=40.0, pnl_dollars=50.0)
        _call(m, pos, tm, current_price=1.10400)
        m.platforms.modify_trade.assert_not_called()


class TestDisabled:
    def test_disabled_flag_noop(self):
        m = _mixin(absolute_be_protection_enabled=False)
        pos = _pos(broker_pnl=50.0)
        tm = _tm(pnl_pips=40.0, pnl_dollars=50.0)
        _call(m, pos, tm, current_price=1.10400)
        m.platforms.modify_trade.assert_not_called()


class TestShort:
    def test_short_moves_sl_below_entry(self):
        m = _mixin()
        pos = _pos(direction="SELL", entry_price=1.10000, sl=1.10200, broker_pnl=18.0)
        tm = _tm(pnl_pips=20.0, pnl_dollars=18.0)
        _call(m, pos, tm, current_price=1.09800)  # +20 pips for a short

        m.platforms.modify_trade.assert_called_once()
        new_sl = m.platforms.modify_trade.call_args.kwargs["new_sl"]
        assert abs(new_sl - 1.09990) < 1e-6  # entry - 1 pip buffer
        assert pos.at_breakeven is True


class TestModifyFailure:
    def test_modify_failure_keeps_sl_unprotected(self):
        m = _mixin()
        m.platforms.modify_trade.return_value = False
        pos = _pos(broker_pnl=18.0)
        tm = _tm(pnl_pips=20.0, pnl_dollars=18.0)
        _call(m, pos, tm, current_price=1.10200)

        assert abs(pos.sl - 1.09800) < 1e-6  # unchanged
        assert pos.at_breakeven is False
        assert "oid-1" not in m._absolute_be_protected  # retried next tick
        m.position_store.update_position.assert_not_called()


class TestNeverMoveStopBackwards:
    def test_stop_already_past_breakeven_no_modify(self):
        m = _mixin()
        # SL already 5 pips above entry — tighter than the BE+buffer target.
        pos = _pos(sl=1.10050, broker_pnl=18.0)
        tm = _tm(pnl_pips=20.0, pnl_dollars=18.0)
        _call(m, pos, tm, current_price=1.10200)

        m.platforms.modify_trade.assert_not_called()
        assert abs(pos.sl - 1.10050) < 1e-6  # untouched
        assert "oid-1" in m._absolute_be_protected


class TestDerivLocalProtection:
    def test_modify_unsupported_records_locally(self):
        m = _mixin()
        pos = _pos(symbol="R_75", platform="deriv", broker_pnl=18.0)
        tm = _tm(pnl_pips=20.0, pnl_dollars=18.0)
        _call(m, pos, tm, current_price=1.10200, supports_modify=False)

        m.platforms.modify_trade.assert_not_called()
        assert pos.at_breakeven is True
        assert "oid-1" in m._absolute_be_protected
        m.position_store.update_position.assert_called_once()
