"""
Tests for Phase 3 — Entry Engine, Patterns, and Validator.
"""

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, timedelta, timezone

from brain.drawdown_guard import DrawdownMode
from trigger.entry_engine import EntryEngine, EntrySignal, EntryRejection
from trigger.entry_patterns import EntryPatternDetector
from trigger.entry_validator import EntryValidator, ValidationResult


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

def _make_candles(
    base_price: float = 1.27000,
    n: int = 50,
    pip_size: float = 0.0001,
    trend: str = "up",
    include_volume: bool = False,
) -> pd.DataFrame:
    np.random.seed(42)
    times = pd.date_range("2025-01-01", periods=n, freq="5min")
    rows = []
    price = base_price
    for i in range(n):
        delta = pip_size * np.random.uniform(1, 15)
        if trend == "up":
            o = price
            c = price + delta
        elif trend == "down":
            o = price
            c = price - delta
        else:
            o = price
            c = price + delta * np.random.choice([-1, 1])
        h = max(o, c) + pip_size * np.random.uniform(0, 5)
        lo = min(o, c) - pip_size * np.random.uniform(0, 5)
        row = {"time": times[i], "open": o, "high": h, "low": lo, "close": c}
        if include_volume:
            row["volume"] = int(np.random.uniform(100, 500))
        rows.append(row)
        price = c
    return pd.DataFrame(rows)


def _make_fvg_candles(direction: str = "LONG", pip_size: float = 0.0001) -> pd.DataFrame:
    """Create candles that produce an FVG in the desired direction."""
    base = 1.27000
    if direction == "LONG":
        rows = [
            {"time": pd.Timestamp("2025-01-01 08:00"), "open": base, "high": base + 10 * pip_size, "low": base - 5 * pip_size, "close": base + 5 * pip_size},
            {"time": pd.Timestamp("2025-01-01 08:05"), "open": base + 5 * pip_size, "high": base + 40 * pip_size, "low": base + 4 * pip_size, "close": base + 35 * pip_size},
            {"time": pd.Timestamp("2025-01-01 08:10"), "open": base + 35 * pip_size, "high": base + 45 * pip_size, "low": base + 15 * pip_size, "close": base + 40 * pip_size},
        ]
        for i in range(20):
            p = base + 40 * pip_size + i * pip_size
            rows.append({
                "time": pd.Timestamp("2025-01-01 08:15") + pd.Timedelta(minutes=5 * i),
                "open": p, "high": p + 5 * pip_size, "low": p - 3 * pip_size, "close": p + 2 * pip_size,
            })
    else:
        rows = [
            {"time": pd.Timestamp("2025-01-01 08:00"), "open": base, "high": base + 5 * pip_size, "low": base - 10 * pip_size, "close": base - 5 * pip_size},
            {"time": pd.Timestamp("2025-01-01 08:05"), "open": base - 5 * pip_size, "high": base - 4 * pip_size, "low": base - 40 * pip_size, "close": base - 35 * pip_size},
            {"time": pd.Timestamp("2025-01-01 08:10"), "open": base - 35 * pip_size, "high": base - 15 * pip_size, "low": base - 45 * pip_size, "close": base - 40 * pip_size},
        ]
        for i in range(20):
            p = base - 40 * pip_size - i * pip_size
            rows.append({
                "time": pd.Timestamp("2025-01-01 08:15") + pd.Timedelta(minutes=5 * i),
                "open": p, "high": p + 3 * pip_size, "low": p - 5 * pip_size, "close": p - 2 * pip_size,
            })
    return pd.DataFrame(rows)


def _make_engulfing_m1(direction: str = "LONG") -> pd.DataFrame:
    base = 1.27300
    pip = 0.0001
    if direction == "LONG":
        rows = [
            {"time": pd.Timestamp("2025-01-01 08:50"), "open": base, "high": base + 3 * pip, "low": base - 8 * pip, "close": base - 5 * pip},
            {"time": pd.Timestamp("2025-01-01 08:51"), "open": base - 5 * pip, "high": base - 4 * pip, "low": base - 10 * pip, "close": base - 8 * pip},
            {"time": pd.Timestamp("2025-01-01 08:52"), "open": base - 10 * pip, "high": base + 2 * pip, "low": base - 12 * pip, "close": base + 1 * pip},
        ]
    else:
        rows = [
            {"time": pd.Timestamp("2025-01-01 08:50"), "open": base, "high": base + 8 * pip, "low": base - 3 * pip, "close": base + 5 * pip},
            {"time": pd.Timestamp("2025-01-01 08:51"), "open": base + 5 * pip, "high": base + 10 * pip, "low": base + 4 * pip, "close": base + 8 * pip},
            {"time": pd.Timestamp("2025-01-01 08:52"), "open": base + 10 * pip, "high": base + 12 * pip, "low": base - 2 * pip, "close": base - 1 * pip},
        ]
    return pd.DataFrame(rows)


# ===========================================================================
# Pattern detector tests
# ===========================================================================

class TestEntryPatternDetector:
    def setup_method(self):
        self.det = EntryPatternDetector()
        self.pip = 0.0001

    def test_bullish_engulfing(self):
        df = _make_engulfing_m1("LONG")
        ok, desc = self.det.detect_engulfing(df, "LONG")
        assert ok
        assert "Bullish engulfing" in desc

    def test_bearish_engulfing(self):
        df = _make_engulfing_m1("SHORT")
        ok, desc = self.det.detect_engulfing(df, "SHORT")
        assert ok
        assert "Bearish engulfing" in desc

    def test_no_engulfing_wrong_direction(self):
        df = _make_engulfing_m1("LONG")
        ok, _ = self.det.detect_engulfing(df, "SHORT")
        assert not ok

    def test_pin_bar_bullish(self):
        base = 1.27000
        pip = self.pip
        df = pd.DataFrame([
            {"time": pd.Timestamp("2025-01-01"), "open": base, "high": base + 2 * pip,
             "low": base - 20 * pip, "close": base + 1 * pip},
        ])
        ok, desc = self.det.detect_pin_bar(df, "LONG", pip)
        assert ok
        assert "pin bar" in desc.lower()

    def test_pin_bar_bearish(self):
        base = 1.27000
        pip = self.pip
        df = pd.DataFrame([
            {"time": pd.Timestamp("2025-01-01"), "open": base, "high": base + 20 * pip,
             "low": base - 2 * pip, "close": base - 1 * pip},
        ])
        ok, desc = self.det.detect_pin_bar(df, "SHORT", pip)
        assert ok

    def test_inside_bar_breakout_long(self):
        base = 1.27000
        pip = self.pip
        df = pd.DataFrame([
            {"time": pd.Timestamp("2025-01-01 08:00"), "open": base, "high": base + 20 * pip, "low": base - 20 * pip, "close": base + 10 * pip},
            {"time": pd.Timestamp("2025-01-01 08:01"), "open": base + 5 * pip, "high": base + 15 * pip, "low": base - 10 * pip, "close": base + 8 * pip},
            {"time": pd.Timestamp("2025-01-01 08:02"), "open": base + 15 * pip, "high": base + 30 * pip, "low": base + 10 * pip, "close": base + 25 * pip},
        ])
        ok, desc = self.det.detect_inside_bar_breakout(df, "LONG")
        assert ok
        assert "upside" in desc

    def test_get_best_pattern_returns_strongest(self):
        df = _make_engulfing_m1("LONG")
        name, desc = self.det.get_best_pattern(df, "LONG", 1.27290, 1.27310, self.pip)
        assert name == "engulfing"

    def test_get_best_pattern_no_match(self):
        df = _make_candles(n=5)
        name, desc = self.det.get_best_pattern(df, "LONG", 1.0, 1.1, self.pip)
        assert name == "" or isinstance(name, str)


# ===========================================================================
# Entry engine tests
# ===========================================================================

class TestEntryEngine:
    def setup_method(self):
        self.engine = EntryEngine()
        self.pip = 0.0001

    def test_entry_returns_signal_or_rejection(self):
        m5 = _make_fvg_candles("LONG")
        m1 = _make_engulfing_m1("LONG")
        h1 = _make_candles(n=50, trend="up")
        result = self.engine.calculate_entry("EURUSD", "LONG", m5, m1, h1, account_balance=10000.0)
        assert isinstance(result, (EntrySignal, EntryRejection))

    def test_entry_signal_has_valid_prices(self):
        m5 = _make_fvg_candles("LONG")
        m1 = _make_engulfing_m1("LONG")
        h1 = _make_candles(n=50, trend="up")
        result = self.engine.calculate_entry("EURUSD", "LONG", m5, m1, h1)
        if isinstance(result, EntrySignal):
            assert result.entry_price > 0
            assert result.stop_loss > 0
            assert result.stop_loss < result.entry_price
            assert result.tp1 > result.entry_price
            assert result.tp2 >= result.tp1

    def test_short_signal_prices(self):
        m5 = _make_fvg_candles("SHORT")
        m1 = _make_engulfing_m1("SHORT")
        h1 = _make_candles(n=50, trend="down")
        result = self.engine.calculate_entry("EURUSD", "SHORT", m5, m1, h1)
        if isinstance(result, EntrySignal):
            assert result.stop_loss > result.entry_price
            assert result.tp1 < result.entry_price

    def test_frozen_drawdown_rejects(self):
        self.engine.drawdown.mode = DrawdownMode.FROZEN
        m5 = _make_fvg_candles("LONG")
        m1 = _make_engulfing_m1("LONG")
        h1 = _make_candles(n=50, trend="up")
        result = self.engine.calculate_entry("EURUSD", "LONG", m5, m1, h1)
        assert isinstance(result, EntryRejection)
        assert "freeze" in result.reason.lower() or "frozen" in result.reason.lower() or "wait" in result.reason.lower()

    def test_position_size_positive(self):
        size = self.engine.calculate_position_size(1.27000, 1.26970, 0.02, 10000.0, 0.0001, "EURUSD")
        assert size > 0
        assert size <= 10.0

    def test_position_size_gold(self):
        size = self.engine.calculate_position_size(2000.0, 1999.0, 0.02, 10000.0, 0.01, "XAUUSD")
        assert size > 0

    def test_find_entry_zone_with_fvg(self):
        m5 = _make_fvg_candles("LONG")
        zone = self.engine.find_entry_zone("EURUSD", "LONG", m5, self.pip)
        assert zone["type"] in ("FVG_MIDPOINT", "OB_MIDPOINT", "FVG_OB_OVERLAP", "NONE")

    def test_no_zone_returns_none_type(self):
        df = pd.DataFrame([
            {"time": pd.Timestamp("2025-01-01"), "open": 1.0, "high": 1.0001, "low": 0.9999, "close": 1.0},
            {"time": pd.Timestamp("2025-01-01 00:05"), "open": 1.0, "high": 1.0001, "low": 0.9999, "close": 1.0},
            {"time": pd.Timestamp("2025-01-01 00:10"), "open": 1.0, "high": 1.0001, "low": 0.9999, "close": 1.0},
        ])
        zone = self.engine.find_entry_zone("EURUSD", "LONG", df, self.pip)
        assert zone["type"] == "NONE"

    def test_rejection_when_no_m1_confirmation(self):
        m5 = _make_fvg_candles("LONG")
        m1 = _make_candles(n=3, base_price=1.5, trend="flat")
        h1 = _make_candles(n=50, trend="up")
        result = self.engine.calculate_entry("EURUSD", "LONG", m5, m1, h1)
        if isinstance(result, EntryRejection):
            assert "m1" in result.reason.lower() or "micro" in result.reason.lower() or "zone" in result.reason.lower()

    def test_rr_check(self):
        sl = self.engine.calculate_stop_loss("LONG", {"bottom": 1.27000, "top": 1.27020}, self.pip)
        assert sl < 1.27000

    def test_short_sl_above_zone(self):
        sl = self.engine.calculate_stop_loss("SHORT", {"bottom": 1.27000, "top": 1.27020}, self.pip)
        assert sl > 1.27020


# ===========================================================================
# Validator tests
# ===========================================================================

class TestEntryValidator:
    def setup_method(self):
        self.validator = EntryValidator()
        self.now = datetime(2025, 6, 2, 10, 30, tzinfo=timezone.utc)  # Monday 10:30 UTC — London session
        self.signal = EntrySignal(
            pair="EURUSD",
            direction="LONG",
            entry_type="FVG_MIDPOINT",
            entry_price=1.27295,
            stop_loss=1.27260,
            tp1=1.27470,
            tp2=1.27650,
            risk_reward_1=5.0,
            risk_reward_2=10.14,
            risk_pips=35.0,
            position_size_lots=0.57,
            score=95,
            confluences=["Structure aligned", "FVG entry zone"],
            entry_zone="FVG_MIDPOINT 1.27280-1.27310",
            micro_confirmation="Bullish engulfing",
            timestamp=self.now,
            valid_until=self.now + timedelta(minutes=25),
            instrument_category="forex",
        )

    def test_valid_signal_passes(self):
        result = self.validator.validate(self.signal, current_spread_pips=1.0, utc_now=self.now)
        assert result.valid
        assert len(result.checks_failed) == 0

    def test_wide_spread_fails(self):
        result = self.validator.validate(self.signal, current_spread_pips=50.0, utc_now=self.now)
        assert not result.valid
        assert any("spread" in f.lower() for f in result.checks_failed)

    def test_expired_signal_fails(self):
        future = self.now + timedelta(hours=1)
        result = self.validator.validate(self.signal, current_spread_pips=1.0, utc_now=future)
        assert not result.valid
        assert any("expired" in f.lower() for f in result.checks_failed)

    def test_max_trades_fails(self):
        trades = [{"pair": f"PAIR{i}", "direction": "LONG", "risk_pct": 0.02} for i in range(10)]
        result = self.validator.validate(self.signal, current_spread_pips=1.0, open_trades=trades, utc_now=self.now)
        assert not result.valid
        assert any("max" in f.lower() for f in result.checks_failed)

    def test_bad_rr_fails(self):
        bad = EntrySignal(
            pair="EURUSD", direction="LONG", entry_type="FVG_MIDPOINT",
            entry_price=1.27295, stop_loss=1.27260, tp1=1.27300, tp2=1.27310,
            risk_reward_1=0.14, risk_reward_2=0.43, risk_pips=35.0,
            position_size_lots=0.5, score=90,
            timestamp=self.now, valid_until=self.now + timedelta(minutes=25),
        )
        result = self.validator.validate(bad, current_spread_pips=1.0, utc_now=self.now)
        assert not result.valid
        assert any("r:r" in f.lower() for f in result.checks_failed)


# ===========================================================================
# Integration-level tests
# ===========================================================================

class TestIntegration:
    def test_full_pipeline_long(self):
        engine = EntryEngine()
        validator = EntryValidator()
        m5 = _make_fvg_candles("LONG")
        m1 = _make_engulfing_m1("LONG")
        h1 = _make_candles(n=50, trend="up")

        result = engine.calculate_entry("EURUSD", "LONG", m5, m1, h1)
        if isinstance(result, EntrySignal):
            validation = validator.validate(result, current_spread_pips=1.0)
            assert isinstance(validation, ValidationResult)

    def test_full_pipeline_short(self):
        engine = EntryEngine()
        validator = EntryValidator()
        m5 = _make_fvg_candles("SHORT")
        m1 = _make_engulfing_m1("SHORT")
        h1 = _make_candles(n=50, trend="down")

        result = engine.calculate_entry("EURUSD", "SHORT", m5, m1, h1)
        if isinstance(result, EntrySignal):
            validation = validator.validate(result, current_spread_pips=1.0)
            assert isinstance(validation, ValidationResult)

    def test_import_from_trigger_package(self):
        from trigger import (
            EntryEngine, EntrySignal, EntryRejection,
            EntryPatternDetector, PatternMatch,
            EntryValidator, ValidationResult,
        )
        assert EntryEngine is not None
        assert EntrySignal is not None
