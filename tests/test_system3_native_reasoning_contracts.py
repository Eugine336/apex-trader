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
    assert any(
        item.startswith("structure=")
        for item in encoded["epistemic_record"]["inferences"]
    )
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
    assert serialized["evidence_lineage_scope"] == "domain_level_candidate_sources"
    assert serialized["distinguishing_evidence"]
    assert serialized["unresolved_unknowns"]


def test_directional_thesis_is_challenged_when_structure_and_flow_disagree():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_evidence(
        "structure", {"displacement": 2.0, "bos": True},
        source="structure.bullish",
    ))
    state.add(_evidence(
        "order_flow", {"delta": -1.0},
        source="order_flow.bearish",
    ))
    state.add(_evidence(
        "volume", {"volume_z": 1.8, "expansion": True},
        source="volume",
    ))
    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())
    assert any(
        "structural displacement and order-flow pressure point in opposite directions" in item
        for item in analysis.contradictions
    )
    hypotheses = reasoner.generate_hypotheses(analysis, "SYNTHETIC")
    continuation = next(item for item in hypotheses if item.hypothesis_kind == "continuation")
    before = continuation.confidence
    reasoner.self_criticize(hypotheses, analysis)
    assert continuation.confidence < before
    assert "challenged by independent contradiction" in continuation.criticism


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


def test_relevant_strategy_lenses_become_evidence_linked_non_directional_candidates():
    state = MarketState(symbol="SYNTHETIC")
    structure = _evidence(
        "structure", {"fvg": True, "displacement": 1.0},
        source="synthetic.structure",
    )
    liquidity = _evidence(
        "liquidity", {"pool": True, "sweep_side": "sell"},
        source="synthetic.liquidity",
    )
    flow = _evidence(
        "order_flow", {"delta": 1.0},
        source="synthetic.order_flow",
    )
    state.add(structure)
    state.add(liquidity)
    state.add(flow)

    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())
    assert any(
        item["key"] == "ict_smc" and item["status"] == "relevant"
        for item in analysis.strategy_lenses
    )
    hypotheses = reasoner.generate_hypotheses(analysis, "SYNTHETIC")
    lens_hypothesis = next(
        item for item in hypotheses if item.hypothesis_kind == "strategy_lens:ict_smc"
    )
    assert lens_hypothesis.direction_implication == FLAT
    assert set(lens_hypothesis.supporting_evidence_ids) == {
        structure.evidence_id, liquidity.evidence_id, flow.evidence_id,
    }
    assert lens_hypothesis.confidence_components["score_kind"] == (
        "lens_relevance_not_forecast_confidence"
    )


def test_self_criticism_does_not_apply_another_thesis_counter_to_this_thesis():
    from cognition.brain_reasoning import SHORT
    from cognition.brain_reasoning import EvidenceAnalysis

    primary_a = BrainHypothesis(
        statement="primary A", confidence=0.6, direction_implication=LONG,
        hypothesis_kind="continuation",
    )
    primary_b = BrainHypothesis(
        statement="primary B", confidence=0.7, direction_implication=SHORT,
        hypothesis_kind="mean_reversion",
    )
    counter_a = BrainHypothesis(
        statement="counter A", confidence=0.95, direction_implication=SHORT,
        hypothesis_kind="counter_hypothesis",
        confidence_components={"primary_hypothesis_id": primary_a.hypothesis_id},
    )
    counter_b = BrainHypothesis(
        statement="counter B", confidence=0.1, direction_implication=LONG,
        hypothesis_kind="counter_hypothesis",
        confidence_components={"primary_hypothesis_id": primary_b.hypothesis_id},
    )
    analysis = EvidenceAnalysis(
        domain_coverage={"structure": 0.9, "volume": 0.9},
        agreement_score=0.9,
        evidence_uncertainty=0.1,
    )
    before_b = primary_b.confidence
    BrainReasoner().self_criticize(
        [primary_a, primary_b, counter_a, counter_b], analysis,
    )
    assert primary_a.confidence < 0.6
    assert primary_b.confidence > before_b
    assert "survived criticism" in primary_b.criticism


def test_each_leading_directional_hypothesis_gets_its_own_counter():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_evidence(
        "structure", {"displacement": 2.0, "bos": True, "timeframe": "M15"},
        source="structure",
    ))
    state.add(_evidence(
        "volume", {"volume_z": 2.0, "expansion": True},
        source="volume",
    ))
    state.add(_evidence(
        "order_flow", {"delta": 1.0},
        source="order_flow",
    ))
    state.add(_evidence(
        "momentum", {"momentum_state": "decelerating", "divergence": True},
        source="momentum",
    ))
    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())
    hypotheses = reasoner.generate_hypotheses(analysis, "SYNTHETIC")
    directional = sorted(
        (item for item in hypotheses if item.is_directional
         and item.hypothesis_kind != "counter_hypothesis"),
        key=lambda item: item.confidence,
        reverse=True,
    )[:3]
    counters = [
        item for item in hypotheses
        if item.hypothesis_kind == "counter_hypothesis"
    ]
    challenged_ids = {
        item.confidence_components.get("primary_hypothesis_id")
        for item in counters
    }
    assert directional
    assert {item.hypothesis_id for item in directional}.issubset(challenged_ids)


def test_unknown_measurements_and_volatility_create_non_directional_hypotheses():
    state = MarketState(symbol="SYNTHETIC")
    unfamiliar = _evidence(
        "other", {"unfamiliar_microstructure_metric": 0.73},
        source="synthetic.unknown_adapter",
    )
    state.add(unfamiliar)
    unfamiliar_second = _evidence(
        "other", {"unfamiliar_regime_signature": "regime-7"},
        source="synthetic.unknown_adapter",
    )
    state.add(unfamiliar_second)
    state.add(_evidence(
        "volatility", {"volatility_state": "expanding"},
        source="synthetic.volatility",
    ))

    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())
    hypotheses = reasoner.generate_hypotheses(analysis, "SYNTHETIC")

    unknown = next(item for item in hypotheses if item.hypothesis_kind == "unknown_pattern")
    volatility = next(item for item in hypotheses if item.hypothesis_kind == "volatility_regime")
    assert unknown.direction_implication == FLAT
    assert unfamiliar.evidence_id in unknown.supporting_evidence_ids
    assert unfamiliar_second.evidence_id in unknown.supporting_evidence_ids
    assert "unfamiliar_microstructure_metric" in unknown.statement
    assert "unfamiliar_regime_signature" in unknown.statement
    assert unknown.assumptions
    assert unknown.unresolved_unknowns
    assert volatility.direction_implication == FLAT
    assert volatility.supporting_evidence_ids


def test_counter_hypothesis_can_challenge_but_never_become_primary_direction():
    primary = BrainHypothesis(
        statement="weak long thesis",
        confidence=0.25,
        direction_implication=LONG,
        hypothesis_kind="continuation",
    )
    counter = BrainHypothesis(
        statement="counter to weak long thesis",
        confidence=0.9,
        direction_implication="SHORT",
        hypothesis_kind="counter_hypothesis",
        confidence_components={"primary_hypothesis_id": primary.hypothesis_id},
    )
    assert BrainReasoner.leading([primary, counter]) is primary


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


def test_native_origination_rejects_empty_records_even_if_consolidation_counts_domains():
    from cognition.brain import CognitiveBrain

    state = MarketState(symbol="SYNTHETIC")
    for domain in ("structure", "liquidity", "momentum"):
        state.add(_evidence(domain, {}, confidence=0.99, source=f"empty.{domain}"))
    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())
    forced_direction = BrainHypothesis(
        statement="test-only directional hypothesis",
        confidence=0.99,
        direction_implication=LONG,
        hypothesis_kind="continuation",
    )
    brain = CognitiveBrain(reasoner=None)
    result = brain._originate_independent(
        "SYNTHETIC",
        {"market_domain_count": 6, "aggregate_uncertainty": 0.01},
        analysis,
        [forced_direction],
        reason_txt="test",
        market_state=state,
        now=state.created_epoch,
    )
    assert result is None


def test_high_confidence_empty_records_do_not_count_as_usable_market_coverage():
    state = MarketState(symbol="SYNTHETIC")
    for domain in ("structure", "liquidity", "momentum"):
        state.add(_evidence(domain, {}, confidence=0.99, source=f"empty.{domain}"))

    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())
    assert analysis.domain_coverage
    assert analysis.informative_domains == []
    assert analysis.coverage_gap == 1.0
    assert "no usable measurement in structure" in analysis.missing_information
    hypotheses = reasoner.generate_hypotheses(analysis, "SYNTHETIC")
    assert reasoner.leading(hypotheses).hypothesis_kind == "no_trade"


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



def test_verified_execution_cost_uses_decision_time_cutoff():
    from cognition.brain import CognitiveBrain

    state = MarketState(symbol="SYNTHETIC")
    old_cost = _evidence(
        "execution_quality",
        {"cost_verified": True, "estimated_total_cost_r": 0.08,
         "cost_source": "prior-quote"},
        source="cost.prior",
    )
    old_cost.timestamp_epoch = 95.0
    old_cost.source_timestamp_epoch = 95.0
    old_cost.source_timestamp_verified = True
    future_cost = _evidence(
        "execution_quality",
        {"cost_verified": True, "estimated_total_cost_r": 0.001,
         "cost_source": "future-quote"},
        source="cost.future",
    )
    future_cost.timestamp_epoch = 101.0
    future_cost.source_timestamp_epoch = 101.0
    future_cost.source_timestamp_verified = True
    state.add(old_cost)
    state.add(future_cost)

    verified, cost, source = CognitiveBrain(reasoner=None)._execution_cost_estimate(
        state, now=100.0,
    )
    assert verified is True
    assert cost == 0.08
    assert source == "prior-quote"


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
