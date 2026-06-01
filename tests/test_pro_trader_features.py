"""Tests for pro-trader features: weekend-gap protection, TP3 ladder, scale-in.

These tests exercise config-gating and production dispatch logic
without requiring pandas, numpy, or live broker connections.
"""
import sys
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace, ModuleType
from unittest.mock import MagicMock, patch, PropertyMock

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
# Helpers
# =====================================================================

def _make_loop():
    """Construct a TradingLoop with all external deps stubbed."""
    from platforms.main_loop import TradingLoop
    loop = TradingLoop.__new__(TradingLoop)
    loop.config = _make_config()
    loop.platforms = MagicMock()
    loop.scanner = MagicMock()
    loop.ranker = MagicMock()
    loop.scheduler = MagicMock()
    loop.orchestrator = MagicMock()
    loop.entry_engine = MagicMock()
    loop.drawdown = MagicMock()
    loop.correlation = MagicMock()
    loop.risk_engine = MagicMock()
    loop.execution_monitor = MagicMock()
    loop.session_engine = MagicMock()
    loop.news_guard = MagicMock()
    loop.journal = MagicMock()
    loop.validator = MagicMock()
    loop.risk_reporter = MagicMock()
    loop.ml = MagicMock()
    loop.re_entry = MagicMock()
    loop.density_tracker = MagicMock()
    loop.vol_monitor = MagicMock()
    from management.trade_manager import TradeManager
    loop.trade_manager = TradeManager()
    loop._journal_loop = MagicMock()
    loop.position_store = MagicMock()
    loop.watchdog = MagicMock()
    loop.maintenance = MagicMock()
    loop._scan_breaker = MagicMock()
    loop._execution_breaker = MagicMock()
    loop._health_check_interval = 10
    loop.system_warnings = []
    loop._MAX_WARNINGS = 200
    loop.managed_positions = {}
    loop._pending_orders = {}
    loop._weekend_protected_oids = set()
    loop.running = False
    loop._last_scan_time = None
    loop._daily_trades = 0
    loop._last_reset_day = None
    return loop


def _make_config(**risk_overrides):
    from config import AppConfig, RiskConfig
    rc = RiskConfig(**risk_overrides)
    return AppConfig(risk=rc)


def _make_managed_pos(symbol="EURUSD", direction="BUY", entry_price=1.1, sl=1.09,
                      lots=0.1, platform="mt5", oid="TEST_1", tp1=1.11, tp2=1.12):
    from platforms.main_loop import ManagedPosition
    order = SimpleNamespace(
        order_id=oid, platform=platform, symbol=symbol,
        direction=direction, lots=lots, fill_price=entry_price,
        sl=sl, tp=tp2,
    )
    pos = ManagedPosition(order=order, tp1=tp1, tp2=tp2, score=90)
    return pos


# =====================================================================
# FEATURE 1 — Weekend-gap protection
# =====================================================================

class TestSessionEngineMinutesToFxClose:
    """SessionEngine.minutes_to_fx_close returns small values only on Friday before 21:00."""

    def test_friday_20_30(self):
        from brain.session_engine import SessionEngine
        se = SessionEngine()
        fri = datetime(2026, 5, 29, 20, 30, tzinfo=timezone.utc)  # Friday
        assert se.minutes_to_fx_close(fri) == 30

    def test_friday_20_45(self):
        from brain.session_engine import SessionEngine
        se = SessionEngine()
        fri = datetime(2026, 5, 29, 20, 45, tzinfo=timezone.utc)
        assert se.minutes_to_fx_close(fri) == 15

    def test_thursday_returns_sentinel(self):
        from brain.session_engine import SessionEngine
        se = SessionEngine()
        thu = datetime(2026, 5, 28, 20, 30, tzinfo=timezone.utc)  # Thursday
        assert se.minutes_to_fx_close(thu) == 99999

    def test_saturday_returns_sentinel(self):
        from brain.session_engine import SessionEngine
        se = SessionEngine()
        sat = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)  # Saturday
        assert se.minutes_to_fx_close(sat) == 99999

    def test_friday_after_close_returns_sentinel(self):
        from brain.session_engine import SessionEngine
        se = SessionEngine()
        fri_late = datetime(2026, 5, 29, 21, 30, tzinfo=timezone.utc)
        assert se.minutes_to_fx_close(fri_late) == 99999


class TestWeekendProtectionConfig:
    """Config flags default OFF."""

    def test_defaults(self):
        from config import RiskConfig
        rc = RiskConfig()
        assert rc.weekend_protection_enabled is False
        assert rc.weekend_protection_mode == "derisk"
        assert rc.weekend_close_buffer_minutes == 15


class TestWeekendProtection:
    """_check_weekend_protection integration tests."""

    def test_default_off_noop(self):
        loop = _make_loop()
        pos = _make_managed_pos()
        loop.managed_positions["T1"] = pos
        fri = datetime(2026, 5, 29, 20, 50, tzinfo=timezone.utc)
        loop._check_weekend_protection(utc_now=fri)
        loop.platforms.close_trade.assert_not_called()
        loop.platforms.modify_trade.assert_not_called()

    def test_flatten_closes_forex_not_synthetic(self):
        loop = _make_loop()
        loop.config = _make_config(
            weekend_protection_enabled=True,
            weekend_protection_mode="flatten",
            weekend_close_buffer_minutes=15,
        )
        loop.session_engine.minutes_to_fx_close.return_value = 10
        forex_pos = _make_managed_pos(symbol="EURUSD", oid="FX1")
        synth_pos = _make_managed_pos(symbol="V75_1S", oid="SY1")
        loop.managed_positions = {"FX1": forex_pos, "SY1": synth_pos}
        close_result = SimpleNamespace(success=True, close_price=1.1, lots_closed=0.1, pnl=0.0, platform="mt5", order_id="FX1")
        loop.platforms.close_trade.return_value = close_result
        loop._record_closed_trade = MagicMock()
        fri = datetime(2026, 5, 29, 20, 50, tzinfo=timezone.utc)
        with patch("platforms.main_loop.is_always_open", side_effect=lambda s: s == "V75_1S"):
            loop._check_weekend_protection(utc_now=fri)
        loop.platforms.close_trade.assert_called_once_with("FX1", "mt5")
        assert "SY1" in loop.managed_positions

    def test_derisk_moves_sl_to_be(self):
        loop = _make_loop()
        loop.config = _make_config(
            weekend_protection_enabled=True,
            weekend_protection_mode="derisk",
            weekend_close_buffer_minutes=15,
        )
        loop.session_engine.minutes_to_fx_close.return_value = 10
        pos = _make_managed_pos(symbol="GBPUSD", entry_price=1.3, sl=1.29, oid="G1")
        loop.managed_positions = {"G1": pos}
        from management.trade_manager import TradeManager, EntrySignal
        tm = TradeManager()
        sig = EntrySignal(
            pair="GBPUSD", direction="BUY", entry_price=1.3,
            stop_loss=1.29, tp1=1.31, tp2=1.32,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.1, score=90,
        )
        trade = tm.open_trade(sig)
        pos.tm_trade_id = trade.trade_id
        loop.trade_manager = tm
        fri = datetime(2026, 5, 29, 20, 50, tzinfo=timezone.utc)
        with patch("platforms.main_loop.is_always_open", return_value=False):
            loop._check_weekend_protection(utc_now=fri)
        loop.platforms.modify_trade.assert_called_once_with("G1", "mt5", new_sl=1.3, new_tp=None)
        assert pos.sl == 1.3
        assert pos.at_breakeven is True

    def test_outside_buffer_noop(self):
        loop = _make_loop()
        loop.config = _make_config(
            weekend_protection_enabled=True,
            weekend_protection_mode="flatten",
            weekend_close_buffer_minutes=15,
        )
        loop.session_engine.minutes_to_fx_close.return_value = 120
        pos = _make_managed_pos(oid="FX1")
        loop.managed_positions = {"FX1": pos}
        fri = datetime(2026, 5, 29, 19, 0, tzinfo=timezone.utc)
        loop._check_weekend_protection(utc_now=fri)
        loop.platforms.close_trade.assert_not_called()

    def test_idempotency(self):
        loop = _make_loop()
        loop.config = _make_config(
            weekend_protection_enabled=True,
            weekend_protection_mode="flatten",
            weekend_close_buffer_minutes=15,
        )
        loop.session_engine.minutes_to_fx_close.return_value = 10
        pos = _make_managed_pos(symbol="EURUSD", oid="FX1")
        loop.managed_positions = {"FX1": pos}
        close_result = SimpleNamespace(success=True, close_price=1.1, lots_closed=0.1, pnl=0.0, platform="mt5", order_id="FX1")
        loop.platforms.close_trade.return_value = close_result
        loop._record_closed_trade = MagicMock()
        fri = datetime(2026, 5, 29, 20, 50, tzinfo=timezone.utc)
        with patch("platforms.main_loop.is_always_open", return_value=False):
            loop._check_weekend_protection(utc_now=fri)
        call_count_1 = loop.platforms.close_trade.call_count
        assert "FX1" in loop._weekend_protected_oids
        loop.managed_positions = {"FX2": _make_managed_pos(symbol="GBPUSD", oid="FX2")}
        close_result2 = SimpleNamespace(success=True, close_price=1.3, lots_closed=0.1, pnl=0.0, platform="mt5", order_id="FX2")
        loop.platforms.close_trade.return_value = close_result2
        with patch("platforms.main_loop.is_always_open", return_value=False):
            loop._check_weekend_protection(utc_now=fri)
        assert loop.platforms.close_trade.call_count == call_count_1 + 1


# =====================================================================
# FEATURE 2 — TP3 Ladder
# =====================================================================

class TestTP3Config:
    """Config flags default OFF."""

    def test_defaults(self):
        from config import RiskConfig
        rc = RiskConfig()
        assert rc.tp3_ladder_enabled is False
        assert rc.tp3_r_multiple == 4.0
        assert rc.tp3_close_ratio == 0.5


class TestTP3TradeManagerConfig:
    """TradeManager accepts TP3 config."""

    def test_default_off(self):
        from management.trade_manager import TradeManager
        tm = TradeManager()
        assert tm.tp3_ladder_enabled is False

    def test_receives_flag(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp3_ladder_enabled=True, tp3_r_multiple=5.0, tp3_close_ratio=0.3)
        assert tm.tp3_ladder_enabled is True
        assert tm.tp3_r_multiple == 5.0
        assert tm.tp3_close_ratio == 0.3


class TestTP3Computation:
    """TP3 is computed on open_trade when enabled and beyond TP2."""

    def _make_signal(self, direction="LONG"):
        from management.trade_manager import EntrySignal
        if direction == "LONG":
            return EntrySignal(
                pair="EURUSD", direction="LONG", entry_price=1.1000,
                stop_loss=1.0950, tp1=1.1050, tp2=1.1100,
                risk_reward_1=1.0, risk_reward_2=2.0,
                position_size_lots=0.10, score=90,
            )
        return EntrySignal(
            pair="EURUSD", direction="SHORT", entry_price=1.1000,
            stop_loss=1.1050, tp1=1.0950, tp2=1.0900,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.10, score=90,
        )

    def test_tp3_none_when_disabled(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp3_ladder_enabled=False)
        trade = tm.open_trade(self._make_signal())
        assert trade.tp3 is None
        assert trade.tp3_hit is False

    def test_tp3_computed_long(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp3_ladder_enabled=True, tp3_r_multiple=4.0)
        trade = tm.open_trade(self._make_signal("LONG"))
        # risk = |1.1 - 1.095| = 0.005; tp3 = 1.1 + 4*0.005 = 1.12
        assert trade.tp3 is not None
        assert abs(trade.tp3 - 1.12) < 1e-8
        assert trade.tp3 > trade.tp2  # beyond TP2

    def test_tp3_computed_short(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp3_ladder_enabled=True, tp3_r_multiple=4.0)
        trade = tm.open_trade(self._make_signal("SHORT"))
        # risk = |1.1 - 1.105| = 0.005; tp3 = 1.1 - 4*0.005 = 1.08
        assert trade.tp3 is not None
        assert abs(trade.tp3 - 1.08) < 1e-8
        assert trade.tp3 < trade.tp2

    def test_tp3_none_when_inside_tp2(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp3_ladder_enabled=True, tp3_r_multiple=1.5)
        sig = self._make_signal("LONG")
        trade = tm.open_trade(sig)
        # risk=0.005; tp3 candidate = 1.1+1.5*0.005=1.1075 < tp2=1.11 → None
        assert trade.tp3 is None


class TestTP3Check:
    """_check_tp3 fires only after partial_closed, reduces remaining_size_lots."""

    def _make_trade_at_tp3(self, tm):
        from management.trade_manager import EntrySignal
        sig = EntrySignal(
            pair="EURUSD", direction="LONG", entry_price=1.1000,
            stop_loss=1.0950, tp1=1.1050, tp2=1.1100,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.10, score=90,
        )
        trade = tm.open_trade(sig)
        trade.partial_closed = True
        trade.remaining_size_lots = 0.05
        return trade

    def test_fires_after_partial(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp3_ladder_enabled=True, tp3_r_multiple=4.0, tp3_close_ratio=0.5)
        trade = self._make_trade_at_tp3(tm)
        assert trade.tp3 is not None
        trade.current_price = trade.tp3 + 0.001
        hit = tm._check_tp3(trade)
        assert hit is True
        assert trade.tp3_hit is True
        assert trade.remaining_size_lots < 0.05

    def test_does_not_fire_before_partial(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp3_ladder_enabled=True, tp3_r_multiple=4.0, tp3_close_ratio=0.5)
        from management.trade_manager import EntrySignal
        sig = EntrySignal(
            pair="EURUSD", direction="LONG", entry_price=1.1000,
            stop_loss=1.0950, tp1=1.1050, tp2=1.1100,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.10, score=90,
        )
        trade = tm.open_trade(sig)
        trade.current_price = 1.15
        hit = tm._check_tp3(trade)
        assert hit is False
        assert trade.tp3_hit is False

    def test_disabled_noop(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp3_ladder_enabled=False)
        from management.trade_manager import EntrySignal
        sig = EntrySignal(
            pair="EURUSD", direction="LONG", entry_price=1.1000,
            stop_loss=1.0950, tp1=1.1050, tp2=1.1100,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.10, score=90,
        )
        trade = tm.open_trade(sig)
        trade.partial_closed = True
        trade.current_price = 1.15
        hit = tm._check_tp3(trade)
        assert hit is False

    def test_tp2_still_closes_after_tp3(self):
        from management.trade_manager import TradeManager
        tm = TradeManager(tp3_ladder_enabled=True, tp3_r_multiple=4.0, tp3_close_ratio=0.5)
        trade = self._make_trade_at_tp3(tm)
        trade.current_price = trade.tp3 + 0.001
        tm._check_tp3(trade)
        assert trade.tp3_hit is True
        remaining_after_tp3 = trade.remaining_size_lots
        assert remaining_after_tp3 > 0
        trade.current_price = trade.tp2 + 0.001
        hit = tm._check_tp2(trade)
        assert hit is True


# =====================================================================
# FEATURE 3 — Scale-in / Pyramiding
# =====================================================================

class TestScaleInConfig:
    """Config flags default OFF."""

    def test_defaults(self):
        from config import RiskConfig
        rc = RiskConfig()
        assert rc.scale_in_enabled is False
        assert rc.scale_in_max_adds == 1
        assert rc.scale_in_min_profit_r == 1.0
        assert rc.scale_in_add_ratio == 0.5


class TestScaleIn:
    """_check_scale_in integration tests."""

    def _setup_winning_position(self, loop):
        from management.trade_manager import TradeManager, EntrySignal
        tm = TradeManager()
        sig = EntrySignal(
            pair="EURUSD", direction="BUY", entry_price=1.1000,
            stop_loss=1.0950, tp1=1.1050, tp2=1.1100,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.10, score=90,
        )
        trade = tm.open_trade(sig)
        trade.partial_closed = True
        trade.breakeven_active = True
        trade.current_price = 1.1100  # 2R in profit
        trade.remaining_size_lots = 0.05
        loop.trade_manager = tm
        pos = _make_managed_pos(
            symbol="EURUSD", direction="BUY", entry_price=1.1,
            sl=1.095, lots=0.1, oid="WIN1",
        )
        pos.tm_trade_id = trade.trade_id
        loop.managed_positions = {"WIN1": pos}
        return pos, trade

    def test_default_off_noop(self):
        loop = _make_loop()
        pos = _make_managed_pos(oid="T1")
        loop.managed_positions = {"T1": pos}
        loop._check_scale_in()
        loop.platforms.execute_entry.assert_not_called()

    def test_does_not_add_when_profit_low(self):
        loop = _make_loop()
        loop.config = _make_config(scale_in_enabled=True, scale_in_min_profit_r=2.0)
        from management.trade_manager import TradeManager, EntrySignal
        tm = TradeManager()
        sig = EntrySignal(
            pair="EURUSD", direction="BUY", entry_price=1.1000,
            stop_loss=1.0950, tp1=1.1050, tp2=1.1100,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.10, score=90,
        )
        trade = tm.open_trade(sig)
        trade.partial_closed = True
        trade.breakeven_active = True
        trade.current_price = 1.1040  # only 0.8R
        loop.trade_manager = tm
        pos = _make_managed_pos(symbol="EURUSD", direction="BUY", entry_price=1.1, sl=1.095, oid="T1")
        pos.tm_trade_id = trade.trade_id
        loop.managed_positions = {"T1": pos}
        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(uses_stake=False, supports_partial_close=True, supports_modify=True)
            loop._check_scale_in()
        loop.platforms.execute_entry.assert_not_called()

    def test_does_not_add_when_not_partial(self):
        loop = _make_loop()
        loop.config = _make_config(scale_in_enabled=True)
        from management.trade_manager import TradeManager, EntrySignal
        tm = TradeManager()
        sig = EntrySignal(
            pair="EURUSD", direction="BUY", entry_price=1.1000,
            stop_loss=1.0950, tp1=1.1050, tp2=1.1100,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.10, score=90,
        )
        trade = tm.open_trade(sig)
        trade.current_price = 1.12
        loop.trade_manager = tm
        pos = _make_managed_pos(symbol="EURUSD", direction="BUY", entry_price=1.1, sl=1.095, oid="T1")
        pos.tm_trade_id = trade.trade_id
        loop.managed_positions = {"T1": pos}
        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(uses_stake=False, supports_partial_close=True, supports_modify=True)
            loop._check_scale_in()
        loop.platforms.execute_entry.assert_not_called()

    def test_respects_max_adds(self):
        loop = _make_loop()
        loop.config = _make_config(scale_in_enabled=True, scale_in_max_adds=1)
        pos, trade = self._setup_winning_position(loop)
        pos.scale_in_count = 1  # Already used the one allowed add
        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(uses_stake=False, supports_partial_close=True, supports_modify=True)
            loop._check_scale_in()
        loop.platforms.execute_entry.assert_not_called()

    def test_blocked_by_correlation(self):
        loop = _make_loop()
        loop.config = _make_config(scale_in_enabled=True)
        pos, trade = self._setup_winning_position(loop)
        loop.correlation.can_open_trade.return_value = False
        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(uses_stake=False, supports_partial_close=True, supports_modify=True)
            loop._check_scale_in()
        loop.platforms.execute_entry.assert_not_called()

    def test_blocked_by_margin(self):
        loop = _make_loop()
        loop.config = _make_config(
            scale_in_enabled=True,
            margin_guardian_enabled=True,
            margin_block_entry_pct=150.0,
        )
        pos, trade = self._setup_winning_position(loop)
        loop.correlation.can_open_trade.return_value = True
        loop._get_margin_level = MagicMock(return_value=120.0)
        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(uses_stake=False, supports_partial_close=True, supports_modify=True)
            loop._check_scale_in()
        loop.platforms.execute_entry.assert_not_called()

    def test_successful_scale_in(self):
        loop = _make_loop()
        loop.config = _make_config(scale_in_enabled=True, scale_in_add_ratio=0.5)
        pos, trade = self._setup_winning_position(loop)
        loop.correlation.can_open_trade.return_value = True
        order_result = SimpleNamespace(
            success=True, order_id="ADD1", fill_price=1.11,
            lots=0.05, symbol="EURUSD", direction="BUY",
            sl=1.1, tp=1.12, platform="mt5",
            requested_price=1.11, slippage_pips=0.0,
        )
        loop.platforms.execute_entry.return_value = order_result
        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(uses_stake=False, supports_partial_close=True, supports_modify=True)
            loop._check_scale_in()
        loop.platforms.execute_entry.assert_called_once()
        assert pos.scale_in_count == 1

    def test_skips_deriv_stake_positions(self):
        loop = _make_loop()
        loop.config = _make_config(scale_in_enabled=True)
        pos, trade = self._setup_winning_position(loop)
        loop.correlation.can_open_trade.return_value = True
        with patch("platforms.main_loop.build_context_for_symbol") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(uses_stake=True, supports_partial_close=False, supports_modify=False)
            loop._check_scale_in()
        loop.platforms.execute_entry.assert_not_called()
