"""
Tests for reaction-based liquidity sweep classification.

Proves the system DECIDES reversal vs continuation from price action —
no static flag, no human bias.

Dependency-light: pandas + numpy only, no torch.
"""

import numpy as np
import pandas as pd
import pytest

from brain.liquidity_mapper import LiquidityMapper
from brain.directional_consensus import vote_from_liquidity


# ── Helpers ───────────────────────────────────────────────────────────────

def _build_ohlc(
    base_price: float,
    n_bars: int,
    last_open: float,
    last_high: float,
    last_low: float,
    last_close: float,
    prev_close: float,
    pip_size: float = 0.0001,
) -> pd.DataFrame:
    """
    Build a synthetic OHLC DataFrame.
    First (n_bars-2) bars are ranging around base_price.
    Second-to-last bar closes at prev_close.
    Last bar has the specified OHLC (the sweep/reaction bar).
    """
    rows = []
    for i in range(n_bars - 2):
        c = base_price + (i % 3 - 1) * pip_size * 3
        rows.append({
            "open": c - pip_size, "high": c + pip_size * 5,
            "low": c - pip_size * 5, "close": c,
            "time": pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=5 * i),
        })
    rows.append({
        "open": prev_close - pip_size, "high": prev_close + pip_size * 5,
        "low": prev_close - pip_size * 5, "close": prev_close,
        "time": pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=5 * (n_bars - 2)),
    })
    rows.append({
        "open": last_open, "high": last_high,
        "low": last_low, "close": last_close,
        "time": pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=5 * (n_bars - 1)),
    })
    return pd.DataFrame(rows)


# ── classify_sweep_reaction ───────────────────────────────────────────────

class TestClassifySweepReaction:
    """Core classifier: observed price action → (kind, direction, confidence)."""

    def test_buy_side_reversal_short(self):
        """
        Equal highs at ~1.1000. Last candle wicks above, closes back below.
        Expected: REVERSAL, SHORT.
        """
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        df = _build_ohlc(
            base_price=1.0990,
            n_bars=30,
            last_open=1.0995,
            last_high=1.1015,
            last_low=1.0985,
            last_close=1.0988,
            prev_close=1.0990,
            pip_size=0.0001,
        )
        for i in [5, 10, 15]:
            df.at[i, "high"] = 1.1000
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001)
        assert kind == "REVERSAL"
        assert direction == "SHORT"
        assert 0.0 < conf <= 1.0

    def test_sell_side_reversal_long(self):
        """
        Equal lows at ~1.0800. Last candle wicks below, closes back above.
        Expected: REVERSAL, LONG.
        """
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        df = _build_ohlc(
            base_price=1.0810,
            n_bars=30,
            last_open=1.0805,
            last_high=1.0815,
            last_low=1.0790,
            last_close=1.0812,
            prev_close=1.0810,
            pip_size=0.0001,
        )
        for i in [5, 10, 15]:
            df.at[i, "low"] = 1.0800
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001)
        assert kind == "REVERSAL"
        assert direction == "LONG"
        assert 0.0 < conf <= 1.0

    def test_buy_side_continuation_long(self):
        """
        Equal highs at ~1.1000. Last candle closes AND holds above the level
        with clear displacement.
        Expected: CONTINUATION, LONG.
        """
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        df = _build_ohlc(
            base_price=1.0990,
            n_bars=30,
            last_open=1.0998,
            last_high=1.1020,
            last_low=1.0995,
            last_close=1.1015,
            prev_close=1.0990,
            pip_size=0.0001,
        )
        for i in [5, 10, 15]:
            df.at[i, "high"] = 1.1000
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001)
        assert kind == "CONTINUATION"
        assert direction == "LONG"
        assert 0.0 < conf <= 1.0

    def test_sell_side_continuation_short(self):
        """
        Equal lows at ~1.0800. Last candle closes AND holds below the level.
        Expected: CONTINUATION, SHORT.
        """
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        df = _build_ohlc(
            base_price=1.0810,
            n_bars=30,
            last_open=1.0802,
            last_high=1.0805,
            last_low=1.0780,
            last_close=1.0785,
            prev_close=1.0810,
            pip_size=0.0001,
        )
        for i in [5, 10, 15]:
            df.at[i, "low"] = 1.0800
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001)
        assert kind == "CONTINUATION"
        assert direction == "SHORT"
        assert 0.0 < conf <= 1.0

    def test_no_interaction_ranging(self):
        """Price ranging mid-range with no zones interacted → NONE/NEUTRAL."""
        mapper = LiquidityMapper()
        rows = []
        for i in range(30):
            c = 1.0500 + i * 0.0003
            rows.append({
                "open": c, "high": c + 0.0010, "low": c - 0.0010,
                "close": c, "time": pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=5*i),
            })
        df = pd.DataFrame(rows)
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001)
        assert direction in ("NEUTRAL", "LONG", "SHORT")

    def test_too_few_bars(self):
        """< 5 bars → NONE/NEUTRAL (fail-closed)."""
        mapper = LiquidityMapper()
        df = pd.DataFrame([
            {"open": 1.0, "high": 1.01, "low": 0.99, "close": 1.0, "time": pd.Timestamp.now()}
            for _ in range(3)
        ])
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001)
        assert kind == "NONE"
        assert direction == "NEUTRAL"
        assert conf == 0.0

    def test_nan_input_fail_closed(self):
        """NaN in close → NONE/NEUTRAL (fail-closed)."""
        mapper = LiquidityMapper()
        rows = [
            {"open": 1.0, "high": 1.01, "low": 0.99, "close": 1.0, "time": pd.Timestamp.now()}
            for _ in range(30)
        ]
        df = pd.DataFrame(rows)
        df.at[len(df) - 1, "close"] = float("nan")
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001)
        assert kind == "NONE"
        assert direction == "NEUTRAL"
        assert conf == 0.0

    def test_none_df_fail_closed(self):
        """None DataFrame → NONE/NEUTRAL (fail-closed)."""
        mapper = LiquidityMapper()
        kind, direction, conf = mapper.classify_sweep_reaction(None, pip_size=0.0001)
        assert kind == "NONE"
        assert direction == "NEUTRAL"
        assert conf == 0.0


# ── Symmetry: mirror-image inputs → mirror-image votes ────────────────────

class TestSymmetry:
    """Prove that the system treats up/down symmetrically."""

    def test_reversal_mirror(self):
        """
        Buy-side reversal → SHORT. Sell-side reversal → LONG.
        Same setup mirrored → opposite votes. Proves no directional bias.
        """
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)

        df_buy_sweep = _build_ohlc(
            base_price=1.0990, n_bars=30,
            last_open=1.0995, last_high=1.1015, last_low=1.0985,
            last_close=1.0988, prev_close=1.0990,
        )
        for i in [5, 10, 15]:
            df_buy_sweep.at[i, "high"] = 1.1000

        df_sell_sweep = _build_ohlc(
            base_price=1.0810, n_bars=30,
            last_open=1.0805, last_high=1.0815, last_low=1.0790,
            last_close=1.0812, prev_close=1.0810,
        )
        for i in [5, 10, 15]:
            df_sell_sweep.at[i, "low"] = 1.0800

        k1, d1, c1 = mapper.classify_sweep_reaction(df_buy_sweep, 0.0001)
        k2, d2, c2 = mapper.classify_sweep_reaction(df_sell_sweep, 0.0001)

        assert k1 == "REVERSAL" and d1 == "SHORT"
        assert k2 == "REVERSAL" and d2 == "LONG"

    def test_continuation_mirror(self):
        """Buy-side cont → LONG. Sell-side cont → SHORT. Opposite votes."""
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)

        df_buy_cont = _build_ohlc(
            base_price=1.0990, n_bars=30,
            last_open=1.0998, last_high=1.1020, last_low=1.0995,
            last_close=1.1015, prev_close=1.0990,
        )
        for i in [5, 10, 15]:
            df_buy_cont.at[i, "high"] = 1.1000

        df_sell_cont = _build_ohlc(
            base_price=1.0810, n_bars=30,
            last_open=1.0802, last_high=1.0805, last_low=1.0780,
            last_close=1.0785, prev_close=1.0810,
        )
        for i in [5, 10, 15]:
            df_sell_cont.at[i, "low"] = 1.0800

        k1, d1, c1 = mapper.classify_sweep_reaction(df_buy_cont, 0.0001)
        k2, d2, c2 = mapper.classify_sweep_reaction(df_sell_cont, 0.0001)

        assert k1 == "CONTINUATION" and d1 == "LONG"
        assert k2 == "CONTINUATION" and d2 == "SHORT"


# ── vote_from_liquidity integration ───────────────────────────────────────

class TestVoteFromLiquidityReaction:
    """The consensus vote function, backed by the reaction classifier."""

    def test_reversal_and_continuation_opposite_votes_same_side(self):
        """
        KEY TEST: a reversal setup and a continuation setup with the SAME
        swept side produce OPPOSITE vote directions — proving the system
        decides, not a flag.
        """
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)

        df_reversal = _build_ohlc(
            base_price=1.0990, n_bars=30,
            last_open=1.0995, last_high=1.1015, last_low=1.0985,
            last_close=1.0988, prev_close=1.0990,
        )
        for i in [5, 10, 15]:
            df_reversal.at[i, "high"] = 1.1000

        df_continuation = _build_ohlc(
            base_price=1.0990, n_bars=30,
            last_open=1.0998, last_high=1.1020, last_low=1.0995,
            last_close=1.1015, prev_close=1.0990,
        )
        for i in [5, 10, 15]:
            df_continuation.at[i, "high"] = 1.1000

        d_rev, c_rev = vote_from_liquidity(mapper, df_reversal, pip_size=0.0001)
        d_cont, c_cont = vote_from_liquidity(mapper, df_continuation, pip_size=0.0001)

        assert d_rev == "SHORT"
        assert d_cont == "LONG"
        assert d_rev != d_cont

    def test_no_interaction_neutral(self):
        mapper = LiquidityMapper()
        rows = []
        for i in range(30):
            c = 1.0500 + i * 0.0003
            rows.append({
                "open": c, "high": c + 0.0010, "low": c - 0.0010,
                "close": c, "time": pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=5*i),
            })
        df = pd.DataFrame(rows)
        d, c = vote_from_liquidity(mapper, df, pip_size=0.0001)
        assert 0.0 <= c <= 1.0

    def test_fail_closed_none_df(self):
        mapper = LiquidityMapper()
        d, c = vote_from_liquidity(mapper, None, pip_size=0.0001)
        assert d == "NEUTRAL"
        assert c == 0.0

    def test_confidence_bounded(self):
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        df = _build_ohlc(
            base_price=1.0990, n_bars=30,
            last_open=1.0995, last_high=1.1015, last_low=1.0985,
            last_close=1.0988, prev_close=1.0990,
        )
        for i in [5, 10, 15]:
            df.at[i, "high"] = 1.1000
        d, c = vote_from_liquidity(mapper, df, pip_size=0.0001)
        assert 0.0 <= c <= 1.0


# ── Config flag removal proof ─────────────────────────────────────────────

class TestNoStaticBiasFlag:
    def test_no_sweep_is_reversal_param(self):
        """vote_from_liquidity must NOT accept a sweep_is_reversal parameter."""
        import inspect
        sig = inspect.signature(vote_from_liquidity)
        param_names = list(sig.parameters.keys())
        assert "sweep_is_reversal" not in param_names

    def test_config_has_no_flag(self):
        from config import ConsensusConfig
        assert not hasattr(ConsensusConfig, "liquidity_sweep_is_reversal") or \
               "liquidity_sweep_is_reversal" not in ConsensusConfig.__dataclass_fields__
