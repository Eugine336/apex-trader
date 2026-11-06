"""Wyckoff and volume readings: provenance, symmetry, boundaries and live wiring."""

from __future__ import annotations

import math
import time

import pandas as pd

from cognition.concepts import CONCEPT_MODULES, run_concepts
from cognition.concepts.wyckoff_volume_readings import (
    WyckoffVolumeReadings,
    _wyckoff_implications,
)
from cognition.market_lenses import analyze_market_frames


def _bars(count: int = 70, *, source_verified: bool = True) -> list[dict]:
    rows = []
    close = 100.0
    for index in range(count):
        prior = close
        close = 100.0 + index * 0.035 + 0.45 * math.sin(index * 0.67)
        opening = prior + 0.12 * math.sin(index * 0.23)
        spread = 0.25 + 0.08 * (1.0 + math.sin(index * 0.37))
        high = max(opening, close) + spread
        low = min(opening, close) - spread
        volume = 100.0 + 16.0 * math.sin(index * 0.41) + index * 0.7
        if index == count - 1:
            volume *= 3.2
            high = max(opening, close) + 0.18
            low = min(opening, close) - 0.18
        rows.append({
            "open": opening,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "volume_source": "provider_volume",
            "timestamp_epoch": 1_780_000_000.0 + index * 300,
            "source_timestamp_verified": source_verified,
        })
    return rows


def _readings(bars: list[dict]):
    result = WyckoffVolumeReadings().read("XAUUSD", {"M5": bars})
    return {item.concept: item for item in result}


def _reflect(bars: list[dict], axis: float = 500.0) -> list[dict]:
    return [
        {
            **bar,
            "open": axis - bar["open"],
            "high": axis - bar["low"],
            "low": axis - bar["high"],
            "close": axis - bar["close"],
        }
        for bar in bars
    ]


def test_wyckoff_volume_reader_is_registered_and_claims_only_its_three_methods():
    modules = [module for module in CONCEPT_MODULES if module.key == "wyckoff_volume_readings"]
    assert len(modules) == 1
    assert set(modules[0].covers_methods) == {
        "wyckoff", "wyckoff_effort_result", "volume_indicators",
    }
    assert modules[0].covers_lenses == ()


def test_effort_result_compares_effort_with_price_result_and_does_not_claim_absorption():
    bars = _bars()
    reading = _readings(bars)["wyckoff_effort_result"]
    facts = reading.facts

    assert reading.applicable is True
    assert facts["volume_percentile_vs_prior_observations"] is not None
    assert facts["range_percentile_vs_prior_observations"] is not None
    assert facts["effort_result_state"] in {
        "volume_effort_rank_exceeds_range_result",
        "range_result_rank_exceeds_volume_effort",
        "effort_result_ranks_aligned",
    }
    assert facts["absorption_is_confirmed"] is False
    assert facts["tick_volume_is_trade_aggressor_volume"] is False
    assert facts["directional_decision_emitted"] is False
    assert {item.direction for item in reading.implications} == {"LONG", "SHORT", "FLAT"}
    assert all(item.requires for item in reading.implications)


def test_volume_indicators_report_core_values_and_confirmed_price_indicator_divergence():
    reading = _readings(_bars())["volume_indicators"]
    facts = reading.facts

    assert reading.applicable is True
    assert facts["obv"] is not None
    assert facts["mfi_14"] is not None
    assert facts["chaikin_money_flow_20"] is not None
    assert facts["accumulation_distribution_line"] is not None
    assert facts["volume_rate_of_change_10"] is not None
    assert facts["confirmed_price_indicator_divergence"]["obv"]["lookahead"] is False
    assert facts["directional_decision_emitted"] is False
    assert {item.direction for item in reading.implications} == {"LONG", "SHORT", "FLAT"}
    assert all(item.requires for item in reading.implications)


def test_wyckoff_reader_keeps_accumulation_and_distribution_as_competing_hypotheses():
    reading = _readings(_bars())["wyckoff"]
    facts = reading.facts

    assert reading.applicable is True
    assert facts["phase"] in {"unresolved", "competing"} or facts["phase"].endswith(("_A", "_B", "_C", "_D", "_E"))
    assert facts["phase_sequence_completeness"] is not None
    assert facts["phase_confidence_semantics"].endswith("not a calibrated probability")
    assert "accumulation_or_markup" in facts["competing_interpretations"]
    assert "distribution_or_markdown" in facts["competing_interpretations"]
    assert facts["phase_not_overclaimed"] is True
    assert facts["directional_decision_emitted"] is False
    assert {item.direction for item in reading.implications} == {"LONG", "SHORT", "FLAT"}
    assert all(item.invalidation_reason for item in reading.implications if item.direction != "FLAT")


def test_wyckoff_interpretation_is_symmetric_under_price_reflection():
    bars = _bars()
    original = _readings(bars)["wyckoff"].facts["competing_interpretations"]
    reflected = _readings(_reflect(bars))["wyckoff"].facts["competing_interpretations"]

    assert bool(original["accumulation_or_markup"]["supporting_observations"]) == bool(
        reflected["distribution_or_markdown"]["supporting_observations"]
    )
    assert bool(original["distribution_or_markdown"]["supporting_observations"]) == bool(
        reflected["accumulation_or_markup"]["supporting_observations"]
    )
    original_invalidation = original["accumulation_or_markup"]["invalidation_level"]
    reflected_invalidation = reflected["distribution_or_markdown"]["invalidation_level"]
    assert original_invalidation is not None and reflected_invalidation is not None
    assert math.isclose(
        original_invalidation,
        500.0 - reflected_invalidation,
        rel_tol=1e-12,
        abs_tol=1e-12,
    )


def test_missing_volume_and_short_history_remain_unknown_not_zero():
    short = _bars(12)
    for bar in short:
        bar["volume"] = None
        bar["volume_source"] = "unavailable"
    readings = _readings(short)

    assert readings["volume_indicators"].applicable is False
    assert readings["volume_indicators"].facts["obv"] is None
    assert readings["wyckoff_effort_result"].applicable is False
    assert readings["wyckoff_effort_result"].facts["volume_percentile_vs_prior_observations"] is None
    assert readings["wyckoff"].applicable is False
    assert readings["wyckoff"].facts["preceding_range"]["high"] is not None
    assert readings["wyckoff"].unknowns


def test_unverified_source_time_never_becomes_a_verified_evidence_timestamp():
    readings = _readings(_bars(source_verified=False))
    assert all(item.source_bar_epoch is None for item in readings.values())
    assert all(
        "source-verified timestamp for the latest completed bar" in item.unknowns
        for item in readings.values()
    )


def test_live_market_lens_preserves_tick_and_real_volume_as_separate_fields():
    bars = _bars()
    rows = []
    now_epoch = float(int(time.time() // 300) * 300)
    for index, bar in enumerate(bars):
        rows.append({
            "time": now_epoch - (len(bars) - index) * 300,
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "close": bar["close"],
            "volume": bar["volume"],
            "tick_volume": bar["volume"],
            "real_volume": 0.0,
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

    evidence = analyze_market_frames("XAUUSD", {"M5": frame}, source_includes_forming_bar=False)
    selected = {
        item.measurements["concept"]: item
        for item in evidence
        if item.measurements.get("source_kind") == "concept_reading"
        and not item.measurements.get("lens_key")
        and item.measurements.get("concept") in {
            "wyckoff", "wyckoff_effort_result", "volume_indicators",
        }
    }
    assert set(selected) == {"wyckoff", "wyckoff_effort_result", "volume_indicators"}
    for item in selected.values():
        assert item.polarity == 0.0
        assert item.source_timestamp_verified is True
    for key in ("wyckoff_effort_result", "volume_indicators"):
        assert selected[key].measurements["facts"]["volume_basis"] == "tick_volume_proxy"
    assert selected["wyckoff"].measurements["facts"]["phase_not_overclaimed"] is True
    assert {item["direction"] for item in selected["wyckoff"].measurements["implications"]} == {"LONG", "SHORT", "FLAT"}
    facts = selected["volume_indicators"].measurements["facts"]
    assert facts["real_volume"]["complete_for_recent_window"] is False



def test_wyckoff_phase_reader_keeps_accumulation_and_distribution_mirrored():
    bars = _bars(100)
    prior_low = min(item["low"] for item in bars[-40:])
    candidate = dict(bars[-1])
    candidate["low"] = prior_low - 1.0
    candidate["open"] = prior_low - 0.2
    candidate["close"] = prior_low + 0.2
    candidate["high"] = prior_low + 0.7
    candidate["volume"] = max(item["volume"] for item in bars[-20:]) + 1.0
    candidate["timestamp_epoch"] += 300
    original = _readings(bars + [candidate])["wyckoff"]
    reflected = _readings(_reflect(bars + [candidate]))["wyckoff"]
    assert {item.direction for item in original.implications} == {"LONG", "SHORT", "FLAT"}
    assert {item.direction for item in reflected.implications} == {"LONG", "SHORT", "FLAT"}
    assert original.facts["current_auction_events"]["spring_candidate"] == reflected.facts["current_auction_events"]["upthrust_candidate"]


def test_wyckoff_full_readings_have_only_real_levels_and_explicit_requirements():
    for reading in _readings(_bars()).values():
        assert reading.implications
        for item in reading.implications:
            assert item.direction in {"LONG", "SHORT", "FLAT"}
            assert item.invalidation_price is None or math.isfinite(item.invalidation_price)
            assert item.objective_price is None or math.isfinite(item.objective_price)
            assert item.requires
            assert item.management


def test_wyckoff_methods_never_emit_decision_or_execution_authority():
    for reading in _readings(_bars()).values():
        assert reading.facts["directional_decision_emitted"] is False
        assert reading.facts["entry_authorization_emitted"] is False



def test_wyckoff_event_candidates_are_prefix_invariant_when_future_bars_are_removed():
    full_bars = _bars(100)
    shorter_bars = full_bars[:82]
    full = _readings(full_bars)["wyckoff"].facts["event_candidate_history"]
    shorter = _readings(shorter_bars)["wyckoff"].facts["event_candidate_history"]
    assert full[:len(shorter)] == shorter
    assert all(
        full[index]["index"] < full[index + 1]["index"]
        for index in range(len(full) - 1)
    )



def test_completed_close_invalidation_cancels_accumulation_implication():
    facts = {
        "phase_sequence_strength_percentile": 0.95,
        "accumulation_or_markup": {
            "phase": "D",
            "events": [{"event": "Spring"}, {"event": "SOS"}],
            "invalidation_level": 99.0,
            "objective_price": 120.0,
            "invalidated": True,
        },
        "distribution_or_markdown": {
            "phase": None, "events": [], "invalidation_level": 110.0,
            "objective_price": None, "invalidated": False,
        },
    }
    implications = _wyckoff_implications(
        [{"open": 98.5, "high": 99.0, "low": 97.0, "close": 98.0}],
        facts,
        [],
    )
    long = next(item for item in implications if item.direction == "LONG")

    assert long.activated is False
    assert long.strength is None
    assert long.objective_price is None
    assert "breached the accumulation invalidation level" in long.activation_reason
    assert any("new completed accumulation sequence" in item for item in long.requires)


def test_completed_close_invalidation_cancels_distribution_implication_mirror():
    facts = {
        "phase_sequence_strength_percentile": 0.95,
        "accumulation_or_markup": {
            "phase": None, "events": [], "invalidation_level": 90.0,
            "objective_price": None, "invalidated": False,
        },
        "distribution_or_markdown": {
            "phase": "D",
            "events": [{"event": "UTAD"}, {"event": "SOW"}],
            "invalidation_level": 101.0,
            "objective_price": 80.0,
            "invalidated": True,
        },
    }
    implications = _wyckoff_implications(
        [{"open": 101.5, "high": 103.0, "low": 101.0, "close": 102.0}],
        facts,
        [],
    )
    short = next(item for item in implications if item.direction == "SHORT")

    assert short.activated is False
    assert short.strength is None
    assert short.objective_price is None
    assert "breached the distribution invalidation level" in short.activation_reason
    assert any("new completed distribution sequence" in item for item in short.requires)
