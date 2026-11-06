"""Tests for real strategy-lens composition and Brain integration."""
from __future__ import annotations

import math

from cognition.concepts import CONCEPT_MODULES, run_concepts
from cognition.concepts.base import ConceptReading, reading_to_evidence
from cognition.contracts import EvidenceDomain
from cognition.concepts.strategy_lens_readings import (
    DATA_GATED_LENSES,
    FULL_LENSES,
    LENS_METHODS,
    StrategyLensReadings,
)
from cognition.strategy_knowledge import STRATEGY_LENSES, assess_strategy_lenses


def _bars(count: int = 180) -> list[dict]:
    rows = []
    close = 100.0
    for index in range(count):
        step = 0.0022 * math.sin(index * 0.37) + 0.0009 * math.cos(index * 0.11)
        close *= math.exp(step)
        width = close * (0.001 + 0.0004 * (1.0 + math.sin(index * 0.19)))
        rows.append({
            "open": close,
            "high": close + width,
            "low": close - width,
            "close": close,
            "volume": 100.0 + index,
            "volume_source": "provider_volume",
            "timestamp_epoch": 1_780_000_000.0 + index * 300.0,
            "source_timestamp_verified": True,
        })
    return rows


def _lens_evidence():
    return [
        item for item in run_concepts("XAUUSD", {"M5": _bars()})
        if item.measurements.get("lens_key") in set(LENS_METHODS)
        or item.measurements.get("concept") in {"crt", "regime_switching"}
    ]


def test_lens_composition_catalog_covers_every_strategy_knowledge_lens_once():
    catalog_keys = {item.key for item in STRATEGY_LENSES}
    assert set(LENS_METHODS) | {"crt", "market_regime_transition"} == catalog_keys
    assert len(LENS_METHODS) == 36
    assert len(FULL_LENSES) == 18
    assert len(DATA_GATED_LENSES) == 18
    assert set(FULL_LENSES).isdisjoint(DATA_GATED_LENSES)


def test_lens_composition_reader_is_registered_and_claims_no_new_methods():
    modules = [item for item in CONCEPT_MODULES if item.key == "strategy_lens_readings"]
    assert len(modules) == 1
    assert isinstance(modules[0], StrategyLensReadings)
    assert modules[0].covers_methods == ()
    assert set(modules[0].covers_lenses) == set(FULL_LENSES)
    assert set(modules[0].data_gated_lenses) == set(DATA_GATED_LENSES)


def test_native_pipeline_composes_method_readings_for_every_lens():
    evidence = _lens_evidence()
    concepts = {
        item.measurements.get("lens_key")
        or (
            "market_regime_transition"
            if item.measurements.get("concept") == "regime_switching"
            else item.measurements.get("concept")
        )
        for item in evidence
        if item.measurements.get("lens_key")
        or item.measurements.get("concept") in {"crt", "regime_switching"}
    }

    assert set(LENS_METHODS) | {"crt", "market_regime_transition"} <= concepts
    full_lens_readings = [
        item for item in evidence
        if item.measurements.get("lens_key") in FULL_LENSES
    ]
    assert {item.measurements["lens_key"] for item in full_lens_readings} == set(FULL_LENSES)
    registered_methods = {
        method for module in CONCEPT_MODULES
        for method in getattr(module, "covers_methods", ())
    }
    declared_full_methods = {
        method for lens_key in FULL_LENSES
        for method in LENS_METHODS[lens_key]
    }
    assert declared_full_methods <= registered_methods
    for item in full_lens_readings:
        required = set(item.measurements["required_methods"])
        available = set(item.measurements["available_methods"])
        missing = set(item.measurements["missing_methods"])
        assert available.isdisjoint(missing)
        assert available | missing == required
        assert item.measurements["missing_methods_semantics"].startswith(
            "No ConceptReading was emitted"
        )
    trend = next(
        item for item in evidence
        if item.measurements.get("lens_key") == "trend_following"
    )
    assert trend.measurements["source_kind"] == "concept_reading"
    assert trend.source_module == "concept.strategy_lens.trend_following.m5"
    assert trend.measurements["required_methods"] == [
        "trend_following", "roc_momentum", "momentum_breakout",
    ]
    assert "method_summaries" in trend.measurements
    assert trend.independence_group == "ohlc:XAUUSD:M5"
    assert trend.polarity == 0.0
    assert trend.measurements["directional_decision_emitted"] is False
    assert trend.measurements["entry_authorization_emitted"] is False


def test_lens_composition_preserves_method_geometry_and_failure_modes():
    evidence = _lens_evidence()
    trend = next(
        item for item in evidence
        if item.measurements.get("lens_key") == "trend_following"
    )
    facts = trend.measurements

    assert "known_failure_modes" in facts
    assert facts["known_failure_modes"]
    assert "invalidation_candidates" in facts
    assert "objective_candidates" in facts
    assert "trail_anchors" in facts
    assert facts["style_is_not_selected_or_authorized"] is True


def test_brain_strategy_lens_assessment_uses_composed_method_evidence():
    evidence = _lens_evidence()
    assessments = {
        item["key"]: item for item in assess_strategy_lenses(evidence)
    }

    assert any("native method composition" in reason for reason in assessments["pattern_discovery"]["reasons"])
    assert assessments["event_macro"]["status"] == "not_evaluable"
    assert any(
        "complete lens interpretation requires" in item
        for item in assessments["event_macro"]["missing_context"]
    )


def test_data_gated_lens_does_not_claim_full_when_external_inputs_are_absent():
    evidence = _lens_evidence()
    event = next(
        item for item in evidence
        if item.measurements.get("lens_key") == "event_macro"
    )

    assert event.measurements["data_gated_reason"] == DATA_GATED_LENSES["event_macro"]
    assert event.measurements["applicable"] is False
    assert any("economic calendar" in item for item in event.measurements["unknowns"])
    assert event.polarity == 0.0



def test_composite_lens_never_promotes_mixed_method_timestamps_to_verified():
    methods = [
        ConceptReading(
            concept="wyckoff_effort_result",
            timeframe="M5",
            domain=EvidenceDomain.VOLUME,
            applicable=True,
            applicability_reason="completed-bar method reading",
            observation="volume effort compared with range result",
            implications=(),
            facts={},
            source_bar_epoch=1_780_000_000.0,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="wyckoff",
            timeframe="M5",
            domain=EvidenceDomain.STRUCTURE,
            applicable=True,
            applicability_reason="completed-bar method reading",
            observation="phase and event sequence",
            implications=(),
            facts={},
            source_bar_epoch=None,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    readings = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    composite = next(item for item in readings if item.concept == "wyckoff_effort_result")

    assert composite.source_bar_epoch is None
    assert composite.independence_group == "ohlc:XAUUSD:M5"
    assert any("not all synchronized" in item for item in composite.unknowns)

    assessment = next(
        item for item in assess_strategy_lenses([reading_to_evidence("XAUUSD", composite)])
        if item["key"] == "wyckoff_effort_result"
    )
    assert assessment["status"] == "watch"
    assert any("not all synchronized" in item for item in assessment["missing_context"])



def test_fully_verified_method_composition_can_be_relevant_to_the_brain():
    methods = [
        ConceptReading(
            concept="anomaly_detection",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="sufficient own-history sample",
            observation="current state novelty is measured against prior states",
            implications=(),
            facts={"novelty_own_history_percentile": 0.72},
            source_bar_epoch=1_780_000_000.0,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="clustering",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="prior states have realized outcomes",
            observation="nearest historical states are available",
            implications=(),
            facts={"historical_state_sample_count": 80},
            source_bar_epoch=1_780_000_000.0,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    composed = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    lens = next(item for item in composed if item.concept == "pattern_discovery")
    assessment = next(
        item for item in assess_strategy_lenses([reading_to_evidence("XAUUSD", lens)])
        if item["key"] == "pattern_discovery"
    )

    assert lens.source_bar_epoch == 1_780_000_000.0
    assert lens.independence_group == "ohlc:XAUUSD:M5"
    assert assessment["status"] == "relevant"


def test_unsynchronized_method_composition_is_watch_not_relevant():
    methods = [
        ConceptReading(
            concept="anomaly_detection",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="available",
            observation="novelty",
            implications=(),
            facts={},
            source_bar_epoch=1_780_000_000.0,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="clustering",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="available",
            observation="analogues",
            implications=(),
            facts={},
            source_bar_epoch=None,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    composed = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    lens = next(item for item in composed if item.concept == "pattern_discovery")
    assessment = next(
        item for item in assess_strategy_lenses([reading_to_evidence("XAUUSD", lens)])
        if item["key"] == "pattern_discovery"
    )

    assert lens.source_bar_epoch is None
    assert assessment["status"] == "watch"
    assert any("not all synchronized" in item for item in assessment["missing_context"])
