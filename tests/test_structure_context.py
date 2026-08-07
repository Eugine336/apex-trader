"""Characterization tests for the extracted structure / M1 micro-context readers.

Phase K decomposition: these pin the exact behaviour of the helpers lifted out of
``event_driven_bootstrap.py`` into ``brain/structure_context.py``. The module is
loaded directly (bypassing the heavy ``brain`` package __init__) so the readers
are verifiable with no third-party dependencies. ``_compute_m1_micro``'s
fail-safe default path is exercised without pandas/broker by feeding a stub whose
data is unavailable.
"""

import importlib.util
import sys
import types
from pathlib import Path

# Stub loguru (absent offline) before importing the module under test.
if "loguru" not in sys.modules:
    _lg = types.ModuleType("loguru")
    _logger = types.SimpleNamespace(**{
        m: (lambda *a, **k: None)
        for m in ("debug", "info", "warning", "error", "critical", "success",
                  "trace", "exception")
    })
    _logger.bind = lambda *a, **k: _logger
    _logger.opt = lambda *a, **k: _logger
    _lg.logger = _logger
    sys.modules["loguru"] = _lg

_PATH = Path(__file__).resolve().parents[1] / "brain" / "structure_context.py"
_spec = importlib.util.spec_from_file_location("_structure_context_uut", _PATH)
sc = importlib.util.module_from_spec(_spec)
sys.modules["_structure_context_uut"] = sc
_spec.loader.exec_module(sc)


class _Trend:
    def __init__(self, value):
        self.value = value


class _SA:
    """Minimal StructureAnalysis double."""
    def __init__(self, trend=None, confidence=0.0, last_event=None,
                 swing_high=None, swing_low=None):
        self.trend = trend
        self.confidence = confidence
        self.last_event = last_event
        self.swing_high = swing_high
        self.swing_low = swing_low


# ── _struct_trend_conf ──────────────────────────────────────────────────────────

def test_trend_conf_reads_enum_value_and_confidence():
    struct = {"H4": _SA(trend=_Trend("BULLISH"), confidence=0.8)}
    assert sc._struct_trend_conf(struct, "H4") == ("BULLISH", 0.8)


def test_trend_conf_accepts_plain_string_trend():
    struct = {"H1": _SA(trend="BEARISH", confidence=0.5)}
    assert sc._struct_trend_conf(struct, "H1") == ("BEARISH", 0.5)


def test_trend_conf_missing_tf_is_unknown():
    assert sc._struct_trend_conf({}, "D1") == ("UNKNOWN", 0.0)


# ── _struct_event ───────────────────────────────────────────────────────────────

def test_event_reads_enum_value():
    struct = {"H4": _SA(last_event=_Trend("BOS_BULLISH"))}
    assert sc._struct_event(struct, "H4") == "BOS_BULLISH"


def test_event_missing_tf_or_no_event_is_none_string():
    assert sc._struct_event({}, "H4") == "NONE"
    assert sc._struct_event({"H4": _SA(last_event=None)}, "H4") == "NONE"


# ── _struct_swings ──────────────────────────────────────────────────────────────

def test_swings_reads_levels():
    struct = {"H4": _SA(swing_high=1.18, swing_low=1.06)}
    assert sc._struct_swings(struct, "H4") == (1.18, 1.06)


def test_swings_missing_tf_is_none_pair():
    assert sc._struct_swings({}, "D1") == (None, None)


# ── _micro_confirmation_from_event ──────────────────────────────────────────────

def test_micro_bos_choch_aligned_is_market():
    assert sc._micro_confirmation_from_event("BOS_BULLISH", "LONG") == ("choch_bos", "MARKET")
    assert sc._micro_confirmation_from_event("CHOCH_BEARISH", "SELL") == ("choch_bos", "MARKET")


def test_micro_misaligned_event_is_pending():
    assert sc._micro_confirmation_from_event("BOS_BEARISH", "LONG") == ("", "PENDING")
    assert sc._micro_confirmation_from_event("NONE", "SHORT") == ("", "PENDING")


def test_micro_pattern_confirms_when_no_structural_event():
    assert sc._micro_confirmation_from_event("NONE", "LONG", "engulfing") == ("engulfing", "MARKET")
    assert sc._micro_confirmation_from_event("NONE", "SHORT", "pin_bar") == ("pin_bar", "MARKET")
    assert sc._micro_confirmation_from_event("NONE", "LONG", "") == ("", "PENDING")


# ── _compute_m1_micro (fail-safe default path, no pandas/broker) ────────────────

def test_compute_m1_micro_returns_safe_defaults_when_data_unavailable():
    class _PM:
        def fetch_market_data(self, symbol, tfs, n):
            return None                       # no data → safe defaults
    out = sc._compute_m1_micro(_PM(), "EURUSD", "LONG", 0.0001)
    assert out == {"m1_aligned_count": 0, "m1_event": "NONE",
                   "m1_trend": "UNKNOWN", "m1_pattern": ""}


def test_compute_m1_micro_never_raises_on_broker_fault():
    class _PM:
        def fetch_market_data(self, symbol, tfs, n):
            raise RuntimeError("broker down")
    out = sc._compute_m1_micro(_PM(), "EURUSD", "SHORT", 0.0001)
    assert out["m1_aligned_count"] == 0 and out["m1_event"] == "NONE"
