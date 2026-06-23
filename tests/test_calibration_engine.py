"""Tests for the CalibrationEngine end-to-end wiring.

Covers news-impact learning, JSON persistence round-trip, the provider hook,
and the config-flag default (calibration on, with cold-start fallback to the
hardcoded category constants until a symbol warms up).
"""

from collections import deque

import pytest

import brain.instrument_profile as ip
from brain.calibration_engine import CalibrationEngine
from brain.instrument_stats import InstrumentStats, InstrumentStatsStore
from config import AppConfig


# ── News-impact learning ──────────────────────────────────────────────────


def test_news_impact_ema_and_sensitivity():
    st = InstrumentStats(symbol="EURUSD", pip_size=0.0001)
    assert st.news_sensitivity("USD") is None          # untracked → None
    st.record_news_impact("USD", move_pips=20.0, atr_pips=10.0)
    assert st.news_sensitivity("USD") == pytest.approx(2.0)
    st.record_news_impact("USD", move_pips=10.0, atr_pips=10.0)
    # EMA folds the second (1.0) toward the first (2.0).
    assert 1.0 < st.news_sensitivity("USD") < 2.0
    # Non-positive ATR is ignored, never raises.
    st.record_news_impact("USD", 5.0, 0.0)


# ── Persistence round-trip ─────────────────────────────────────────────────


def _seed(st: InstrumentStats) -> None:
    st.atr_by_tf["M5"] = 0.0008
    st.atr_pips_by_tf["M5"] = 8.0
    st._atr_pips_window["M5"] = deque([8.0] * 20, maxlen=200)
    for s in (1.0, 1.2, 0.8):
        st.update_spread(s)
    st.record_zone_outcome(touched=True, held=True)
    st.record_structure_outcome(True)
    st.record_news_impact("USD", 15.0, 5.0)


def test_instrument_stats_roundtrip():
    st = InstrumentStats(symbol="EURUSD", pip_size=0.0001)
    _seed(st)
    rt = InstrumentStats.from_dict(st.to_dict())
    assert rt.is_calibrated("M5")
    assert rt.atr_pips("M5") == pytest.approx(8.0)
    assert rt.spread_median == pytest.approx(1.0)
    assert rt.zone_hold_rate == pytest.approx(1.0)
    assert rt.news_sensitivity("USD") == pytest.approx(3.0)


def test_store_dict_roundtrip():
    store = InstrumentStatsStore()
    _seed(store.get_or_create("EURUSD", 0.0001))
    blob = store.to_dict()
    store2 = InstrumentStatsStore()
    assert store2.load_dict(blob) == 1
    assert store2.get("EURUSD").atr_pips("M5") == pytest.approx(8.0)


# ── CalibrationEngine: writer + provider + persistence ─────────────────────


def test_engine_is_provider_and_writer():
    eng = CalibrationEngine()
    eng.update_spread("GBPUSD", 1.5, 0.0001)
    eng.record_news_impact("GBPUSD", "GBP", 15.0, 5.0)
    eng.record_zone_outcome("GBPUSD", touched=True, held=False)
    assert eng("GBPUSD") is not None                    # provider returns stats
    assert eng("NOPE") is None
    assert eng("GBPUSD").news_sensitivity("GBP") == pytest.approx(3.0)


def test_engine_save_load(tmp_path):
    path = str(tmp_path / "calib.json")
    eng = CalibrationEngine(state_path=path)
    eng.record_news_impact("GBPUSD", "GBP", 15.0, 5.0)
    assert eng.save() is True
    eng2 = CalibrationEngine(state_path=path)
    assert eng2.load() == 1
    assert eng2("GBPUSD").news_sensitivity("GBP") == pytest.approx(3.0)


def test_load_missing_file_is_zero(tmp_path):
    eng = CalibrationEngine(state_path=str(tmp_path / "nope.json"))
    assert eng.load() == 0


# ── Config flag default + provider opt-in ──────────────────────────────────


def test_calibration_enabled_by_default():
    # Per-symbol calibration is now wired into AppConfig and enabled by default.
    # Cold-start behaviour is still unchanged (the provider falls back to the
    # hardcoded category constants until a symbol warms up).
    assert AppConfig().calibration.enabled is True


def test_get_profile_unchanged_without_provider():
    # No provider registered → hardcoded category constants.
    ip.set_stats_provider(None)
    assert ip.get_profile("ZZZ_UNKNOWN") is ip._FOREX_PROFILE


def test_get_profile_uses_engine_when_registered():
    eng = CalibrationEngine()
    # Warm a symbol so it is calibrated at a known ATR.
    st = eng.store.get_or_create("ZZZ_UNKNOWN", 0.0001)
    st.atr_pips_by_tf["M5"] = 40.0
    st._atr_pips_window["M5"] = deque([40.0] * 20, maxlen=200)
    try:
        ip.set_stats_provider(eng)
        prof = ip.get_profile("ZZZ_UNKNOWN")
        assert prof is not ip._FOREX_PROFILE
        assert prof.fvg_proximity_pips == pytest.approx(24.0)   # 0.60 * 40
    finally:
        ip.set_stats_provider(None)
