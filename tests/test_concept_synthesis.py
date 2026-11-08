from cognition.brain_reasoning import BrainReasoner, EvidenceAnalysis
from cognition.concept_synthesis import synthesize_concept_readings
from cognition.contracts import Evidence, EvidenceDomain, MarketState


def _reading(concept, direction, strength, evidence_id, *, activated=True, timeframe="M5", epoch=1000.0, independence_group=None):
    return {
        "concept": concept,
        "timeframe": timeframe,
        "source_bar_epoch": epoch,
        "source_timestamp_epoch": epoch,
        "relevance_horizon_seconds": 300.0,
        "source_timestamp_verified": True,
        "symbol": "EURUSD",
        "data_basis": "completed_ohlc",
        "provisional": False,
        "independence_group": f"ohlc:{timeframe}" if independence_group is None else independence_group,
        "applicable": True,
        "implications": [{
            "direction": direction,
            "strength": strength,
            "activated": activated,
            "statement": f"{concept} supports {direction}",
            "invalidation_reason": "structure invalidated",
            "invalidation_price": 99.0 if direction == "LONG" else 101.0,
            "objective_price": 105.0 if direction == "LONG" else 95.0,
            "requires": [],
            "conflicts_with": [],
        }],
        "evidence_id": evidence_id,
        "facts": {},
        "unknowns": [],
    }


def test_same_context_concepts_are_grouped_without_additive_confidence():
    readings = [
        _reading("crt", "LONG", 0.8, "e1"),
        _reading("market_structure", "LONG", 0.6, "e2"),
        _reading("liquidity", "SHORT", 0.7, "e3"),
    ]

    groups = synthesize_concept_readings(readings)

    assert len(groups) == 2
    long_group = next(group for group in groups if group["direction"] == "LONG")
    assert long_group["concepts"] == ["crt", "market_structure"]
    assert long_group["evidence_ids"] == ["e1", "e2"]
    assert long_group["strength"] == 0.8
    assert long_group["opposing_strength"] == 0.7
    assert long_group["confidence"] == 0.8 * (1.0 - 0.5 * 0.7)
    assert long_group["concept_count"] == 2
    assert long_group["confluence_count"] == 1
    assert long_group["independence_status"] == "single_source"
    assert long_group["geometry_divergent"] is False


def test_different_timeframes_and_source_bars_are_not_merged():
    groups = synthesize_concept_readings([
        _reading("crt", "LONG", 0.8, "e1", timeframe="M5", epoch=1000),
        _reading("crt", "LONG", 0.8, "e2", timeframe="H1", epoch=1000),
        _reading("crt", "LONG", 0.8, "e3", timeframe="M5", epoch=1300),
    ])
    assert len(groups) == 3


def test_unverified_provisional_and_non_completed_readings_are_excluded():
    unverified = _reading("crt", "LONG", 0.9, "e1")
    unverified["source_timestamp_verified"] = False
    provisional = _reading("fvg", "LONG", 0.9, "e2")
    provisional["provisional"] = True
    wrong_basis = _reading("liquidity", "LONG", 0.9, "e3")
    wrong_basis["data_basis"] = "forming_ohlc"

    assert synthesize_concept_readings([unverified, provisional, wrong_basis]) == []


def test_brain_emits_one_hypothesis_per_synthesized_concept_group():
    readings = [
        _reading("crt", "LONG", 0.8, "e1"),
        _reading("market_structure", "LONG", 0.6, "e2"),
        _reading("liquidity", "SHORT", 0.7, "e3"),
    ]
    reasoner = BrainReasoner()
    analysis = EvidenceAnalysis(concept_readings=readings)

    hypotheses = reasoner.generate_hypotheses(analysis, "EURUSD")
    concept_groups = [
        item for item in hypotheses
        if item.hypothesis_kind.startswith("concept_group:")
    ]

    assert len(concept_groups) == 2
    long_group = next(item for item in concept_groups if item.direction_implication == "LONG")
    assert long_group.supporting_evidence_ids == ["e1", "e2"]
    assert long_group.contradicting_evidence_ids == ["e3"]
    assert long_group.confidence_components["additive_confluence_boost"] is False
    assert long_group.confidence_components["concept_count"] == 2
    assert long_group.confidence_components["confluence_count"] == 1
    assert long_group.confidence_components["independence_status"] == "single_source"


def test_distinct_verified_independence_groups_are_counted_without_confidence_boost():
    groups = synthesize_concept_readings([
        _reading("crt", "LONG", 0.8, "e1", independence_group="ohlc:EURUSD:M5"),
        _reading("order_book", "LONG", 0.7, "e2", independence_group="dom:EURUSD"),
    ])
    (long_group,) = groups

    assert long_group["concept_count"] == 2
    assert long_group["confluence_count"] == 2
    assert long_group["independence_status"] == "multiple_independent_sources"
    assert long_group["independence_groups"] == ["dom:EURUSD", "ohlc:EURUSD:M5"]
    assert long_group["strength"] == 0.8
    assert long_group["confidence"] == 0.8


def test_missing_independence_provenance_is_not_counted_as_confluence():
    groups = synthesize_concept_readings([
        _reading("crt", "LONG", 0.8, "e1", independence_group=""),
        _reading("market_structure", "LONG", 0.7, "e2", independence_group=""),
    ])
    (long_group,) = groups

    assert long_group["concept_count"] == 2
    assert long_group["confluence_count"] == 0
    assert long_group["independence_status"] == "provenance_unknown"



def test_unknown_strength_remains_unknown_instead_of_becoming_zero():
    reading = _reading("crt", "LONG", None, "e1")
    (group,) = synthesize_concept_readings([reading])

    assert group["strength"] is None
    assert group["confidence"] is None


def test_unknown_opposing_strength_does_not_become_zero_conflict_or_fake_confidence():
    groups = synthesize_concept_readings([
        _reading("crt", "LONG", 0.8, "e1"),
        _reading("order_flow", "SHORT", None, "e2"),
    ])
    long_group = next(group for group in groups if group["direction"] == "LONG")
    short_group = next(group for group in groups if group["direction"] == "SHORT")

    assert long_group["opposing_concepts"] == ["order_flow"]
    assert long_group["opposing_strength"] is None
    assert long_group["opposing_strength_unknown"] is True
    assert long_group["confidence"] is None
    assert short_group["strength"] is None
    assert short_group["confidence"] is None


def test_brain_hypothesis_exposes_unknown_opposing_strength_as_uncertainty():
    readings = [
        _reading("crt", "LONG", 0.8, "e1"),
        _reading("order_flow", "SHORT", None, "e2"),
    ]
    hypotheses = BrainReasoner().generate_hypotheses(
        EvidenceAnalysis(concept_readings=readings), "EURUSD",
    )
    long_group = next(
        item for item in hypotheses
        if item.hypothesis_kind.startswith("concept_group:")
        and item.direction_implication == "LONG"
    )
    assert long_group.confidence == 0.0
    assert long_group.confidence_components["opposing_active_strength"] is None
    assert long_group.confidence_components["opposing_strength_unknown"] is True
    assert "opposing activated concept strength is unknown" in long_group.unresolved_unknowns


def test_wrong_symbol_and_future_dated_readings_cannot_qualify():
    wrong_symbol = _reading("crt", "LONG", 0.9, "wrong-symbol")
    wrong_symbol["symbol"] = "USDJPY"
    future_bar = _reading("market_structure", "LONG", 0.9, "future-bar", epoch=1200.0)
    future_source = _reading("liquidity", "LONG", 0.9, "future-source", epoch=900.0)
    future_source["source_timestamp_epoch"] = 1200.0

    assert synthesize_concept_readings(
        [wrong_symbol, future_bar, future_source],
        symbol="EURUSD",
        decision_cutoff_epoch=1100.0,
    ) == []


def test_duplicate_evidence_identity_is_excluded_instead_of_counted_twice():
    first = _reading("crt", "LONG", 0.8, "duplicate-id")
    second = _reading("order_book", "LONG", 0.9, "duplicate-id")

    assert synthesize_concept_readings([first, second]) == []


def test_brain_passes_symbol_and_decision_cutoff_to_concept_synthesis():
    wrong_symbol = _reading("crt", "LONG", 0.9, "wrong-symbol")
    wrong_symbol["symbol"] = "USDJPY"
    future_bar = _reading("market_structure", "LONG", 0.9, "future-bar", epoch=1200.0)
    analysis = EvidenceAnalysis(
        concept_readings=[wrong_symbol, future_bar],
        decision_timestamp_epoch=1100.0,
    )

    hypotheses = BrainReasoner().generate_hypotheses(analysis, "EURUSD")
    assert not any(item.hypothesis_kind.startswith("concept_group:") for item in hypotheses)


def test_evidence_projection_preserves_symbol_and_one_decision_time_cutoff():
    def evidence(symbol, concept, source_epoch, evidence_id):
        return Evidence(
            source_module=f"concept.{concept}.m5",
            domain=EvidenceDomain.STRUCTURE,
            symbol=symbol,
            observation=f"{concept} reading",
            measurements={
                "source_kind": "concept_reading",
                "concept": concept,
                "timeframe": "M5",
                "source_bar_epoch": source_epoch,
                "applicable": True,
                "implications": [{
                    "direction": "LONG",
                    "strength": 0.8,
                    "activated": True,
                    "statement": f"{concept} supports LONG",
                    "invalidation_price": 99.0,
                    "objective_price": 105.0,
                }],
                "facts": {},
                "unknowns": [],
            },
            evidence_id=evidence_id,
            timestamp_epoch=1100.0,
            relevance_horizon_seconds=300.0,
            source_timestamp_epoch=source_epoch,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            provisional=False,
        )

    state = MarketState(symbol="EURUSD")
    state.add(evidence("EURUSD", "crt", 1000.0, "valid"))
    state.add(evidence("USDJPY", "market_structure", 1000.0, "wrong-symbol"))
    state.add(evidence("EURUSD", "liquidity", 1200.0, "future"))

    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, now=1100.0)
    assert analysis.decision_timestamp_epoch == 1100.0
    assert {row["evidence_id"] for row in analysis.concept_readings} == {
        "valid", "wrong-symbol", "future",
    }

    hypotheses = reasoner.generate_hypotheses(analysis, "EURUSD")
    groups = [item for item in hypotheses if item.hypothesis_kind.startswith("concept_group:")]
    assert len(groups) == 1
    assert groups[0].supporting_evidence_ids == ["valid"]


def test_stale_source_bar_cannot_qualify_when_recently_reingested():
    stale = _reading("crt", "LONG", 0.9, "stale", epoch=700.0)
    stale["relevance_horizon_seconds"] = 300.0

    assert synthesize_concept_readings(
        [stale], symbol="EURUSD", decision_cutoff_epoch=1100.0,
    ) == []
