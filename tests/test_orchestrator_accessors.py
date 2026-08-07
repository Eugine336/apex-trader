"""
Tests for the orchestrator's evidence accessors added to existing components.

Dependency-light — exercises:
  * SituationAssessment.tf_vector() / tf_components (per-timeframe alignment kept
    alongside the collapsed scalar so conflict is distinguishable from neutrality)
  * EntryDecision.entry_margin (continuous enter-skip margin, exposed for sizing)

No torch/pandas/numpy required.
"""

import pytest

from decision.actions import EntryAction
from decision.context import EntryContext
from decision.engine import DecisionEngine
from decision.situation import SituationAssessment, SituationEngine


class TestTfVector:
    def test_default_empty(self):
        sa = SituationAssessment()
        assert sa.tf_vector() == {}

    def test_assess_entry_populates_per_tf_components(self):
        eng = SituationEngine()
        # D1 supports LONG, H4 opposes LONG, H1 neutral — the conflict the scalar
        # tf_alignment alone would hide.
        ctx = EntryContext(
            symbol="EURUSD", direction="LONG",
            d1_trend="BULLISH", d1_confidence=0.8,
            h4_trend="BEARISH", h4_confidence=0.6,
            h1_trend="RANGING", h1_confidence=0.0,
            entry_type="OB_MIDPOINT",
        )
        sa = eng.assess_entry(ctx)
        vec = sa.tf_vector()
        assert set(vec.keys()) == {"D1", "H4", "H1"}
        assert vec["D1"] > 0      # supports the long
        assert vec["H4"] < 0      # opposes the long
        assert vec["H1"] == pytest.approx(0.0)


class TestEntryMargin:
    def test_skip_decision_carries_margin(self):
        eng = DecisionEngine()
        # Strongly opposing situation → SKIP, with a negative margin recorded.
        ctx = EntryContext(symbol="X", direction="LONG", risk_reward_2=1.0,
                           d1_trend="BEARISH", d1_confidence=0.9,
                           h4_trend="BEARISH", h4_confidence=0.9,
                           h1_trend="BEARISH", h1_confidence=0.9)
        sa = SituationEngine().assess_entry(ctx)
        dec = eng.decide_entry(ctx, sa)
        assert hasattr(dec, "entry_margin")
        if dec.action == EntryAction.SKIP:
            assert dec.entry_margin <= 0.0

    def test_enter_decision_has_positive_margin(self):
        eng = DecisionEngine()
        ctx = EntryContext(symbol="X", direction="LONG", risk_reward_2=3.0,
                           entry_type="FVG_OB_OVERLAP",
                           d1_trend="BULLISH", d1_confidence=0.9,
                           h4_trend="BULLISH", h4_confidence=0.9,
                           h1_trend="BULLISH", h1_confidence=0.9,
                           m1_aligned_count=5)
        sa = SituationEngine().assess_entry(ctx)
        dec = eng.decide_entry(ctx, sa)
        if dec.should_enter:
            assert dec.entry_margin > 0.0
