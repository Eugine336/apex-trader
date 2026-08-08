"""
Tests for the entry-engine SL floor.

Regression coverage for the live blocker where a tight entry zone (or a
low-volatility ATR stop) produced a sub-minimum stop loss that the
risk-distance gate rejected outright:

    [EURUSD] ATR stop override — structure 1.15958 -> ATR 1.15948 (+1.1 pips)
    ❌ REJECTED LONG EURUSD (score 85) — Risk distance 2.1 pips below minimum 5.0

Instead of killing the setup, the SL is now floored up to the per-category
minimum so the trade proceeds with a sane risk distance.
"""

import pytest

from config import AppConfig
from trigger.entry_engine import EntryEngine


@pytest.fixture
def engine():
    return EntryEngine(config=AppConfig())


class TestEntrySLFloorForex:
    pip = 0.0001
    entry = 1.15960
    min_risk_distance = 5.0 * 0.0001  # forex: 5 pips

    def test_too_tight_long_stop_widened_to_minimum(self, engine):
        # 2.1-pip stop below the 5.0-pip forex floor — the live blocker.
        risk = 2.1 * self.pip
        sl_in = self.entry - risk
        sl_out, risk_out = engine._apply_sl_floor(
            direction="LONG",
            entry_price=self.entry,
            stop_loss=sl_in,
            risk_distance=risk,
            pip_size=self.pip,
            category="forex",
            min_risk_distance=self.min_risk_distance,
            pair="EURUSD",
        )
        assert risk_out == pytest.approx(self.min_risk_distance)
        assert sl_out == pytest.approx(self.entry - self.min_risk_distance)
        # Floored stop is wider (further below entry) than the rejected one.
        assert sl_out < sl_in

    def test_too_tight_short_stop_widened_to_minimum(self, engine):
        risk = 2.1 * self.pip
        sl_in = self.entry + risk
        sl_out, risk_out = engine._apply_sl_floor(
            direction="SHORT",
            entry_price=self.entry,
            stop_loss=sl_in,
            risk_distance=risk,
            pip_size=self.pip,
            category="forex",
            min_risk_distance=self.min_risk_distance,
            pair="EURUSD",
        )
        assert risk_out == pytest.approx(self.min_risk_distance)
        assert sl_out == pytest.approx(self.entry + self.min_risk_distance)
        assert sl_out > sl_in

    def test_stop_already_above_minimum_unchanged(self, engine):
        risk = 12.0 * self.pip
        sl_in = self.entry - risk
        sl_out, risk_out = engine._apply_sl_floor(
            direction="LONG",
            entry_price=self.entry,
            stop_loss=sl_in,
            risk_distance=risk,
            pip_size=self.pip,
            category="forex",
            min_risk_distance=self.min_risk_distance,
            pair="EURUSD",
        )
        assert sl_out == sl_in
        assert risk_out == risk

    def test_degenerate_zone_not_floored(self, engine):
        # Sub-pip distance is a broken zone — leave it so the caller's
        # invalid-zone rejection still fires instead of inventing a stop.
        risk = 0.4 * self.pip
        sl_in = self.entry - risk
        sl_out, risk_out = engine._apply_sl_floor(
            direction="LONG",
            entry_price=self.entry,
            stop_loss=sl_in,
            risk_distance=risk,
            pip_size=self.pip,
            category="forex",
            min_risk_distance=self.min_risk_distance,
            pair="EURUSD",
        )
        assert sl_out == sl_in
        assert risk_out == risk


class TestEntrySLFloorOtherCategories:
    def test_index_too_tight_stop_widened(self, engine):
        pip = 0.1
        entry = 18000.0
        min_risk_distance = 15.0 * pip  # index: 15 pips
        risk = 5.0 * pip
        sl_in = entry - risk
        sl_out, risk_out = engine._apply_sl_floor(
            direction="LONG",
            entry_price=entry,
            stop_loss=sl_in,
            risk_distance=risk,
            pip_size=pip,
            category="index",
            min_risk_distance=min_risk_distance,
            pair="US100",
        )
        assert risk_out == pytest.approx(min_risk_distance)
        assert sl_out == pytest.approx(entry - min_risk_distance)

    def test_synthetic_uses_percentage_floor(self, engine):
        pip = 0.01
        entry = 857.0
        # min_risk_pips=50 -> 0.5 pts, but the 0.3% price floor (2.571 pts) binds.
        min_risk_distance = 50.0 * pip
        pct_floor = entry * 0.003
        risk = 1.0  # 1.0 pts, below the 0.3% floor
        sl_in = entry - risk
        sl_out, risk_out = engine._apply_sl_floor(
            direction="LONG",
            entry_price=entry,
            stop_loss=sl_in,
            risk_distance=risk,
            pip_size=pip,
            category="synthetic",
            min_risk_distance=min_risk_distance,
            pair="V100",
        )
        assert risk_out == pytest.approx(pct_floor)
        assert sl_out == pytest.approx(entry - pct_floor)

    def test_crypto_uses_percentage_floor(self, engine):
        pip = 0.01
        entry = 60000.0
        min_risk_distance = 20.0 * pip
        pct_floor = entry * 0.0015
        risk = 10.0  # below the 0.15% floor (90.0)
        sl_in = entry - risk
        sl_out, risk_out = engine._apply_sl_floor(
            direction="LONG",
            entry_price=entry,
            stop_loss=sl_in,
            risk_distance=risk,
            pip_size=pip,
            category="crypto",
            min_risk_distance=min_risk_distance,
            pair="BTCUSD",
        )
        assert risk_out == pytest.approx(pct_floor)
        assert sl_out == pytest.approx(entry - pct_floor)

    def test_synthetic_wide_stop_unchanged(self, engine):
        pip = 0.01
        entry = 857.0
        min_risk_distance = 50.0 * pip
        risk = 5.0  # 5 pts > 0.3% floor (2.571)
        sl_in = entry - risk
        sl_out, risk_out = engine._apply_sl_floor(
            direction="LONG",
            entry_price=entry,
            stop_loss=sl_in,
            risk_distance=risk,
            pip_size=pip,
            category="synthetic",
            min_risk_distance=min_risk_distance,
            pair="V100",
        )
        assert sl_out == sl_in
        assert risk_out == risk
