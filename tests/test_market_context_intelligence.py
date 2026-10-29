"""System 2 market context, instrument economics and epistemic safeguards."""
from cognition.brain_reasoning import BrainReasoner
from cognition.contracts import Evidence, MarketState
from cognition.market_context import analyze_market_context


def _evidence(domain, measurements, *, symbol="XAUUSD", source="test.source"):
    return Evidence(
        source_module=source,
        domain=domain,
        symbol=symbol,
        observation="explicit test observation",
        confidence=0.95,
        uncertainty=0.05,
        measurements=measurements,
        relevance_horizon_seconds=900,
    )


def test_missing_instrument_and_session_data_remains_missing():
    result = analyze_market_context([], "XAUUSD")
    assert result["instrument_economics_and_contracts"]["status"] == "missing"
    assert result["sessions_and_execution_constraints"]["session_status"] == "missing"
    assert result["sessions_and_execution_constraints"]["execution_status"] == "missing"
    assert result["epistemic_policy"]["missing_data_is_missing"] is True
    assert any("contract" in item for item in result["missing_information"])


def test_correlation_is_recorded_as_association_not_causation():
    result = analyze_market_context([
        _evidence("correlation", {
            "related_symbol": "DXY",
            "correlation_coefficient": -0.72,
            "sample_count": 120,
            "synchronized_timestamps": True,
            "measurement_window": "rolling_30d",
            "source_verified": True,
        })
    ], "XAUUSD")
    relationship = result["cross_market_relationships"]["records"][0]
    assert relationship["coefficient"] == -0.72
    assert relationship["synchronized"] is True
    assert relationship["causation_established"] is False
    assert result["cross_market_relationships"]["interpretation"] == "observed_association_only"


def test_historical_confidence_or_sample_count_does_not_prove_predictive_skill():
    result = analyze_market_context([
        _evidence("historical_analogue", {
            "sample_count": 5000,
            "model_confidence": 0.99,
            "in_sample": True,
        })
    ], "XAUUSD")
    history = result["historical_evidence"]
    assert history["records"][0]["validation_status"] == "unverified"
    assert history["predictive_ability_demonstrated"] is False


def test_historical_validation_requires_multiple_independent_controls():
    controls = {
        "historical_sample_count": 500,
        "out_of_sample": True,
        "walk_forward": True,
        "untouched_holdout": True,
        "data_leakage_checked": True,
        "selection_bias_checked": True,
        "overfitting_checked": True,
    }
    result = analyze_market_context([
        _evidence("historical_analogue", controls)
    ], "XAUUSD")
    record = result["historical_evidence"]["records"][0]
    assert record["validation_status"] == "validated_controls_reported"
    # Reported metadata is not a substitute for independently verifying results.
    assert record["predictive_ability_demonstrated"] is False


def test_unfamiliar_measurements_are_preserved_without_becoming_validated_patterns():
    result = analyze_market_context([
        _evidence("other", {"novel_wave_signature": 0.83})
    ], "XAUUSD")
    assert result["unclassified_observations"][0]["measurement_keys"] == ["novel_wave_signature"]
    assert result["unclassified_observations"][0]["status"] == "unclassified_observation"


def test_native_analysis_exposes_market_context_and_unknowns():
    state = MarketState(symbol="XAUUSD")
    state.add(_evidence("structure", {"displacement": 1.2}))
    analysis = BrainReasoner().analyze_evidence(state, state.consolidation())
    encoded = analysis.to_dict()
    assert encoded["market_context"]["symbol"] == "XAUUSD"
    assert encoded["market_context"]["epistemic_policy"]["model_confidence_is_not_predictive_validation"]
    assert "historical outcome evidence with leakage, selection-bias and overfitting controls" in encoded["market_context"]["missing_information"]
