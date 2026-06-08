"""
Shutdown persistence — regression tests.

Verifies that TradingLoop.stop() routes position saves through
_save_position_checked (not the raw save_position), so a degraded
store at shutdown is surfaced loudly rather than silently dropped
behind a "positions persisted" log that lies.
"""

import sys
from unittest.mock import MagicMock, patch, call

import pytest

# ── Stub heavy deps before any app imports ────────────────────────────────
_STUB_MODULES = [
    "MetaTrader5", "requests", "loguru", "websockets",
    "websockets.sync", "websockets.sync.client",
    "aiosqlite", "sqlalchemy",
    "torch", "torch.nn", "torch.nn.functional",
    "torch.optim", "torch.distributions",
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

from persistence.domain_events import PERSISTENCE_DEGRADED
from platforms.base_connector import OrderResult
from platforms.trading_loop.positions import ManagedPosition


def _make_managed(order_id="ORD-1", symbol="EURUSD", direction="BUY"):
    """Minimal ManagedPosition for testing."""
    order = OrderResult(
        success=True,
        order_id=order_id,
        fill_price=1.10000,
        requested_price=1.10000,
        slippage_pips=0.0,
        lots=0.10,
        symbol=symbol,
        direction=direction,
        sl=1.09500,
        tp=1.11000,
        platform="mt5",
    )
    return ManagedPosition(
        order=order,
        tp1=1.11000,
        tp2=1.12000,
        score=85,
        regime="BULLISH",
        session="LONDON",
    )


class _FakeLoop:
    """Stand-in for TradingLoop with the attrs stop() touches."""

    def __init__(self, store_healthy=True):
        self.running = True
        self.position_store = MagicMock()
        self.position_store.is_healthy.return_value = store_healthy
        self.position_store.degraded_reason.return_value = (
            "" if store_healthy else "save ORD-1: disk full"
        )
        self.platforms = MagicMock()
        self._daily_trades = 3
        self._journal_loop = MagicMock()
        self.managed_positions = {}
        self._current_cycle_id = "cycle-1"
        self._current_setup_id = "setup-1"

    def _save_position_checked(self, managed):
        from platforms.main_loop import TradingLoop
        TradingLoop._save_position_checked(self, managed)

    def stop(self):
        from platforms.main_loop import TradingLoop
        TradingLoop.stop(self)


class TestShutdownHealthyStore:

    def test_calls_checked_save_for_each_position(self):
        loop = _FakeLoop(store_healthy=True)
        p1 = _make_managed("ORD-1", "EURUSD")
        p2 = _make_managed("ORD-2", "GBPUSD")
        loop.managed_positions = {"ORD-1": p1, "ORD-2": p2}

        with patch("platforms.main_loop.get_event_store", return_value=MagicMock()):
            loop.stop()

        assert loop.position_store.save_position.call_count == 2
        saved = {c.args[0].order_id for c in loop.position_store.save_position.call_args_list}
        assert saved == {"ORD-1", "ORD-2"}

    def test_healthy_store_logs_normal_shutdown(self):
        loop = _FakeLoop(store_healthy=True)
        loop.managed_positions = {"ORD-1": _make_managed()}
        logger = _loguru.logger

        with patch("platforms.main_loop.get_event_store", return_value=MagicMock()):
            logger.reset_mock()
            loop.stop()

        info_calls = [c for c in logger.info.call_args_list if "persisted for restart recovery" in str(c)]
        assert len(info_calls) == 1

    def test_close_and_disconnect_called(self):
        loop = _FakeLoop(store_healthy=True)
        loop.managed_positions = {"ORD-1": _make_managed()}

        with patch("platforms.main_loop.get_event_store", return_value=MagicMock()):
            loop.stop()

        loop.position_store.close.assert_called_once()
        loop.platforms.disconnect_all.assert_called_once()


class TestShutdownDegradedStore:

    def test_degraded_store_emits_persistence_degraded(self):
        loop = _FakeLoop(store_healthy=False)
        loop.managed_positions = {"ORD-1": _make_managed()}
        mock_store = MagicMock()

        with patch("platforms.main_loop.get_event_store", return_value=mock_store):
            loop.stop()

        mock_store.emit.assert_called()
        emit_kwargs = mock_store.emit.call_args
        assert emit_kwargs.kwargs["event_type"] == PERSISTENCE_DEGRADED

    def test_degraded_store_logs_error_not_info(self):
        loop = _FakeLoop(store_healthy=False)
        loop.managed_positions = {"ORD-1": _make_managed()}
        logger = _loguru.logger

        with patch("platforms.main_loop.get_event_store", return_value=MagicMock()):
            logger.reset_mock()
            loop.stop()

        error_calls = [c for c in logger.error.call_args_list if "may NOT be on disk" in str(c)]
        assert len(error_calls) == 1
        info_calls = [c for c in logger.info.call_args_list if "persisted for restart recovery" in str(c)]
        assert len(info_calls) == 0

    def test_degraded_store_still_closes_and_disconnects(self):
        loop = _FakeLoop(store_healthy=False)
        loop.managed_positions = {"ORD-1": _make_managed()}

        with patch("platforms.main_loop.get_event_store", return_value=MagicMock()):
            loop.stop()

        loop.position_store.close.assert_called_once()
        loop.platforms.disconnect_all.assert_called_once()


class TestShutdownSaveRaises:

    def test_exception_in_save_does_not_prevent_shutdown(self):
        loop = _FakeLoop(store_healthy=True)
        p1 = _make_managed("ORD-1", "EURUSD")
        p2 = _make_managed("ORD-2", "GBPUSD")
        loop.managed_positions = {"ORD-1": p1, "ORD-2": p2}
        loop.position_store.save_position.side_effect = [Exception("disk dead"), None]

        with patch("platforms.main_loop.get_event_store", return_value=MagicMock()):
            loop.stop()

        loop.position_store.close.assert_called_once()
        loop.platforms.disconnect_all.assert_called_once()

    def test_all_positions_attempted_despite_earlier_failure(self):
        loop = _FakeLoop(store_healthy=True)
        p1 = _make_managed("ORD-1", "EURUSD")
        p2 = _make_managed("ORD-2", "GBPUSD")
        loop.managed_positions = {"ORD-1": p1, "ORD-2": p2}
        loop.position_store.save_position.side_effect = [Exception("disk dead"), None]

        with patch("platforms.main_loop.get_event_store", return_value=MagicMock()):
            loop.stop()

        assert loop.position_store.save_position.call_count == 2
