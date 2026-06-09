"""Tests for broker-handoff failure handling in _update_positions (STEP 3).

Covers two silent money-losing defects:
1. modify_trade return value was discarded — phantom breakeven / SL trail.
2. partial-close failure was swallowed — permanent shadow/broker divergence.
"""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ── Stub heavy dependencies before any app import ─────────────────────
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

from platforms.base_connector import OrderResult, CloseResult
from platforms.trading_loop.positions import ManagedPosition
from management.trade_manager import ManagedTrade, TradeStatus


# ── Helpers ───────────────────────────────────────────────────────────

def _make_managed(order_id="ORD-1", symbol="EURUSD", direction="BUY",
                  sl=1.09500, lots=0.10):
    order = OrderResult(
        success=True,
        order_id=order_id,
        fill_price=1.10000,
        requested_price=1.10000,
        slippage_pips=0.0,
        lots=lots,
        symbol=symbol,
        direction=direction,
        sl=sl,
        tp=1.11000,
        platform="mt5",
    )
    return ManagedPosition(
        order=order, tp1=1.10500, tp2=1.12000, score=85,
        regime="BULLISH", session="LONDON",
    )


def _make_tm_trade(
    trade_id="T-1", direction="BUY", stop_loss=1.09500, tp2=1.12000,
    breakeven_active=False, partial_closed=False, remaining=0.10,
    status=TradeStatus.OPEN, new_sl=None, new_tp2=None,
):
    return ManagedTrade(
        trade_id=trade_id,
        pair="EURUSD",
        direction=direction,
        entry_price=1.10000,
        current_price=1.10500,
        stop_loss=new_sl if new_sl is not None else stop_loss,
        original_stop_loss=stop_loss,
        tp1=1.10500,
        tp2=new_tp2 if new_tp2 is not None else tp2,
        original_tp2=tp2,
        position_size_lots=0.10,
        remaining_size_lots=remaining,
        status=status,
        pnl_pips=50.0,
        pnl_dollars=50.0,
        entry_time=datetime.now(timezone.utc),
        tp1_hit_time=None,
        close_time=None,
        close_reason=None,
        candles_since_entry=20,
        highest_price_since_entry=1.10600,
        lowest_price_since_entry=1.09800,
        trailing_stop=1.09800,
        breakeven_active=breakeven_active,
        partial_closed=partial_closed,
        re_entry_eligible=False,
        score=85,
        pip_size=0.00010,
        pip_value_per_lot=10.0,
    )


_MT5_CTX = SimpleNamespace(
    supports_partial_close=True,
    supports_modify=True,
)


class _FakeLoop:
    """Minimal stand-in wired to the real _update_positions method."""

    def __init__(self, modify_returns=True, close_success=True):
        self.position_store = MagicMock()
        self.position_store.is_healthy.return_value = True
        self.platforms = MagicMock()
        self.platforms.modify_trade.return_value = modify_returns
        self.platforms.close_trade.return_value = CloseResult(
            success=close_success, order_id="ORD-1", close_price=1.10500,
            lots_closed=0.05, pnl=50.0, platform="mt5",
        )
        self.platforms.get_price.return_value = SimpleNamespace(
            bid=1.10500, ask=1.10510, spread=0.00010, time=datetime.now(timezone.utc),
        )
        self.platforms.get_open_positions_snapshot.return_value = SimpleNamespace(
            positions=[
                SimpleNamespace(
                    order_id="ORD-1", symbol="EURUSD", direction="BUY",
                    lots=0.10, open_price=1.10000, current_price=1.10500,
                    sl=1.09500, tp=1.11000, pnl=50.0, swap=0.0,
                    open_time=datetime.now(timezone.utc), platform="mt5",
                ),
            ],
            confirmed_platforms={"mt5"},
        )
        self.trade_manager = MagicMock()
        self.managed_positions = {}
        self.config = MagicMock()
        self.config.risk.margin_guardian_enabled = False
        self.config.risk.reconcile_max_unconfirmed_cycles = 3
        self.config.risk.tp3_close_ratio = 0.5
        self._current_cycle_id = "cycle-1"
        self._current_setup_id = "setup-1"
        self._add_warning = MagicMock()

    def _update_positions(self):
        from platforms.main_loop import TradingLoop
        return TradingLoop._update_positions(self)

    def _margin_guardian_check(self):
        pass

    def _record_closed_trade(self, *a, **kw):
        pass

    def _check_re_entry(self, *a, **kw):
        pass


# ── FIX 1 — modify_trade return value gated ──────────────────────────

class TestModifyTradeReturnGated:
    """pos.sl/at_breakeven must NOT change when modify_trade returns False."""

    def _run(self, modify_returns, breakeven_active=True, new_sl=1.10000):
        loop = _FakeLoop(modify_returns=modify_returns)
        pos = _make_managed()
        original_sl = pos.sl
        pos.tm_trade_id = "T-1"

        tm_before = _make_tm_trade(
            stop_loss=original_sl, breakeven_active=False,
            partial_closed=False, remaining=0.10,
        )
        tm_after = _make_tm_trade(
            new_sl=new_sl,
            breakeven_active=breakeven_active,
            partial_closed=False, remaining=0.10,
        )
        loop.trade_manager.get_trade.return_value = tm_before
        loop.trade_manager.update.return_value = tm_after
        loop.managed_positions = {"ORD-1": pos}

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._update_positions()

        return loop, pos, original_sl

    def test_modify_failure_keeps_original_sl(self):
        loop, pos, original_sl = self._run(modify_returns=False)
        assert pos.sl == original_sl

    def test_modify_failure_keeps_at_breakeven_false(self):
        loop, pos, _ = self._run(modify_returns=False, breakeven_active=True)
        assert pos.at_breakeven is False

    def test_modify_failure_does_not_persist_breakeven(self):
        loop, pos, _ = self._run(modify_returns=False, breakeven_active=True)
        for call_args in loop.position_store.update_position.call_args_list:
            assert call_args.kwargs.get("at_breakeven") is not True, \
                "update_position must NOT persist at_breakeven=True on broker failure"

    def test_modify_failure_logs_error(self):
        loop, pos, _ = self._run(modify_returns=False)
        _loguru.logger.error.assert_called()
        error_msg = _loguru.logger.error.call_args[0][0]
        assert "MODIFY FAILED" in error_msg

    def test_modify_success_updates_sl(self):
        loop, pos, _ = self._run(modify_returns=True, new_sl=1.10000)
        assert pos.sl == 1.10000

    def test_modify_success_sets_breakeven(self):
        loop, pos, _ = self._run(modify_returns=True, breakeven_active=True, new_sl=1.10000)
        assert pos.at_breakeven is True

    def test_modify_success_persists_breakeven(self):
        loop, pos, _ = self._run(modify_returns=True, breakeven_active=True, new_sl=1.10000)
        loop.position_store.update_position.assert_any_call(
            "ORD-1", sl=1.10000, at_breakeven=True,
        )

    def test_modify_failure_tp2_unchanged(self):
        loop = _FakeLoop(modify_returns=False)
        pos = _make_managed()
        original_tp2 = pos.tp2
        pos.tm_trade_id = "T-1"
        tm_before = _make_tm_trade(tp2=original_tp2)
        tm_after = _make_tm_trade(new_tp2=1.13000)
        loop.trade_manager.get_trade.return_value = tm_before
        loop.trade_manager.update.return_value = tm_after
        loop.managed_positions = {"ORD-1": pos}
        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._update_positions()
        assert pos.tp2 == original_tp2


# ── FIX 2 — partial-close rollback ───────────────────────────────────

class TestPartialCloseRollback:
    """Shadow state must be rolled back when close_trade fails for TP1."""

    def _run(self, close_success):
        loop = _FakeLoop(close_success=close_success)
        pos = _make_managed(lots=0.10)
        pos.tm_trade_id = "T-1"

        tm_before = _make_tm_trade(
            partial_closed=False, remaining=0.10,
            status=TradeStatus.OPEN,
        )
        tm_after = _make_tm_trade(
            partial_closed=True, remaining=0.05,
            status=TradeStatus.TP1_HIT,
        )
        loop.trade_manager.get_trade.return_value = tm_before
        loop.trade_manager.update.return_value = tm_after
        loop.managed_positions = {"ORD-1": pos}

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._update_positions()

        return loop, pos, tm_after

    def test_partial_failure_keeps_tp1_hit_false(self):
        _, pos, _ = self._run(close_success=False)
        assert pos.tp1_hit is False

    def test_partial_failure_keeps_lots_unchanged(self):
        _, pos, _ = self._run(close_success=False)
        assert pos.lots == 0.10

    def test_partial_failure_rolls_back_partial_closed(self):
        _, _, tm = self._run(close_success=False)
        assert tm.partial_closed is False

    def test_partial_failure_rolls_back_remaining_size(self):
        _, _, tm = self._run(close_success=False)
        assert tm.remaining_size_lots == 0.10

    def test_partial_failure_rolls_back_status(self):
        _, _, tm = self._run(close_success=False)
        assert tm.status == TradeStatus.OPEN

    def test_partial_failure_does_not_persist(self):
        loop, _, _ = self._run(close_success=False)
        for call_args in loop.position_store.update_position.call_args_list:
            assert call_args.kwargs.get("tp1_hit") is not True

    def test_partial_failure_logs_error(self):
        self._run(close_success=False)
        _loguru.logger.error.assert_called()
        error_msg = _loguru.logger.error.call_args[0][0]
        assert "PARTIAL CLOSE FAILED" in error_msg

    def test_partial_success_updates_tp1(self):
        _, pos, _ = self._run(close_success=True)
        assert pos.tp1_hit is True

    def test_partial_success_decrements_lots(self):
        _, pos, _ = self._run(close_success=True)
        assert pos.lots == 0.05

    def test_partial_success_persists(self):
        loop, pos, _ = self._run(close_success=True)
        loop.position_store.update_position.assert_any_call(
            "ORD-1", tp1_hit=True, lots=0.05,
        )

    def test_partial_success_does_not_roll_back(self):
        _, _, tm = self._run(close_success=True)
        assert tm.partial_closed is True
        assert tm.remaining_size_lots == 0.05
