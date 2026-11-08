"""Broker instrument economics: provenance, constraints and Brain wiring."""

from __future__ import annotations

import math
import time
from types import SimpleNamespace

import pandas as pd

from cognition.concepts import CONCEPT_MODULES
from cognition.concepts.instrument_economics_readings import InstrumentEconomicsReadings
from cognition.market_lenses import analyze_market_frames
from platforms.mt5 import mt5_connector as mt5_connector_module
from platforms.mt5.mt5_connector import MT5Connector


def _spec(**overrides):
    base = {
        "symbol": "XAUUSD",
        "broker_symbol": "XAUUSD.a",
        "source": "mt5.symbol_info",
        "source_verified": True,
        "specification_verified": True,
        "trade_contract_size": 100.0,
        "trade_tick_size": 0.01,
        "trade_tick_value": 1.0,
        "trade_tick_value_profit": 1.0,
        "trade_tick_value_loss": 1.0,
        "trade_mode": 4,
        "trade_calc_mode": 1,
        "trade_stops_level": 20,
        "trade_freeze_level": 5,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "digits": 2,
        "point": 0.01,
        "currency_base": "XAU",
        "currency_profit": "USD",
        "currency_margin": "USD",
        "swap_long": -2.5,
        "swap_short": 0.8,
        "swap_mode": 32,
        "swap_mode_name": "points",
        "swap_rollover3days": 3,
        "swap_schedule": {
            "sunday": 0, "monday": 1, "tuesday": 1, "wednesday": 3,
            "thursday": 1, "friday": 1, "saturday": 0,
        },
        "broker_session_timezone_verified": False,
        "broker_holiday_calendar_verified": False,
        "broker_trading_sessions": [],
        "raw_symbol_info": {"trade_contract_size": 100.0},
    }
    base.update(overrides)
    return base


def _frame():
    now = float(int(time.time() // 300) * 300)
    rows = []
    for index in range(40):
        close = 2300.0 + index * 0.1 + math.sin(index * 0.5) * 0.2
        rows.append({
            "time": now - (40 - index) * 300,
            "open": close - 0.05,
            "high": close + 0.15,
            "low": close - 0.15,
            "close": close,
            "volume": 100 + index,
            "tick_volume": 100 + index,
            "real_volume": 0,
        })
    frame = pd.DataFrame(rows)
    frame.attrs.update({
        "source_symbol": "XAUUSD",
        "source_timestamp_origin": "broker_ohlcv",
        "source_broker": "mt5",
        "source_timestamp_verified": True,
        "source_timestamp_unit": "unix_seconds",
        "volume_basis": "tick_volume_alias_with_real_volume_preserved",
    })
    return frame


def _readings(spec):
    readings = InstrumentEconomicsReadings().read_with_context(
        "XAUUSD", {}, {"symbol_spec": spec},
    )
    return {reading.concept: reading for reading in readings}


def _reading(spec):
    return _readings(spec)["instrument_economics"]


def _carry(spec):
    return _readings(spec)["carry"]


def test_broker_verified_contract_spec_is_read_as_independent_neutral_evidence():
    reading = _reading(_spec())
    facts = reading.facts

    assert reading.applicable is True
    assert reading.timeframe == "SPEC"
    assert reading.domain.value == "execution_quality"
    assert reading.implications == ()
    assert facts["status"] == "verified_but_incomplete"
    assert facts["source_verified"] is True
    assert facts["contract_size"] == 100.0
    assert facts["tick_to_point_ratio"] if "tick_to_point_ratio" in facts else facts["price_precision"]["tick_to_point_ratio"] == 1.0
    assert facts["broker_tick_value_per_price_unit"] == 100.0
    assert facts["volume_constraints"]["minimum_step_aligned"] is True
    assert facts["volume_constraints"]["maximum_step_aligned"] is True
    assert facts["stop_distance_price_units"] == 0.2
    assert facts["freeze_distance_price_units"] == 0.05
    assert facts["trade_mode_interpretation"] == "full"
    assert facts["swap"]["unit_interpreted"] is False
    assert facts["execution_authorization_emitted"] is False
    assert any("provider-originated specification timestamp" in item for item in reading.unknowns)


def test_unavailable_or_unverified_broker_spec_stays_unknown_without_config_defaults():
    for spec in ({}, _spec(source_verified=False), _spec(specification_verified=False)):
        reading = _reading(spec)
        assert reading.applicable is False
        assert reading.facts["status"] == "unavailable_or_unverified"
        assert reading.facts["contract_size"] is None
        assert reading.facts["no_config_fallback_used"] is True
        if spec:
            assert reading.facts["unverified_reported_fields"]["trade_contract_size"] == 100.0
        assert reading.unknowns


def test_inconsistent_contract_volume_constraints_are_rejected_not_repaired():
    reading = _reading(_spec(volume_min=0.03, volume_max=1.0, volume_step=0.02))
    assert reading.applicable is False
    assert reading.facts["status"] == "broker_specification_inconsistent"
    assert "volume_min is not aligned to volume_step" in reading.facts["validation_errors"]


def test_absent_swap_semantics_remain_raw_and_uninterpreted():
    reading = _reading(_spec(swap_mode=None, swap_long=None, swap_short=None))
    assert reading.facts["swap"] == {
        "long": None, "short": None, "mode": None, "unit_interpreted": False,
    }


def test_live_market_lens_passes_broker_spec_into_brain_with_independent_lineage():
    evidence = analyze_market_frames(
        "XAUUSD",
        {"M5": _frame()},
        source_includes_forming_bar=False,
        concept_context={"symbol_spec": _spec()},
    )
    selected = {
        ev.measurements["concept"]: ev
        for ev in evidence
        if ev.measurements.get("source_kind") == "concept_reading"
        and ev.measurements.get("concept") in {"instrument_economics", "carry"}
    }
    assert set(selected) == {"instrument_economics", "carry"}
    item = selected["instrument_economics"]
    carry = selected["carry"]
    assert carry.polarity == 0.0
    assert carry.data_basis == "broker_symbol_specification"
    assert carry.independence_group == item.independence_group
    assert carry.source_timestamp_verified is False
    assert carry.source_timestamp_epoch is None
    assert carry.measurements["facts"]["swap_mode_name"] == "points"
    assert item.polarity == 0.0
    assert item.data_basis == "broker_symbol_specification"
    assert item.independence_group == "broker_symbol_spec:mt5.symbol_info:XAUUSD"
    assert item.source_timestamp_verified is False
    assert item.source_timestamp_epoch is None
    assert item.measurements["facts"]["source_verified"] is True
    assert item.measurements["facts"]["trade_mode_interpretation"] == "full"
    assert item.measurements["unknowns"]


def test_broker_spec_reading_is_emitted_even_when_ohlc_history_is_absent():
    evidence = analyze_market_frames(
        "XAUUSD",
        {},
        concept_context={"symbol_spec": _spec()},
    )
    selected = {
        ev.measurements["concept"]: ev
        for ev in evidence
        if ev.measurements.get("source_kind") == "concept_reading"
        and ev.measurements.get("concept") in {"instrument_economics", "carry"}
    }
    assert set(selected) == {"instrument_economics", "carry"}
    assert selected["instrument_economics"].measurements["facts"]["contract_size"] == 100.0
    assert selected["carry"].measurements["facts"]["swap_mode_name"] == "points"
    assert selected["instrument_economics"].source_timestamp_verified is False
    assert selected["carry"].source_timestamp_verified is False



def test_mt5_swap_mode_resolution_uses_the_installed_package_constants(monkeypatch):
    constants = {
        "SYMBOL_SWAP_MODE_DISABLED": 31,
        "SYMBOL_SWAP_MODE_POINTS": 32,
        "SYMBOL_SWAP_MODE_CURRENCY_SYMBOL": 33,
        "SYMBOL_SWAP_MODE_CURRENCY_MARGIN": 34,
        "SYMBOL_SWAP_MODE_CURRENCY_DEPOSIT": 35,
        "SYMBOL_SWAP_MODE_CURRENCY_PROFIT": 36,
        "SYMBOL_SWAP_MODE_INTEREST_CURRENT": 37,
        "SYMBOL_SWAP_MODE_INTEREST_OPEN": 38,
        "SYMBOL_SWAP_MODE_REOPEN_CURRENT": 39,
        "SYMBOL_SWAP_MODE_REOPEN_BID": 40,
    }
    monkeypatch.setattr(
        mt5_connector_module,
        "mt5",
        SimpleNamespace(**constants),
    )
    assert MT5Connector._swap_mode_name(32) == "points"
    assert MT5Connector._swap_mode_name(37) == "interest_current"
    assert MT5Connector._swap_mode_name(40) == "reopen_bid"
    assert MT5Connector._swap_mode_name(999) is None


def test_instrument_and_carry_method_registration_is_explicit():
    module = next(item for item in CONCEPT_MODULES if item.key == "instrument_economics_readings")
    assert set(module.covers_methods) == {"instrument_economics"}
    assert module.data_gated_methods == {
        "carry": (
            "verified direction-specific broker swap/financing schedule, rollover multipliers, "
            "and instrument/account-currency conversion"
        ),
    }
    assert module.covers_lenses == ()
    assert _carry(_spec()).facts["coverage_status"] == "PARTIAL"
    assert "position-specific cash financing amount after broker contract volume and calculation mode are applied" in _carry(_spec()).facts["unimplemented_components"]


def test_carry_points_mode_interprets_units_without_fabricating_cash_cost():
    facts = _carry(_spec(swap_mode_name="points", swap_long=2.5, swap_short=-1.0)).facts

    assert facts["swap_units"] == "points"
    assert facts["long"]["price_unit_equivalent"] == 0.025
    assert facts["short"]["price_unit_equivalent"] == -0.01
    assert facts["long"]["cash_amount"] is None
    assert facts["long"]["unit_note"].startswith("price-unit equivalent only")
    assert facts["rollover"]["triple_swap_weekday"] == "wednesday"
    assert facts["rollover"]["daily_multiplier"]["wednesday"] == 3.0
    assert facts["cash_carry_cost_calculated"] is False
    assert facts["execution_authorization_emitted"] is False
    assert any("provider-originated financing specification timestamp" in item for item in _carry(_spec()).unknowns)


def test_carry_currency_modes_keep_the_declared_currency_denominator():
    cases = (
        ("currency_symbol", "XAU", "money_in_symbol_base_currency"),
        ("currency_margin", "USD", "money_in_symbol_margin_currency"),
        ("currency_profit", "USD", "money_in_symbol_profit_currency"),
    )
    for mode, currency, units in cases:
        facts = _carry(_spec(swap_mode_name=mode)).facts
        assert facts["swap_units"] == units
        assert facts["long"]["currency_code"] == currency
        assert facts["short"]["currency_code"] == currency


def test_carry_deposit_currency_mode_does_not_guess_the_account_currency():
    facts = _carry(_spec(swap_mode_name="currency_deposit")).facts
    assert facts["swap_units"] == "money_in_client_deposit_currency"
    assert facts["long"]["currency_code"] is None
    assert facts["long"]["currency_label"].startswith("client deposit currency")


def test_carry_interest_modes_use_documented_360_day_basis_without_cash_conversion():
    for mode in ("interest_current", "interest_open"):
        facts = _carry(_spec(
            swap_mode_name=mode, swap_long=2.5, swap_short=-1.25,
        )).facts
        assert facts["interest_year_basis_days"] == 360
        assert facts["long"]["annual_interest_percent"] == 2.5
        assert math.isclose(
            facts["long"]["daily_rate_fraction_360_day_basis"],
            2.5 / 100.0 / 360.0,
        )
        assert facts["cash_carry_cost_calculated"] is False


def test_carry_reopen_modes_preserve_forced_reopen_semantics():
    expected = {
        "reopen_current": "previous_session_close",
        "reopen_bid": "new_day_bid",
    }
    for mode, reference in expected.items():
        facts = _carry(_spec(swap_mode_name=mode)).facts
        assert facts["forced_reopen_at_session_end"] is True
        assert facts["long"]["forced_reopen"] is True
        assert facts["long"]["reopen_reference"] == reference


def test_unknown_carry_enum_and_incomplete_rollover_schedule_remain_unknown():
    unknown_mode = _carry(_spec(swap_mode_name=None))
    assert unknown_mode.applicable is False
    assert unknown_mode.facts["status"] == "verified_but_incomplete"
    assert any("swap calculation mode resolved" in item for item in unknown_mode.unknowns)

    incomplete = _carry(_spec(swap_schedule={}))
    assert incomplete.applicable is True
    assert any("complete broker weekday swap-multiplier schedule" in item for item in incomplete.unknowns)


def test_disabled_carry_mode_is_explicitly_no_charge():
    facts = _carry(_spec(swap_mode_name="disabled")).facts
    assert facts["swap_units"] == "no_swap_charge"
    assert facts["long"]["charge"] == "none"
    assert facts["short"]["charge"] == "none"
