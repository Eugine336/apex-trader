"""
Cycle supervision — regression tests.

Verifies that:
  1. _run_supervised_cycle absorbs per-cycle exceptions without killing
     the loop (running stays True, consecutive counter increments).
  2. A successful cycle resets the consecutive counter.
  3. max_consecutive_cycle_failures back-to-back failures halt the loop.
  4. KeyboardInterrupt is re-raised (not swallowed).
  5. dashboard/state.py is_live reads the real trading_loop.running flag.
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ── Stub heavy deps before any app imports ────────────────────────────────
_STUB_MODULES = [
    "MetaTrader5", "requests", "loguru", "websockets",
    "websockets.sync", "websockets.sync.client",
    "aiosqlite", "sqlalchemy",
    "torch", "torch.nn", "torch.nn.functional",
    "torch.optim", "torch.distributions",
    "fastapi", "fastapi.middleware", "fastapi.middleware.cors",
    "fastapi.responses", "fastapi.staticfiles", "uvicorn",
    "starlette", "starlette.websockets",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_loguru = sys.modules["loguru"]
_loguru.logger = MagicMock()
_loguru.logger.contextualize = MagicMock(return_value=MagicMock(
    __enter__=MagicMock(return_value=None),
    __exit__=MagicMock(return_value=False),
))
_loguru.logger.exception = MagicMock()
_loguru.logger.critical = MagicMock()


# ── Minimal TradingLoop stub ─────────────────────────────────────────────
# We bind TradingLoop._run_supervised_cycle to a lightweight stub instead
# of constructing a full TradingLoop (which drags in broker connectors).

from platforms.main_loop import TradingLoop


def _make_stub(max_failures=3):
    """Build a minimal object that _run_supervised_cycle can operate on."""
    stub = SimpleNamespace(
        running=True,
        _consecutive_cycle_failures=0,
        config=SimpleNamespace(max_consecutive_cycle_failures=max_failures),
        _current_cycle_id=None,
    )
    stub._check_daily_reset = MagicMock()
    stub.run_once = MagicMock()
    return stub


@patch("platforms.main_loop.get_event_store")
class TestSupervisedCycleSingleFailure:
    def test_single_exception_does_not_kill_loop(self, mock_store):
        stub = _make_stub()
        stub.run_once.side_effect = ValueError("bad tick data")

        TradingLoop._run_supervised_cycle(stub)

        assert stub.running is True
        assert stub._consecutive_cycle_failures == 1

    def test_successful_cycle_resets_counter(self, mock_store):
        stub = _make_stub()
        stub._consecutive_cycle_failures = 2
        stub.run_once.return_value = {}

        TradingLoop._run_supervised_cycle(stub)

        assert stub._consecutive_cycle_failures == 0
        assert stub.running is True


@patch("platforms.main_loop.get_event_store")
class TestSupervisedCycleHalt:
    def test_consecutive_failures_halt_loop(self, mock_store):
        stub = _make_stub(max_failures=3)

        stub.run_once.side_effect = RuntimeError("broker down")

        for _ in range(3):
            TradingLoop._run_supervised_cycle(stub)

        assert stub.running is False
        assert stub._consecutive_cycle_failures == 3

    def test_halt_emits_events(self, mock_store):
        store_instance = MagicMock()
        mock_store.return_value = store_instance
        stub = _make_stub(max_failures=2)
        stub.run_once.side_effect = RuntimeError("oops")

        for _ in range(2):
            TradingLoop._run_supervised_cycle(stub)

        emit_calls = store_instance.emit.call_args_list
        event_types = [c.kwargs.get("event_type") or c[1].get("event_type", c[0][0] if c[0] else None) for c in emit_calls]
        from persistence.domain_events import CYCLE_FAILED, TRADING_LOOP_HALTED
        assert CYCLE_FAILED in event_types
        assert TRADING_LOOP_HALTED in event_types

    def test_event_emit_failure_does_not_crash(self, mock_store):
        mock_store.return_value.emit.side_effect = Exception("store dead")
        stub = _make_stub(max_failures=1)
        stub.run_once.side_effect = RuntimeError("bad")

        TradingLoop._run_supervised_cycle(stub)

        assert stub.running is False


@patch("platforms.main_loop.get_event_store")
class TestSupervisedCycleKeyboardInterrupt:
    def test_keyboard_interrupt_propagates(self, mock_store):
        stub = _make_stub()
        stub.run_once.side_effect = KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            TradingLoop._run_supervised_cycle(stub)

        assert stub.running is True


class TestDashboardIsLive:
    def test_is_live_reflects_running_true(self):
        from dashboard.state import LiveState
        state = LiveState()
        fake_loop = SimpleNamespace(running=True)
        state._trading_loop = fake_loop
        assert state.is_live is True

    def test_is_live_reflects_running_false(self):
        from dashboard.state import LiveState
        state = LiveState()
        fake_loop = SimpleNamespace(running=False)
        state._trading_loop = fake_loop
        assert state.is_live is False

    def test_is_live_no_loop(self):
        from dashboard.state import LiveState
        state = LiveState()
        assert state.is_live is False
