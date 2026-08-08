"""Tests for the self-calibrating instrument layer.

Covers ``brain.instrument_stats`` (universal ATR-normalised formulas + rolling
stats) and ``brain.instrument_profile`` (ATR-derived geometry with cold-start
fallback to the category constants).
"""

from collections import deque

import pytest

from brain.instrument_stats import (
    InstrumentStats,
    InstrumentStatsStore,
    atr_regime_ratio,
    classify_atr_regime,
    is_spread_wide,
    spread_ratio,
)
import brain.instrument_profile as ip


# ── Universal ATR-normalised formulas ─────────────────────────────────────


def test_spread_ratio_and_wide():
    assert spread_ratio(1.5, 10.0) == pytest.approx(0.15)
    assert spread_ratio(1.0, 0.0) == 0.0          # unknown ATR → 0, never raises
    assert is_spread_wide(2.0, 10.0) is True       # 0.20 > 0.15
    assert is_spread_wide(1.0, 10.0) is False      # 0.10 < 0.15


def test_atr_regime_classification_is_universal():
    assert classify_atr_regime(18.0, 10.0) == "EXPANSION"
    assert classify_atr_regime(5.0, 10.0) == "COMPRESSION"
    assert classify_atr_regime(11.0, 10.0) == "NORMAL"
    assert atr_regime_ratio(20.0, 10.0) == pytest.approx(2.0)
    assert atr_regime_ratio(5.0, 0.0) == 1.0       # unknown baseline → neutral


# ── Rolling stats ──────────────────────────────────────────────────────────


def test_spread_median_and_p95():
    st = InstrumentStats(symbol="EURUSD", pip_size=0.0001)
    for s in (1.0, 1.2, 0.8, 1.5, 5.0):
        st.update_spread(s)
    assert st.spread_median == pytest.approx(1.2)
    assert st.spread_p95 >= st.spread_median


def test_structure_and_zone_outcomes():
    st = InstrumentStats(symbol="X")
    assert st.structure_reliability is None        # untracked → None
    for held in (True, True, False, True):
        st.record_structure_outcome(held)
    assert st.structure_reliability == pytest.approx(0.75)
    st.record_zone_outcome(touched=True, held=True)
    st.record_zone_outcome(touched=True, held=False)
    st.record_zone_outcome(touched=False)
    assert st.zone_hit_rate == pytest.approx(2 / 3)
    assert st.zone_hold_rate == pytest.approx(0.5)


def test_calibration_gate():
    st = InstrumentStats(symbol="X", pip_size=0.0001)
    assert st.is_calibrated("M5") is False
    st.atr_pips_by_tf["M5"] = 8.0
    st._atr_pips_window["M5"] = deque([8.0] * 20, maxlen=200)
    assert st.is_calibrated("M5") is True
    assert st.median_atr_pips("M5") == pytest.approx(8.0)
    assert st.atr_regime("M5") == "NORMAL"


def test_store_get_or_create():
    store = InstrumentStatsStore()
    a = store.get_or_create("eurusd", pip_size=0.0001)
    b = store.get_or_create("EURUSD")
    assert a is b                                   # case-insensitive key
    assert store.get("EURUSD") is a
    assert store.get("MISSING") is None


# ── Self-calibrating profile derivation ───────────────────────────────────


class _FakeStats:
    def __init__(self, atr, calibrated=True):
        self._atr = atr
        self._cal = calibrated

    def is_calibrated(self, tf="M5"):
        return self._cal

    def atr_pips(self, tf="M5"):
        return self._atr


def test_derive_falls_back_when_uncalibrated():
    base = ip._FOREX_PROFILE
    assert ip.derive_profile(base, None) is base
    assert ip.derive_profile(base, _FakeStats(0.0, calibrated=False)) is base


def test_derive_reproduces_forex_constants_at_anchor_atr():
    # The universal ratios are anchored to forex at ~5-pip M5 ATR, so derivation
    # reproduces today's constants — behaviour-preserving at the anchor.
    d = ip.derive_profile(ip._FOREX_PROFILE, _FakeStats(5.0))
    assert d.fvg_proximity_pips == pytest.approx(3.0)
    assert d.min_risk_pips == pytest.approx(5.0)
    assert d.ob_min_impulse_pips == pytest.approx(10.0)


def test_derive_scales_with_volatility():
    # A high-ATR instrument gets proportionally wider geometry from ONE formula.
    d = ip.derive_profile(ip._FOREX_PROFILE, _FakeStats(40.0))
    assert d.fvg_proximity_pips == pytest.approx(24.0)
    assert d.min_risk_pips == pytest.approx(40.0)
    # Enable flags and structural fields are not ATR-derived → unchanged.
    assert d.currency_strength_enabled == ip._FOREX_PROFILE.currency_strength_enabled
    assert d.swing_lookback == ip._FOREX_PROFILE.swing_lookback
    assert d.min_entry_score == ip._FOREX_PROFILE.min_entry_score


def test_derive_floors_at_half_the_prior():
    d = ip.derive_profile(ip._FOREX_PROFILE, _FakeStats(0.5))
    assert d.fvg_proximity_pips == pytest.approx(ip._FOREX_PROFILE.fvg_proximity_pips * 0.5)


def test_get_profile_provider_is_opt_in():
    # No provider → category constants, unchanged behaviour.
    ip.set_stats_provider(None)
    assert ip.get_profile("ZZZ_UNKNOWN") is ip._FOREX_PROFILE
    try:
        ip.set_stats_provider(lambda s: _FakeStats(40.0))
        gp = ip.get_profile("ZZZ_UNKNOWN")
        assert gp is not ip._FOREX_PROFILE
        assert gp.fvg_proximity_pips == pytest.approx(24.0)
    finally:
        ip.set_stats_provider(None)
    assert ip.get_profile("ZZZ_UNKNOWN") is ip._FOREX_PROFILE


# ── Wick-to-body ratio tracking ────────────────────────────────────────────


class _Bar:
    """Minimal OHLC row supporting both attribute and item access via a dict."""

    def __init__(self, o, h, l, c):
        self._d = {"open": o, "high": h, "low": l, "close": c}

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


class _Frame:
    """Tiny stand-in for a candle frame exposing ``.iloc[-1]``."""

    def __init__(self, bar):
        self._bar = bar

    @property
    def iloc(self):
        return [self._bar]


def _feed_wick_body(st, ratio, n):
    """Push ``n`` M5 candles whose wick-to-body ratio is exactly ``ratio``.

    Body is fixed at 1.0 (open=100, close=101); the wicks are extended so the
    full range equals ``ratio`` → ``(high - low) / |close - open| == ratio``.
    Requires ``ratio >= 1`` so the range can contain the body.
    """
    half = (ratio - 1.0) / 2.0
    for _ in range(n):
        bar = _Bar(o=100.0, h=101.0 + half, l=100.0 - half, c=101.0)
        st._roll_wick_body(_Frame(bar))


def test_wick_body_ratio_needs_min_samples():
    st = InstrumentStats(symbol="X", pip_size=0.0001)
    assert st.wick_body_ratio() is None
    _feed_wick_body(st, 3.0, 49)
    assert st.wick_body_ratio() is None          # below the 50-sample gate
    _feed_wick_body(st, 3.0, 1)
    assert st.wick_body_ratio() == pytest.approx(3.0)


def test_wick_body_ratio_skips_doji():
    st = InstrumentStats(symbol="X")
    doji = _Frame(_Bar(o=100.0, h=100.5, l=99.5, c=100.0))  # body == 0
    st._roll_wick_body(doji)
    assert len(st._wick_body_samples) == 0       # doji ignored, no divide-by-zero


def test_wick_body_ratio_persists_round_trip():
    st = InstrumentStats(symbol="X", pip_size=0.0001)
    _feed_wick_body(st, 2.5, 60)
    blob = st.to_dict()
    assert blob["wick_body_ratio"] == pytest.approx(2.5)
    restored = InstrumentStats.from_dict(blob)
    assert restored.wick_body_ratio() == pytest.approx(2.5)


# ── Self-calibrating swing_lookback ────────────────────────────────────────


class _StatsWBR:
    """Calibrated stats stub that also exposes a wick-to-body ratio."""

    def __init__(self, atr, wbr):
        self._atr = atr
        self._wbr = wbr

    def is_calibrated(self, tf="M5"):
        return True

    def atr_pips(self, tf="M5"):
        return self._atr

    def wick_body_ratio(self):
        return self._wbr


def test_swing_lookback_unchanged_without_wbr_method():
    # Stats stub lacking wick_body_ratio → swing_lookback keeps the category default.
    d = ip.derive_profile(ip._FOREX_PROFILE, _FakeStats(5.0))
    assert d.swing_lookback == ip._FOREX_PROFILE.swing_lookback


def test_swing_lookback_unchanged_when_wbr_none():
    d = ip.derive_profile(ip._FOREX_PROFILE, _StatsWBR(5.0, None))
    assert d.swing_lookback == ip._FOREX_PROFILE.swing_lookback


def test_swing_lookback_widens_for_wicky_instrument():
    # ratio 3.5 vs reference 2.0 → scale 1.75 → round(5 * 1.75) = 9
    d = ip.derive_profile(ip._FOREX_PROFILE, _StatsWBR(5.0, 3.5))
    assert d.swing_lookback == 9


def test_swing_lookback_tightens_for_clean_instrument():
    # ratio 1.4 vs reference 2.0 → scale 0.7 → round(5 * 0.7) = 4 (then >= 3 floor)
    d = ip.derive_profile(ip._FOREX_PROFILE, _StatsWBR(5.0, 1.4))
    assert d.swing_lookback == 4


def test_swing_lookback_bounds_enforced():
    # Extreme wickiness clamps the scale at 2.0x then the absolute cap at 15.
    hi = ip.derive_profile(ip._INDEX_PROFILE, _StatsWBR(10.0, 50.0))
    assert 3 <= hi.swing_lookback <= 15
    assert hi.swing_lookback == min(15, ip._INDEX_PROFILE.swing_lookback * 2)
    # Extreme cleanliness clamps the scale at 0.6x then the absolute floor at 3.
    lo = ip.derive_profile(ip._FOREX_PROFILE, _StatsWBR(5.0, 0.01))
    assert lo.swing_lookback == max(3, round(ip._FOREX_PROFILE.swing_lookback * 0.6))
