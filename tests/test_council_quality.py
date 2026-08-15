"""Council reasoning-quality evaluation (Article XIX) — advisor integration is
weighted by reasoning DEPTH, not merely counted for quorum.

Covers :meth:`CognitiveBrain._evaluate_reasoning_quality`, its effect on sizing
for a shallow vs deep opinion, and the observability-only quality tracking added
to :class:`~cognition.influence.InfluenceLedger`.
"""

from types import SimpleNamespace

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, MarketState
from cognition.influence import InfluenceLedger
from llm.reasoner import LLMOpinion


def _brain():
    return CognitiveBrain(reasoner=None, min_confidence_to_act=0.55,
                          max_uncertainty_to_act=0.6, reward_r_default=2.0,
                          min_expected_value=0.0)


def _decide(opinion, uncertainty=0.1):
    # V-002 — origination requires a first-class, ACTIVATED opportunity. These
    # tests exercise the reasoning-quality sizing path, so attach a matching
    # ACTIVE opportunity when the opinion carries none (reasoning quality is still
    # scored from the opinion's own depth fields).
    if not getattr(opinion, "opportunities", None) and opinion.direction in ("LONG", "SHORT"):
        opinion.opportunities = [{
            "id": "auto", "direction": opinion.direction, "state": "ACTIVE",
            "quality": opinion.confidence, "asymmetry": opinion.confidence,
            "evidence_strength": opinion.confidence,
        }]
        opinion.preferred_opportunity_id = "auto"
    return _brain()._from_opinion(
        "BTCUSD", MarketState(symbol="BTCUSD"),
        {"aggregate_uncertainty": uncertainty}, opinion, None,
    )


def _deep_opinion(confidence=0.9):
    return LLMOpinion(
        symbol="BTCUSD", direction="LONG", confidence=confidence,
        primary_hypothesis="displacement then continuation",
        invalidation="reclaim of the origin of the move",
        expected_favorable_excursion="2R",
        expected_adverse_excursion="0.8R",
        alternative_hypotheses=["liquidity grab reversal"],
        key_uncertainty="whether volume sustains",
        what_would_change_my_mind=["volume collapses"],
        thesis_confidence=confidence, opportunity_confidence=confidence,
        timing_confidence=confidence, execution_confidence=confidence,
    )


def test_full_reasoning_scores_high():
    score, note = CognitiveBrain._evaluate_reasoning_quality(_deep_opinion())
    assert score >= 0.8
    assert "reasoning quality" in note


def test_shallow_reasoning_scores_low():
    shallow = SimpleNamespace(direction="LONG", confidence=0.9)
    score, _note = CognitiveBrain._evaluate_reasoning_quality(shallow)
    assert score <= 0.3


def test_quality_weights_confidence():
    # A shallow opinion (bare direction + confidence 0.9) is sized down: its
    # conviction is pulled toward the act threshold, while a fully reasoned
    # opinion at the same stated confidence keeps its sizing.
    shallow = _decide(LLMOpinion(symbol="BTCUSD", direction="LONG", confidence=0.9))
    deep = _decide(_deep_opinion(confidence=0.9))
    assert shallow.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert deep.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    # Shallow reasoning is sized strictly below the deep opinion, and below its
    # own stated confidence (pulled toward min_confidence_to_act = 0.55).
    assert shallow.campaign.desired_exposure < deep.campaign.desired_exposure
    assert shallow.campaign.desired_exposure < 0.9
    assert "quality" in shallow.decision.questions_answered["advisor_reasoning_quality"].lower()
    assert "quality-weighted confidence" in shallow.decision.questions_answered["sizing_rationale"]


def test_influence_ledger_quality_tracking():
    ledger = InfluenceLedger()
    # Neutral (1.0) until a source has been observed.
    assert ledger.quality_for("cerebras") == 1.0
    ledger.observe_quality("cerebras", 0.8)
    ledger.observe_quality("cerebras", 0.6)
    # Running average: (0.8 + 0.6) / 2 = 0.7.
    assert abs(ledger.quality_for("cerebras") - 0.7) < 1e-9
    # Independent per source; win/loss tracking is unaffected.
    assert ledger.quality_for("groq") == 1.0
    ledger.observe("cerebras", won=True)
    assert abs(ledger.quality_for("cerebras") - 0.7) < 1e-9
