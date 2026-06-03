"""
Tests for the live ATR-based volatility stop wiring in MTFOrchestrator._resolve_stop_loss.

Covers:
  (a) mode "off" → returns structure SL unchanged
  (b) mode "on" + valid ATR → returns clamped ATR stop on correct side of entry
  (c) mode "on" + ATR unavailable → falls back to structure SL
"""

import pandas as pd
import pytest

from brain.mtf_orchestrator import MTFOrchestrator


def _make_m1_df(closes: list[float], *, n: int = 20) -> pd.DataFrame:
    """Build a minimal M1 DataFrame with the required OHLC columns."""
    rows = []
    for i, c in enumerate(closes[-n:]):
        rows.append({
            "time": pd.Timestamp("2025-01-01") + pd.Timedelta(minutes=i),
            "open": c - 0.0002,
            "high": c + 0.0005,
            "low": c - 0.0005,
            "close": c,
        })
    return pd.DataFrame(rows)


def _structure_sl(direction: str, m1_df: pd.DataFrame, entry_price: float,
                  pip_size: float = 0.0001) -> float:
    """Reproduce the original structure-only SL logic for comparison."""
    buffer = 2 * pip_size
    if direction == "LONG":
        swing_low = float(m1_df["low"].tail(10).min())
        return min(swing_low - buffer, entry_price - 5 * pip_size)
    swing_high = float(m1_df["high"].tail(10).max())
    return max(swing_high + buffer, entry_price + 5 * pip_size)


class TestModeOff:
    """When volatility_stop_mode != 'on', _resolve_stop_loss returns
    the structure SL byte-for-byte."""

    def test_long_mode_off(self):
        closes = [1.1000 + i * 0.0001 for i in range(20)]
        m1 = _make_m1_df(closes)
        entry = 1.1020
        orch = MTFOrchestrator(volatility_stop_mode="off")
        result = orch._resolve_stop_loss("LONG", m1, entry, "EURUSD")
        expected = _structure_sl("LONG", m1, entry)
        assert result == expected

    def test_short_mode_off(self):
        closes = [1.1000 + i * 0.0001 for i in range(20)]
        m1 = _make_m1_df(closes)
        entry = 1.0990
        orch = MTFOrchestrator(volatility_stop_mode="off")
        result = orch._resolve_stop_loss("SHORT", m1, entry, "EURUSD")
        expected = _structure_sl("SHORT", m1, entry)
        assert result == expected

    def test_default_mode_is_off(self):
        orch = MTFOrchestrator()
        assert orch._volatility_stop_mode == "off"


class TestModeOnValidATR:
    """When mode is 'on' and ATR data is sufficient, _resolve_stop_loss
    returns a clamped ATR-based stop on the correct side of entry."""

    def _build_steady_m1(self, base: float, atr_approx: float, n: int = 50):
        """Build an M1 df where the true range ≈ atr_approx per bar."""
        rows = []
        for i in range(n):
            c = base + (i % 5) * 0.00001
            rows.append({
                "time": pd.Timestamp("2025-01-01") + pd.Timedelta(minutes=i),
                "open": c,
                "high": c + atr_approx / 2,
                "low": c - atr_approx / 2,
                "close": c,
            })
        return pd.DataFrame(rows)

    def test_long_atr_stop_below_entry(self):
        atr_approx = 0.0020
        m1 = self._build_steady_m1(1.1000, atr_approx, n=50)
        entry = 1.1000
        orch = MTFOrchestrator(
            volatility_stop_mode="on",
            atr_stop_mult=1.5,
            atr_stop_period=14,
        )
        result = orch._resolve_stop_loss("LONG", m1, entry, "EURUSD")
        assert result < entry, f"LONG ATR stop {result} should be below entry {entry}"

    def test_short_atr_stop_above_entry(self):
        atr_approx = 0.0020
        m1 = self._build_steady_m1(1.1000, atr_approx, n=50)
        entry = 1.1000
        orch = MTFOrchestrator(
            volatility_stop_mode="on",
            atr_stop_mult=1.5,
            atr_stop_period=14,
        )
        result = orch._resolve_stop_loss("SHORT", m1, entry, "EURUSD")
        assert result > entry, f"SHORT ATR stop {result} should be above entry {entry}"

    def test_atr_stop_differs_from_structure(self):
        """With enough volatility data, the ATR stop should generally
        differ from the structure-only stop."""
        atr_approx = 0.0040
        m1 = self._build_steady_m1(1.1000, atr_approx, n=50)
        entry = 1.1000
        orch = MTFOrchestrator(
            volatility_stop_mode="on",
            atr_stop_mult=1.5,
            atr_stop_period=14,
        )
        atr_result = orch._resolve_stop_loss("LONG", m1, entry, "EURUSD")
        struct_result = _structure_sl("LONG", m1, entry)
        assert atr_result != struct_result, (
            f"ATR stop {atr_result} should differ from structure {struct_result} "
            f"with ATR ≈ {atr_approx}"
        )


class TestModeOnATRUnavailable:
    """When mode is 'on' but ATR is unavailable (too few bars, NaN, etc.),
    _resolve_stop_loss falls back to the structure SL."""

    def test_fallback_with_too_few_bars(self):
        closes = [1.1000, 1.1001]
        m1 = _make_m1_df(closes, n=2)
        entry = 1.1000
        orch = MTFOrchestrator(
            volatility_stop_mode="on",
            atr_stop_period=14,
        )
        result = orch._resolve_stop_loss("LONG", m1, entry, "EURUSD")
        expected = _structure_sl("LONG", m1, entry)
        assert result == expected, (
            f"Should fall back to structure SL {expected}, got {result}"
        )

    def test_fallback_short_with_too_few_bars(self):
        closes = [1.1000, 1.1001]
        m1 = _make_m1_df(closes, n=2)
        entry = 1.0990
        orch = MTFOrchestrator(
            volatility_stop_mode="on",
            atr_stop_period=14,
        )
        result = orch._resolve_stop_loss("SHORT", m1, entry, "EURUSD")
        expected = _structure_sl("SHORT", m1, entry)
        assert result == expected

    def test_fallback_with_no_pair(self):
        """When pair is empty, get_profile returns None and we fall back
        gracefully using default min_risk_pips."""
        closes = [1.1000 + i * 0.0001 for i in range(20)]
        m1 = _make_m1_df(closes)
        entry = 1.1020
        orch = MTFOrchestrator(
            volatility_stop_mode="on",
            atr_stop_period=14,
        )
        result = orch._resolve_stop_loss("LONG", m1, entry)
        assert isinstance(result, float)
        assert result < entry


class TestConstructorWiring:
    """Verify the constructor stores all ATR params correctly."""

    def test_all_params_stored(self):
        orch = MTFOrchestrator(
            volatility_stop_mode="on",
            atr_stop_period=20,
            atr_stop_mult=2.0,
            atr_stop_ratio_min=0.3,
            atr_stop_ratio_max=3.0,
            atr_stop_max_risk_mult=5.0,
        )
        assert orch._volatility_stop_mode == "on"
        assert orch._atr_stop_period == 20
        assert orch._atr_stop_mult == 2.0
        assert orch._atr_stop_ratio_min == 0.3
        assert orch._atr_stop_ratio_max == 3.0
        assert orch._atr_stop_max_risk_mult == 5.0

    def test_defaults_match_config(self):
        orch = MTFOrchestrator()
        assert orch._volatility_stop_mode == "off"
        assert orch._atr_stop_period == 14
        assert orch._atr_stop_mult == 1.5
        assert orch._atr_stop_ratio_min == 0.5
        assert orch._atr_stop_ratio_max == 2.0
        assert orch._atr_stop_max_risk_mult == 4.0
