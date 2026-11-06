"""Tests for broker-grounded execution-cost evidence."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cognition.concepts.base import reading_to_evidence
from cognition.concepts.execution_cost_readings import ExecutionCostReadings
from cognition.execution_cost_model import build_execution_cost_reading
from cognition.loop import EvidenceConsolidator


def _quote(*, age_seconds=0.2, verified=True, kind="bid_ask", symbol="EURUSD",
           bid=1.0999, ask=1.1001):
    return SimpleNamespace(
        bid=bid, ask=ask,
        time=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        source_timestamp_verified=verified, quote_kind=kind, symbol=symbol,
    )


def _spec():
    return {
        "source": "mt5.symbol_info",
        "source_verified": True,
        "specification_verified": True,
        "trade_tick_size": 0.00001,
        "trade_tick_value": 1.0,
    }


def test_cost_model_preserves_quote_lineage_and_separates_partial_costs():
    reading = build_execution_cost_reading(
        "EURUSD", _quote(), _spec(),
        commission_per_lot_round_trip=5.0, commission_model_verified=True,
        financing_per_lot_round_trip={"EURUSD": 1.0}, financing_model_verified=True,
        now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert reading["source_timestamp_verified"] is True
    assert reading["quote"]["symbol"] == "EURUSD"
    assert reading["quote"]["quote_kind"] == "bid_ask"
    assert reading["quote"]["spread"] == pytest.approx(0.0002)
    assert reading["money_per_price_account_ccy_per_lot"] == pytest.approx(100000.0)
    assert reading["estimated_spread_account_ccy_per_lot"] == pytest.approx(20.0)
    assert reading["estimated_commission_account_ccy_per_lot"] == pytest.approx(5.0)
    assert reading["estimated_financing_account_ccy_per_lot"] == pytest.approx(1.0)
    assert reading["estimated_total_cost_account_ccy_per_lot"] is None
    assert reading["cost_verified"] is False
    assert any("round-trip" in item for item in reading["cost_unknown_components"])


def test_cost_model_rejects_mid_only_wrong_symbol_stale_and_unverified_quotes():
    for quote in (_quote(kind="mid_only"), _quote(symbol="GBPUSD"),
                  _quote(age_seconds=9.0), _quote(verified=False)):
        reading = build_execution_cost_reading(
            "EURUSD", quote, _spec(),
            now_epoch=datetime.now(timezone.utc).timestamp(),
        )
        assert reading["source_timestamp_verified"] is False
        assert reading["quote"] is None
        assert reading["cost_verified"] is False


def test_cost_model_requires_both_tick_value_sides_without_generic_value():
    spec = _spec()
    spec.pop("trade_tick_value")
    spec["trade_tick_value_profit"] = 1.1
    reading = build_execution_cost_reading(
        "EURUSD", _quote(), spec,
        commission_per_lot_round_trip=0.0, commission_model_verified=True,
        financing_per_lot_round_trip={"EURUSD": 0.0}, financing_model_verified=True,
    )
    assert reading["money_per_price_account_ccy_per_lot"] is None
    assert any("tick size/value" in item for item in reading["cost_unknown_components"])


def test_cost_model_verifies_total_only_with_verified_round_trip_slippage():
    reading = build_execution_cost_reading(
        "EURUSD", _quote(), _spec(),
        commission_per_lot_round_trip=5.0, commission_model_verified=True,
        financing_per_lot_round_trip={"EURUSD": 1.0}, financing_model_verified=True,
        execution_observations={
            "round_trip_slippage_verified": True,
            "observed_round_trip_slippage_sample_count": 20,
            "observed_round_trip_abs_slippage_avg_pips": 0.4,
            "pip_size": 0.0001,
        },
        now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    assert reading["estimated_round_trip_slippage_account_ccy_per_lot"] == pytest.approx(4.0)
    assert reading["estimated_total_cost_account_ccy_per_lot"] == pytest.approx(30.0)
    assert reading["cost_verified"] is True


def test_concept_reader_preserves_quote_timestamp_as_provider_time():
    payload = build_execution_cost_reading(
        "EURUSD", _quote(), _spec(),
        commission_per_lot_round_trip=5.0, commission_model_verified=True,
        financing_per_lot_round_trip={"EURUSD": 1.0}, financing_model_verified=True,
        now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    readings = ExecutionCostReadings().read_with_context(
        "EURUSD", {}, {"execution_cost": payload},
    )
    assert len(readings) == 1
    evidence = reading_to_evidence("EURUSD", readings[0])
    assert evidence.domain.value == "execution_quality"
    assert evidence.polarity == 0.0
    assert evidence.source_timestamp_verified is True
    assert evidence.source_timestamp_epoch == pytest.approx(payload["source_timestamp_epoch"])
    assert evidence.measurements["facts"]["cost_verified"] is False


def test_concept_reader_emits_nothing_without_source_payload():
    assert ExecutionCostReadings().read_with_context("EURUSD", {}, {}) == []



def test_cost_evidence_exposes_brain_gate_and_analytical_measurements():
    from cognition.analytical_knowledge import assess_analytical_knowledge

    payload = build_execution_cost_reading(
        "EURUSD", _quote(), _spec(),
        commission_per_lot_round_trip=5.0,
        commission_model_verified=True,
        financing_per_lot_round_trip={"EURUSD": 1.0},
        financing_model_verified=True,
        execution_observations={
            "market_entry_attempt_count": 20,
            "market_entry_fill_success_ratio": 0.9,
            "market_entry_fill_rate_sample_floor_met": True,
            "market_entry_fill_probability_min_samples": 20,
            "market_entry_fill_probability_sample_count": 20,
            "observed_entry_abs_slippage_avg_pips": 0.2,
            "observed_entry_slippage_sample_count": 20,
            "observed_submit_response_latency_avg_ms": 120.0,
            "observed_submit_response_latency_sample_count": 20,
            "round_trip_slippage_verified": True,
            "observed_round_trip_slippage_sample_count": 20,
            "observed_round_trip_abs_slippage_avg_pips": 0.4,
            "pip_size": 0.0001,
        },
        now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    evidence = reading_to_evidence(
        "EURUSD",
        ExecutionCostReadings().read_with_context(
            "EURUSD", {}, {"execution_cost": payload},
        )[0],
    )
    # Brain reads these values at the top level, not only from measurements.facts.
    assert evidence.measurements["cost_verified"] is True
    assert evidence.measurements["cost_source"]
    assert evidence.measurements["estimated_total_cost_account_ccy_per_lot"] == pytest.approx(30.0)
    assert evidence.measurements["money_per_price_account_ccy_per_lot"] == pytest.approx(100000.0)

    assessments = assess_analytical_knowledge([evidence])
    cost_assessment = next(item for item in assessments if item["key"] == "execution_cost")
    assert set(cost_assessment["matched_measurements"]) == {
        "spread", "commission", "slippage", "swap", "latency", "fill_probability",
    }



def test_execution_cost_source_is_added_to_the_brains_market_state():
    payload = build_execution_cost_reading(
        "EURUSD", _quote(), _spec(),
        now_epoch=datetime.now(timezone.utc).timestamp(),
    )
    state = EvidenceConsolidator(
        execution_cost_source=lambda symbol: payload,
    ).build("EURUSD")
    cost_rows = [
        item for item in state.evidence
        if item.source_module == "concept.execution_cost.spec"
    ]
    assert len(cost_rows) == 1
    assert cost_rows[0].domain.value == "execution_quality"
    assert cost_rows[0].measurements["cost_verified"] is False
    assert cost_rows[0].source_timestamp_verified is True


def test_concept_reader_rejects_non_finite_provider_epoch():
    payload = {
        "source_timestamp_epoch": float("inf"),
        "source_timestamp_verified": True,
        "cost_verified": False,
        "cost_components_complete": False,
        "cost_unknown_components": ["provider timestamp invalid"],
    }
    reading = ExecutionCostReadings().read_with_context(
        "EURUSD", {}, {"execution_cost": payload},
    )[0]
    assert reading.applicable is False
    assert reading.source_timestamp_epoch is None
    assert reading.source_timestamp_verified is False
