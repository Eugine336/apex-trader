"""
Signal finiteness & H4 bias gate — regression tests.

BUG 1: Verifies that NaN/Inf prices in OHLC data produce EntryRejection
       (not an EntrySignal), and that the EntryValidator finiteness check
       catches any non-finite signal that slips through.
CHANGE 2: Verifies the H4 bias gate rejects contradicting trends when
          enabled, and is inert when disabled (default).
"""

import math
import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import AppConfig
from trigger.entry_engine import EntryEngine, EntrySignal, EntryRejection
from trigger.entry_validator import EntryValidator, ValidationResult


# ═══════════════════════════════════════════════════════════════════════════
# Helpers — match existing test_entry_engine.py patterns
# ═══════════════════════════════════════════════════════════════════════════

def _make_scan_result(pair="EURUSD", direction="LONG", score=85):
    return SimpleNamespace(
        pair=pair, direction=direction, score=score,
        confluences=["Structure aligned", "FVG entry zone"],
    )


def _make_candles(
    base_price=1.27000, n=50, pip_size=0.0001, trend="up",
) -> pd.DataFrame:
    np.random.seed(42)
    times = pd.date_range("2025-01-01", periods=n, freq="5min")
    rows = []
    price = base_price
    for i in range(n):
        delta = pip_size * np.random.uniform(1, 15)
        if trend == "up":
            o, c = price, price + delta
        elif trend == "down":
            o, c = price, price - delta
        else:
            o, c = price, price + delta * np.random.choice([-1, 1])
        h = max(o, c) + pip_size * np.random.uniform(0, 5)
        lo = min(o, c) - pip_size * np.random.uniform(0, 5)
        rows.append({"time": times[i], "open": o, "high": h, "low": lo, "close": c})
        price = c
    df = pd.DataFrame(rows)
    df["tick_volume"] = np.random.randint(100, 500, size=n)
    return df


def _make_fvg_candles(direction="LONG", pip_size=0.0001) -> pd.DataFrame:
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
    df = pd.DataFrame(rows)
    df["tick_volume"] = np.random.randint(100, 500, size=len(df))
    return df


def _inject_nan(df: pd.DataFrame, col="close", indices=None):
    """Inject NaN into specific rows (default: last row)."""
    df = df.copy()
    indices = indices if indices is not None else [-1]
    for idx in indices:
        df.loc[df.index[idx], col] = float("nan")
    return df


def _make_signal(**overrides) -> EntrySignal:
    defaults = dict(
        pair="EURUSD", direction="LONG", entry_type="FVG_MIDPOINT",
        entry_price=1.27500, stop_loss=1.27000, tp1=1.28000, tp2=1.29000,
        risk_reward_1=1.50, risk_reward_2=3.00, risk_pips=50.0,
        position_size_lots=0.05, score=85, confluences=["test"],
        timestamp=datetime.now(timezone.utc),
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    defaults.update(overrides)
    return EntrySignal(**defaults)


# ═══════════════════════════════════════════════════════════════════════════
# BUG 1 — NaN finiteness guard in EntryEngine
# ═══════════════════════════════════════════════════════════════════════════

class TestNaNRejectionInEntryEngine:
    """NaN in OHLC must produce EntryRejection, never an EntrySignal."""

    def setup_method(self):
        self.engine = EntryEngine()

    def _run_entry(self, m5_df, m1_df, h1_df, direction="LONG", score=85):
        scan = _make_scan_result(direction=direction, score=score)
        return self.engine.calculate_entry(
            pair="EURUSD", direction=direction,
            m5_df=m5_df, m1_df=m1_df, h1_df=h1_df,
            scan_result=scan, account_balance=10000.0,
        )

    def test_nan_close_in_m5_rejects(self):
        m5 = _inject_nan(_make_fvg_candles("LONG"), "close", [-1])
        m1 = _make_candles(base_price=1.27300, n=100, trend="up")
        h1 = _make_candles(base_price=1.27000, n=200, trend="up")
        result = self._run_entry(m5, m1, h1)
        assert not isinstance(result, EntrySignal), \
            f"NaN in M5 close must not produce EntrySignal, got {type(result).__name__}"

    def test_nan_high_in_m5_rejects(self):
        m5 = _inject_nan(_make_fvg_candles("LONG"), "high", [-2, -3])
        m1 = _make_candles(base_price=1.27300, n=100, trend="up")
        h1 = _make_candles(base_price=1.27000, n=200, trend="up")
        result = self._run_entry(m5, m1, h1)
        assert not isinstance(result, EntrySignal)

    def test_inf_close_in_m5_rejects(self):
        m5 = _make_fvg_candles("LONG").copy()
        m5.loc[m5.index[-1], "close"] = float("inf")
        m1 = _make_candles(base_price=1.27300, n=100, trend="up")
        h1 = _make_candles(base_price=1.27000, n=200, trend="up")
        result = self._run_entry(m5, m1, h1)
        assert not isinstance(result, EntrySignal)

    def test_clean_data_can_still_produce_signal_or_rejection(self):
        m5 = _make_fvg_candles("LONG")
        m1 = _make_candles(base_price=1.27300, n=100, trend="up")
        h1 = _make_candles(base_price=1.27000, n=200, trend="up")
        result = self._run_entry(m5, m1, h1)
        assert isinstance(result, (EntrySignal, EntryRejection))
        if isinstance(result, EntrySignal):
            assert math.isfinite(result.entry_price)
            assert math.isfinite(result.stop_loss)
            assert math.isfinite(result.tp1)
            assert math.isfinite(result.tp2)


class TestNaNRejectionDirect:
    """Directly test the finiteness guard with a zone that produces NaN."""

    def setup_method(self):
        self.engine = EntryEngine()

    def test_nan_zone_midpoint_produces_rejection(self):
        nan_zone = {
            "type": "FVG_MIDPOINT",
            "top": float("nan"), "bottom": float("nan"),
            "midpoint": float("nan"),
            "fvg": None, "ob": None, "has_sweep": False,
        }
        with patch.object(self.engine, "find_entry_zone", return_value=nan_zone), \
             patch.object(self.engine, "confirm_m1_entry", return_value=(True, "mocked")):
            m5 = _make_candles(n=50, trend="up")
            m1 = _make_candles(n=100, trend="up")
            h1 = _make_candles(n=200, trend="up")
            scan = _make_scan_result()
            result = self.engine.calculate_entry(
                "EURUSD", "LONG", m5, m1, h1, scan, 10000.0,
            )
            assert isinstance(result, EntryRejection)
            assert "non-finite" in result.reason.lower() or "Non-finite" in result.reason


# ═══════════════════════════════════════════════════════════════════════════
# BUG 1 — Defense-in-depth: EntryValidator finiteness check
# ═══════════════════════════════════════════════════════════════════════════

class TestValidatorFiniteness:

    def test_finite_signal_passes_finiteness_check(self):
        validator = EntryValidator()
        ok, msg = validator.check_price_finiteness(_make_signal())
        assert ok
        assert "finite" in msg.lower()

    def test_nan_entry_price_fails(self):
        validator = EntryValidator()
        sig = _make_signal(entry_price=float("nan"))
        ok, msg = validator.check_price_finiteness(sig)
        assert not ok
        assert "entry_price" in msg

    def test_nan_stop_loss_fails(self):
        validator = EntryValidator()
        sig = _make_signal(stop_loss=float("nan"))
        ok, msg = validator.check_price_finiteness(sig)
        assert not ok
        assert "stop_loss" in msg

    def test_nan_tp1_fails(self):
        validator = EntryValidator()
        sig = _make_signal(tp1=float("nan"))
        ok, msg = validator.check_price_finiteness(sig)
        assert not ok
        assert "tp1" in msg

    def test_nan_tp2_fails(self):
        validator = EntryValidator()
        sig = _make_signal(tp2=float("nan"))
        ok, msg = validator.check_price_finiteness(sig)
        assert not ok
        assert "tp2" in msg

    def test_inf_entry_price_fails(self):
        validator = EntryValidator()
        sig = _make_signal(entry_price=float("inf"))
        ok, msg = validator.check_price_finiteness(sig)
        assert not ok

    def test_neg_inf_stop_loss_fails(self):
        validator = EntryValidator()
        sig = _make_signal(stop_loss=float("-inf"))
        ok, msg = validator.check_price_finiteness(sig)
        assert not ok

    def test_validate_rejects_nan_signal(self):
        validator = EntryValidator()
        sig = _make_signal(entry_price=float("nan"))
        result = validator.validate(sig, current_spread_pips=1.0)
        assert not result.valid
        has_finiteness_fail = any("Non-finite" in f or "non-finite" in f for f in result.checks_failed)
        assert has_finiteness_fail, f"Expected finiteness failure in {result.checks_failed}"


# ═══════════════════════════════════════════════════════════════════════════
# CHANGE 2 — H4 bias gate
# ═══════════════════════════════════════════════════════════════════════════

class TestH4BiasGate:

    def _make_trending_df(self, direction, n=50, pip_size=0.0001):
        return _make_candles(
            base_price=1.27000, n=n, pip_size=pip_size,
            trend="up" if direction == "BULLISH" else "down",
        )

    def _mock_h4_analysis(self, trend_str):
        from brain.structure_engine import Trend, StructureEvent, StructureAnalysis
        return StructureAnalysis(
            trend=Trend(trend_str),
            last_event=StructureEvent.NONE,
            swing_high=None, swing_low=None,
            last_bos_level=None, last_choch_level=None,
            structure_broken=False,
            bullish_swing_points=[], bearish_swing_points=[],
            confidence=0.5,
        )

    def test_h4_gate_disabled_by_default(self):
        engine = EntryEngine()
        assert not engine.config.risk.h4_bias_gate_enabled

    def test_h4_gate_off_allows_contradicting_trend(self):
        engine = EntryEngine()
        m5 = _make_fvg_candles("LONG")
        m1 = _make_candles(base_price=1.27300, n=100, trend="up")
        h1 = _make_candles(base_price=1.27000, n=200, trend="up")
        h4_bearish = self._make_trending_df("BEARISH", n=50)
        scan = _make_scan_result(direction="LONG")
        result = engine.calculate_entry(
            "EURUSD", "LONG", m5, m1, h1, scan, 10000.0,
            h4_df=h4_bearish,
        )
        assert not (isinstance(result, EntryRejection) and "H4 bias gate" in result.reason), \
            "H4 bias gate should NOT fire when disabled"

    def test_h4_gate_on_rejects_long_against_bearish_h4(self):
        cfg = AppConfig()
        cfg.risk.h4_bias_gate_enabled = True
        engine = EntryEngine(config=cfg)
        m5 = _make_fvg_candles("LONG")
        m1 = _make_candles(base_price=1.27300, n=100, trend="up")
        h1 = _make_candles(base_price=1.27000, n=200, trend="up")
        h4_bearish = self._make_trending_df("BEARISH", n=50)
        scan = _make_scan_result(direction="LONG")
        mock_analysis = self._mock_h4_analysis("BEARISH")
        with patch("brain.structure_engine.StructureEngine.analyze", return_value=mock_analysis):
            result = engine.calculate_entry(
                "EURUSD", "LONG", m5, m1, h1, scan, 10000.0,
                h4_df=h4_bearish,
            )
        assert isinstance(result, EntryRejection)
        assert "H4 bias gate" in result.reason
        assert "BEARISH" in result.reason

    def test_h4_gate_on_rejects_short_against_bullish_h4(self):
        cfg = AppConfig()
        cfg.risk.h4_bias_gate_enabled = True
        engine = EntryEngine(config=cfg)
        m5 = _make_fvg_candles("SHORT")
        m1 = _make_candles(base_price=1.26700, n=100, trend="down")
        h1 = _make_candles(base_price=1.27000, n=200, trend="down")
        h4_bullish = self._make_trending_df("BULLISH", n=50)
        scan = _make_scan_result(direction="SHORT")
        mock_analysis = self._mock_h4_analysis("BULLISH")
        with patch("brain.structure_engine.StructureEngine.analyze", return_value=mock_analysis):
            result = engine.calculate_entry(
                "EURUSD", "SHORT", m5, m1, h1, scan, 10000.0,
                h4_df=h4_bullish,
            )
        assert isinstance(result, EntryRejection)
        assert "H4 bias gate" in result.reason
        assert "BULLISH" in result.reason

    def test_h4_gate_on_allows_aligned_trend(self):
        cfg = AppConfig()
        cfg.risk.h4_bias_gate_enabled = True
        engine = EntryEngine(config=cfg)
        m5 = _make_fvg_candles("LONG")
        m1 = _make_candles(base_price=1.27300, n=100, trend="up")
        h1 = _make_candles(base_price=1.27000, n=200, trend="up")
        h4_bullish = self._make_trending_df("BULLISH", n=50)
        scan = _make_scan_result(direction="LONG")
        result = engine.calculate_entry(
            "EURUSD", "LONG", m5, m1, h1, scan, 10000.0,
            h4_df=h4_bullish,
        )
        if isinstance(result, EntryRejection):
            assert "H4 bias gate" not in result.reason, \
                "H4 gate should not reject when H4 trend aligns with direction"

    def test_h4_gate_skipped_when_h4_df_none(self):
        cfg = AppConfig()
        cfg.risk.h4_bias_gate_enabled = True
        engine = EntryEngine(config=cfg)
        m5 = _make_fvg_candles("LONG")
        m1 = _make_candles(base_price=1.27300, n=100, trend="up")
        h1 = _make_candles(base_price=1.27000, n=200, trend="up")
        scan = _make_scan_result(direction="LONG")
        result = engine.calculate_entry(
            "EURUSD", "LONG", m5, m1, h1, scan, 10000.0,
            h4_df=None,
        )
        if isinstance(result, EntryRejection):
            assert "H4 bias gate" not in result.reason

    def test_h4_gate_skipped_when_h4_df_too_short(self):
        cfg = AppConfig()
        cfg.risk.h4_bias_gate_enabled = True
        engine = EntryEngine(config=cfg)
        m5 = _make_fvg_candles("LONG")
        m1 = _make_candles(base_price=1.27300, n=100, trend="up")
        h1 = _make_candles(base_price=1.27000, n=200, trend="up")
        h4_short = self._make_trending_df("BEARISH", n=10)
        scan = _make_scan_result(direction="LONG")
        result = engine.calculate_entry(
            "EURUSD", "LONG", m5, m1, h1, scan, 10000.0,
            h4_df=h4_short,
        )
        if isinstance(result, EntryRejection):
            assert "H4 bias gate" not in result.reason
