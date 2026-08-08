"""APEX TRADER — Gold (XAUUSD) awareness across the brain layer.

Verifies the Phase 2 Gold enhancements:
  1. Session engine — XAUUSD is session-active during London/NY and earns a
     graded kill-zone bonus that folds into the session score (capped at 15).
  2. Currency strength — XAU is registered as a "currency" so Gold gets ranked
     alongside the majors via the existing RSI momentum calc.
  3. Correlation engine — XAUUSD is registered as inversely correlated with the
     USD-strength proxies (USDCHF, USDJPY).

These are filters/bonuses that sharpen entry quality, never gates.
"""

from datetime import datetime, timezone

import pandas as pd

from brain.session_engine import SessionEngine
from brain.currency_strength import CurrencyStrengthMeter, CURRENCY_PAIRS, MAJOR_CURRENCIES
from brain.correlation_engine import CorrelationEngine, INVERSE_CORRELATION


# ── Session engine — kill zones & scoring ────────────────────────────────────

def test_xauusd_active_during_london():
    engine = SessionEngine()
    ldn = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
    assert engine.is_pair_active("XAUUSD", ldn) is True


def test_gold_kill_zone_bonus_inside_zone():
    engine = SessionEngine()
    # 07:30 UTC — inside the London-open kill zone (07:00-08:30).
    t = datetime(2026, 5, 25, 7, 30, tzinfo=timezone.utc)
    assert engine.get_gold_kill_zone_bonus(t) == 5


def test_gold_kill_zone_bonus_near_zone():
    engine = SessionEngine()
    # 06:45 UTC — 15 min before the London-open kill zone.
    t = datetime(2026, 5, 25, 6, 45, tzinfo=timezone.utc)
    assert engine.get_gold_kill_zone_bonus(t) == 3


def test_gold_kill_zone_bonus_active_session_no_zone():
    engine = SessionEngine()
    # 10:00 UTC — London live but not a kill zone.
    t = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
    assert engine.get_gold_kill_zone_bonus(t) == 1


def test_gold_kill_zone_bonus_outside_sessions():
    engine = SessionEngine()
    # 03:00 UTC — Asian hours, far from any Gold kill zone.
    t = datetime(2026, 5, 25, 3, 0, tzinfo=timezone.utc)
    assert engine.get_gold_kill_zone_bonus(t) == 0


def test_gold_session_score_adds_kill_zone_bonus():
    engine = SessionEngine()
    # 13:00 UTC — NY-open kill zone during the London/NY overlap (base 10).
    t = datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc)
    base = engine.get_session_score(t)
    gold = engine.get_session_score(t, symbol="XAUUSD")
    assert base == 10
    assert gold == 15          # 10 + 5, capped at 15
    assert gold >= base


def test_gold_session_score_is_non_gating_and_capped():
    engine = SessionEngine()
    # Across a full day the Gold score stays in [0, 15] and is never worse
    # than the base session score — it is a bonus, never a penalty/gate.
    for hour in range(24):
        t = datetime(2026, 5, 25, hour, 0, tzinfo=timezone.utc)
        base = engine.get_session_score(t)
        gold = engine.get_session_score(t, symbol="XAUUSD")
        assert 0 <= gold <= 15
        assert gold >= base


def test_non_gold_symbol_unaffected():
    engine = SessionEngine()
    t = datetime(2026, 5, 25, 7, 30, tzinfo=timezone.utc)
    assert engine.get_session_score(t, symbol="EURUSD") == engine.get_session_score(t)


# ── Currency strength — XAU registration ─────────────────────────────────────

def _rising_df(n: int = 60, start: float = 2000.0, step: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame({"close": [start + i * step for i in range(n)]})


def test_xau_registered():
    assert "XAU" in MAJOR_CURRENCIES
    assert CURRENCY_PAIRS["XAUUSD"] == ("XAU", "USD")


def test_rising_gold_ranks_xau_above_usd():
    meter = CurrencyStrengthMeter()
    analysis = meter.calculate({"XAUUSD": _rising_df()})
    ranks = {r.currency: r.rank for r in analysis.rankings}
    assert "XAU" in ranks
    # Lower rank number = stronger. Rising Gold → XAU strong, USD weak.
    assert ranks["XAU"] < ranks["USD"]


# ── Correlation engine — Gold/DXY inverse correlation ────────────────────────

def test_gold_inverse_correlation_registered():
    assert "XAUUSD" in INVERSE_CORRELATION
    eng = CorrelationEngine()
    inv = eng.inverse_correlates("xauusd")
    assert "USDCHF" in inv and "USDJPY" in inv


def test_usd_proxies_point_back_to_gold():
    eng = CorrelationEngine()
    assert "XAUUSD" in eng.inverse_correlates("USDCHF")
    assert "XAUUSD" in eng.inverse_correlates("USDJPY")


def test_unregistered_pair_has_no_inverse():
    eng = CorrelationEngine()
    assert eng.inverse_correlates("EURUSD") == ()
