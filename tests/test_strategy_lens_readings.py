"""Tests for real strategy-lens composition and Brain integration."""
from __future__ import annotations

import math

from cognition.concepts import CONCEPT_MODULES, run_concepts
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
        or item.measurements.get("concept") in {"crt", "market_regime_transition"}
    ]


def test_lens_composition_catalog_covers_every_strategy_knowledge_lens_once():
    catalog_keys = {item.key for item in STRATEGY_LENSES}
    assert set(LENS_METHODS) | {"crt", "market_regime_transition"} == catalog_keys
    assert len(LENS_METHODS) == 36
    assert len(FULL_LENSES) == 19
    assert len(DATA_GATED_LENSES) == 17
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
        item.measurements.get("concept")
        for item in evidence
        if item.measurements.get("lens_key") or item.measurements.get("concept") in {"crt", "market_regime_transition"}
    }

    assert set(LENS_METHODS) | {"crt", "market_regime_transition"} <= concepts
    trend = next(
        item for item in evidence
        if item.measurements.get("lens_key") == "trend_following"
    )
    assert trend.measurements["source_kind"] == "concept_reading"
    assert trend.measurements["required_methods"] == [
        "trend_following", "roc_momentum", "momentum_breakout",
    ]
    assert "method_summaries" in trend.measurements
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

    assert assessments["trend_following"]["status"] == "relevant"
    assert any("native method composition" in reason for reason in assessments["trend_following"]["reasons"])
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
