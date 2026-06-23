"""Tests for the remaining data-path wiring (WS-2/3/4/5).

Covers:
- ``entry.m1_patterns.detect_m1_pattern`` (engulfing / pin bar).
- ``_micro_confirmation_from_event`` reaching MARKET on an aligned pattern.
- ``decision.situation.compute_in_trade_context_pressure`` (WS-3).
- ``SituationEngine.assess_entry`` OQ/EQ confidence wiring (WS-5).
- ``DecisionEngine._is_counter_htf`` honouring the zone flag (WS-5).
"""

import pandas as pd

from brain.backtest_engine import _micro_confirmation_from_event
from decision.context import EntryContext, TradeContext
from decision.engine import DecisionEngine
from decision.situation import SituationEngine, compute_in_trade_context_pressure
from entry.m1_patterns import detect_m1_pattern


def _df(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


# ── WS-2: M1 pattern detection ────────────────────────────────────────────


def test_bullish_engulfing_for_long():
    df = _df([[10.0, 10.2, 8.8, 9.0], [9.0, 11.0, 8.9, 10.8]])
    assert detect_m1_pattern(df, is_long=True) == "engulfing"


def test_bearish_engulfing_for_short():
    df = _df([[9.0, 11.2, 8.9, 10.8], [11.0, 11.1, 8.8, 9.0]])
    assert detect_m1_pattern(df, is_long=False) == "engulfing"


def test_hammer_pin_bar_for_long():
    df = _df([[10.0, 10.2, 9.0, 9.5], [10.0, 10.1, 9.0, 9.95]])
    assert detect_m1_pattern(df, is_long=True) == "pin_bar"


def test_no_pattern_returns_empty():
    df = _df([[10.0, 10.5, 9.5, 10.1], [10.0, 10.6, 9.6, 10.2]])
    assert detect_m1_pattern(df, is_long=True) == ""


def test_pattern_handles_thin_data():
    assert detect_m1_pattern(None, is_long=True) == ""
    assert detect_m1_pattern(_df([[1.0, 1.0, 1.0, 1.0]]), is_long=True) == ""


def test_micro_confirmation_market_on_pattern():
    # No structural event, but an aligned candle pattern → MARKET fast-path.
    mc, mode = _micro_confirmation_from_event("NONE", "LONG", "engulfing")
    assert (mc, mode) == ("engulfing", "MARKET")
    # Event still wins and reports choch_bos.
    mc2, mode2 = _micro_confirmation_from_event("BOS_BULLISH", "LONG", "pin_bar")
    assert (mc2, mode2) == ("choch_bos", "MARKET")
    # Nothing confirms → PENDING (unchanged behaviour).
    assert _micro_confirmation_from_event("NONE", "LONG", "") == ("", "PENDING")


# ── WS-3: in-trade context pressure ───────────────────────────────────────


def test_context_pressure_counts_opposing_signals():
    tc = TradeContext(
        direction="BUY",
        d1_event="BOS_BEARISH",
        h4_event="CHOCH_BEARISH",
        scan_direction="SHORT",
        scan_score=90,
        fast_opposition_streak=4,
        pnl_pips=-10.0,
        original_risk_pips=10.0,
    )
    pressure, structural, details = compute_in_trade_context_pressure(tc)
    assert structural == 2          # D1 + H4 breaks
    assert pressure == 5            # + scan + fast-opp + loss
    assert any("D1" in d for d in details)


def test_context_pressure_zero_when_aligned():
    tc = TradeContext(
        direction="BUY",
        d1_event="BOS_BULLISH",
        scan_direction="LONG",
        scan_score=90,
    )
    assert compute_in_trade_context_pressure(tc)[0] == 0


# ── WS-5: OQ/EQ entry confidence wiring ───────────────────────────────────


def _entry(**kw):
    base = dict(direction="LONG", entry_type="FVG_MIDPOINT",
                d1_trend="BULLISH", d1_confidence=0.8)
    base.update(kw)
    return EntryContext(**base)


def test_high_entry_quality_boosts_confidence():
    se = SituationEngine()
    base = se.assess_entry(_entry())
    hi = se.assess_entry(_entry(eq=9.0, oq=8.0))
    assert hi.read_confidence > base.read_confidence
    assert hi.confidence_components.get("quality_eq") == 9.0


def test_low_opportunity_quality_reduces_confidence():
    se = SituationEngine()
    base = se.assess_entry(_entry())
    lo = se.assess_entry(_entry(oq=1.0))
    assert lo.read_confidence < base.read_confidence


def test_unknown_quality_leaves_confidence_untouched():
    se = SituationEngine()
    base = se.assess_entry(_entry())
    assert "quality_adj" not in base.confidence_components


# ── WS-5: counter-trend flag honoured by the engine ───────────────────────


def test_is_counter_htf_honours_zone_flag():
    # H4 supports the long, but the multi-HTF zone flag says counter-trend.
    ctx = EntryContext(direction="LONG", h4_trend="BULLISH", is_counter_trend=True)
    assert DecisionEngine._is_counter_htf(ctx) is True
    # No flag + supportive H4 → not counter (unchanged behaviour).
    ctx2 = EntryContext(direction="LONG", h4_trend="BULLISH", is_counter_trend=False)
    assert DecisionEngine._is_counter_htf(ctx2) is False
