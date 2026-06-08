"""
Persistence durability — regression tests.

Defect 1: Verifies that a failed position_store.save_position is surfaced
          loudly (logger.error + PERSISTENCE_DEGRADED event) and the position
          remains in managed_positions (never abandoned or closed).
Defect 2a: Verifies adopted orphans get a finite, positive, correct-side TP2
           instead of the previous 0.0 poison value.
Defect 2b: Verifies _adjust_tp2 returns early when tp2 <= 0, preventing a
           near-zero TP2 from being computed and treated as instantly hit.
"""

import sys
import math
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch, PropertyMock

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


# ═══════════════════════════════════════════════════════════════════════════
# Defect 1 — _save_position_checked surfaces degraded persistence
# ═══════════════════════════════════════════════════════════════════════════

from persistence.domain_events import PERSISTENCE_DEGRADED
from platforms.base_connector import OrderResult
from platforms.trading_loop.positions import ManagedPosition
from platforms.trading_loop.recovery_mixin import RecoveryReconciliationMixin


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
    """Minimal stand-in for TradingLoop with position_store and event store."""

    def __init__(self, store_healthy=True):
        self.position_store = MagicMock()
        self.position_store.is_healthy.return_value = store_healthy
        self.position_store.degraded_reason.return_value = (
            "" if store_healthy else "save ORD-1: disk full"
        )
        self.managed_positions = {}
        self._current_cycle_id = "cycle-1"
        self._current_setup_id = "setup-1"

    def _save_position_checked(self, managed):
        from platforms.main_loop import TradingLoop
        TradingLoop._save_position_checked(self, managed)


class TestSavePositionChecked:

    def test_healthy_store_no_error(self):
        loop = _FakeLoop(store_healthy=True)
        managed = _make_managed()

        with patch("platforms.main_loop.get_event_store") as mock_es:
            loop._save_position_checked(managed)

        loop.position_store.save_position.assert_called_once_with(managed)
        mock_es_inst = mock_es.return_value
        assert not any(
            call.kwargs.get("event_type") == PERSISTENCE_DEGRADED
            for call in getattr(mock_es_inst, "emit", MagicMock()).call_args_list
        )

    def test_degraded_store_emits_error_and_event(self):
        loop = _FakeLoop(store_healthy=False)
        managed = _make_managed()

        mock_store = MagicMock()
        with patch("platforms.main_loop.get_event_store", return_value=mock_store):
            loop._save_position_checked(managed)

        loop.position_store.save_position.assert_called_once_with(managed)
        mock_store.emit.assert_called_once()
        call_kwargs = mock_store.emit.call_args
        assert call_kwargs.kwargs["event_type"] == PERSISTENCE_DEGRADED
        assert call_kwargs.kwargs["severity"] == "ERROR"
        assert call_kwargs.kwargs["symbol"] == "EURUSD"
        assert "ORD-1" in call_kwargs.kwargs["payload"]["order_id"]

    def test_degraded_store_position_retained(self):
        loop = _FakeLoop(store_healthy=False)
        managed = _make_managed()
        loop.managed_positions["ORD-1"] = managed

        with patch("platforms.main_loop.get_event_store", return_value=MagicMock()):
            loop._save_position_checked(managed)

        assert "ORD-1" in loop.managed_positions

    def test_event_emit_failure_does_not_crash(self):
        loop = _FakeLoop(store_healthy=False)
        managed = _make_managed()

        with patch("platforms.main_loop.get_event_store", side_effect=Exception("event store dead")):
            loop._save_position_checked(managed)

        loop.position_store.save_position.assert_called_once_with(managed)


# ═══════════════════════════════════════════════════════════════════════════
# Defect 2a — _reconstructed_adopted_tp2 produces finite, correct-side TP2
# ═══════════════════════════════════════════════════════════════════════════

class TestReconstructedAdoptedTp2:

    def test_long_tp2_above_entry(self):
        tp2 = RecoveryReconciliationMixin._reconstructed_adopted_tp2(
            "BUY", entry_price=1.10000, sl=1.09500, tp1=1.10750,
        )
        assert tp2 > 1.10000
        assert math.isfinite(tp2)
        expected = round(1.10000 + 2.5 * abs(1.10000 - 1.09500), 8)
        assert tp2 == expected

    def test_short_tp2_below_entry(self):
        tp2 = RecoveryReconciliationMixin._reconstructed_adopted_tp2(
            "SELL", entry_price=1.10000, sl=1.10500, tp1=1.09250,
        )
        assert tp2 < 1.10000
        assert math.isfinite(tp2)
        expected = round(1.10000 - 2.5 * abs(1.10000 - 1.10500), 8)
        assert tp2 == expected

    def test_long_direction_token_LONG(self):
        tp2 = RecoveryReconciliationMixin._reconstructed_adopted_tp2(
            "LONG", entry_price=1.10000, sl=1.09500, tp1=1.10750,
        )
        assert tp2 > 1.10000

    def test_short_direction_token_SHORT(self):
        tp2 = RecoveryReconciliationMixin._reconstructed_adopted_tp2(
            "SHORT", entry_price=1.10000, sl=1.10500, tp1=1.09250,
        )
        assert tp2 < 1.10000

    def test_unusable_sl_returns_zero(self):
        tp2 = RecoveryReconciliationMixin._reconstructed_adopted_tp2(
            "BUY", entry_price=1.10000, sl=0.0, tp1=1.10750,
        )
        assert tp2 == 0.0

    def test_sl_equal_entry_returns_zero(self):
        tp2 = RecoveryReconciliationMixin._reconstructed_adopted_tp2(
            "BUY", entry_price=1.10000, sl=1.10000, tp1=1.10750,
        )
        assert tp2 == 0.0

    def test_result_never_zero_when_sl_usable(self):
        tp2 = RecoveryReconciliationMixin._reconstructed_adopted_tp2(
            "SELL", entry_price=1.10000, sl=1.10500, tp1=1.09250,
        )
        assert tp2 != 0.0
        assert tp2 > 0


# ═══════════════════════════════════════════════════════════════════════════
# Defect 2b — _adjust_tp2 guard on tp2 <= 0
# ═══════════════════════════════════════════════════════════════════════════

import pandas as pd
from management.trade_manager import TradeManager, ManagedTrade, TradeStatus


def _make_managed_trade(tp2=0.0, partial_closed=True, direction="BUY"):
    """Minimal ManagedTrade with tp2 poison scenario."""
    return ManagedTrade(
        trade_id="T-1",
        pair="EURUSD",
        direction=direction,
        entry_price=1.10000,
        current_price=1.10500,
        stop_loss=1.09500,
        original_stop_loss=1.09500,
        tp1=1.11000,
        tp2=tp2,
        original_tp2=tp2,
        position_size_lots=0.10,
        remaining_size_lots=0.05,
        status=TradeStatus.OPEN,
        pnl_pips=50.0,
        pnl_dollars=50.0,
        entry_time=datetime.now(timezone.utc),
        tp1_hit_time=datetime.now(timezone.utc),
        close_time=None,
        close_reason=None,
        candles_since_entry=20,
        highest_price_since_entry=1.10600,
        lowest_price_since_entry=1.09800,
        trailing_stop=1.09800,
        breakeven_active=True,
        partial_closed=partial_closed,
        re_entry_eligible=False,
        score=85,
        pip_size=0.00010,
        pip_value_per_lot=10.0,
    )


def _make_m5_df(n=20):
    """Minimal M5 DataFrame with enough rows."""
    data = {
        "open": [1.10000 + i * 0.00010 for i in range(n)],
        "high": [1.10050 + i * 0.00010 for i in range(n)],
        "low": [1.09950 + i * 0.00010 for i in range(n)],
        "close": [1.10020 + i * 0.00010 for i in range(n)],
        "volume": [100] * n,
    }
    return pd.DataFrame(data)


class TestAdjustTp2Guard:

    def test_tp2_zero_returns_early(self):
        trade = _make_managed_trade(tp2=0.0, partial_closed=True)
        tm = TradeManager.__new__(TradeManager)
        original_tp2 = trade.tp2
        tm._adjust_tp2(trade, _make_m5_df())
        assert trade.tp2 == original_tp2

    def test_tp2_negative_returns_early(self):
        trade = _make_managed_trade(tp2=-1.0, partial_closed=True)
        tm = TradeManager.__new__(TradeManager)
        tm._adjust_tp2(trade, _make_m5_df())
        assert trade.tp2 == -1.0

    def test_tp2_none_returns_early(self):
        trade = _make_managed_trade(tp2=0.0, partial_closed=True)
        trade.tp2 = None
        tm = TradeManager.__new__(TradeManager)
        tm._adjust_tp2(trade, _make_m5_df())
        assert trade.tp2 is None

    def test_tp2_positive_proceeds_normally(self):
        trade = _make_managed_trade(tp2=1.12000, partial_closed=True)
        tm = TradeManager.__new__(TradeManager)
        tm._is_long = lambda d: d.upper() in ("BUY", "LONG")
        tm._adjust_tp2(trade, _make_m5_df())
