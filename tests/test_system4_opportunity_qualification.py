"""System 4 probability and opportunity qualification contracts."""

import time
from types import SimpleNamespace

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, Evidence, EvidenceDomain, MarketState
from test_support.market_geometry import add_market_geometry


def _calibration(
    *, direction="LONG", verified=True, symbol="SYNTHETIC",
    sample_count=240, brier_score=0.17, reliability_gap=0.08,
):
    from test_support.calibration import register_test_calibration

    now = time.time()
    if verified:
        artifact = register_test_calibration(
            symbol, direction, horizon_seconds=300,
            probability=0.61, sample_count=sample_count,
            brier_score=brier_score, reliability_gap=reliability_gap,
        )
        measurements = artifact.to_measurements()
        stamp = artifact.issued_at_epoch
    else:
        measurements = {
            "calibration_artifact_id": "forged-unregistered-artifact",
            "calibration_side": direction,
            "win_probability": 0.61,
            "sample_count": sample_count,
            "brier_score": brier_score,
            "reliability_gap": reliability_gap,
        }
        stamp = now
    return Evidence(
        source_module="calibration_producer.test_fixture",
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        symbol=symbol,
        observation="direction-specific test calibration artifact",
        confidence=0.0,
        uncertainty=0.0,
        measurements=measurements,
        timestamp_epoch=stamp,
        source_timestamp_epoch=stamp,
        relevance_horizon_seconds=3600,
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
    verified, expected_return, source = CognitiveBrain(
        reasoner=None,
    )._calibrated_expected_return(state, "LONG", horizon_seconds=300)
    assert verified is False
    assert probability == 0.0
    assert "no fresh verified" in source


def test_verified_direction_specific_calibration_supplies_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration())
    verified, probability, source = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG", horizon_seconds=300)
    assert verified is True
    assert expected_return == 0.25
    assert "chronological_base_rate_fixed_horizon_v1" in source

def test_calibration_for_opposite_direction_cannot_qualify_candidate():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(direction="SHORT"))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG", horizon_seconds=300)
    assert verified is False

def test_unverified_calibration_record_cannot_supply_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(verified=False))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG", horizon_seconds=300)
    assert verified is False

def test_calibration_from_another_symbol_cannot_qualify_candidate():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(symbol="OTHER"))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG", horizon_seconds=300)
    assert verified is False



def test_valid_short_calibration_is_direction_specific():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(direction="SHORT"))
    verified, expected_return, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "SHORT", horizon_seconds=300)
    assert verified is True
    assert expected_return == 0.25


def test_stale_calibration_cannot_supply_probability():
    state = MarketState(symbol="SYNTHETIC")
    evidence = _calibration()
    evidence.timestamp_epoch = time.time() - 1200.0
    evidence.source_timestamp_epoch = evidence.timestamp_epoch
    evidence.relevance_horizon_seconds = 60.0
    state.add(evidence)
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG", horizon_seconds=300)
    assert verified is False

def test_future_calibration_cannot_supply_probability():
    state = MarketState(symbol="SYNTHETIC")
    evidence = _calibration()
    evidence.timestamp_epoch = time.time() + 60.0
    evidence.source_timestamp_epoch = evidence.timestamp_epoch
    state.add(evidence)
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG", horizon_seconds=300)
    assert verified is False

def test_insufficient_calibration_sample_count_cannot_supply_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(sample_count=29))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG", horizon_seconds=300)
    assert verified is False

def test_invalid_brier_score_cannot_supply_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(brier_score=0.30))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG", horizon_seconds=300)
    assert verified is False

def test_historical_calibration_metadata_does_not_pad_market_domain_coverage():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration())
    consolidated = state.consolidation()
    assert consolidated["market_domain_count"] == 0
    assert "historical_analogue" not in consolidated["market_domains_present"]


def test_poor_reliability_gap_cannot_qualify_calibration():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(reliability_gap=0.26))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG", horizon_seconds=300)
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
    assert "confidence is not expected return" in out.decision.questions_answered[
        "expected_return_verification"
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



def test_execution_cost_from_another_symbol_cannot_qualify_candidate():
    state = MarketState(symbol="SYNTHETIC")
    state.add(Evidence(
        source_module="execution.other_symbol_quote",
        domain=EvidenceDomain.EXECUTION_QUALITY,
        symbol="OTHER",
        observation="verified quote for a different instrument",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "cost_verified": True,
            "estimated_total_cost_r": 0.01,
            "cost_source": "other-symbol quote",
        },
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=60,
    ))
    verified, _, source = CognitiveBrain(
        reasoner=None,
    )._execution_cost_estimate(state)
    assert verified is False
    assert "not verified" in source



def test_cost_flag_without_attributable_source_cannot_qualify():
    state = MarketState(symbol="SYNTHETIC")
    state.add(Evidence(
        source_module="execution.unattributed",
        domain=EvidenceDomain.EXECUTION_QUALITY,
        symbol="SYNTHETIC",
        observation="cost flag with no source attribution",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "cost_verified": True,
            "estimated_total_cost_r": 0.01,
        },
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=60,
    ))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._execution_cost_estimate(state)
    assert verified is False
