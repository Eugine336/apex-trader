"""Contract tests for point-in-time quantitative outcome readers."""
from __future__ import annotations

import math

from cognition.concepts.base import ConceptReading
from cognition.contracts import EvidenceDomain
from cognition.concepts.quantitative_outcome_readings import (
    DATA_GATED_METHODS,
    QuantitativeOutcomeReadings,
)


def _bars(count: int = 40):
    return [
        {
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.5 + index,
            "timestamp_epoch": 1_780_000_000.0 + index * 300.0,
            "source_timestamp_verified": True,
        }
        for index in range(count)
    ]


def _readings(context=None):
    return QuantitativeOutcomeReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, context or {},
    )


def test_quantitative_outcome_methods_are_explicitly_data_gated():
    assert set(DATA_GATED_METHODS) == {"bayesian", "calibration", "monte_carlo"}
    readings = _readings()
    assert {item.concept for item in readings} == set(DATA_GATED_METHODS)
    for item in readings:
        assert item.applicable is False
        assert item.facts["data_gated_reason"]
        assert item.unknowns
        assert item.implications == ()


def test_bayesian_reader_uses_verified_mature_outcomes_and_state_conditional_beta_posterior():
    cutoff = _bars()[-1]["timestamp_epoch"]
    rows = [
        {
            "symbol": "XAUUSD", "concept": "trend_following", "market_state": "trend",
            "decision_epoch": cutoff - 900, "outcome_epoch": cutoff - 600,
            "source_timestamp_epoch": cutoff - 600, "source_timestamp_verified": True,
            "outcome": outcome,
        }
        for outcome in (1, 1, 0)
    ]
    item = next(
        reading for reading in _readings({
            "market_state": "trend", "concept_outcome_history": rows,
        }) if reading.concept == "bayesian"
    )
    posterior = item.facts["posterior_by_concept"]["trend_following"]["posterior"]
    assert item.applicable is True
    assert posterior["alpha"] == 2.5
    assert posterior["beta"] == 1.5
    assert math.isclose(posterior["mean"], 0.625)
    lower, upper = posterior["credible_interval_95"]
    assert 0.0 < lower < posterior["mean"] < upper < 1.0
    assert item.facts["posterior_is_current_trade_probability"] is False



def test_bayesian_reader_derives_market_state_from_prior_time_series_reading():
    cutoff = _bars()[-1]["timestamp_epoch"]
    state_reading = ConceptReading(
        concept="time_series",
        timeframe="M5",
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=True,
        applicability_reason="confirmed swing state available",
        observation="upward confirmed swing structure",
        implications=(),
        facts={"structural_regime_from_confirmed_swings": "up"},
        source_bar_epoch=cutoff,
        source_timestamp_verified=True,
    )
    rows = [
        {
            "symbol": "XAUUSD", "concept": "trend_following", "market_state": "up",
            "decision_epoch": cutoff - 900, "outcome_epoch": cutoff - 600,
            "source_timestamp_epoch": cutoff - 600, "source_timestamp_verified": True,
            "outcome": outcome,
        }
        for outcome in (1, 0, 1)
    ]
    readings = QuantitativeOutcomeReadings().read_with_context(
        "XAUUSD", {"M5": _bars()},
        {"concept_readings": (state_reading,), "concept_outcome_history": rows},
    )
    item = next(reading for reading in readings if reading.concept == "bayesian")
    assert item.applicable is True
    assert item.facts["current_market_state"] == "up"
    assert item.facts["posterior_by_concept"]["trend_following"]["sample_count"] == 3


def test_bayesian_reader_rejects_future_or_unverified_outcomes():
    cutoff = _bars()[-1]["timestamp_epoch"]
    rows = [
        {
            "symbol": "XAUUSD", "concept": "trend_following", "market_state": "trend",
            "decision_epoch": cutoff - 300, "outcome_epoch": cutoff + 300,
            "source_timestamp_epoch": cutoff + 300, "source_timestamp_verified": True,
            "outcome": 1,
        },
        {
            "symbol": "XAUUSD", "concept": "trend_following", "market_state": "trend",
            "decision_epoch": cutoff - 300, "outcome_epoch": cutoff - 100,
            "source_timestamp_epoch": cutoff - 100, "source_timestamp_verified": False,
            "outcome": 1,
        },
    ]
    item = next(
        reading for reading in _readings({
            "market_state": "trend", "concept_outcome_history": rows,
        }) if reading.concept == "bayesian"
    )
    assert item.applicable is False
    assert item.facts["outcome_rows_accepted"] == 0
    assert item.facts["outcome_rows_rejected"] == 2


def test_calibration_reader_uses_only_selection_complete_holdout_forecasts():
    cutoff = _bars()[-1]["timestamp_epoch"]
    rows = [
        {
            "symbol": "XAUUSD", "concept": "mean_reversion",
            "forecast_probability": probability, "outcome": outcome,
            "forecast_epoch": cutoff - 900, "outcome_epoch": cutoff - 300,
            "source_timestamp_epoch": cutoff - 300, "source_timestamp_verified": True,
            "split": "holdout", "selection_complete": True,
        }
        for probability, outcome in ((0.8, 1), (0.7, 0), (0.6, 1))
    ]
    rows.append({
        "symbol": "XAUUSD", "concept": "mean_reversion",
        "forecast_probability": 0.99, "outcome": 1,
        "forecast_epoch": cutoff - 900, "outcome_epoch": cutoff - 300,
        "source_timestamp_epoch": cutoff - 300, "source_timestamp_verified": True,
        "split": "development", "selection_complete": True,
    })
    item = next(
        reading for reading in _readings({"concept_probability_forecasts": rows})
        if reading.concept == "calibration"
    )
    result = item.facts["calibration_by_concept"]["mean_reversion"]
    assert item.applicable is True
    assert result["sample_count"] == 3
    assert math.isclose(result["brier_score"], ((0.8 - 1) ** 2 + 0.7 ** 2 + (0.6 - 1) ** 2) / 3)
    assert result["holdout_only"] is True
    assert item.facts["predictive_ability_demonstrated"] is False


def test_calibration_reader_rejects_unverified_and_non_holdout_records():
    cutoff = _bars()[-1]["timestamp_epoch"]
    rows = [{
        "symbol": "XAUUSD", "concept": "momentum",
        "forecast_probability": 0.7, "outcome": 1,
        "forecast_epoch": cutoff - 900, "outcome_epoch": cutoff - 300,
        "source_timestamp_epoch": cutoff - 300, "source_timestamp_verified": True,
        "split": "development", "selection_complete": True,
    }]
    item = next(
        reading for reading in _readings({"concept_probability_forecasts": rows})
        if reading.concept == "calibration"
    )
    assert item.applicable is False
    assert item.facts["calibration_by_concept"] == {}
    assert item.facts["forecast_rows_rejected"] == 1


def test_monte_carlo_reader_runs_deterministic_historical_trade_bootstrap():
    cutoff = _bars()[-1]["timestamp_epoch"]
    rows = [
        {
            "trade_id": f"trade-{index}", "portfolio_id": "account-main",
            "return_r": value, "closed_epoch": cutoff - (index + 1) * 300,
            "source_timestamp_epoch": cutoff - (index + 1) * 300,
            "source_timestamp_verified": True,
        }
        for index, value in enumerate((1.0, -0.5, 0.7, -1.0, 0.4))
    ]
    context = {
        "trade_outcome_samples": rows,
        "portfolio_plan": {
            "portfolio_id": "account-main", "portfolio_scope": "whole_book",
            "equity": 10000.0, "risk_fraction": 0.01,
            "planned_trade_count": 10,
            "plan_as_of_epoch": cutoff + 30.0, "plan_attested": True,
        },
        "analysis_epoch": cutoff + 60.0,
        "monte_carlo_iterations": 500,
    }
    first = next(item for item in _readings(context) if item.concept == "monte_carlo")
    second = next(item for item in _readings(context) if item.concept == "monte_carlo")
    assert first.applicable is True
    assert first.facts["simulation_count"] == 500
    assert first.facts["bootstrap_terminal_return_r_5pct"] == second.facts["bootstrap_terminal_return_r_5pct"]
    assert first.facts["monte_carlo_drawdown_95pct_r"] >= 0.0
    assert first.facts["current_portfolio_plan_stressed"] is True
    assert first.facts["portfolio_scope"] == "whole_book"
    assert first.facts["probability_of_ruin"] is not None



def test_monte_carlo_rejects_stale_portfolio_plan_even_with_valid_trade_history():
    cutoff = _bars()[-1]["timestamp_epoch"]
    rows = [
        {
            "trade_id": f"trade-{index}", "portfolio_id": "account-main",
            "return_r": value, "closed_epoch": cutoff - (index + 1) * 300,
            "source_timestamp_epoch": cutoff - (index + 1) * 300,
            "source_timestamp_verified": True,
        }
        for index, value in enumerate((1.0, -0.5, 0.7))
    ]
    item = next(
        reading for reading in _readings({
            "trade_outcome_samples": rows,
            "portfolio_plan": {
                "portfolio_id": "account-main", "portfolio_scope": "whole_book",
                "equity": 10000.0, "risk_fraction": 0.01,
                "planned_trade_count": 10,
                "plan_as_of_epoch": cutoff - 30.0,
                "plan_attested": True,
            },
            "analysis_epoch": cutoff + 60.0,
        })
        if reading.concept == "monte_carlo"
    )
    assert item.applicable is False
    assert item.facts["simulation_count"] == 0
    assert item.facts["current_portfolio_plan_stressed"] is False
    assert any("timestamped after" in item for item in item.unknowns)



def test_monte_carlo_positive_only_trade_paths_do_not_report_phantom_drawdown():
    cutoff = _bars()[-1]["timestamp_epoch"]
    rows = [
        {
            "trade_id": f"positive-{index}", "portfolio_id": "account-main",
            "return_r": value, "closed_epoch": cutoff - (index + 1) * 300,
            "source_timestamp_epoch": cutoff - (index + 1) * 300,
            "source_timestamp_verified": True,
        }
        for index, value in enumerate((0.5, 0.7, 0.2))
    ]
    item = next(
        reading for reading in _readings({
            "trade_outcome_samples": rows,
            "portfolio_plan": {
                "portfolio_id": "account-main", "portfolio_scope": "whole_book",
                "equity": 10000.0, "risk_fraction": 0.01,
                "planned_trade_count": 12,
                "plan_as_of_epoch": cutoff + 30.0, "plan_attested": True,
            },
            "analysis_epoch": cutoff + 60.0,
            "monte_carlo_iterations": 300,
        })
        if reading.concept == "monte_carlo"
    )
    assert item.applicable is True
    assert item.facts["monte_carlo_drawdown_95pct_r"] == 0.0
    assert item.facts["portfolio_drawdown_95pct_fraction"] == 0.0
    assert item.facts["probability_of_ruin"] == 0.0


def test_monte_carlo_reader_rejects_duplicate_and_future_trade_outcomes():
    cutoff = _bars()[-1]["timestamp_epoch"]
    rows = [
        {"trade_id": "duplicate", "portfolio_id": "account-main", "return_r": 1.0,
         "closed_epoch": cutoff - 300, "source_timestamp_epoch": cutoff - 300,
         "source_timestamp_verified": True},
        {"trade_id": "duplicate", "portfolio_id": "account-main", "return_r": -1.0,
         "closed_epoch": cutoff - 300, "source_timestamp_epoch": cutoff - 300,
         "source_timestamp_verified": True},
        {"trade_id": "future", "portfolio_id": "account-main", "return_r": 2.0,
         "closed_epoch": cutoff + 300, "source_timestamp_epoch": cutoff + 300,
         "source_timestamp_verified": True},
    ]
    item = next(
        reading for reading in _readings({
            "trade_outcome_samples": rows,
            "portfolio_plan": {
                "portfolio_id": "account-main", "portfolio_scope": "whole_book",
                "equity": 10000.0, "risk_fraction": 0.01,
                "planned_trade_count": 10,
                "plan_as_of_epoch": cutoff, "plan_attested": True,
            },
            "analysis_epoch": cutoff + 60.0,
        })
        if reading.concept == "monte_carlo"
    )
    assert item.applicable is False
    assert item.facts["trade_sample_count"] == 1
    assert item.facts["data_gated_reason"]
