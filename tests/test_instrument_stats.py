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
