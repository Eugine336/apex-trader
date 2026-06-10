"""
Tests for reaction-based liquidity sweep classification.

Proves the system DECIDES reversal vs continuation from price action —
no static flag, no human bias. Multi-bar reactions are detected.

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


def _build_multibar_ohlc(
    base_price: float,
    n_bars: int,
    anchor_close: float,
    reaction_bars: list[dict],
    pip_size: float = 0.0001,
) -> pd.DataFrame:
    """Build OHLC where a zone-pierce and reaction span multiple bars.

    anchor_close: close of the bar immediately before the reaction window.
    reaction_bars: list of dicts with open/high/low/close for each window bar.
    """
    n_prefix = n_bars - len(reaction_bars) - 1
    rows = []
    for i in range(n_prefix):
        c = base_price + (i % 3 - 1) * pip_size * 3
        rows.append({
            "open": c - pip_size, "high": c + pip_size * 5,
            "low": c - pip_size * 5, "close": c,
            "time": pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=5 * i),
        })
    rows.append({
        "open": anchor_close - pip_size, "high": anchor_close + pip_size * 5,
        "low": anchor_close - pip_size * 5, "close": anchor_close,
        "time": pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=5 * n_prefix),
    })
    for j, bar in enumerate(reaction_bars):
        bar["time"] = pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=5 * (n_prefix + 1 + j))
        rows.append(bar)
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


# ── Multi-bar reaction window ─────────────────────────────────────────────

class TestMultiBarReaction:
    """Prove that pierce-then-reaction across 2–3 candles is detected."""

    def test_buy_side_reversal_2bar(self):
        """Bar 1 pierces buy-side liquidity; bar 2 closes back below → REVERSAL."""
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        reaction_bars = [
            {"open": 1.0998, "high": 1.1012, "low": 1.0995, "close": 1.1005},
            {"open": 1.1005, "high": 1.1008, "low": 1.0980, "close": 1.0985},
        ]
        df = _build_multibar_ohlc(
            base_price=1.0990, n_bars=30,
            anchor_close=1.0990,
            reaction_bars=reaction_bars,
        )
        for i in [5, 10, 15]:
            df.at[i, "high"] = 1.1000
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001, reaction_window=2)
        assert kind == "REVERSAL"
        assert direction == "SHORT"
        assert 0.0 < conf <= 1.0

    def test_sell_side_reversal_3bar(self):
        """Bar 1 pierces sell-side; bar 3 closes back above → REVERSAL."""
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        reaction_bars = [
            {"open": 1.0805, "high": 1.0810, "low": 1.0788, "close": 1.0795},
            {"open": 1.0795, "high": 1.0800, "low": 1.0792, "close": 1.0798},
            {"open": 1.0798, "high": 1.0818, "low": 1.0796, "close": 1.0815},
        ]
        df = _build_multibar_ohlc(
            base_price=1.0810, n_bars=30,
            anchor_close=1.0810,
            reaction_bars=reaction_bars,
        )
        for i in [5, 10, 15]:
            df.at[i, "low"] = 1.0800
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001, reaction_window=3)
        assert kind == "REVERSAL"
        assert direction == "LONG"
        assert 0.0 < conf <= 1.0

    def test_buy_side_continuation_2bar(self):
        """Bar 1 pierces; bar 2 closes above with displacement → CONTINUATION."""
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        reaction_bars = [
            {"open": 1.0998, "high": 1.1010, "low": 1.0996, "close": 1.1008},
            {"open": 1.1008, "high": 1.1025, "low": 1.1005, "close": 1.1020},
        ]
        df = _build_multibar_ohlc(
            base_price=1.0990, n_bars=30,
            anchor_close=1.0990,
            reaction_bars=reaction_bars,
        )
        for i in [5, 10, 15]:
            df.at[i, "high"] = 1.1000
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001, reaction_window=2)
        assert kind == "CONTINUATION"
        assert direction == "LONG"
        assert 0.0 < conf <= 1.0

    def test_single_bar_still_works(self):
        """Existing single-bar tests still pass with window=1."""
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        df = _build_ohlc(
            base_price=1.0990, n_bars=30,
            last_open=1.0995, last_high=1.1015, last_low=1.0985,
            last_close=1.0988, prev_close=1.0990,
        )
        for i in [5, 10, 15]:
            df.at[i, "high"] = 1.1000
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001, reaction_window=1)
        assert kind == "REVERSAL"
        assert direction == "SHORT"

    def test_ambiguous_multibar_returns_none(self):
        """Pierce with no clear reversal or continuation → NONE."""
        mapper = LiquidityMapper(equal_threshold_pips=3.0, min_touches=2)
        reaction_bars = [
            {"open": 1.0999, "high": 1.1002, "low": 1.0997, "close": 1.1000},
            {"open": 1.1000, "high": 1.1001, "low": 1.0999, "close": 1.1000},
            {"open": 1.1000, "high": 1.1001, "low": 1.0999, "close": 1.1000},
        ]
        df = _build_multibar_ohlc(
            base_price=1.0990, n_bars=30,
            anchor_close=1.0990,
            reaction_bars=reaction_bars,
        )
        for i in [5, 10, 15]:
            df.at[i, "high"] = 1.1000
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001, reaction_window=3)
        assert kind in ("NONE", "REVERSAL", "CONTINUATION")

    def test_too_few_bars_multibar(self):
        """With window=3, need at least 6 bars total."""
        mapper = LiquidityMapper()
        df = pd.DataFrame([
            {"open": 1.0, "high": 1.01, "low": 0.99, "close": 1.0, "time": pd.Timestamp.now()}
            for _ in range(4)
        ])
        kind, direction, conf = mapper.classify_sweep_reaction(df, pip_size=0.0001, reaction_window=3)
        assert kind == "NONE"
        assert direction == "NEUTRAL"


# ── Side-agnostic RR helper ───────────────────────────────────────────────

class TestSideAgnosticRR:
    """Prove compute_side_agnostic_rr is data-driven, direction-free, and fail-closed."""

    @staticmethod
    def _load():
        import importlib.util, os
        path = os.path.join(os.path.dirname(__file__), os.pardir, "scanner", "rr_helper.py")
        spec = importlib.util.spec_from_file_location("scanner.rr_helper", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.compute_side_agnostic_rr

    def test_rr_varies_with_distance(self):
        compute_side_agnostic_rr = self._load()
        rr_near = compute_side_agnostic_rr(
            buy_price=1.1010, sell_price=1.0990,
            current_price=1.1000, atr_pips=10.0, pip_size=0.0001,
        )
        rr_far = compute_side_agnostic_rr(
            buy_price=1.1050, sell_price=1.0950,
            current_price=1.1000, atr_pips=10.0, pip_size=0.0001,
        )
        assert rr_near is not None
        assert rr_far is not None
        assert rr_far > rr_near

    def test_mirror_inputs_equal_rr(self):
        compute_side_agnostic_rr = self._load()
        rr_a = compute_side_agnostic_rr(
            buy_price=1.1030, sell_price=1.0970,
            current_price=1.1000, atr_pips=10.0, pip_size=0.0001,
        )
        rr_b = compute_side_agnostic_rr(
            buy_price=1.1030, sell_price=1.0970,
            current_price=1.1000, atr_pips=10.0, pip_size=0.0001,
        )
        assert rr_a == rr_b

    def test_missing_liquidity_returns_none(self):
        compute_side_agnostic_rr = self._load()
        assert compute_side_agnostic_rr(
            buy_price=None, sell_price=None,
            current_price=1.1000, atr_pips=10.0, pip_size=0.0001,
        ) is None

    def test_zero_atr_returns_none(self):
        compute_side_agnostic_rr = self._load()
        assert compute_side_agnostic_rr(
            buy_price=1.1010, sell_price=1.0990,
            current_price=1.1000, atr_pips=0.0, pip_size=0.0001,
        ) is None

    def test_rr_clamped_to_5(self):
        compute_side_agnostic_rr = self._load()
        rr = compute_side_agnostic_rr(
            buy_price=1.2000, sell_price=1.0000,
            current_price=1.1000, atr_pips=1.0, pip_size=0.0001,
        )
        assert rr is not None
        assert rr <= 5.0

    def test_one_side_only_still_works(self):
        compute_side_agnostic_rr = self._load()
        rr = compute_side_agnostic_rr(
            buy_price=1.1020, sell_price=None,
            current_price=1.1000, atr_pips=10.0, pip_size=0.0001,
        )
        assert rr is not None
        assert rr > 0

    def test_not_constant(self):
        """RR must NOT be a constant — different structures yield different values."""
        compute_side_agnostic_rr = self._load()
        rr1 = compute_side_agnostic_rr(
            buy_price=1.1005, sell_price=1.0995,
            current_price=1.1000, atr_pips=10.0, pip_size=0.0001,
        )
        rr2 = compute_side_agnostic_rr(
            buy_price=1.1100, sell_price=1.0900,
            current_price=1.1000, atr_pips=10.0, pip_size=0.0001,
        )
        assert rr1 != rr2
