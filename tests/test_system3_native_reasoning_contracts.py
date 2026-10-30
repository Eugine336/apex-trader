"""System 3 contracts: native evidence reasoning, conflict handling and provenance."""

from cognition.brain_reasoning import BrainHypothesis, BrainReasoner, LONG, FLAT
from cognition.contracts import Evidence, MarketState


def _evidence(domain, measurements, *, confidence=0.9, source="test"):
    return Evidence(
        source_module=source,
        domain=domain,
        symbol="SYNTHETIC",
        observation=f"synthetic {domain} observation",
        confidence=confidence,
        uncertainty=1.0 - confidence,
        measurements=dict(measurements),
        relevance_horizon_seconds=900.0,
    )


def test_native_analysis_preserves_evidence_lineage_and_epistemic_categories():
    state = MarketState(symbol="SYNTHETIC")
    structure = _evidence(
        "structure",
        {"displacement": 2.0, "bos": True, "timeframe": "M15",
         "opportunity_horizon": "intraday"},
        source="synthetic.structure",
    )
    volume = _evidence(
        "volume", {"volume_z": 1.8, "expansion": True},
        source="synthetic.volume",
    )
    state.add(structure)
    state.add(volume)

    analysis = BrainReasoner().analyze_evidence(state, state.consolidation())
    encoded = analysis.to_dict()

    assert {row["evidence_id"] for row in encoded["evidence_index"]} == {
        structure.evidence_id, volume.evidence_id,
    }
    assert structure.evidence_id in encoded["pattern_evidence_ids"]["displacement_confirmed"]
    assert encoded["epistemic_record"]["observed_facts"]
    assert encoded["epistemic_record"]["inferences"]
    assert encoded["epistemic_record"]["unknowns"]
    assert encoded["epistemic_record"]["assumptions"] == []


def test_conflicting_sources_are_not_collapsed_into_one_direction():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_evidence("structure", {"displacement": 2.0}, source="structure.a"))
    state.add(_evidence("structure", {"displacement": -1.0}, source="structure.b"))
    state.add(_evidence("order_flow", {"delta": 1.0}, source="flow.a"))
    state.add(_evidence("order_flow", {"delta": -1.0}, source="flow.b"))
    state.add(_evidence("volume", {"volume_z": 1.5, "expansion": True}))

    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())

    assert analysis.regime_indicators["structure"] == "conflicted"
    assert analysis.regime_indicators["order_flow"] == "conflicted"
    assert any("structural sources disagree" in item for item in analysis.contradictions)
    assert any("order-flow sources disagree" in item for item in analysis.contradictions)
    hypotheses = reasoner.generate_hypotheses(analysis, "SYNTHETIC")
    continuation = next(h for h in hypotheses if h.hypothesis_kind == "continuation")
    assert continuation.direction_implication == FLAT
    assert continuation.criticism == ""  # criticism is a separate explicit stage


def test_hypotheses_carry_support_challenge_unknowns_and_uncalibrated_confidence():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_evidence("structure", {"displacement": 2.0, "bos": True, "timeframe": "M15", "opportunity_horizon": "intraday"}))
    state.add(_evidence("volume", {"volume_z": 1.8, "expansion": True}))
    state.add(_evidence("order_flow", {"delta": 1.0}))
    state.add(_evidence("momentum", {"momentum_state": "accelerating"}))

    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())
    hypotheses = reasoner.self_criticize(
        reasoner.generate_hypotheses(analysis, "SYNTHETIC"), analysis,
    )
    continuation = next(h for h in hypotheses if h.hypothesis_kind == "continuation")
    serialized = continuation.to_dict()

    assert continuation.hypothesis_id
    assert continuation.supporting_evidence_ids
    assert continuation.horizon == "intraday"
    assert set(continuation.supporting_evidence_ids).issubset(
        {row["evidence_id"] for row in analysis.evidence_index}
    )
    assert serialized["confidence_semantics"] == "uncalibrated_reasoning_strength"
    assert serialized["confidence_components"]["calibrated_probability"] is False
    assert serialized["distinguishing_evidence"]
    assert serialized["unresolved_unknowns"]


def test_same_timeframe_and_domain_conflicts_remain_explicit():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_evidence(
        "structure",
        {"source_kind": "market_lens", "timeframe": "M15", "trend_state": "uptrend"},
        source="structure.m15.a",
    ))
    state.add(_evidence(
        "structure",
        {"source_kind": "market_lens", "timeframe": "M15", "trend_state": "downtrend"},
        source="structure.m15.b",
    ))
    state.add(_evidence(
        "momentum", {"momentum_state": "accelerating"},
        source="momentum.a",
    ))
    state.add(_evidence(
        "momentum", {"momentum_state": "decelerating"},
        source="momentum.b",
    ))
    state.add(_evidence(
        "volatility", {"volatility_state": "compressing"},
        source="volatility.a",
    ))
    state.add(_evidence(
        "volatility", {"volatility_state": "expanding"},
        source="volatility.b",
    ))

    analysis = BrainReasoner().analyze_evidence(state, state.consolidation())
    regime = analysis.regime_indicators
    assert regime["trend_context"] == "mixed"
    assert regime["trend_conflicted_timeframes"] == ["M15"]
    assert regime["momentum"] == "conflicted"
    assert regime["volatility"] == "conflicted"
    assert any("trend readings conflict" in item for item in analysis.contradictions)
    assert any("momentum evidence simultaneously" in item for item in analysis.contradictions)
    assert any("volatility evidence simultaneously" in item for item in analysis.contradictions)


def test_leading_hypothesis_compares_no_trade_instead_of_prioritizing_direction():
    directional = BrainHypothesis(
        statement="weak directional thesis",
        confidence=0.35,
        direction_implication=LONG,
        hypothesis_kind="continuation",
    )
    no_trade = BrainHypothesis(
        statement="evidence is incomplete; wait",
        confidence=0.8,
        direction_implication=FLAT,
        hypothesis_kind="no_trade",
    )
    assert BrainReasoner.leading([directional, no_trade]) is no_trade


def test_stale_and_future_evidence_never_enters_native_hypothesis_lineage():
    state = MarketState(symbol="SYNTHETIC")
    stale = _evidence("structure", {"displacement": 3.0})
    stale.timestamp_epoch = -1000.0
    future = _evidence("order_flow", {"delta": 1.0})
    future.timestamp_epoch = state.created_epoch + 3600.0
    state.add(stale)
    state.add(future)

    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())
    assert analysis.evidence_index == []
    assert analysis.pattern_evidence_ids == {}
    assert analysis.unknowns
    hypotheses = reasoner.generate_hypotheses(analysis, "SYNTHETIC")
    assert all(not h.supporting_evidence_ids for h in hypotheses)
    assert all(not h.is_directional for h in hypotheses)



def test_native_analysis_uses_same_decision_time_cutoff_as_consolidation():
    state = MarketState(symbol="SYNTHETIC")
    valid = _evidence("structure", {"displacement": 1.0}, source="valid")
    valid.timestamp_epoch = 95.0
    future = _evidence("order_flow", {"delta": 1.0}, source="future")
    future.timestamp_epoch = 101.0
    stale = _evidence("momentum", {"momentum_state": "accelerating"}, source="stale")
    stale.timestamp_epoch = -1000.0
    state.add(valid)
    state.add(future)
    state.add(stale)

    reasoner = BrainReasoner()
    consolidation = state.consolidation(now=100.0)
    analysis = reasoner.analyze_evidence(state, consolidation, now=100.0)

    assert [item["evidence_id"] for item in analysis.evidence_index] == [valid.evidence_id]
    assert "no order_flow evidence" in analysis.missing_information
    assert "no momentum evidence" in analysis.missing_information
