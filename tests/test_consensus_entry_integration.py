"""Tests for directional-consensus integration into the entry decision.

Verifies the analysis-plane vote panel reaches the DecisionEngine as a real
decision dimension — supporting, opposing, or vetoing an entry — WITHOUT being
collapsed into a single scalar gate. The full per-module breakdown is preserved
on the SituationAssessment.
"""

from brain.directional_consensus import Vote
from decision.context import EntryContext
from decision.situation import SituationEngine
from decision.engine import DecisionEngine


def _ctx(**overrides) -> EntryContext:
    defaults = dict(
        symbol="EURUSD",
        direction="LONG",
        scan_score=75,
        entry_type="FVG_MIDPOINT",
        entry_price=1.0850,
        stop_loss=1.0820,
        tp1=1.0895,
        tp2=1.0940,
        risk_reward_2=3.0,
        risk_pips=30.0,
        d1_trend="BULLISH",
        d1_confidence=0.70,
        h4_trend="BULLISH",
        h4_confidence=0.70,
        h1_trend="BULLISH",
        h1_confidence=0.65,
        m1_trend="BULLISH",
        m1_aligned_count=3,
        session_name="LONDON",
        session_tradeable=True,
        regime="TRENDING",
    )
    defaults.update(overrides)
    return EntryContext(**defaults)


def test_no_votes_is_inert():
    """An empty panel must not move the consensus dimension at all."""
    se = SituationEngine()
    sa = se.assess_entry(_ctx(consensus_votes=[]))
    assert sa.consensus_alignment == 0.0
    assert sa.consensus_vector() == {}


def test_aligned_panel_supports_entry():
    """A panel agreeing with the trade direction lifts enter score + conviction."""
    se = SituationEngine()
    de = DecisionEngine()

    votes = [
        Vote("structure", "LONG", 0.8, 3.0),
        Vote("volume", "LONG", 0.7, 1.0),
        Vote("wyckoff", "LONG", 0.6, 1.5),
    ]
    aligned = se.assess_entry(_ctx(consensus_votes=votes))
    bare = se.assess_entry(_ctx(consensus_votes=[]))

    assert aligned.consensus_alignment > 0.5
    assert set(aligned.consensus_vector()["for"]) == {"structure", "volume", "wyckoff"}
    # Conviction is higher with an aligned panel than without one.
    assert de.compute_conviction(aligned) > de.compute_conviction(bare)


def test_opposing_panel_adds_skip_pressure():
    """A panel opposing the trade direction must push the decision toward SKIP."""
    se = SituationEngine()
    de = DecisionEngine()

    opposing = [
        Vote("structure", "SHORT", 0.8, 3.0),
        Vote("volume", "SHORT", 0.7, 1.0),
        Vote("wyckoff", "SHORT", 0.7, 1.5),
        Vote("momentum", "SHORT", 0.7, 1.0),
    ]
    sa_opp = se.assess_entry(_ctx(consensus_votes=opposing))
    assert sa_opp.consensus_alignment < -0.5

    dec_opp = de.decide_entry(_ctx(consensus_votes=opposing), sa_opp)
    dec_bare = de.decide_entry(_ctx(consensus_votes=[]), se.assess_entry(_ctx(consensus_votes=[])))
    # The opposing panel lowers the entry margin relative to no panel.
    assert dec_opp.entry_margin < dec_bare.entry_margin


def test_high_authority_opposition_vetoes():
    """A high-authority module opposing with confidence is decisive (veto)."""
    se = SituationEngine()
    de = DecisionEngine()

    # currency_strength (high authority) strongly opposes a LONG.
    votes = [
        Vote("structure", "LONG", 0.6, 3.0),
        Vote("currency_strength", "SHORT", 0.85, 2.0),
    ]
    sa = se.assess_entry(_ctx(consensus_votes=votes))
    assert "currency_strength" in sa.consensus_vector()["high_authority_oppose"]

    dec = de.decide_entry(_ctx(consensus_votes=votes), sa)
    assert dec.action.value == "SKIP", dec.reason


def test_consensus_aware_disabled_is_inert():
    """With consensus_aware=False the panel must not change the decision."""
    se = SituationEngine()
    de_on = DecisionEngine()
    de_off = DecisionEngine(consensus_aware=False)

    opposing = [
        Vote("structure", "SHORT", 0.8, 3.0),
        Vote("currency_strength", "SHORT", 0.85, 2.0),
    ]
    sa = se.assess_entry(_ctx(consensus_votes=opposing))
    margin_on = de_on.decide_entry(_ctx(consensus_votes=opposing), sa).entry_margin
    margin_off = de_off.decide_entry(_ctx(consensus_votes=opposing), sa).entry_margin
    # Disabling the feature removes the consensus SKIP pressure → higher margin.
    assert margin_off > margin_on
