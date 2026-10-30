"""System 4 probability and opportunity qualification contracts."""

import time
from types import SimpleNamespace

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, Evidence, EvidenceDomain, MarketState
from test_support.market_geometry import add_market_geometry


def _calibration(*, direction="LONG", verified=True, symbol="SYNTHETIC"):
    return Evidence(
        source_module="validation.out_of_sample_probability_calibration",
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        symbol=symbol,
        observation="direction-specific out-of-sample calibration record",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "probability_calibration_verified": verified,
            "source_verified": True,
            "out_of_sample": True,
            "walk_forward": True,
            "untouched_holdout": True,
            "data_leakage_checked": True,
            "selection_bias_checked": True,
            "overfitting_checked": True,
            "calibration_side": direction,
            "win_probability": 0.61,
            "sample_count": 240,
            "brier_score": 0.17,
            "reliability_gap": 0.08,
            "calibration_method": "isotonic",
            "calibration_id": "holdout-2026-q3",
        },
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=900,
    )


def test_uncalibrated_reasoning_confidence_is_not_a_win_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(Evidence(
        source_module="reasoning.confidence",
        domain=EvidenceDomain.REASONING,
        symbol="SYNTHETIC",
        observation="high native reasoning confidence",
        confidence=0.95,
        uncertainty=0.05,
        measurements={"confidence": 0.95},
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=900,
    ))
    verified, probability, source = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is False
    assert probability == 0.0
    assert "no fresh verified" in source


def test_verified_direction_specific_calibration_supplies_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration())
    verified, probability, source = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is True
    assert probability == 0.61
    assert "isotonic:holdout-2026-q3" in source


def test_calibration_for_opposite_direction_cannot_qualify_candidate():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(direction="SHORT"))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is False


def test_unverified_calibration_record_cannot_supply_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(verified=False))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is False


def test_calibration_from_another_symbol_cannot_qualify_candidate():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(symbol="OTHER"))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is False



def test_historical_calibration_metadata_does_not_pad_market_domain_coverage():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration())
    consolidated = state.consolidation()
    assert consolidated["market_domain_count"] == 0
    assert "historical_analogue" not in consolidated["market_domains_present"]


def test_poor_reliability_gap_cannot_qualify_calibration():
    state = MarketState(symbol="SYNTHETIC")
    evidence = _calibration()
    evidence.measurements["reliability_gap"] = 0.26
    state.add(evidence)
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is False



def test_uncalibrated_directional_opinion_cannot_create_campaign_or_claim_positive_ev():
    state = MarketState(symbol="SYNTHETIC")
    add_market_geometry(state, "LONG")
    state.evidence = [
        item for item in state.evidence
        if getattr(item.domain, "value", item.domain) != "historical_analogue"
    ]
    for domain in ("liquidity", "volume", "order_flow"):
        state.add(Evidence(
            source_module=f"test.{domain}",
            domain=domain,
            symbol="SYNTHETIC",
            observation=f"synthetic {domain} measurement",
            confidence=0.9,
            uncertainty=0.1,
            measurements={"test_measurement": 1.0},
            timestamp_epoch=time.time(),
            relevance_horizon_seconds=900,
        ))
    state.add(Evidence(
        source_module="reasoning_engine.test_council",
        domain=EvidenceDomain.REASONING,
        symbol="SYNTHETIC",
        observation="test advisor quorum",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "advisors_responded": 2,
            "advisors_available": 2,
            "advisors_total": 2,
        },
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=900,
    ))
    state.add(Evidence(
        source_module="execution.test_quote",
        domain=EvidenceDomain.EXECUTION_QUALITY,
        symbol="SYNTHETIC",
        observation="verified test execution cost",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "cost_verified": True,
            "estimated_total_cost_r": 0.05,
            "cost_source": "synthetic quote",
        },
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=60,
    ))
    opinion = SimpleNamespace(
        direction="LONG",
        confidence=0.95,
        rationale="synthetic directional thesis",
        competing_hypotheses=[],
        missing_information=[],
        opportunities=[{
            "id": "candidate",
            "direction": "LONG",
            "state": "ACTIVE",
            "quality": 0.9,
            "asymmetry": 0.9,
            "evidence_strength": 0.9,
        }],
        preferred_opportunity_id="candidate",
    )
    brain = CognitiveBrain(reasoner=None, min_expected_value=0.0)
    out = brain._from_opinion(
        "SYNTHETIC", state, state.consolidation(), opinion, None,
    )
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None
    assert out.decision.expected_value == 0.0
    assert "DIAGNOSTIC ONLY" in out.decision.questions_answered["expected_value"]
    assert "confidence is not probability" in out.decision.questions_answered[
        "win_probability_verification"
    ]



def test_calibration_side_metadata_is_confined_to_historical_analogue_domain():
    state = MarketState(symbol="SYNTHETIC")
    ordinary = Evidence(
        source_module="untrusted.market_adapter",
        domain=EvidenceDomain.MOMENTUM,
        symbol="SYNTHETIC",
        observation="ordinary market evidence",
        confidence=0.9,
        uncertainty=0.1,
        measurements={"calibration_side": "LONG", "momentum_state": "accelerating"},
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=900,
    )
    state.add(ordinary)
    assert "calibration_side" not in ordinary.measurements
    historical = _calibration()
    state.add(historical)
    assert historical.measurements["calibration_side"] == "LONG"
