"""
Tests for the Regime Detection Engine (adaptive/regime_detector.py, L7) and its
TunerAgent adapter (adaptive.tunable_adapters.RegimeDetectorTunable).

Covers feature extraction + rule-cascade classification (trending / ranging /
volatile / quiet), hysteresis (a single spike never re-labels the market),
per-pair independence, confidence scoring, transition logging, SQLite
persistence round-trips, the cold-start UNKNOWN no-op, the disabled no-op,
regime-weight nudges + the soft should_trade opinion, the dashboard snapshot
shape, and Tunable protocol compliance.

Deterministic: feeds fixed close streams and uses temp SQLite DBs so nothing
touches the real data/ dir.
"""

import os
import tempfile

import pytest

from config import RegimeDetectionConfig
from adaptive.regime_detector import (
    RegimeDetector,
    RegimeState,
    REGIME_TRENDING_UP,
    REGIME_TRENDING_DOWN,
    REGIME_RANGING,
    REGIME_VOLATILE,
    REGIME_QUIET,
    REGIME_UNKNOWN,
)
from adaptive.tunable import Tunable, TuneContext, TuneFrequency
from adaptive.tunable_adapters import RegimeDetectorTunable


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except OSError:
            pass


def _detector(tmp_db, **kw):
    params = dict(
        db_path=tmp_db,
        enabled=True,
        lookback_bars=20,
        hysteresis_bars=2,
        volatility_short_window=3,
        volatility_long_window=10,
    )
    params.update(kw)
    return RegimeDetector(**params)


# Deterministic close streams (20 bars each) for each regime.
_UPTREND = [100.0 + i for i in range(20)]
_DOWNTREND = [120.0 - i for i in range(20)]
_VOLATILE = [100.0] * 17 + [100.0, 140.0, 60.0]
_QUIET = (
    [100, 103, 98, 102, 97, 101, 99, 100, 101, 99]
    + [100, 102, 98, 101, 99, 100, 100, 100, 100, 100]
)
_RANGING = [100.0 if i % 2 == 0 else 101.0 for i in range(20)]


def _commit(det, pair, closes, n=3):
    """Feed the same series n times so hysteresis commits the candidate."""
    state = None
    for _ in range(n):
        state = det.update(pair, closes)
    return state


# ── Classification ──────────────────────────────────────────────────────────


def test_uptrend_detected(tmp_db):
    det = _detector(tmp_db)
    st = _commit(det, "EURUSD", _UPTREND)
    assert st.regime == REGIME_TRENDING_UP
    assert st.confidence > 0.5
    det.close()


def test_downtrend_detected(tmp_db):
    det = _detector(tmp_db)
    st = _commit(det, "EURUSD", _DOWNTREND)
    assert st.regime == REGIME_TRENDING_DOWN
    det.close()


def test_ranging_detected(tmp_db):
    det = _detector(tmp_db)
    st = _commit(det, "EURUSD", _RANGING)
    assert st.regime == REGIME_RANGING
    det.close()


def test_volatile_detected(tmp_db):
    det = _detector(tmp_db)
    st = _commit(det, "EURUSD", _VOLATILE)
    assert st.regime == REGIME_VOLATILE
    det.close()


def test_quiet_detected(tmp_db):
    det = _detector(tmp_db)
    st = _commit(det, "EURUSD", _QUIET)
    assert st.regime == REGIME_QUIET
    det.close()


# ── Hysteresis ──────────────────────────────────────────────────────────────


def test_hysteresis_requires_consecutive_bars(tmp_db):
    det = _detector(tmp_db, hysteresis_bars=3)
    # Two trending observations are not enough to flip from UNKNOWN.
    det.update("EURUSD", _UPTREND)
    st = det.update("EURUSD", _UPTREND)
    assert st.regime == REGIME_UNKNOWN
    # Third consecutive trending observation commits the flip.
    st = det.update("EURUSD", _UPTREND)
    assert st.regime == REGIME_TRENDING_UP
    det.close()


def test_single_spike_does_not_flip(tmp_db):
    det = _detector(tmp_db, hysteresis_bars=3)
    _commit(det, "EURUSD", _UPTREND, n=3)
    assert det.get_regime_label("EURUSD") == REGIME_TRENDING_UP
    # One contradicting observation must not re-label the committed regime.
    det.update("EURUSD", _RANGING)
    assert det.get_regime_label("EURUSD") == REGIME_TRENDING_UP
    det.close()


# ── Per-pair independence ───────────────────────────────────────────────────


def test_per_pair_independence(tmp_db):
    det = _detector(tmp_db)
    _commit(det, "EURUSD", _UPTREND)
    _commit(det, "GBPUSD", _RANGING)
    assert det.get_regime_label("EURUSD") == REGIME_TRENDING_UP
    assert det.get_regime_label("GBPUSD") == REGIME_RANGING
    det.close()


# ── Confidence ──────────────────────────────────────────────────────────────


def test_confidence_in_unit_range(tmp_db):
    det = _detector(tmp_db)
    st = _commit(det, "EURUSD", _UPTREND)
    assert 0.0 <= st.confidence <= 1.0
    det.close()


# ── Transition logging ──────────────────────────────────────────────────────


def test_transition_logged(tmp_db):
    det = _detector(tmp_db)
    _commit(det, "EURUSD", _UPTREND)
    state = det.get_state()
    assert any(t["new_regime"] == REGIME_TRENDING_UP for t in state["transitions"])
    det.close()


# ── Persistence ─────────────────────────────────────────────────────────────


def test_persistence_roundtrip(tmp_db):
    det = _detector(tmp_db)
    _commit(det, "EURUSD", _UPTREND)
    det.close()
    reopened = _detector(tmp_db)
    assert reopened.get_regime_label("EURUSD") == REGIME_TRENDING_UP
    reopened.close()


# ── Cold start / disabled ───────────────────────────────────────────────────


def test_cold_start_unknown(tmp_db):
    det = _detector(tmp_db)
    assert det.get_regime_label("NEWPAIR") == REGIME_UNKNOWN
    assert det.get_regime("NEWPAIR").confidence == 0.0
    det.close()


def test_short_series_unknown(tmp_db):
    det = _detector(tmp_db)
    st = det.update("EURUSD", [100.0, 100.5])  # below the min window
    assert st.regime == REGIME_UNKNOWN
    det.close()


def test_disabled_is_noop(tmp_db):
    det = _detector(tmp_db, enabled=False)
    st = det.update("EURUSD", _UPTREND)
    assert st.regime == REGIME_UNKNOWN
    assert det.get_regime_weights("EURUSD") == {}
    ok, _reason = det.should_trade("EURUSD")
    assert ok is True
    det.close()


# ── Advisory outputs ────────────────────────────────────────────────────────


def test_regime_weights_nudge_trend_modules(tmp_db):
    det = _detector(tmp_db)
    _commit(det, "EURUSD", _UPTREND)
    weights = det.get_regime_weights("EURUSD")
    # Trend-following modules nudged up, reversion modules nudged down.
    assert weights.get("structure", 0.0) > 1.0
    assert weights.get("order_block", 2.0) < 1.0
    det.close()


def test_should_trade_sits_out_quiet(tmp_db):
    det = _detector(tmp_db)
    _commit(det, "EURUSD", _QUIET)
    ok, reason = det.should_trade("EURUSD")
    assert ok is False
    assert "quiet" in reason.lower()
    det.close()


def test_should_trade_allows_trend(tmp_db):
    det = _detector(tmp_db)
    _commit(det, "EURUSD", _UPTREND)
    ok, _reason = det.should_trade("EURUSD")
    assert ok is True
    det.close()


# ── Dashboard snapshot ──────────────────────────────────────────────────────


def test_dashboard_state_shape(tmp_db):
    det = _detector(tmp_db)
    _commit(det, "EURUSD", _UPTREND)
    state = det.get_state()
    for key in ("enabled", "pair_count", "regime_distribution", "pairs",
                "transitions", "performance", "thresholds"):
        assert key in state
    assert state["pair_count"] == 1
    assert state["pairs"][0]["pair"] == "EURUSD"
    det.close()


def test_regime_context_dict(tmp_db):
    det = _detector(tmp_db)
    _commit(det, "EURUSD", _UPTREND)
    ctx = det.get_regime_context()
    assert "EURUSD" in ctx
    assert ctx["EURUSD"]["regime"] == REGIME_TRENDING_UP
    det.close()


def test_record_performance_surfaces(tmp_db):
    det = _detector(tmp_db)
    _commit(det, "EURUSD", _UPTREND)
    det.record_performance("EURUSD", 1.5)
    det.record_performance("EURUSD", -0.5)
    perf = {p["regime"]: p for p in det.get_state()["performance"]}
    assert REGIME_TRENDING_UP in perf
    assert perf[REGIME_TRENDING_UP]["trades"] == 2
    det.close()


# ── Tunable adapter ─────────────────────────────────────────────────────────


def test_tunable_protocol_compliance(tmp_db):
    det = _detector(tmp_db)
    adapter = RegimeDetectorTunable(det)
    assert isinstance(adapter, Tunable)
    assert adapter.tunable_name == "regime_detector"
    assert adapter.frequency == TuneFrequency.ON_DEMAND
    params = adapter.get_current_params()
    assert "trending_threshold" in params
    ok, _ = adapter.validate_params(params)
    assert ok is True
    res = adapter.tune(TuneContext(force=True))
    assert res.success and res.skipped
    det.close()


def test_tunable_rejects_bad_params(tmp_db):
    det = _detector(tmp_db)
    adapter = RegimeDetectorTunable(det)
    ok, _reason = adapter.validate_params({"trending_threshold": 1.5})
    assert ok is False
    ok, _reason = adapter.validate_params({"lookback_bars": 0})
    assert ok is False
    det.close()


def test_apply_params_roundtrip(tmp_db):
    det = _detector(tmp_db)
    det.apply_params({"trending_threshold": 0.8, "hysteresis_bars": 7})
    assert abs(det.trending_threshold - 0.8) < 1e-9
    assert det.hysteresis_bars == 7
    det.close()


# ── Config validation ───────────────────────────────────────────────────────


def test_config_defaults_active():
    cfg = RegimeDetectionConfig()
    assert cfg.enabled is True


def test_config_rejects_bad_windows():
    with pytest.raises(ValueError):
        RegimeDetectionConfig(volatility_short_window=50, volatility_long_window=10)
    with pytest.raises(ValueError):
        RegimeDetectionConfig(trending_threshold=2.0)
