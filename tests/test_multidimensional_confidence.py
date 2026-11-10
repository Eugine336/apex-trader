"""Advisor-reported confidence fields are descriptive evidence only.

The Brain does not synthesize, calibrate, aggregate, or act on confidence.
Expected return, execution costs, opportunity activation and risk admission own
the origination decision.
"""

from types import SimpleNamespace
import time

from cognition.brain import CognitiveBrain
from test_support.market_geometry import add_market_geometry
from cognition.contracts import DecisionType, Evidence, MarketState
from cognition.evidence_adapters import evidence_from_reasoning
from llm.reasoner import LLMOpinion
from llm.reasoning_orchestrator import EngineOpinion


def _brain():
    return CognitiveBrain(reasoner=None, min_confidence_to_act=0.55,
                          reward_r_default=2.0,
                          min_expected_value=0.0)


def _decide(opinion, uncertainty=0.1):
    # V-002 — origination requires a first-class, ACTIVATED opportunity. These
    # tests exercise the multidimensional-confidence act/sizing path, so attach a
    # matching ACTIVE opportunity when the opinion carries none (the confidence
    # dimensions and EV still come from the opinion itself).
    if not getattr(opinion, "opportunities", None) and opinion.direction in ("LONG", "SHORT"):
        opinion.opportunities = [{
            "id": "auto", "direction": opinion.direction, "state": "ACTIVE",
            "candidate_family": "continuation", "horizon": "M5",
            "quality": opinion.confidence, "asymmetry": opinion.confidence,
            "evidence_strength": opinion.confidence,
        }]
        opinion.preferred_opportunity_id = "auto"
    ms = MarketState(symbol="BTCUSD")
    for domain in ("structure", "momentum", "liquidity"):
        ms.add(Evidence(
            source_module=f"test_{domain}", domain=domain, symbol="BTCUSD",
            observation="test market evidence", confidence=0.9, uncertainty=0.1,
            relevance_horizon_seconds=600.0,
        ))
    ms.add(Evidence(
        source_module="reasoning_engine.test_council", domain="reasoning",
        symbol="BTCUSD", observation="verified test council coverage",
        confidence=0.9, uncertainty=0.1,
        measurements={
            "advisors_responded": 2,
            "advisors_available": 2,
            "advisors_total": 2,
        },
        relevance_horizon_seconds=600.0,
    ))
    cost_epoch = time.time()
    ms.add(Evidence(
        source_module="execution_cost.test_quote", domain="execution_quality",
        symbol="BTCUSD", observation="verified test execution cost",
        confidence=0.9, uncertainty=0.1,
        measurements={"estimated_total_cost_r": 0.05, "cost_verified": True, "cost_source": "test live quote"},
        timestamp_epoch=cost_epoch,
        source_timestamp_epoch=cost_epoch,
        source_timestamp_verified=True,
        relevance_horizon_seconds=60.0,
    ))
    # These sizing scenarios need enough calibrated EV headroom above the
    # 0.10 minimum structural payoff from this synthetic geometry, otherwise
    # both strong and weak profiles are correctly capped at the same EV size.
    add_market_geometry(ms, opinion.direction, expected_return_r=0.8)
    consolidation = ms.consolidation()
    consolidation["aggregate_uncertainty"] = uncertainty
    return _brain()._from_opinion(
        "BTCUSD", ms, consolidation, opinion, None,
    )


# Confidence dimensions are source-reported observations, not a Brain score.


def test_to_dict_preserves_only_source_reported_dimensions():
    d = LLMOpinion(
        symbol="X", direction="LONG", confidence=0.7,
        execution_confidence=0.2,
    ).to_dict()
    assert d["confidence"] == 0.7
    assert d["thesis_confidence"] is None
    assert d["execution_confidence"] == 0.2
    assert "effective_confidence" not in d


def test_single_scalar_opinion_can_only_act_on_verified_opportunity_economics():
    out = _decide(LLMOpinion(symbol="BTCUSD", direction="LONG", confidence=0.8))
    assert out.decision.questions_answered.get("expected_return_verification", "").startswith(
        "VERIFIED"
    ), out.decision.questions_answered.get("expected_return_verification")
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert abs(out.decision.confidence - 0.8) < 1e-9
    assert out.campaign.desired_exposure > 0.0
    assert "reasoning strength is excluded" in out.decision.questions_answered["sizing_rationale"]
    assert out.decision.questions_answered["confidence_authority"].startswith("descriptive advisor metadata")


def test_source_reported_execution_dimension_does_not_change_ev_or_exposure():
    strong = _decide(LLMOpinion(symbol="BTCUSD", direction="LONG", confidence=0.9))
    weaker = _decide(LLMOpinion(
        symbol="BTCUSD", direction="LONG", confidence=0.9,
        execution_confidence=0.2,
    ))
    assert strong.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert weaker.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert weaker.decision.expected_value == strong.decision.expected_value
    assert weaker.campaign.desired_exposure == strong.campaign.desired_exposure
    assert weaker.decision.questions_answered["advisor_confidence_dimensions"]["execution"] == 0.2


def test_council_evidence_carries_raw_dimensions_without_effective_score():
    cog = LLMOpinion(
        symbol="BTCUSD", direction="LONG", confidence=0.8,
        execution_confidence=0.3, opportunity="sweep-reclaim long",
    ).to_dict()
    op = EngineOpinion(
        engine="cerebras", direction="LONG", confidence=0.8,
        rationale="r", latency_ms=10.0, cognition=cog,
    )
    measurements = evidence_from_reasoning(
        "BTCUSD", SimpleNamespace(opinions=[op]),
    )[0].measurements
    assert measurements["execution_confidence"] == 0.3
    assert "effective_confidence" not in measurements

def test_unknown_or_insufficient_advisor_quorum_uses_native_qualification(monkeypatch):
    """An available but under-covered advisor cannot suppress native reasoning."""
    for responded in (None, 1):
        brain = CognitiveBrain(
            reasoner=SimpleNamespace(
                available=True,
                reason=lambda symbol, evidence, now=None: LLMOpinion(
                    symbol=symbol, direction="LONG", confidence=0.99,
                ),
            ),
            min_advisors_for_action=2,
        )
        analysis = object()
        hypotheses = [object()]
        marker = object()
        native_calls = []

        monkeypatch.setattr(
            brain, "_native_reasoning",
            lambda market_state, consolidation, symbol, now=None: (analysis, hypotheses),
        )

        def native_qualification(*args, **kwargs):
            native_calls.append((args, kwargs))
            return marker

        monkeypatch.setattr(brain, "_originate_independent", native_qualification)
        monkeypatch.setattr(
            brain, "_from_opinion",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("under-quorum advisor opinion must not reach origination")
            ),
        )

        market_state = MarketState(symbol="BTCUSD")
        if responded is not None:
            market_state.add(Evidence(
                source_module="reasoning_engine.partial_council",
                domain="reasoning",
                symbol="BTCUSD",
                observation="only one advisor responded",
                confidence=1.0,
                uncertainty=0.0,
                measurements={
                    "advisors_responded": responded,
                    "advisors_available": 2,
                    "advisors_total": 2,
                },
            ))

        output = brain.reason(market_state)
        assert output is marker
        assert len(native_calls) == 1
        assert native_calls[0][0][0] == "BTCUSD"
        assert native_calls[0][1]["analysis"] is analysis
        assert native_calls[0][1]["advisor_status"] == "available"
