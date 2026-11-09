"""Tests for completed-bar quantitative state, analogue and novelty readers."""
from __future__ import annotations

import math
from statistics import pstdev
from unittest.mock import patch

from cognition.concepts import CONCEPT_MODULES, run_concepts
from cognition.concepts.quantitative_state_readings import (
    QuantitativeStateReadings,
    _state_vector,
    _returns,
)


def _bars(count: int = 180, *, reflected: bool = False) -> list[dict]:
    rows = []
    close = 100.0
    for index in range(count):
        # A deterministic mixture of persistence, cyclicity and changing range.
        step = 0.0018 * math.sin(index * 0.31) + 0.0007 * math.cos(index * 0.073)
        step += 0.00025 * math.sin(index * 0.011) * (1.0 if index % 37 < 19 else -1.0)
        close *= math.exp(step)
        price = 500.0 - close if reflected else close
        width = abs(price) * (0.001 + 0.0003 * (1.0 + math.sin(index * 0.17)))
        rows.append({
            "open": price,
            "high": price + width,
            "low": price - width,
            "close": price,
            "volume": 100.0 + index,
            "volume_source": "provider_volume",
            "timestamp_epoch": 1_780_000_000.0 + index * 300.0,
            "source_timestamp_verified": True,
        })
    return rows


def _read(bars: list[dict]):
    return {
        item.concept: item
        for item in QuantitativeStateReadings().read("XAUUSD", {"M5": bars})
    }


def test_quantitative_state_reader_is_registered_and_claims_only_implemented_methods():
    modules = [item for item in CONCEPT_MODULES if item.key == "quantitative_state_readings"]
    assert len(modules) == 1
    assert set(modules[0].covers_methods) == {"time_series", "clustering", "anomaly_detection"}
    assert modules[0].covers_lenses == ()


def test_time_series_reports_return_and_volatility_dependence_and_adf_limitations():
    reading = _read(_bars())["time_series"]
    facts = reading.facts

    assert reading.applicable is True
    assert set(facts["log_return_autocorrelation_lags_1_to_5"]) == {1, 2, 3, 4, 5}
    assert set(facts["squared_return_autocorrelation_lags_1_to_5"]) == {1, 2, 3, 4, 5}
    assert facts["adf_zero_lag_constant"]["status"] == "statistic_available"
    assert facts["statistical_tests_are_diagnostics_not_directional_forecasts"] is True
    assert reading.independence_group == "ohlc:XAUUSD:M5"
    assert reading.implications == ()


def test_clustering_returns_nearest_prior_states_and_realized_outcomes_only():
    reading = _read(_bars())["clustering"]
    facts = reading.facts

    assert reading.applicable is True
    assert facts["historical_state_sample_count"] > 0
    assert facts["nearest_neighbour_count"] >= 1
    assert facts["nearest_historical_states"]
    assert all(
        record["bar_index"] + 20 < len(_bars())
        for record in facts["nearest_historical_states"]
    )
    assert facts["historical_analogue_results_are_not_forward_probabilities"] is True
    assert facts["directional_decision_emitted"] is False


def test_anomaly_detection_reports_own_history_novelty_without_signal():
    reading = _read(_bars())["anomaly_detection"]
    facts = reading.facts

    assert reading.applicable is True
    assert facts["novelty_distance"] is not None
    assert facts["novelty_own_history_percentile"] is not None
    assert 0.0 <= facts["novelty_own_history_percentile"] <= 1.0
    assert facts["novelty_is_not_a_trade_signal"] is True
    assert facts["novelty_requires_wider_uncertainty_not_directional_action"] is True
    assert reading.implications == ()


def test_short_history_and_unverified_time_remain_unknown():
    short = _read(_bars(12))
    assert short["clustering"].applicable is False
    assert short["anomaly_detection"].applicable is False
    assert short["clustering"].unknowns
    assert short["anomaly_detection"].unknowns

    bars = _bars()
    bars[-1]["source_timestamp_verified"] = False
    readings = _read(bars)
    for reading in readings.values():
        assert reading.source_bar_epoch is None
        assert "source-verified timestamp for the latest completed bar" in reading.unknowns


def test_reflected_price_history_remains_evaluable_without_directional_authority():
    direct = _read(_bars())
    reflected = _read(_bars(reflected=True))

    assert direct["anomaly_detection"].applicable is True
    assert reflected["anomaly_detection"].applicable is True
    assert math.isfinite(direct["anomaly_detection"].facts["novelty_distance"])
    assert math.isfinite(reflected["anomaly_detection"].facts["novelty_distance"])
    assert direct["clustering"].facts["historical_state_sample_count"] == (
        reflected["clustering"].facts["historical_state_sample_count"]
    )
    assert direct["clustering"].facts["directional_decision_emitted"] is False
    assert reflected["clustering"].facts["directional_decision_emitted"] is False


def test_state_vector_is_prefix_local_and_has_no_future_bar_dependency():
    bars = _bars()
    prefix = _state_vector(bars, 79)
    extended = _state_vector(bars + _bars(5)[1:], 79)
    assert prefix == extended


def test_state_vector_population_volatility_matches_reference_calculation():
    bars = _bars(60)
    vector = _state_vector(bars, 59)
    assert vector is not None
    returns = _returns([row["close"] for row in bars[-20:]])
    assert math.isclose(vector[1], pstdev(returns), rel_tol=1e-12, abs_tol=1e-15)


def test_analogue_and_novelty_share_one_state_history_build_per_timeframe():
    from cognition.concepts import quantitative_state_readings as module

    bars = _bars()
    with patch.object(
        module, "_historical_state_rows", wraps=module._historical_state_rows,
    ) as build_rows:
        readings = module.QuantitativeStateReadings().read(
            "XAUUSD", {"M5": bars, "M15": bars},
        )

    assert build_rows.call_count == 2
    assert len(readings) == 6
    for timeframe in ("M5", "M15"):
        by_concept = {item.concept: item for item in readings if item.timeframe == timeframe}
        assert by_concept["clustering"].facts["historical_state_sample_count"] > 0
        assert by_concept["anomaly_detection"].facts["historical_state_count"] > 0


def test_native_pipeline_emits_non_directional_quantitative_evidence():
    evidence = run_concepts("XAUUSD", {"M5": _bars()})
    selected = {
        item.source_module: item
        for item in evidence
        if item.source_module in {
            "concept.time_series.m5",
            "concept.clustering.m5",
            "concept.anomaly_detection.m5",
        }
    }

    assert set(selected) == {
        "concept.time_series.m5",
        "concept.clustering.m5",
        "concept.anomaly_detection.m5",
    }
    assert all(item.polarity == 0.0 for item in selected.values())
    assert all(item.measurements["source_kind"] == "concept_reading" for item in selected.values())
