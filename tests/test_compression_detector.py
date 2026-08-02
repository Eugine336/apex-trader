"""
Tests for the global compression detector (brain/compression_detector.py).

Coverage:
  * classify_state — deterministic classification for each of the four
    MarketStates (TRENDING / RANGING / COMPRESSING / EXPANDING) plus the
    threshold boundaries and the compression-before-expansion invariant.
  * percentile_rank — insufficient-data sentinel, all-ties, min/max, NaN warm-up.
  * compression_score — the squeeze-intensity mapping.
  * bollinger_band_width — volatility monotonicity sanity.
  * CompressionDetector.update — end-to-end state classification on synthetic
    OHLC for each state, expansion detection AFTER a compression, multi-timeframe
    aggregation, and the unknown-symbol / untracked-timeframe / insufficient-data
    guards.
  * Per-instrument tuning — InstrumentProfile override vs EntryConfig fallback,
    and the new profile/config default fields.

Deterministic: fixed RNG seeds and hand-built candle series; no network or disk.
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from brain.compression_detector import (
    CompressionDetector,
    MarketState,
    TimeframeState,
    bollinger_band_width,
    classify_state,
    compression_score,
    percentile_rank,
)
from brain.instrument_profile import get_profile
from entry.models import EntryConfig


# ── Helpers ─────────────────────────────────────────────────────────────────

def _thr(**overrides):
    base = dict(
        compression_threshold_pct=15.0,
        adx_trending_threshold=25.0,
        expansion_threshold_pct=70.0,
    )
    base.update(overrides)
    return base


def _make_df(closes, wick=0.1):
    """Build an OHLC DataFrame from a close series (open = prev close)."""
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": np.concatenate([[closes[0]], closes[:-1]]),
            "high": closes + wick,
            "low": closes - wick,
            "close": closes,
            "volume": np.ones(len(closes)),
        }
    )


# A lightweight profile stub so tests run with a small lookback (fast) while
# still exercising the profile-first tuning-resolution path.
_TEST_PROFILE = SimpleNamespace(
    compression_lookback=60,
    compression_threshold_pct=15.0,
    adx_trending_threshold=25.0,
    expansion_threshold_pct=70.0,
)


def _detector(**kw):
    params = dict(profile_lookup=lambda _s: _TEST_PROFILE, timeframes=("M5", "M15"))
    params.update(kw)
    return CompressionDetector(**params)


def _volatile_then_flat(seed=3, volatile=90, flat=40):
    """Volatile history collapsing into a dead-flat squeeze at the tail."""
    rng = np.random.default_rng(seed)
    vol = 100 + np.cumsum(rng.normal(0, 2.0, volatile))
    calm = np.full(flat, vol[-1])
    return vol, calm


def _compressing_df(seed=3):
    vol, calm = _volatile_then_flat(seed)
    return _make_df(np.concatenate([vol, calm]))


def _expansion_df(seed=3, spike=30):
    """The compressing series with a sharp volatility spike appended."""
    vol, calm = _volatile_then_flat(seed)
    rng = np.random.default_rng(seed + 1)
    breakout = calm[-1] + np.cumsum(rng.normal(0, 4.0, spike))
    return _make_df(np.concatenate([vol, calm, breakout]))


def _trending_df(seed=7, quiet=70, trend=60):
    """Quiet base, then a strong clean uptrend (high ADX, mid/high BBW)."""
    rng = np.random.default_rng(seed)
    base = 100 + rng.normal(0, 0.03, quiet)
    ramp = base[-1] + np.cumsum(np.abs(rng.normal(0.6, 0.15, trend)))
    return _make_df(np.concatenate([base, ramp]))


def _ranging_df(seed=11, n=160):
    """Choppy, non-directional oscillation with varying amplitude (low ADX)."""
    rng = np.random.default_rng(seed)
    amp = 1.0 + 0.6 * np.sin(np.arange(n) / 9.0)
    base = 100 + amp * np.sin(np.arange(n) / 2.0) + rng.normal(0, 0.25, n)
    return _make_df(base, wick=0.15)


# ── classify_state (pure) ────────────────────────────────────────────────────

def test_classify_compressing():
    state, was = classify_state(
        percentile=5.0, adx=10.0, was_compressed=False, **_thr()
    )
    assert state is MarketState.COMPRESSING
    assert was is True  # squeeze memory is now armed


def test_classify_trending():
    state, _ = classify_state(
        percentile=50.0, adx=30.0, was_compressed=False, **_thr()
    )
    assert state is MarketState.TRENDING


def test_classify_ranging():
    state, _ = classify_state(
        percentile=50.0, adx=10.0, was_compressed=False, **_thr()
    )
    assert state is MarketState.RANGING


def test_expanding_requires_prior_compression():
    # A high BBW percentile alone is NOT an expansion without a prior squeeze.
    state, _ = classify_state(
        percentile=85.0, adx=10.0, was_compressed=False, **_thr()
    )
    assert state is MarketState.RANGING

    # With squeeze memory armed, the same read is an EXPANSION, and the memory
    # is consumed so it fires exactly once.
    state2, was2 = classify_state(
        percentile=85.0, adx=10.0, was_compressed=True, **_thr()
    )
    assert state2 is MarketState.EXPANDING
    assert was2 is False


def test_compression_takes_priority_over_trend():
    # Even with a strong ADX, a bottom-percentile BBW is a squeeze, not a trend.
    state, _ = classify_state(
        percentile=3.0, adx=99.0, was_compressed=False, **_thr()
    )
    assert state is MarketState.COMPRESSING


def test_classify_insufficient_percentile_preserves_memory():
    state, was = classify_state(
        percentile=-1.0, adx=99.0, was_compressed=True, **_thr()
    )
    assert state is MarketState.RANGING
    assert was is True  # memory untouched while data is insufficient


def test_classify_threshold_boundaries():
    # percentile exactly at the compression threshold is inclusive.
    assert classify_state(
        percentile=15.0, adx=10.0, was_compressed=False, **_thr()
    )[0] is MarketState.COMPRESSING
    # percentile exactly at the expansion threshold (with memory) is inclusive.
    assert classify_state(
        percentile=70.0, adx=10.0, was_compressed=True, **_thr()
    )[0] is MarketState.EXPANDING
    # ADX exactly at the trending threshold is inclusive.
    assert classify_state(
        percentile=50.0, adx=25.0, was_compressed=False, **_thr()
    )[0] is MarketState.TRENDING


# ── percentile_rank edge cases ───────────────────────────────────────────────

def test_percentile_rank_insufficient_data_returns_sentinel():
    pct, _ = percentile_rank(pd.Series([1.0, 2.0, 3.0]), lookback=10)
    assert pct == -1.0


def test_percentile_rank_min_and_max():
    ascending = pd.Series(np.arange(100, dtype=float))  # last value is the max
    pct, cur = percentile_rank(ascending, lookback=100)
    assert cur == 99.0
    assert pct == pytest.approx(99.0)

    descending = pd.Series(np.arange(99, -1, -1, dtype=float))  # last is the min
    pct2, cur2 = percentile_rank(descending, lookback=100)
    assert cur2 == 0.0
    assert pct2 == 0.0


def test_percentile_rank_all_ties_is_zero():
    pct, cur = percentile_rank(pd.Series([5.0] * 50), lookback=50)
    assert cur == 5.0
    assert pct == 0.0  # nothing is strictly less than the current value


def test_percentile_rank_drops_nan_warmup():
    series = pd.Series([np.nan] * 20 + list(np.arange(60, dtype=float)))
    pct, cur = percentile_rank(series, lookback=40)
    assert cur == 59.0
    assert pct == pytest.approx(97.5)  # 39 of the 40 windowed values are smaller


# ── compression_score ────────────────────────────────────────────────────────

def test_compression_score_mapping():
    assert compression_score(0.0, 15.0) == 1.0     # deepest squeeze
    assert compression_score(7.5, 15.0) == pytest.approx(0.5)
    assert compression_score(15.0, 15.0) == 0.0    # top edge of the zone
    assert compression_score(50.0, 15.0) == 0.0    # outside the zone
    assert compression_score(-1.0, 15.0) == 0.0    # not computed


# ── bollinger_band_width ─────────────────────────────────────────────────────

def test_bbw_larger_when_more_volatile():
    calm = pd.Series([100.0 + 0.01 * ((-1) ** i) for i in range(80)])
    wild = pd.Series(100 + np.cumsum(np.random.default_rng(0).normal(0, 2.0, 80)))
    bbw_calm = bollinger_band_width(calm).dropna().iloc[-1]
    bbw_wild = bollinger_band_width(wild).dropna().iloc[-1]
    assert bbw_wild > bbw_calm


# ── CompressionDetector.update — end-to-end states ───────────────────────────

def test_update_detects_compression():
    det = _detector(timeframes=("M5",))
    result = det.update("EURUSD", "M5", _compressing_df())
    assert isinstance(result, TimeframeState)
    assert result.state is MarketState.COMPRESSING
    assert result.percentile <= _TEST_PROFILE.compression_threshold_pct
    assert result.compression_score > 0.0
    assert det.get_market_state("EURUSD") is MarketState.COMPRESSING
    assert det.get_compression_score("EURUSD") > 0.0


def test_update_detects_expansion_after_compression():
    det = _detector(timeframes=("M5",))
    # First close arms the squeeze memory.
    first = det.update("EURUSD", "M5", _compressing_df())
    assert first.state is MarketState.COMPRESSING
    assert first.was_compressed is True
    # A later close whose BBW breaks back up is the expansion.
    second = det.update("EURUSD", "M5", _expansion_df())
    assert second.state is MarketState.EXPANDING
    assert second.percentile >= _TEST_PROFILE.expansion_threshold_pct
    assert det.get_market_state("EURUSD") is MarketState.EXPANDING


def test_expansion_does_not_fire_without_prior_compression():
    det = _detector(timeframes=("M5",))
    # Feeding the expansion series first (no squeeze memory) must NOT expand.
    result = det.update("EURUSD", "M5", _expansion_df())
    assert result.state is not MarketState.EXPANDING


def test_update_detects_trending():
    det = _detector(timeframes=("M5",))
    result = det.update("US100", "M5", _trending_df())
    assert result.state is MarketState.TRENDING
    assert result.adx >= _TEST_PROFILE.adx_trending_threshold
    assert result.compression_score == 0.0


def test_update_detects_ranging():
    det = _detector(timeframes=("M5",))
    result = det.update("EURUSD", "M5", _ranging_df())
    assert result.state is MarketState.RANGING
    assert result.adx < _TEST_PROFILE.adx_trending_threshold


def test_update_default_profile_lookup_real_symbol():
    # Exercise the real InstrumentProfile path (default lookback of 100 bars).
    det = CompressionDetector(timeframes=("M5",))
    # 90 + 40 = 130 bars > 100 lookback, so a state is produced.
    result = det.update("EURUSD", "M5", _compressing_df(seed=3))
    assert result is not None
    assert result.state is MarketState.COMPRESSING


# ── Multi-timeframe aggregation ──────────────────────────────────────────────

def test_market_state_aggregates_by_priority():
    det = _detector()
    det.update("XAUUSD", "M15", _trending_df())      # TRENDING on M15
    det.update("XAUUSD", "M5", _compressing_df())    # COMPRESSING on M5
    # COMPRESSING outranks TRENDING in the aggregate.
    assert det.get_market_state("XAUUSD") is MarketState.COMPRESSING
    # The reported squeeze is the tightest across timeframes.
    assert det.get_compression_score("XAUUSD") == pytest.approx(
        det.get_state_detail("XAUUSD")["M5"].compression_score
    )


def test_expanding_outranks_everything():
    det = _detector()
    det.update("XAUUSD", "M15", _trending_df())
    det.update("XAUUSD", "M5", _compressing_df())
    det.update("XAUUSD", "M5", _expansion_df())
    assert det.get_market_state("XAUUSD") is MarketState.EXPANDING


# ── Guards / defaults ────────────────────────────────────────────────────────

def test_unknown_symbol_defaults_to_ranging():
    det = _detector()
    assert det.get_market_state("NEVER_SEEN") is MarketState.RANGING
    assert det.get_compression_score("NEVER_SEEN") == 0.0


def test_untracked_timeframe_is_ignored():
    det = _detector(timeframes=("M5", "M15"))
    assert det.update("EURUSD", "H1", _compressing_df()) is None
    # Nothing was stored, so the symbol stays at the neutral default.
    assert det.get_market_state("EURUSD") is MarketState.RANGING


def test_insufficient_data_returns_none():
    det = _detector(timeframes=("M5",))
    tiny = _make_df(np.linspace(100.0, 101.0, 10))
    assert det.update("EURUSD", "M5", tiny) is None


# ── Per-instrument tuning: profile override vs EntryConfig fallback ───────────

def test_param_prefers_profile_over_entry_config():
    det = CompressionDetector(
        entry_config=EntryConfig(compression_lookback=123),
        profile_lookup=lambda _s: SimpleNamespace(compression_lookback=55),
    )
    assert det._param("X", "compression_lookback", 100) == 55


def test_param_falls_back_to_entry_config():
    # Profile has no such attribute → EntryConfig default is used.
    det = CompressionDetector(
        entry_config=EntryConfig(compression_lookback=123),
        profile_lookup=lambda _s: SimpleNamespace(),
    )
    assert det._param("X", "compression_lookback", 100) == 123


def test_profile_threshold_changes_classification():
    # A ranging BBW percentile (~mid-20s) is NOT compressed at the default 15%
    # threshold but IS compressed once a profile widens the threshold — proving
    # the per-instrument tuning actually drives the classification.
    strict = _detector(
        profile_lookup=lambda _s: SimpleNamespace(
            compression_lookback=60,
            compression_threshold_pct=15.0,
            adx_trending_threshold=25.0,
            expansion_threshold_pct=70.0,
        ),
        timeframes=("M5",),
    )
    loose = _detector(
        profile_lookup=lambda _s: SimpleNamespace(
            compression_lookback=60,
            compression_threshold_pct=40.0,
            adx_trending_threshold=25.0,
            expansion_threshold_pct=70.0,
        ),
        timeframes=("M5",),
    )
    df = _ranging_df()
    strict_state = strict.update("EURUSD", "M5", df).state
    loose_state = loose.update("EURUSD", "M5", df).state
    assert strict_state is MarketState.RANGING
    assert loose_state is MarketState.COMPRESSING


def test_instrument_profile_has_compression_defaults():
    prof = get_profile("EURUSD")
    assert prof.compression_lookback == 100
    assert prof.compression_threshold_pct == 15.0
    assert prof.adx_trending_threshold == 25.0
    assert prof.expansion_threshold_pct == 70.0


def test_entry_config_has_compression_defaults():
    cfg = EntryConfig()
    assert cfg.compression_lookback == 100
    assert cfg.compression_threshold_pct == 15.0
    assert cfg.adx_trending_threshold == 25.0
    assert cfg.expansion_threshold_pct == 70.0
