"""
Tests for StructureEngine._find_swings prominence-based filter.
Imports only pandas — no torch dependency.
"""

import pandas as pd
import pytest

from brain.structure_engine import StructureEngine


def _make_df(highs: list, lows: list) -> pd.DataFrame:
    """Build a minimal OHLC DataFrame from highs and lows."""
    _n = len(highs)
    opens = [(h + lo) / 2 for h, lo in zip(highs, lows)]
    closes = opens[:]
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
    })


class TestSwingProminenceFilter:
    """Verify the prominence-based swing filter rejects noise and keeps real pivots."""

    def test_noise_on_wide_bar_rejected(self):
        """A bar that is the window max but whose prominence over neighbours is
        below min_swing_size must NOT be returned — even if the bar's own
        high-low range is large (old code would have kept it)."""

        pip = 0.0001
        engine = StructureEngine(swing_lookback=2, min_swing_size_pips=5.0, pip_size=pip)
        # min_swing_size = 5 * 0.0001 = 0.0005

        # Build a flat-ish series where bar 4 is the window max but only
        # barely above its neighbours (prominence < 0.0005).
        # However, bar 4 has a huge candle range (wide body) so old code
        # kept it via candle_range >= min_swing_size.
        highs = [1.1000, 1.1000, 1.1001, 1.1000, 1.1002, 1.1000, 1.1001, 1.1000, 1.1000]
        lows =  [1.0990, 1.0990, 1.0990, 1.0990, 1.0950, 1.0990, 1.0990, 1.0990, 1.0990]
        # Bar 4: high=1.1002, candle range = 1.1002 - 1.0950 = 0.0052 (huge)
        # But prominence = 1.1002 - max(neighbours' highs) = 1.1002 - 1.1001 = 0.0001
        # 0.0001 < 0.0005 → should be REJECTED

        df = _make_df(highs, lows)
        swings = engine._find_swings(df)

        high_swings = [s for s in swings if s["type"] == "HIGH"]
        high_indices = [s["index"] for s in high_swings]
        assert 4 not in high_indices, (
            "Noise pivot on wide bar at index 4 should be rejected by prominence filter"
        )

    def test_real_pivot_on_narrow_bar_kept(self):
        """A genuine pivot whose prominence over neighbours >= min_swing_size
        must be returned — even if the bar's own candle range is small
        (old code would have dropped it)."""

        pip = 0.0001
        engine = StructureEngine(swing_lookback=2, min_swing_size_pips=5.0, pip_size=pip)
        # min_swing_size = 0.0005

        # Bar 5 is a clear swing high: prominence is large but bar range is tiny.
        highs = [1.1000, 1.1000, 1.1000, 1.1000, 1.1000, 1.1020, 1.1000, 1.1000, 1.1000, 1.1000]
        lows =  [1.0990, 1.0990, 1.0990, 1.0990, 1.0990, 1.1018, 1.0990, 1.0990, 1.0990, 1.0990]
        # Bar 5: high=1.1020, low=1.1018 → candle range = 0.0002 (tiny, old code rejects)
        # Prominence = 1.1020 - max(neighbours) = 1.1020 - 1.1000 = 0.0020
        # 0.0020 >= 0.0005 → should be KEPT

        df = _make_df(highs, lows)
        swings = engine._find_swings(df)

        high_swings = [s for s in swings if s["type"] == "HIGH"]
        high_indices = [s["index"] for s in high_swings]
        assert 5 in high_indices, (
            "Real pivot on narrow bar at index 5 should be kept by prominence filter"
        )

    def test_noise_low_on_wide_bar_rejected(self):
        """Same as test_noise_on_wide_bar_rejected but for a swing LOW."""

        pip = 0.0001
        engine = StructureEngine(swing_lookback=2, min_swing_size_pips=5.0, pip_size=pip)

        highs = [1.1010, 1.1010, 1.1010, 1.1010, 1.1060, 1.1010, 1.1010, 1.1010, 1.1010]
        lows =  [1.1000, 1.1000, 1.0999, 1.1000, 1.0998, 1.1000, 1.0999, 1.1000, 1.1000]
        # Bar 4: low=1.0998, candle range = 1.1060 - 1.0998 = 0.0062 (huge)
        # Prominence = min(neighbour lows) - 1.0998 = 1.0999 - 1.0998 = 0.0001
        # 0.0001 < 0.0005 → should be REJECTED

        df = _make_df(highs, lows)
        swings = engine._find_swings(df)

        low_swings = [s for s in swings if s["type"] == "LOW"]
        low_indices = [s["index"] for s in low_swings]
        assert 4 not in low_indices, (
            "Noise low pivot on wide bar at index 4 should be rejected"
        )

    def test_real_low_pivot_on_narrow_bar_kept(self):
        """Genuine low pivot with small candle range but large prominence."""

        pip = 0.0001
        engine = StructureEngine(swing_lookback=2, min_swing_size_pips=5.0, pip_size=pip)

        highs = [1.1010, 1.1010, 1.1010, 1.1010, 1.1010, 1.0982, 1.1010, 1.1010, 1.1010, 1.1010]
        lows =  [1.1000, 1.1000, 1.1000, 1.1000, 1.1000, 1.0980, 1.1000, 1.1000, 1.1000, 1.1000]
        # Bar 5: low=1.0980, high=1.0982 → candle range = 0.0002 (tiny)
        # Prominence = min(neighbours) - 1.0980 = 1.1000 - 1.0980 = 0.0020
        # 0.0020 >= 0.0005 → should be KEPT

        df = _make_df(highs, lows)
        swings = engine._find_swings(df)

        low_swings = [s for s in swings if s["type"] == "LOW"]
        low_indices = [s["index"] for s in low_swings]
        assert 5 in low_indices, (
            "Real low pivot on narrow bar at index 5 should be kept"
        )

    def test_clean_uptrend_produces_alternating_hh_hl(self):
        """A clearly stepped up-trend must yield the expected alternating
        HH/HL swing structure (not broken by the prominence change)."""

        pip = 0.0001
        engine = StructureEngine(swing_lookback=2, min_swing_size_pips=3.0, pip_size=pip)

        # Zigzag uptrend with clear peaks/troughs.
        # Peak at bar 4 (high=1.104), trough at bar 8 (low=1.099),
        # higher peak at bar 14 (high=1.106).  Each prominence >= 0.001 >> 0.0003.
        highs = [1.100, 1.101, 1.102, 1.103, 1.104,
                 1.103, 1.102, 1.101, 1.100, 1.101,
                 1.102, 1.103, 1.104, 1.105, 1.106,
                 1.105, 1.104, 1.103, 1.102, 1.103]
        lows =  [1.099, 1.100, 1.101, 1.102, 1.103,
                 1.102, 1.101, 1.100, 1.099, 1.100,
                 1.101, 1.102, 1.103, 1.104, 1.105,
                 1.104, 1.103, 1.102, 1.101, 1.102]

        df = _make_df(highs, lows)
        swings = engine._find_swings(df)

        assert len(swings) >= 2, "Zigzag uptrend should produce at least 2 swings"
        types = [s["type"] for s in swings]
        for i in range(1, len(types)):
            if types[i] == types[i - 1]:
                pytest.fail(
                    f"Consecutive same-type swings at positions {i-1},{i}: "
                    f"{types[i-1]},{types[i]} — dedup should have cleaned this"
                )
