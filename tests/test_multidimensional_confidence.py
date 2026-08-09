"""Multidimensional confidence (Part XXV) — decompose the single scalar into
thesis / opportunity / timing / execution and let the Brain ACT on the profile.

A great thesis with poor execution or bad timing is not an act-now trade. The
Brain now blends to an EFFECTIVE conviction = the WEAKEST dimension, which drives
EV, the act gate AND sizing. A legacy reply carrying only the overall scalar is
unchanged (every dimension resolves to it) — zero regression.
"""

from types import SimpleNamespace

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, MarketState
from cognition.evidence_adapters import evidence_from_reasoning
from cognition.expected_value import effective_confidence
from llm.reasoner import LLMOpinion
from llm.reasoning_orchestrator import EngineOpinion


def _brain():
    return CognitiveBrain(reasoner=None, min_confidence_to_act=0.55,
                          max_uncertainty_to_act=0.6, reward_r_default=2.0,
                          min_expected_value=0.0)


def _decide(opinion, uncertainty=0.1):
    return _brain()._from_opinion(
        "BTCUSD", MarketState(symbol="BTCUSD"),
        {"aggregate_uncertainty": uncertainty}, opinion, None,
    )


# ── the blend ───────────────────────────────────────────────────────────────

def test_effective_confidence_scalar_only_is_unchanged():
    assert effective_confidence(SimpleNamespace(confidence=0.8)) == 0.8
    assert effective_confidence({"confidence": 0.42}) == 0.42


def test_effective_confidence_is_the_weakest_dimension():
    op = LLMOpinion(symbol="X", direction="LONG", confidence=0.8,
                    thesis_confidence=0.8, opportunity_confidence=0.75,
                    timing_confidence=0.8, execution_confidence=0.3)
    assert abs(effective_confidence(op, fallback=0.8) - 0.3) < 1e-9


def test_to_dict_resolves_dims_and_effective():
    # execution supplied (0.2); the rest absent ⇒ resolve to the overall 0.7.
    d = LLMOpinion(symbol="X", direction="LONG", confidence=0.7,
                   execution_confidence=0.2).to_dict()
    assert d["thesis_confidence"] == 0.7 and d["timing_confidence"] == 0.7
    assert d["execution_confidence"] == 0.2
    assert d["effective_confidence"] == 0.2


# ── zero regression: legacy single-scalar opinion behaves exactly as before ──

def test_single_scalar_opinion_opens_campaign_unchanged():
    out = _decide(LLMOpinion(symbol="BTCUSD", direction="LONG", confidence=0.8))
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert abs(out.decision.confidence - 0.8) < 1e-9
    assert abs(out.campaign.desired_exposure - 0.8) < 1e-9


# ── weak execution: WAIT (thesis preserved), never a hard reject ─────────────

def test_weak_execution_waits_not_rejects():
    out = _decide(LLMOpinion(symbol="BTCUSD", direction="LONG", confidence=0.8,
                             execution_confidence=0.2))
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    rr = out.decision.risk_rationale.lower()
    assert "waiting" in rr and "execution" in rr
    assert abs(out.decision.confidence - 0.2) < 1e-9   # honest effective conviction
    assert out.direction == "FLAT"


# ── moderate execution: still acts, but sizes DOWN ──────────────────────────

def test_weak_execution_sizes_down_when_it_still_acts():
    strong = _decide(LLMOpinion(symbol="BTCUSD", direction="LONG", confidence=0.9))
    weaker = _decide(LLMOpinion(symbol="BTCUSD", direction="LONG", confidence=0.9,
                                execution_confidence=0.6))
    assert strong.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert weaker.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert weaker.campaign.desired_exposure < strong.campaign.desired_exposure
    assert abs(weaker.campaign.desired_exposure - 0.6) < 1e-9
    assert "execution 0.60" in weaker.decision.questions_answered["confidence_profile"]


# ── council carries each advisor's confidence PROFILE to the Brain ──────────

def test_council_evidence_carries_confidence_profile():
    cog = LLMOpinion(symbol="BTCUSD", direction="LONG", confidence=0.8,
                     execution_confidence=0.3, opportunity="sweep-reclaim long").to_dict()
    op = EngineOpinion(engine="cerebras", direction="LONG", confidence=0.8,
                       rationale="r", latency_ms=10.0, cognition=cog)
    m = evidence_from_reasoning("BTCUSD", SimpleNamespace(opinions=[op]))[0].measurements
    assert m["thesis_confidence"] == 0.8
    assert m["execution_confidence"] == 0.3
    assert m["effective_confidence"] == 0.3
