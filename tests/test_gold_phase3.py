"""APEX TRADER — Phase 3 Gold (XAUUSD) specialist tuning.

Verifies the four brain-level Gold tuning changes (scoring/filtering only — no
entry/risk/execution changes):
  1. decision_core — H1/H4-emphasised evidence weights for Gold.
  2. wyckoff_engine — wider accumulation range + boosted spring/building-cause
     confidence for Gold.
  3. session_engine — NewsGuard admits Gold-moving keyword events for XAUUSD.
  4. outcome_feedback — single-instrument feedback tuning + symbol-filtered
     accuracy.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from brain.decision_core import (
    _EVIDENCE_WEIGHTS,
    _GOLD_EVIDENCE_WEIGHTS,
    _active_weights,
)
from brain.outcome_feedback import OutcomeFeedback
from brain.session_engine import NewsEvent, NewsGuard
from brain.structure_engine import Trend
from brain.wyckoff_engine import WyckoffEngine


# ── 1. decision_core — Gold evidence weights ─────────────────────────────────

def test_active_weights_gold_for_xauusd():
    assert _active_weights("XAUUSD") is _GOLD_EVIDENCE_WEIGHTS
    assert _active_weights("xauusd") is _GOLD_EVIDENCE_WEIGHTS


def test_active_weights_default_for_others():
    assert _active_weights("EURUSD") is _EVIDENCE_WEIGHTS
    assert _active_weights(None) is _EVIDENCE_WEIGHTS
    assert _active_weights() is _EVIDENCE_WEIGHTS


# ── 2. wyckoff_engine — Gold-tuned phases ────────────────────────────────────

def _flat_range_df(n: int = 30, high: float = 2440.0, low: float = 2400.0,
                   close: float = 2400.0) -> pd.DataFrame:
    # range_width = (2440-2400)/2400 ≈ 0.0167 — between the FX (0.015) and
    # Gold (0.025) range thresholds.
    return pd.DataFrame({
        "open": [close] * n,
        "high": [high] * n,
        "low": [low] * n,
        "close": [close] * n,
    })


def test_wyckoff_range_threshold_wider_for_gold():
    df = _flat_range_df()
    fx = WyckoffEngine(pip_size=0.0001)
    gold = WyckoffEngine(pip_size=0.01, symbol="XAUUSD")
    assert fx._range_state(df)[2] is False
    assert gold._range_state(df)[2] is True


def test_wyckoff_spring_confidence_boosted_for_gold():
    vol = SimpleNamespace(climax_detected=False, confirmation_bias="NEUTRAL")
    fx = WyckoffEngine(pip_size=0.0001)
    gold = WyckoffEngine(pip_size=0.01, symbol="XAUUSD")
    _, sub_fx, _, fx_conf = fx._classify(
        in_range=True, trend=Trend.RANGING, trend_return=0.0,
        spring=True, upthrust=False, volume=vol,
    )
    _, sub_gold, _, gold_conf = gold._classify(
        in_range=True, trend=Trend.RANGING, trend_return=0.0,
        spring=True, upthrust=False, volume=vol,
    )
    assert sub_fx == "SPRING" and sub_gold == "SPRING"
    assert fx_conf == 0.82
    assert gold_conf == 0.90


def test_wyckoff_building_cause_boosted_for_gold():
    vol = SimpleNamespace(climax_detected=False, confirmation_bias="NEUTRAL")
    fx = WyckoffEngine(pip_size=0.0001)
    gold = WyckoffEngine(pip_size=0.01, symbol="XAUUSD")
    fx_res = fx._classify(
        in_range=True, trend=Trend.RANGING, trend_return=0.0,
        spring=False, upthrust=False, volume=vol,
    )
    gold_res = gold._classify(
        in_range=True, trend=Trend.RANGING, trend_return=0.0,
        spring=False, upthrust=False, volume=vol,
    )
    assert fx_res[1] == "BUILDING_CAUSE" and fx_res[3] == 0.6
    assert gold_res[1] == "BUILDING_CAUSE" and gold_res[3] == 0.70


# ── 3. session_engine — NewsGuard Gold keywords ──────────────────────────────

def _evt(title: str, currency: str, minutes: int, now: datetime,
         impact: str = "HIGH") -> NewsEvent:
    return NewsEvent(
        title=title,
        currency=currency,
        impact=impact,
        time_utc=now + timedelta(minutes=minutes),
        minutes_away=minutes,
        direction="BEFORE",
    )


def test_newsguard_gold_keyword_blocks_for_xauusd():
    now = datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc)
    guard = NewsGuard()
    # EUR-tagged event (not in XAUUSD's USD affected set) but with Gold-moving
    # keywords in the title — must still block Gold.
    treasury = _evt("US Treasury yields spike on geopolitical risk", "EUR", 5, now)
    guard._fetch_events = lambda _utc: [treasury]
    status = guard.check(["XAUUSD"], utc_now=now)
    assert status.is_clear is False
    assert treasury in status.events_nearby


def test_newsguard_keyword_ignored_without_gold():
    now = datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc)
    guard = NewsGuard()
    treasury = _evt("US Treasury yields spike", "EUR", 5, now)
    guard._fetch_events = lambda _utc: [treasury]
    # GBPUSD → affected {GBP, USD}; EUR event with no gold expansion is ignored.
    status = guard.check(["GBPUSD"], utc_now=now)
    assert status.is_clear is True
    assert treasury not in status.events_nearby


# ── 4. outcome_feedback — single-instrument tuning + symbol filter ────────────

def test_gold_specialist_reduces_lookback(tmp_path):
    cfg = SimpleNamespace(
        enabled=True,
        journal_path=str(tmp_path / "fb.jsonl"),
        gold_specialist=True,
    )
    fb = OutcomeFeedback(cfg)
    assert fb._lookback == 100
    assert fb._max_records == 5000


def test_no_gold_specialist_keeps_defaults(tmp_path):
    cfg = SimpleNamespace(
        enabled=True,
        journal_path=str(tmp_path / "fb.jsonl"),
    )
    fb = OutcomeFeedback(cfg)
    assert fb._lookback == 300
    assert fb._max_records == 20000


def test_module_accuracy_symbol_filter(tmp_path):
    cfg = SimpleNamespace(enabled=True, journal_path=str(tmp_path / "fb.jsonl"))
    fb = OutcomeFeedback(cfg)
    fb.record_entry(
        "g1", {"symbol": "XAUUSD", "votes": {"wyckoff": ["LONG", 0.9]}, "horizon": "SWING"},
    )
    fb.record_outcome("g1", {"won": True, "pnl_r": 2.0})
    fb.record_entry(
        "e1", {"symbol": "EURUSD", "votes": {"wyckoff": ["SHORT", 0.5]}, "horizon": "SWING"},
    )
    fb.record_outcome("e1", {"won": False, "pnl_r": -1.0})

    gold_only = fb.module_accuracy(symbol_filter="XAUUSD")
    assert gold_only["total_trades"] == 1
    assert gold_only["overall_win_rate"] == 1.0

    all_trades = fb.module_accuracy()
    assert all_trades["total_trades"] == 2
