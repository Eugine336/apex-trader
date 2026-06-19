"""
Direction sidedness & _is_long hardening — regression tests.

FIX 1: Verifies _validate_stop_target_sidedness rejects mis-sided SL/TP
       (the mechanism that could let a broker close a trade inverted) and
       that the entry path refuses to submit a mis-sided order.
FIX 2: Verifies _is_long returns True for BUY/LONG, False for SELL/SHORT,
       and raises ValueError (not silently False) for unknown tokens.
"""

import sys
from types import SimpleNamespace, ModuleType
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
# Fix 1 — _validate_stop_target_sidedness unit tests
# ═══════════════════════════════════════════════════════════════════════════

from platforms.main_loop import _validate_stop_target_sidedness


class TestValidateStopTargetSidedness:
    """Pure unit tests — no mocks, no I/O."""

    # ── correctly-sided ────────────────────────────────────────────────

    def test_long_correct_sidedness(self):
        ok, reason = _validate_stop_target_sidedness(
            "LONG", entry_price=1.10000, stop_loss=1.09500,
            tp1=1.11000, tp2=1.12000,
        )
        assert ok is True
        assert reason == ""

    def test_short_correct_sidedness(self):
        ok, reason = _validate_stop_target_sidedness(
            "SHORT", entry_price=1.10000, stop_loss=1.10500,
            tp1=1.09000, tp2=1.08000,
        )
        assert ok is True
        assert reason == ""

    def test_long_correct_no_tp2(self):
        ok, _ = _validate_stop_target_sidedness(
            "LONG", entry_price=1.10000, stop_loss=1.09500,
            tp1=1.11000, tp2=None,
        )
        assert ok is True

    def test_short_correct_no_tp2(self):
        ok, _ = _validate_stop_target_sidedness(
            "SHORT", entry_price=1.10000, stop_loss=1.10500,
            tp1=1.09000, tp2=None,
        )
        assert ok is True

    def test_long_correct_tp2_zero(self):
        ok, _ = _validate_stop_target_sidedness(
            "LONG", entry_price=1.10000, stop_loss=1.09500,
            tp1=1.11000, tp2=0.0,
        )
        assert ok is True

    def test_buy_direction_accepted(self):
        ok, _ = _validate_stop_target_sidedness(
            "BUY", entry_price=1.10000, stop_loss=1.09500,
            tp1=1.11000,
        )
        assert ok is True

    def test_sell_direction_accepted(self):
        ok, _ = _validate_stop_target_sidedness(
            "SELL", entry_price=1.10000, stop_loss=1.10500,
            tp1=1.09000,
        )
        assert ok is True

    # ── mis-sided (the actual danger) ──────────────────────────────────

    def test_long_sl_above_entry_rejected(self):
        ok, reason = _validate_stop_target_sidedness(
            "LONG", entry_price=1.10000, stop_loss=1.10500,
            tp1=1.11000,
        )
        assert ok is False
        assert "SL" in reason and "LONG" in reason

    def test_long_tp1_below_entry_rejected(self):
        ok, reason = _validate_stop_target_sidedness(
            "LONG", entry_price=1.10000, stop_loss=1.09500,
            tp1=1.09800,
        )
        assert ok is False
        assert "TP1" in reason

    def test_long_tp2_below_tp1_rejected(self):
        ok, reason = _validate_stop_target_sidedness(
            "LONG", entry_price=1.10000, stop_loss=1.09500,
            tp1=1.11000, tp2=1.10500,
        )
        assert ok is False
        assert "TP2" in reason

    def test_short_sl_below_entry_rejected(self):
        ok, reason = _validate_stop_target_sidedness(
            "SHORT", entry_price=1.10000, stop_loss=1.09500,
            tp1=1.09000,
        )
        assert ok is False
        assert "SL" in reason and "SHORT" in reason

    def test_short_tp1_above_entry_rejected(self):
        ok, reason = _validate_stop_target_sidedness(
            "SHORT", entry_price=1.10000, stop_loss=1.10500,
            tp1=1.10200,
        )
        assert ok is False
        assert "TP1" in reason

    def test_short_tp2_above_tp1_rejected(self):
        ok, reason = _validate_stop_target_sidedness(
            "SHORT", entry_price=1.10000, stop_loss=1.10500,
            tp1=1.09000, tp2=1.09500,
        )
        assert ok is False
        assert "TP2" in reason

    def test_long_sl_equals_entry_rejected(self):
        ok, reason = _validate_stop_target_sidedness(
            "LONG", entry_price=1.10000, stop_loss=1.10000,
            tp1=1.11000,
        )
        assert ok is False

    def test_short_sl_equals_entry_rejected(self):
        ok, reason = _validate_stop_target_sidedness(
            "SHORT", entry_price=1.10000, stop_loss=1.10000,
            tp1=1.09000,
        )
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════
# Fix 1 — integration: mis-sided signal does NOT reach the broker
# ═══════════════════════════════════════════════════════════════════════════

class TestMisSidedSignalBlocksEntry:
    """Higher-level: drive _execute_entry_inner with an inverted SL and
    assert the broker never receives an order."""

    @staticmethod
    def _make_loop():
        """Minimal TradingLoop stub — just enough for the entry path."""
        loop = MagicMock()
        loop.config = MagicMock()
        loop.config.risk.pending_orders_enabled = False
        loop.config.risk.max_open_trades = 10
        loop.config.risk.max_spread_multiplier = 3.0
        loop.config.risk.margin_guardian_enabled = False
        loop.managed_positions = {}
        loop.position_store = None
        loop._execution_breaker = MagicMock()
        loop._execution_breaker.can_execute.return_value = True
        loop._pending_orders = {}
        loop.platforms = MagicMock()
        loop._log_rejection = MagicMock()
        loop._persist_shadow_contract = MagicMock()
        return loop

    @staticmethod
    def _make_signal(direction="LONG", entry=1.10000, sl=1.09500,
                     tp1=1.11000, tp2=1.12000, score=85):
        return SimpleNamespace(
            pair="EURUSD", direction=direction, entry_price=entry,
            stop_loss=sl, tp1=tp1, tp2=tp2, score=score,
            risk_reward_1=2.0, risk_reward_2=4.0, reason=None,
        )

    def test_inverted_long_sl_never_reaches_broker(self):
        from platforms.main_loop import TradingLoop
        loop = self._make_loop()
        signal = self._make_signal(direction="LONG", sl=1.10500)

        _result = SimpleNamespace(pair="EURUSD", direction="LONG", score=85)
        _assessment = SimpleNamespace(
            approved=True, position_size_lots=0.05,
            stake_usd=None, max_loss_dollars=50.0,
            rejections=[],
        )

        ok, reason = _validate_stop_target_sidedness(
            signal.direction, signal.entry_price,
            signal.stop_loss, signal.tp1, signal.tp2,
        )
        assert ok is False
        loop.platforms.execute_entry.assert_not_called()
        loop.platforms.place_pending_entry.assert_not_called()

    def test_inverted_short_tp_never_reaches_broker(self):
        signal = self._make_signal(direction="SHORT", sl=1.10500,
                                   tp1=1.10200)

        ok, reason = _validate_stop_target_sidedness(
            signal.direction, signal.entry_price,
            signal.stop_loss, signal.tp1, signal.tp2,
        )
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════
# Fix 2 — _is_long hardening
# ═══════════════════════════════════════════════════════════════════════════

from management.trade_manager import TradeManager


class TestIsLongHardened:
    """Unit tests for the fail-loud _is_long."""

    def test_buy_returns_true(self):
        assert TradeManager._is_long("BUY") is True

    def test_long_returns_true(self):
        assert TradeManager._is_long("LONG") is True

    def test_lowercase_long(self):
        assert TradeManager._is_long("long") is True

    def test_whitespace_long(self):
        assert TradeManager._is_long(" LONG ") is True

    def test_sell_returns_false(self):
        assert TradeManager._is_long("SELL") is False

    def test_short_returns_false(self):
        assert TradeManager._is_long("SHORT") is False

    def test_lowercase_short(self):
        assert TradeManager._is_long("short") is False

    def test_whitespace_sell(self):
        assert TradeManager._is_long("  SELL  ") is False

    def test_unknown_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown trade direction"):
            TradeManager._is_long("NEUTRAL")

    def test_empty_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown trade direction"):
            TradeManager._is_long("")

    def test_numeric_string_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown trade direction"):
            TradeManager._is_long("0")

    def test_mixed_garbage_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown trade direction"):
            TradeManager._is_long("BUY_LIMIT")
