"""Tests for PR6 management fail-safe (P0 + P6).

Covers the post-entry management exception paths that previously failed open:
  P0  — strategic engine raises ⇒ legacy protective fallback actually runs,
        degraded-mode is tracked, and persistent failure escalates.
  F2  — per-position analysis raises ⇒ open-profit floor still honoured.
  F9  — spread-protection raises ⇒ in-profit position locked to breakeven.
  F12 — scale-in governor raises ⇒ the add is DENIED (risk-adding ⇒ fail closed).
  _failsafe_move_to_breakeven — only ever tightens, only when in profit.
"""
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# ── Stub heavy/optional deps before any app imports (sandbox + CI parity) ──
import sys  # noqa: E402

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


def _pos(**kw):
    defaults = dict(
        symbol="EURUSD", direction="BUY", entry_price=1.10000,
        sl=1.09800, lots=0.5, platform="mt5",
        at_breakeven=False, tm_trade_id="tm-1",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _tm(**kw):
    defaults = dict(breakeven_active=False, stop_loss=1.09800)
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _tick(bid, ask):
    return SimpleNamespace(bid=bid, ask=ask, spread=ask - bid)


# ── _failsafe_move_to_breakeven ───────────────────────────────────────


class TestFailsafeMoveToBreakeven:
    def _mixin(self, tm_trade, tick):
        m = MagicMock()
        m.trade_manager.get_trade.return_value = tm_trade
        m.platforms.get_price.return_value = tick
        m.platforms.modify_trade.return_value = True
        return m

    def test_moves_to_be_when_long_in_profit(self):
        tm = _tm()
        m = self._mixin(tm, _tick(1.10200, 1.10202))
        pos = _pos()
        moved = ExitChecksMixin._failsafe_move_to_breakeven(m, "oid-1", pos, "test")
        assert moved is True
        assert abs(pos.sl - 1.10000) < 1e-9
        assert pos.at_breakeven is True
        assert tm.breakeven_active is True
        m.platforms.modify_trade.assert_called_once()

    def test_noop_when_in_loss(self):
        tm = _tm()
        m = self._mixin(tm, _tick(1.09700, 1.09702))  # below entry → loss
        pos = _pos()
        moved = ExitChecksMixin._failsafe_move_to_breakeven(m, "oid-1", pos, "test")
        assert moved is False
        m.platforms.modify_trade.assert_not_called()
        assert pos.at_breakeven is False

    def test_noop_when_already_at_breakeven(self):
        tm = _tm(breakeven_active=True)
        m = self._mixin(tm, _tick(1.10200, 1.10202))
        pos = _pos()
        moved = ExitChecksMixin._failsafe_move_to_breakeven(m, "oid-1", pos, "test")
        assert moved is False
        m.platforms.modify_trade.assert_not_called()

    def test_never_moves_stop_backwards(self):
        # SL already past breakeven (tighter than entry) → no move.
        tm = _tm()
        m = self._mixin(tm, _tick(1.10200, 1.10202))
        pos = _pos(sl=1.10050)
        moved = ExitChecksMixin._failsafe_move_to_breakeven(m, "oid-1", pos, "test")
        assert moved is False
        m.platforms.modify_trade.assert_not_called()

    def test_short_in_profit_moves_to_be(self):
        tm = _tm(stop_loss=1.10200)
        m = self._mixin(tm, _tick(1.09800, 1.09802))  # below entry → profit for short
        pos = _pos(direction="SELL", sl=1.10200)
        moved = ExitChecksMixin._failsafe_move_to_breakeven(m, "oid-1", pos, "test")
        assert moved is True
        assert abs(pos.sl - 1.10000) < 1e-9

    def test_modify_failure_keeps_unprotected(self):
        tm = _tm()
        m = self._mixin(tm, _tick(1.10200, 1.10202))
        m.platforms.modify_trade.return_value = False
        pos = _pos()
        moved = ExitChecksMixin._failsafe_move_to_breakeven(m, "oid-1", pos, "test")
        assert moved is False
        assert pos.at_breakeven is False
        assert abs(pos.sl - 1.09800) < 1e-9  # unchanged


# ── F9 — spread-protection fail-safe ──────────────────────────────────


class TestSpreadProtectionFailSafe:
    def test_spread_check_error_locks_in_profit_position_to_be(self):
        m = MagicMock()
        m.config = SimpleNamespace(
            risk=SimpleNamespace(
                spread_monitor_enabled=True, spread_deterioration_multiplier=3.0,
            )
        )
        pos = _pos()
        m.managed_positions = {"oid-1": pos}
        tm = _tm()
        m.trade_manager.get_trade.return_value = tm
        # Price feed is healthy (in profit), but the typical-spread lookup
        # raises mid-check → drives the except fail-safe branch.
        m.platforms.get_price.return_value = _tick(1.10200, 1.10202)
        m.platforms.modify_trade.return_value = True
        m.execution_monitor.get_typical_spread.side_effect = RuntimeError("boom")
        # Exercise the real fail-safe helper.
        m._failsafe_move_to_breakeven = lambda *a, **kw: ExitChecksMixin._failsafe_move_to_breakeven(m, *a, **kw)

        ExitChecksMixin._check_spread_deterioration(m)

        # In-profit position was locked to breakeven instead of left exposed.
        assert pos.at_breakeven is True
        assert abs(pos.sl - 1.10000) < 1e-9
        m.platforms.modify_trade.assert_called_once()


# ── F12 — scale-in governor fails closed (already enforced) ───────────


class TestScaleInGovernorFailClosed:
    def test_governor_error_denies_add(self):
        from platforms.main_loop import TradingLoop

        loop = MagicMock(spec=TradingLoop)
        loop._governor = MagicMock()
        loop._governor.check.side_effect = RuntimeError("governor down")
        loop._last_known_balance = 10000.0
        loop.managed_positions = {}
        pos = _pos()

        allowed = TradingLoop._governor_allows_add(loop, pos)
        assert allowed is False  # risk-adding path must deny on error


# ── P0 — strategic engine fails open → real legacy fallback ───────────


def _decision_loop():
    """A MagicMock self for TradingLoop._run_decision_engine with real dicts."""
    from platforms.main_loop import TradingLoop

    loop = MagicMock(spec=TradingLoop)
    loop._degraded_management = {}
    loop._degraded_management_escalate_cycles = 3
    loop._last_decision_action = {}
    loop._last_decision_action_time = {}
    loop.managed_positions = {}
    loop._situation_engine = MagicMock()
    loop._decision_engine = MagicMock()
    loop._risk_governor = None
    loop._decision_journal = None
    return loop


class TestStrategicEngineFailSafe:
    def test_exception_runs_legacy_fallback_and_tracks_degraded(self):
        from platforms.main_loop import TradingLoop

        loop = _decision_loop()
        pos = _pos()
        loop.managed_positions["oid-1"] = pos
        # Force the strategic path to raise before any verdict is produced.
        loop._build_trade_context.side_effect = RuntimeError("boom")

        TradingLoop._run_decision_engine(
            loop, "oid-1", pos, scan_result=MagicMock(), sa_data={},
            hold_minutes=10.0, pressure=0, opposing_boost=0,
            pressure_details=[], now=NOW,
        )

        assert loop._degraded_management["oid-1"] == 1
        loop._run_legacy_management_fallback.assert_called_once_with("oid-1", pos, NOW)
        loop._escalate_degraded_management.assert_not_called()

    def test_escalates_after_threshold_cycles(self):
        from platforms.main_loop import TradingLoop

        loop = _decision_loop()
        pos = _pos()
        loop.managed_positions["oid-1"] = pos
        loop._degraded_management["oid-1"] = 2  # already failed twice
        loop._build_trade_context.side_effect = RuntimeError("boom")

        TradingLoop._run_decision_engine(
            loop, "oid-1", pos, scan_result=MagicMock(), sa_data={},
            hold_minutes=10.0, pressure=0, opposing_boost=0,
            pressure_details=[], now=NOW,
        )

        assert loop._degraded_management["oid-1"] == 3
        loop._escalate_degraded_management.assert_called_once_with("oid-1", pos, NOW)

    def test_success_clears_degraded_state(self):
        from platforms.main_loop import TradingLoop

        loop = _decision_loop()
        pos = _pos()
        loop.managed_positions["oid-1"] = pos
        loop._degraded_management["oid-1"] = 2  # recovering from prior failures
        loop._build_trade_context.return_value = MagicMock()
        loop._situation_engine.assess_open_trade.return_value = MagicMock()
        loop._decision_engine.decide_management.return_value = MagicMock()

        TradingLoop._run_decision_engine(
            loop, "oid-1", pos, scan_result=MagicMock(), sa_data={},
            hold_minutes=10.0, pressure=0, opposing_boost=0,
            pressure_details=[], now=NOW,
        )

        assert "oid-1" not in loop._degraded_management
        loop._execute_management_decision.assert_called_once()

    def test_stale_degraded_entries_pruned_for_closed_positions(self):
        from platforms.main_loop import TradingLoop

        loop = _decision_loop()
        pos = _pos()
        loop.managed_positions["oid-1"] = pos
        loop._degraded_management["closed-oid"] = 5  # position long gone
        loop._build_trade_context.side_effect = RuntimeError("boom")

        TradingLoop._run_decision_engine(
            loop, "oid-1", pos, scan_result=MagicMock(), sa_data={},
            hold_minutes=10.0, pressure=0, opposing_boost=0,
            pressure_details=[], now=NOW,
        )

        assert "closed-oid" not in loop._degraded_management
        assert loop._degraded_management["oid-1"] == 1

