"""Tests for scale-in idempotency, correlation guard fix, and TP3 rollback.

Covers:
1. Both scale-in paths now route through _submit_scale_in with idempotency keys.
2. _check_scale_in correlation guard properly unpacks tuple (was dead code).
3. _check_scale_in_on_scan profit_r uses original_stop_loss (was inflated).
4. TP3 partial-close failure rolls back shadow state for retry.
"""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

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
from platforms.order_idempotency import generate_idempotency_key
from management.trade_manager import ManagedTrade, TradeStatus


def _make_managed(order_id="ORD-1", symbol="EURUSD", direction="BUY",
                  sl=1.09500, lots=0.10, score=85):
    order = OrderResult(
        success=True, order_id=order_id, fill_price=1.10000,
        requested_price=1.10000, slippage_pips=0.0, lots=lots,
        symbol=symbol, direction=direction, sl=sl, tp=1.11000, platform="mt5",
    )
    return ManagedPosition(
        order=order, tp1=1.10500, tp2=1.12000, score=score,
        regime="BULLISH", session="LONDON",
    )


def _make_tm_trade(
    trade_id="T-1", direction="BUY", stop_loss=1.09500, original_stop_loss=1.09500,
    tp2=1.12000, breakeven_active=True, partial_closed=True,
    remaining=0.05, status=TradeStatus.TRAILING, current_price=1.11500,
    tp3_hit=False,
):
    t = ManagedTrade(
        trade_id=trade_id, pair="EURUSD", direction=direction,
        entry_price=1.10000, current_price=current_price,
        stop_loss=stop_loss, original_stop_loss=original_stop_loss,
        tp1=1.10500, tp2=tp2, original_tp2=tp2,
        position_size_lots=0.10, remaining_size_lots=remaining,
        status=status, pnl_pips=150.0, pnl_dollars=150.0,
        entry_time=datetime.now(timezone.utc),
        tp1_hit_time=None, close_time=None, close_reason=None,
        candles_since_entry=40, highest_price_since_entry=1.11600,
        lowest_price_since_entry=1.09800, trailing_stop=1.10200,
        breakeven_active=breakeven_active, partial_closed=partial_closed,
        re_entry_eligible=False, score=85, pip_size=0.00010,
        pip_value_per_lot=10.0,
    )
    t.tp3_hit = tp3_hit
    return t


_MT5_CTX = SimpleNamespace(
    supports_partial_close=True, supports_modify=True, uses_stake=False,
)


class _FakeScaleInLoop:
    """Minimal stand-in wired to _submit_scale_in and _check_scale_in."""

    def __init__(self, order_success=True):
        self.position_store = MagicMock()
        self.platforms = MagicMock()
        self.platforms.execute_entry.return_value = OrderResult(
            success=order_success, order_id="FILL-1", fill_price=1.11500,
            requested_price=1.11500, slippage_pips=0.0, lots=0.01,
            symbol="EURUSD", direction="BUY", sl=1.09500, tp=1.12000,
            platform="mt5",
        )
        self.platforms.get_price.return_value = SimpleNamespace(
            bid=1.11500, ask=1.11510, spread=0.00010,
            time=datetime.now(timezone.utc),
        )
        self.trade_manager = MagicMock()
        self.managed_positions = {}
        self.config = MagicMock()
        self.config.risk.scale_in_enabled = True
        self.config.risk.scale_in_max_adds = 3
        self.config.risk.scale_in_min_profit_r = 1.5
        self.config.risk.scale_in_add_ratio = 0.5
        self.config.risk.max_open_trades = 10
        self.config.risk.margin_guardian_enabled = False
        self.config.scoring.min_entry_score = 70
        self.risk_engine = MagicMock()
        self.risk_engine.drawdown_guard.risk_map = {"normal": 0.01}
        self.risk_engine.drawdown_guard.mode = "normal"
        self.correlation = MagicMock()
        self.correlation.can_open_trade.return_value = (True, "ok")
        self._portfolio_risk_sm = None

    def _submit_scale_in(self, pos, add_lots, new_sl, new_tp, score_tag):
        from platforms.main_loop import TradingLoop
        return TradingLoop._submit_scale_in(self, pos, add_lots, new_sl, new_tp, score_tag)

    def _check_scale_in(self):
        from platforms.main_loop import TradingLoop
        return TradingLoop._check_scale_in(self)

    def _check_scale_in_on_scan(self, oid, pos, scan_result):
        from platforms.main_loop import TradingLoop
        return TradingLoop._check_scale_in_on_scan(self, oid, pos, scan_result)

    def _get_margin_level(self):
        return 500.0


# ── FIX 1: Idempotency via _submit_scale_in ──────────────────────────

class TestSubmitScaleInIdempotency:

    def test_passes_idempotency_key_to_broker(self):
        loop = _FakeScaleInLoop(order_success=True)
        pos = _make_managed()
        pos.tm_trade_id = "T-1"

        loop._submit_scale_in(pos, 0.05, 1.09500, 1.12000, "SCALEIN")

        call_kwargs = loop.platforms.execute_entry.call_args
        assert call_kwargs.kwargs.get("idempotency_key") or call_kwargs[1].get("idempotency_key"), \
            "execute_entry must receive a non-empty idempotency_key"

    def test_same_bucket_produces_same_key(self):
        keys = []
        for _ in range(2):
            k = generate_idempotency_key("EURUSD", "BUY", 0.05)
            keys.append(k)
        assert keys[0] == keys[1]

    def test_success_resolves_in_flight_and_increments(self):
        loop = _FakeScaleInLoop(order_success=True)
        pos = _make_managed()
        pos.scale_in_count = 0

        result = loop._submit_scale_in(pos, 0.05, 1.09500, 1.12000, "SCALEIN")

        assert result is True
        assert pos.scale_in_count == 1
        loop.position_store.resolve_in_flight.assert_called_once()

    def test_failure_cancels_in_flight_no_increment(self):
        loop = _FakeScaleInLoop(order_success=False)
        pos = _make_managed()
        pos.scale_in_count = 0

        result = loop._submit_scale_in(pos, 0.05, 1.09500, 1.12000, "SCALEIN")

        assert result is False
        assert pos.scale_in_count == 0
        loop.position_store.cancel_in_flight.assert_called_once()

    def test_records_in_flight_before_submit(self):
        loop = _FakeScaleInLoop(order_success=True)
        pos = _make_managed()

        call_order = []
        loop.position_store.record_in_flight.side_effect = lambda *a, **kw: call_order.append("record")
        orig_exec = loop.platforms.execute_entry
        loop.platforms.execute_entry = lambda *a, **kw: (call_order.append("exec"), orig_exec(*a, **kw))[1]

        loop._submit_scale_in(pos, 0.05, 1.09500, 1.12000, "SCALEIN")

        assert call_order.index("record") < call_order.index("exec")


class TestCheckScaleInUsesHelper:

    def test_check_scale_in_passes_idempotency_key(self):
        loop = _FakeScaleInLoop(order_success=True)
        pos = _make_managed()
        pos.scale_in_count = 0
        pos.tm_trade_id = "T-1"
        tm = _make_tm_trade(
            stop_loss=1.10000, original_stop_loss=1.09500,
            breakeven_active=True, partial_closed=True,
            current_price=1.11500,
        )
        loop.trade_manager.get_trade.return_value = tm
        loop.trade_manager._is_long.return_value = True
        loop.managed_positions = {"ORD-1": pos}

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._check_scale_in()

        assert loop.platforms.execute_entry.called
        call_kwargs = loop.platforms.execute_entry.call_args
        assert call_kwargs.kwargs.get("idempotency_key"), "must have idempotency_key"

    def test_scan_path_passes_idempotency_key(self):
        loop = _FakeScaleInLoop(order_success=True)
        pos = _make_managed()
        pos.scale_in_count = 0
        pos.tm_trade_id = "T-1"
        tm = _make_tm_trade(
            stop_loss=1.10000, original_stop_loss=1.09500,
            breakeven_active=True, partial_closed=True,
        )
        loop.trade_manager.get_trade.return_value = tm
        loop.trade_manager._is_long.return_value = True
        scan_result = SimpleNamespace(direction="LONG", score=80)

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._check_scale_in_on_scan("ORD-1", pos, scan_result)

        assert loop.platforms.execute_entry.called
        call_kwargs = loop.platforms.execute_entry.call_args
        assert call_kwargs.kwargs.get("idempotency_key"), "must have idempotency_key"


# ── FIX 2: Correlation guard properly unpacks ─────────────────────────

class TestCorrelationGuardUnpacked:

    def test_correlation_false_blocks_scale_in(self):
        loop = _FakeScaleInLoop(order_success=True)
        pos = _make_managed()
        pos.scale_in_count = 0
        pos.tm_trade_id = "T-1"
        tm = _make_tm_trade(
            stop_loss=1.10000, original_stop_loss=1.09500,
            breakeven_active=True, partial_closed=True,
            current_price=1.11500,
        )
        loop.trade_manager.get_trade.return_value = tm
        loop.trade_manager._is_long.return_value = True
        loop.managed_positions = {"ORD-1": pos}
        loop.correlation.can_open_trade.return_value = (False, "Too many correlated")

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._check_scale_in()

        assert not loop.platforms.execute_entry.called, \
            "correlation returning (False, reason) must block scale-in"

    def test_correlation_true_allows_scale_in(self):
        loop = _FakeScaleInLoop(order_success=True)
        pos = _make_managed()
        pos.scale_in_count = 0
        pos.tm_trade_id = "T-1"
        tm = _make_tm_trade(
            stop_loss=1.10000, original_stop_loss=1.09500,
            breakeven_active=True, partial_closed=True,
            current_price=1.11500,
        )
        loop.trade_manager.get_trade.return_value = tm
        loop.trade_manager._is_long.return_value = True
        loop.managed_positions = {"ORD-1": pos}
        loop.correlation.can_open_trade.return_value = (True, "ok")

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._check_scale_in()

        assert loop.platforms.execute_entry.called


# ── FIX 3: profit_r uses original_stop_loss ───────────────────────────

class TestScanPathUsesOriginalStop:

    def test_trailed_stop_does_not_inflate_profit_r(self):
        loop = _FakeScaleInLoop(order_success=True)
        pos = _make_managed()
        pos.scale_in_count = 0
        pos.tm_trade_id = "T-1"
        tm = _make_tm_trade(
            stop_loss=1.10050,
            original_stop_loss=1.09500,
            breakeven_active=True, partial_closed=True,
        )
        loop.trade_manager.get_trade.return_value = tm
        loop.trade_manager._is_long.return_value = True
        loop.config.risk.scale_in_min_profit_r = 10.0
        loop.platforms.get_price.return_value = SimpleNamespace(
            bid=1.11000, ask=1.11010, spread=0.00010,
            time=datetime.now(timezone.utc),
        )
        scan_result = SimpleNamespace(direction="LONG", score=80)

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._check_scale_in_on_scan("ORD-1", pos, scan_result)

        assert not loop.platforms.execute_entry.called, \
            "profit_r based on original_stop gives ~2R, threshold 10R should block"


# ── FIX 4: TP3 partial-close failure rollback ─────────────────────────

class _FakeUpdateLoop:
    def __init__(self, close_success=True):
        self.position_store = MagicMock()
        self.position_store.is_healthy.return_value = True
        self.platforms = MagicMock()
        self.platforms.modify_trade.return_value = True
        self.platforms.close_trade.return_value = CloseResult(
            success=close_success, order_id="ORD-1", close_price=1.11500,
            lots_closed=0.05, pnl=50.0, platform="mt5",
        )
        self.platforms.get_price.return_value = SimpleNamespace(
            bid=1.11500, ask=1.11510, spread=0.00010,
            time=datetime.now(timezone.utc),
        )
        self.platforms.get_open_positions_snapshot.return_value = SimpleNamespace(
            positions=[
                SimpleNamespace(
                    order_id="ORD-1", symbol="EURUSD", direction="BUY",
                    lots=0.10, open_price=1.10000, current_price=1.11500,
                    sl=1.09500, tp=1.12000, pnl=50.0, swap=0.0,
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


class TestTP3RollbackOnFailure:

    def test_tp3_failure_rolls_back_shadow(self):
        loop = _FakeUpdateLoop(close_success=False)
        pos = _make_managed()
        pos.tm_trade_id = "T-1"

        tm_before = _make_tm_trade(
            breakeven_active=True, partial_closed=True,
            remaining=0.05, tp3_hit=False,
        )
        tm_after = _make_tm_trade(
            breakeven_active=True, partial_closed=True,
            remaining=0.025, tp3_hit=True,
        )
        loop.trade_manager.get_trade.return_value = tm_before
        loop.trade_manager.update.return_value = tm_after
        loop.managed_positions = {"ORD-1": pos}

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._update_positions()

        assert tm_after.tp3_hit is False, "tp3_hit must be rolled back on broker failure"
        assert tm_after.remaining_size_lots == 0.05, "remaining_size_lots must be restored"

    def test_tp3_success_keeps_shadow(self):
        loop = _FakeUpdateLoop(close_success=True)
        pos = _make_managed()
        pos.tm_trade_id = "T-1"

        tm_before = _make_tm_trade(
            breakeven_active=True, partial_closed=True,
            remaining=0.05, tp3_hit=False,
        )
        tm_after = _make_tm_trade(
            breakeven_active=True, partial_closed=True,
            remaining=0.025, tp3_hit=True,
        )
        loop.trade_manager.get_trade.return_value = tm_before
        loop.trade_manager.update.return_value = tm_after
        loop.managed_positions = {"ORD-1": pos}

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._update_positions()

        assert tm_after.tp3_hit is True
        assert pos.lots == round(0.10 - 0.05, 2)

    def test_tp3_failure_logs_error(self):
        loop = _FakeUpdateLoop(close_success=False)
        pos = _make_managed()
        pos.tm_trade_id = "T-1"

        tm_before = _make_tm_trade(
            breakeven_active=True, partial_closed=True,
            remaining=0.05, tp3_hit=False,
        )
        tm_after = _make_tm_trade(
            breakeven_active=True, partial_closed=True,
            remaining=0.025, tp3_hit=True,
        )
        loop.trade_manager.get_trade.return_value = tm_before
        loop.trade_manager.update.return_value = tm_after
        loop.managed_positions = {"ORD-1": pos}

        with patch("platforms.main_loop.build_context_for_symbol", return_value=_MT5_CTX):
            loop._update_positions()

        _loguru.logger.error.assert_called()
