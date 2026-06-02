"""
Tests for Phase 4 — Trade Management Engine.
Covers: open, stop loss, TP1 partial close, breakeven, trailing,
TP2 full close, time-based exit, re-entry eligibility, partial close math.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

from management.trade_manager import (
    EntrySignal,
    ManagedTrade,
    TradeManager,
    TradeStatus,
)
from management.partial_close import PartialCloseCalculator
from management.trailing_stop import StructureTrailingStop


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _signal(
    pair: str = "EURUSD",
    direction: str = "LONG",
    entry: float = 1.10000,
    sl: float = 1.09800,
    tp1: float = 1.10200,
    tp2: float = 1.10400,
    lots: float = 0.50,
    score: int = 92,
) -> EntrySignal:
    return EntrySignal(
        pair=pair,
        direction=direction,
        entry_price=entry,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        risk_reward_1=1.0,
        risk_reward_2=2.0,
        position_size_lots=lots,
        score=score,
        confluences=["structure", "fvg", "session"],
        entry_zone="FVG 1.10000-1.10020",
    )


def _m5_df(rows: int = 30) -> pd.DataFrame:
    np.random.seed(42)
    closes = np.cumsum(np.random.randn(rows) * 0.0005) + 1.10
    return pd.DataFrame({
        "time": pd.date_range("2025-01-01", periods=rows, freq="5min"),
        "open": closes - 0.0002,
        "high": closes + 0.0010,
        "low": closes - 0.0010,
        "close": closes,
        "tick_volume": np.random.randint(50, 300, rows),
    })


# ===================================================================
# TradeManager — open_trade
# ===================================================================

class TestOpenTrade:
    def test_creates_valid_managed_trade(self):
        tm = TradeManager()
        sig = _signal()
        trade = tm.open_trade(sig)

        assert isinstance(trade, ManagedTrade)
        assert trade.pair == "EURUSD"
        assert trade.direction == "LONG"
        assert trade.entry_price == 1.10000
        assert trade.status == TradeStatus.OPEN
        assert trade.remaining_size_lots == 0.50
        assert trade.partial_closed is False
        assert trade.breakeven_active is False
        assert trade.pnl_pips == 0.0

    def test_trade_stored_internally(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        assert tm.get_trade(trade.trade_id) is trade
        assert trade in tm.get_open_trades()

    def test_pip_size_from_registry(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(pair="USDJPY", sl=149.500, tp1=150.500, tp2=151.0))
        assert trade.pip_size == 0.01


# ===================================================================
# Stop Loss
# ===================================================================

class TestStopLoss:
    def test_long_stop_loss_hit(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        tm.update(trade, 1.09790)
        assert trade.status == TradeStatus.STOPPED
        assert trade.close_reason == "Stop loss hit"

    def test_short_stop_loss_hit(self):
        tm = TradeManager()
        trade = tm.open_trade(
            _signal(direction="SHORT", sl=1.10200, tp1=1.09800, tp2=1.09600)
        )
        tm.update(trade, 1.10210)
        assert trade.status == TradeStatus.STOPPED

    def test_price_above_sl_no_trigger(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        tm.update(trade, 1.09900)
        assert trade.status == TradeStatus.OPEN


# ===================================================================
# TP1 — Partial Close
# ===================================================================

class TestTP1:
    def test_tp1_triggers_partial_close(self):
        tm = TradeManager(partial_close_ratio=0.5)
        trade = tm.open_trade(_signal(lots=1.00))
        tm.update(trade, 1.10200)
        assert trade.partial_closed is True
        assert trade.status in {TradeStatus.TP1_HIT, TradeStatus.BREAKEVEN}
        assert trade.remaining_size_lots == 0.50
        assert trade.tp1_hit_time is not None

    def test_tp1_only_fires_once(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(lots=1.00))
        tm.update(trade, 1.10200)
        old_remaining = trade.remaining_size_lots
        tm.update(trade, 1.10250)
        assert trade.remaining_size_lots == old_remaining

    def test_short_tp1_hit(self):
        tm = TradeManager()
        trade = tm.open_trade(
            _signal(direction="SHORT", entry=1.10000, sl=1.10200, tp1=1.09800, tp2=1.09600)
        )
        tm.update(trade, 1.09790)
        assert trade.partial_closed is True


# ===================================================================
# Breakeven
# ===================================================================

class TestBreakeven:
    def test_breakeven_set_after_tp1(self):
        tm = TradeManager(breakeven_buffer_pips=2.0)
        trade = tm.open_trade(_signal())
        tm.update(trade, 1.10200)
        assert trade.breakeven_active is True
        expected_be = 1.10000 + 2.0 * 0.0001
        assert abs(trade.stop_loss - expected_be) < 1e-6

    def test_breakeven_short(self):
        tm = TradeManager(breakeven_buffer_pips=2.0)
        trade = tm.open_trade(
            _signal(direction="SHORT", sl=1.10200, tp1=1.09800, tp2=1.09600)
        )
        tm.update(trade, 1.09790)
        expected_be = 1.10000 - 2.0 * 0.0001
        assert abs(trade.stop_loss - expected_be) < 1e-6

    def test_stopped_at_breakeven_sets_re_entry(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        tm.update(trade, 1.10200)
        assert trade.breakeven_active
        tm.update(trade, trade.stop_loss - 0.0001)
        assert trade.status == TradeStatus.STOPPED
        assert trade.re_entry_eligible is True
        assert trade.close_reason == "Stopped at breakeven"


# ===================================================================
# Trailing Stop
# ===================================================================

class TestTrailingStop:
    def test_trailing_only_after_breakeven(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        df = _m5_df()
        tm.update(trade, 1.10050, df)
        assert trade.trailing_stop is None

    def test_trailing_can_move_sl(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        tm.update(trade, 1.10200)
        old_sl = trade.stop_loss
        df = _m5_df()
        tm.update(trade, 1.10300, df)
        if trade.trailing_stop is not None:
            assert trade.trailing_stop >= old_sl


# ===================================================================
# TP2 — Full Close
# ===================================================================

class TestTP2:
    def test_tp2_full_close(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        tm.update(trade, 1.10200)
        tm.update(trade, 1.10400)
        assert trade.status == TradeStatus.CLOSED
        assert trade.close_reason == "TP2 hit"

    def test_short_tp2(self):
        tm = TradeManager()
        trade = tm.open_trade(
            _signal(direction="SHORT", sl=1.10200, tp1=1.09800, tp2=1.09600)
        )
        tm.update(trade, 1.09790)
        tm.update(trade, 1.09590)
        assert trade.status == TradeStatus.CLOSED


# ===================================================================
# Time-Based Exit (stall detection)
# ===================================================================

class TestTimeExit:
    def test_stall_exit(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        trade.entry_time = datetime.now(timezone.utc) - timedelta(minutes=80)
        tm.update(trade, 1.10001)
        assert trade.status == TradeStatus.TIME_EXIT
        assert "stall" in trade.close_reason.lower()

    def test_no_stall_if_profit(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        trade.entry_time = datetime.now(timezone.utc) - timedelta(minutes=80)
        tm.update(trade, 1.10100)
        assert trade.status != TradeStatus.TIME_EXIT

    def test_no_stall_if_tp1_hit(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        tm.update(trade, 1.10200)
        assert trade.partial_closed
        trade.entry_time = datetime.now(timezone.utc) - timedelta(minutes=80)
        tm.update(trade, 1.10001)
        assert trade.status != TradeStatus.TIME_EXIT


# ===================================================================
# PnL Calculation
# ===================================================================

class TestPnL:
    def test_long_positive_pnl(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        tm.update(trade, 1.10100)
        assert trade.pnl_pips > 0

    def test_long_negative_pnl(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        tm.update(trade, 1.09900)
        assert trade.pnl_pips < 0

    def test_short_positive_pnl(self):
        tm = TradeManager()
        trade = tm.open_trade(
            _signal(direction="SHORT", sl=1.10200, tp1=1.09800, tp2=1.09600)
        )
        tm.update(trade, 1.09900)
        assert trade.pnl_pips > 0


# ===================================================================
# PartialCloseCalculator
# ===================================================================

class TestPartialClose:
    def test_50_50_split(self):
        lots_close, lots_remain = PartialCloseCalculator.calculate_partial(1.00, 0.5)
        assert lots_close == 0.50
        assert lots_remain == 0.50

    def test_custom_ratio(self):
        lots_close, lots_remain = PartialCloseCalculator.calculate_partial(1.00, 0.7)
        assert lots_close == 0.70
        assert lots_remain == 0.30

    def test_rounds_to_two_decimals(self):
        lots_close, lots_remain = PartialCloseCalculator.calculate_partial(0.33, 0.5)
        assert lots_close == round(0.33 * 0.5, 2)

    def test_pnl_at_partial(self):
        pnl = PartialCloseCalculator.calculate_pnl_at_partial(
            entry_price=1.10000, tp1_price=1.10100,
            lots_closed=0.50, pip_size=0.0001, pip_value=10.0,
        )
        assert pnl == 50.0

    def test_breakeven_long(self):
        be = PartialCloseCalculator.calculate_breakeven_level(1.10000, "LONG", 2.0, 0.0001)
        assert abs(be - 1.10020) < 1e-6

    def test_breakeven_short(self):
        be = PartialCloseCalculator.calculate_breakeven_level(1.10000, "SHORT", 2.0, 0.0001)
        assert abs(be - 1.09980) < 1e-6


# ===================================================================
# StructureTrailingStop
# ===================================================================

class TestStructureTrailingStop:
    def test_should_trail_requires_breakeven(self):
        ts = StructureTrailingStop()
        assert ts.should_trail(15.0, breakeven_active=True) is True
        assert ts.should_trail(15.0, breakeven_active=False) is False

    def test_should_trail_requires_min_pips(self):
        ts = StructureTrailingStop(min_pips_to_trail=10.0)
        assert ts.should_trail(5.0, breakeven_active=True) is False

    def test_returns_none_when_no_improvement(self):
        ts = StructureTrailingStop()
        df = _m5_df()
        result = ts.calculate_trail("LONG", 999.0, df, 0.0001)
        assert result is None


# ===================================================================
# Trade Summary
# ===================================================================

class TestTradeSummary:
    def test_summary_dict(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        summary = tm.get_trade_summary(trade)
        assert summary["pair"] == "EURUSD"
        assert summary["status"] == "OPEN"
        assert "pnl_pips" in summary
        assert "pnl_dollars" in summary


# ===================================================================
# Terminal trades are not updated
# ===================================================================

class TestTerminalTrades:
    def test_closed_trade_not_updated(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        tm.close_trade(trade, "manual", 1.10100)
        old_pnl = trade.pnl_pips
        tm.update(trade, 1.20000)
        assert trade.pnl_pips == old_pnl
