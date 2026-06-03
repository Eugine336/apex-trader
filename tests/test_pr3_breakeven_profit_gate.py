"""
Tests for PR-3: Breakeven profit gate.
Verifies that _activate_breakeven and the breakeven path in update()
require positive P&L excursion (pnl_pips > 0) before engaging.
A losing position must never be moved to breakeven — and therefore
trailing (which is downstream of breakeven_active) must also not engage.
"""

from management.trade_manager import (
    EntrySignal,
    TradeManager,
    TradeStatus,
)


def _signal(
    pair="EURUSD",
    direction="LONG",
    entry=1.10000,
    sl=1.09800,
    tp1=1.10200,
    tp2=1.10400,
    lots=0.50,
    score=92,
):
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
        confluences=["structure"],
        entry_zone="FVG",
    )


class TestBreakevenProfitGate:
    """Case 1: LONG, partial_closed=True, pnl_pips < 0 — breakeven must NOT activate."""

    def test_long_losing_no_breakeven(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(entry=1.10000, sl=1.09800, tp1=1.10200))
        trade.partial_closed = True
        trade.breakeven_active = False
        trade.current_price = 1.09500
        trade.pnl_pips = -50.0
        tm._activate_breakeven(trade)
        assert trade.breakeven_active is False
        assert trade.status != TradeStatus.BREAKEVEN

    def test_long_losing_update_no_breakeven(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(entry=1.10000, sl=1.09800, tp1=1.10200))
        trade.partial_closed = True
        trade.breakeven_active = False
        tm.update(trade, current_price=1.09500)
        assert trade.breakeven_active is False
        assert trade.status != TradeStatus.BREAKEVEN


class TestBreakevenProfitGateRegression:
    """Case 2: LONG, partial_closed=True, pnl_pips > 0 — breakeven activates normally."""

    def test_long_winning_breakeven_activates(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(entry=1.10000, sl=1.09800, tp1=1.10200))
        trade.partial_closed = True
        trade.breakeven_active = False
        trade.current_price = 1.10300
        trade.pnl_pips = 30.0
        tm._activate_breakeven(trade)
        assert trade.breakeven_active is True
        assert trade.status == TradeStatus.BREAKEVEN

    def test_long_winning_update_breakeven_activates(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(entry=1.10000, sl=1.09800, tp1=1.10200))
        trade.partial_closed = True
        trade.breakeven_active = False
        tm.update(trade, current_price=1.10300)
        assert trade.breakeven_active is True


class TestShortLosingNoBreakeven:
    """Case 3: SHORT, partial_closed=True, pnl_pips < 0 — no breakeven."""

    def test_short_losing_no_breakeven(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(
            direction="SHORT", entry=1.10000, sl=1.10200,
            tp1=1.09800, tp2=1.09600,
        ))
        trade.partial_closed = True
        trade.breakeven_active = False
        trade.current_price = 1.10500
        trade.pnl_pips = -50.0
        tm._activate_breakeven(trade)
        assert trade.breakeven_active is False
        assert trade.status != TradeStatus.BREAKEVEN


class TestDirectCallGuard:
    """Case 4: Direct _activate_breakeven with pnl_pips <= 0 is a no-op."""

    def test_direct_call_zero_pnl_noop(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(entry=1.10000, sl=1.09800, tp1=1.10200))
        trade.partial_closed = True
        trade.breakeven_active = False
        trade.pnl_pips = 0.0
        tm._activate_breakeven(trade)
        assert trade.breakeven_active is False

    def test_direct_call_negative_pnl_noop(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(entry=1.10000, sl=1.09800, tp1=1.10200))
        trade.partial_closed = True
        trade.breakeven_active = False
        trade.pnl_pips = -10.0
        tm._activate_breakeven(trade)
        assert trade.breakeven_active is False


class TestTrailingTransitivelyBlocked:
    """Case 5: With pnl_pips < 0 and partial_closed=True, trailing cannot engage
    because breakeven_active stays False (trailing is gated on breakeven_active)."""

    def test_trailing_blocked_when_losing(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(entry=1.10000, sl=1.09800, tp1=1.10200))
        trade.partial_closed = True
        trade.breakeven_active = False
        tm.update(trade, current_price=1.09500)
        assert trade.breakeven_active is False
        assert trade.status != TradeStatus.BREAKEVEN
        assert trade.status != TradeStatus.TRAILING
