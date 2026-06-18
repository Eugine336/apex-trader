"""Tests for execution/position_snapshot.py — PositionSnapshot and builder."""

import pytest
from datetime import datetime, timezone
from types import SimpleNamespace

from execution.position_snapshot import PositionSnapshot, build_position_snapshot


class TestPositionSnapshot:
    def test_frozen(self):
        snap = PositionSnapshot(
            order_id="T1", platform="mt5", symbol="EURUSD",
            direction="BUY", entry_price=1.10, lots=0.1,
            remaining_lots=0.1, open_time=datetime.now(timezone.utc),
            score=85, sl=1.09, sl_original=1.09, tp1=1.11, tp2=1.12,
            tp2_original=1.12,
        )
        with pytest.raises(AttributeError):
            snap.sl = 1.095

    def test_is_long_buy(self):
        snap = PositionSnapshot(
            order_id="T1", platform="mt5", symbol="EURUSD",
            direction="BUY", entry_price=1.10, lots=0.1,
            remaining_lots=0.1, open_time=datetime.now(timezone.utc),
            score=85, sl=1.09, sl_original=1.09, tp1=1.11, tp2=1.12,
            tp2_original=1.12,
        )
        assert snap.is_long is True

    def test_is_long_short(self):
        snap = PositionSnapshot(
            order_id="T1", platform="mt5", symbol="EURUSD",
            direction="SELL", entry_price=1.10, lots=0.1,
            remaining_lots=0.1, open_time=datetime.now(timezone.utc),
            score=85, sl=1.11, sl_original=1.11, tp1=1.09, tp2=1.08,
            tp2_original=1.08,
        )
        assert snap.is_long is False

    def test_risk_pips(self):
        snap = PositionSnapshot(
            order_id="T1", platform="mt5", symbol="EURUSD",
            direction="BUY", entry_price=1.10000, lots=0.1,
            remaining_lots=0.1, open_time=datetime.now(timezone.utc),
            score=85, sl=1.09900, sl_original=1.09900,
            tp1=1.11, tp2=1.12, tp2_original=1.12,
            pip_size=0.0001,
        )
        assert abs(snap.risk_pips - 10.0) < 0.01

    def test_pnl_r(self):
        snap = PositionSnapshot(
            order_id="T1", platform="mt5", symbol="EURUSD",
            direction="BUY", entry_price=1.10000, lots=0.1,
            remaining_lots=0.1, open_time=datetime.now(timezone.utc),
            score=85, sl=1.09900, sl_original=1.09900,
            tp1=1.11, tp2=1.12, tp2_original=1.12,
            pip_size=0.0001, pnl_pips=20.0,
        )
        assert abs(snap.pnl_r - 2.0) < 0.01


class TestBuildPositionSnapshot:
    def _make_pos(self, **overrides):
        defaults = dict(
            order_id="ORD1", platform="mt5", symbol="EURUSD",
            direction="BUY", lots=0.1, entry_price=1.10000,
            sl=1.09900, tp1=1.11000, tp2=1.12000,
            score=85, regime="trending", session="london",
            entry_type="MARKET", open_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            tp1_hit=False, at_breakeven=False, trailing=False,
            last_update=datetime.now(timezone.utc),
            re_entry_eligible=False, tm_trade_id="tm_001",
            stake_usd=0.0, multiplier=100, broker_pnl=0.0,
            broker_lots=0.1, scale_in_count=0, idempotency_key="",
            revalidation_pending=False, unconfirmed_cycles=0,
            confluences=["fvg", "ob"],
            initial_risk_dollars=10.0, entry_spread=0.0,
            entry_slippage_pips=0.0, plan_id="", plan_sl_pips=0.0,
            plan_scale_in_allowed=None, strategy_fingerprint="",
            execution_profile_name="", behavior_features={},
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _make_trade(self, **overrides):
        defaults = dict(
            trade_id="tm_001", pair="EURUSD", direction="BUY",
            entry_price=1.10000, current_price=1.10500,
            stop_loss=1.09900, original_stop_loss=1.09900,
            tp1=1.11000, tp2=1.12000, original_tp2=1.12000,
            position_size_lots=0.1, remaining_size_lots=0.1,
            status="OPEN", pnl_pips=50.0, pnl_dollars=5.0,
            entry_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            tp1_hit_time=None, close_time=None, close_reason=None,
            candles_since_entry=10,
            highest_price_since_entry=1.10600,
            lowest_price_since_entry=1.09950,
            trailing_stop=None, breakeven_active=False,
            partial_closed=False, re_entry_eligible=False,
            score=85, pip_size=0.0001, pip_value_per_lot=10.0,
            confluences=["fvg", "ob"], entry_zone="FVG_M5",
            entry_timeframe="M5", tp3=None, original_tp3=None,
            tp3_hit=False, platform="mt5",
            plan_be_trigger_r=None, plan_trail_activation_r=None,
            plan_trail_strategy=None, plan_partial_ratio=None,
            entry_oq=None, entry_eq=None,
            strategic_structure_integrity=None,
            strategic_tf_alignment=None,
            strategic_assessment_time=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_basic_build(self):
        pos = self._make_pos()
        snap = build_position_snapshot(pos, current_price=1.105)
        assert snap.order_id == "ORD1"
        assert snap.symbol == "EURUSD"
        assert snap.direction == "BUY"
        assert snap.current_price == 1.105
        assert snap.is_long is True
        assert isinstance(snap.confluences, tuple)

    def test_with_tm_trade(self):
        pos = self._make_pos()
        tm = self._make_trade(pnl_pips=25.0, breakeven_active=True)
        snap = build_position_snapshot(pos, tm_trade=tm, current_price=1.105)
        assert snap.pnl_pips == 25.0
        assert snap.sl_original == 1.09900
        assert snap.candles_since_entry == 10
        assert snap.highest_since_entry == 1.10600
        assert snap.pip_size == 0.0001

    def test_with_score_history(self):
        pos = self._make_pos()
        snap = build_position_snapshot(
            pos, current_price=1.105,
            score_history=(90, 87, 84, 80),
        )
        assert snap.score_history == (90, 87, 84, 80)

    def test_without_tm_trade_uses_pos_values(self):
        pos = self._make_pos(sl=1.098, tp1=1.115)
        snap = build_position_snapshot(pos, current_price=1.105)
        assert snap.sl == 1.098
        assert snap.tp1 == 1.115
        assert snap.pnl_pips == 0.0

    def test_deriv_position(self):
        pos = self._make_pos(platform="deriv", stake_usd=50.0, multiplier=200)
        snap = build_position_snapshot(pos, current_price=1.105)
        assert snap.platform == "deriv"
        assert snap.stake_usd == 50.0
        assert snap.multiplier == 200

    def test_trade_status_enum_to_string(self):
        from enum import Enum

        class MockStatus(Enum):
            TRAILING = "TRAILING"

        pos = self._make_pos()
        tm = self._make_trade(status=MockStatus.TRAILING)
        snap = build_position_snapshot(pos, tm_trade=tm, current_price=1.105)
        assert snap.trade_status == "TRAILING"
