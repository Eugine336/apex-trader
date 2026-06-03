"""
Tests for PR-1: TP sentinel guard.
Verifies that _check_tp1, _check_tp2, and _check_tp3 return False
when take-profit levels are None or <= 0, preventing false TP triggers
on adopted positions with missing/unset broker TPs.
"""

from datetime import datetime, timezone

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


# ===================================================================
# _check_tp1 sentinel guard
# ===================================================================

class TestCheckTp1SentinelGuard:
    def test_long_tp1_zero_does_not_fire(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(tp1=0.0))
        trade.current_price = 1.09500  # adverse — below entry
        result = tm._check_tp1(trade)
        assert result is False
        assert trade.partial_closed is False
        assert trade.status == TradeStatus.OPEN

    def test_short_tp1_zero_does_not_fire(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(
            direction="SHORT", entry=1.10000, sl=1.10200,
            tp1=0.0, tp2=1.09600,
        ))
        trade.current_price = 1.10500  # adverse — above entry
        result = tm._check_tp1(trade)
        assert result is False
        assert trade.partial_closed is False

    def test_long_tp1_negative_does_not_fire(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(tp1=-1.0))
        trade.current_price = 1.11000
        result = tm._check_tp1(trade)
        assert result is False
        assert trade.partial_closed is False

    def test_long_valid_tp1_above_entry_still_fires(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(tp1=1.10200))
        trade.current_price = 1.10300  # favorable — above TP1
        result = tm._check_tp1(trade)
        assert result is True
        assert trade.partial_closed is True
        assert trade.status == TradeStatus.TP1_HIT

    def test_long_valid_tp1_not_reached_does_not_fire(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(tp1=1.10200))
        trade.current_price = 1.10100  # not yet at TP1
        result = tm._check_tp1(trade)
        assert result is False
        assert trade.partial_closed is False

    def test_short_valid_tp1_below_entry_still_fires(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(
            direction="SHORT", entry=1.10000, sl=1.10200,
            tp1=1.09800, tp2=1.09600,
        ))
        trade.current_price = 1.09700  # favorable — below TP1
        result = tm._check_tp1(trade)
        assert result is True
        assert trade.partial_closed is True


# ===================================================================
# _check_tp2 sentinel guard
# ===================================================================

class TestCheckTp2SentinelGuard:
    def test_long_tp2_zero_does_not_fire(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(tp2=0.0))
        trade.current_price = 1.11000
        result = tm._check_tp2(trade)
        assert result is False
        assert trade.status != TradeStatus.CLOSED

    def test_short_tp2_zero_does_not_fire(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(
            direction="SHORT", entry=1.10000, sl=1.10200,
            tp1=1.09800, tp2=0.0,
        ))
        trade.current_price = 0.50000
        result = tm._check_tp2(trade)
        assert result is False

    def test_long_tp2_negative_does_not_fire(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(tp2=-5.0))
        trade.current_price = 1.12000
        result = tm._check_tp2(trade)
        assert result is False

    def test_long_valid_tp2_still_fires(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(tp2=1.10400))
        trade.current_price = 1.10500
        result = tm._check_tp2(trade)
        assert result is True
        assert trade.status == TradeStatus.CLOSED

    def test_long_valid_tp2_not_reached_does_not_fire(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(tp2=1.10400))
        trade.current_price = 1.10300
        result = tm._check_tp2(trade)
        assert result is False


# ===================================================================
# _check_tp3 sentinel guard
# ===================================================================

class TestCheckTp3SentinelGuard:
    def test_tp3_zero_does_not_fire(self):
        tm = TradeManager(tp3_ladder_enabled=True)
        trade = tm.open_trade(_signal())
        trade.tp3 = 0.0
        trade.partial_closed = True
        trade.current_price = 1.12000
        result = tm._check_tp3(trade)
        assert result is False
        assert trade.tp3_hit is False

    def test_tp3_negative_does_not_fire(self):
        tm = TradeManager(tp3_ladder_enabled=True)
        trade = tm.open_trade(_signal())
        trade.tp3 = -1.0
        trade.partial_closed = True
        trade.current_price = 1.12000
        result = tm._check_tp3(trade)
        assert result is False

    def test_tp3_none_does_not_fire(self):
        tm = TradeManager(tp3_ladder_enabled=True)
        trade = tm.open_trade(_signal())
        trade.tp3 = None
        trade.partial_closed = True
        trade.current_price = 1.12000
        result = tm._check_tp3(trade)
        assert result is False

    def test_tp3_valid_still_fires(self):
        tm = TradeManager(tp3_ladder_enabled=True)
        trade = tm.open_trade(_signal())
        trade.tp3 = 1.10600
        trade.partial_closed = True
        trade.current_price = 1.10700
        result = tm._check_tp3(trade)
        assert result is True
        assert trade.tp3_hit is True
