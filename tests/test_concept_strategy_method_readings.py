"""Boundary and live-path tests for descriptive strategy-method readers."""

from __future__ import annotations

import math
import time
from statistics import median

import pandas as pd

from cognition.concepts import CONCEPT_MODULES, run_concepts
from cognition.concepts.strategy_method_readings import (
    StrategyMethodReadings,
    _historical_breakout_holds,
)
from cognition.market_lenses import analyze_market_frames


def _bars(closes: list[float], *, start_epoch: float = 1_780_000_000.0) -> list[dict]:
    rows = []
    for index, close in enumerate(closes):
        rows.append({
            "open": close,
            "high": close + 0.4,
            "low": close - 0.4,
            "close": close,
            "volume": 100.0 + index,
            "volume_source": "provider_volume",
            "timestamp_epoch": start_epoch + index * 300,
            "source_timestamp_verified": True,
        })
    return rows


def _uptrend_bars() -> list[dict]:
    return _bars([
        100, 104, 99, 106, 101, 108, 103, 110, 105, 112, 107, 114, 109, 113,
    ])


def _readings(bars: list[dict]):
    module = StrategyMethodReadings()
    return {
        reading.concept: reading
        for reading in module.read("XAUUSD", {"M5": bars})
    }


def _breakout_bars(closes: list[float]) -> list[dict]:
    """Provide a full 20-bar adjacent-window baseline before the event segment."""
    prior_history = [96, 99, 95, 98, 94, 97, 93, 99]
    return _bars(prior_history + closes)


def test_strategy_method_readings_are_registered_once_and_only_claim_catalogued_methods():
    modules = [module for module in CONCEPT_MODULES if module.key == "strategy_method_readings"]
    assert len(modules) == 1
    assert set(modules[0].covers_methods) == {
        "trend_following", "momentum_breakout", "mean_reversion",
    }
    assert modules[0].covers_lenses == ()


def test_trend_reading_uses_confirmed_swings_and_reports_pullback_and_invalidation():
    reading = _readings(_uptrend_bars())["trend_following"]
    facts = reading.facts

    assert reading.applicable is True
    assert facts["trend_state_from_confirmed_swings"] == "up"
    assert facts["pullback"]["depth"] is not None
    assert facts["pullback_depth_own_history_percentile"] is not None
    assert facts["structural_invalidation_level"] is not None
    assert facts["completed_close_beyond_invalidation"] is False
    assert facts["directional_decision_emitted"] is False
    assert facts["entry_authorization_emitted"] is False
    assert reading.implications == ()


def test_trend_reading_is_directionally_symmetric_under_price_reflection():
    bars = _uptrend_bars()
    reflected = [
        {
            **bar,
            "open": 500.0 - bar["open"],
            "high": 500.0 - bar["low"],
            "low": 500.0 - bar["high"],
            "close": 500.0 - bar["close"],
        }
        for bar in bars
    ]

    up = _readings(bars)["trend_following"]
    down = _readings(reflected)["trend_following"]
    assert up.facts["trend_state_from_confirmed_swings"] == "up"
    assert down.facts["trend_state_from_confirmed_swings"] == "down"
    assert up.implications == down.implications == ()


def test_breakout_reader_distinguishes_first_close_retest_hold_and_failure():
    initial = _breakout_bars([100, 104, 99, 106, 101, 108, 103, 110, 105, 112, 107, 111, 114])
    initial_reading = _readings(initial)["momentum_breakout"]
    assert initial_reading.applicable is True
    assert initial_reading.facts["upside_boundary_observation"]["status"] == (
        "first_completed_close_beyond"
    )
    assert initial_reading.facts["volume_basis"] == "provider_volume"
    assert initial_reading.facts["volume_ratio_to_baseline"] is not None
    assert initial_reading.facts["true_range_expansion_ratio"] is not None
    assert initial_reading.facts["absolute_momentum_speed_acceleration"] is not None
    assert initial_reading.facts["upside_boundary_observation"]["event_hold_bars"] == 1

    retest = _breakout_bars([100, 104, 99, 106, 101, 108, 103, 110, 105, 112, 107, 111, 114, 113])
    retest[-1]["high"] = 115.0
    retest[-1]["low"] = 111.8
    held = _readings(retest)["momentum_breakout"]
    assert held.facts["upside_boundary_observation"]["status"] == "retested_and_held"

    failed = _breakout_bars([100, 104, 99, 106, 101, 108, 103, 110, 105, 112, 107, 111, 114, 111])
    returned_inside = _readings(failed)["momentum_breakout"]
    assert returned_inside.facts["upside_boundary_observation"]["status"] == "failed_return_inside"
    assert held.implications == returned_inside.implications == ()


def test_breakout_acceptance_horizon_uses_completed_instrument_history():
    bars = _bars([100, 104, 106, 107, 103, 108, 110, 102, 109, 111, 112, 103, 108])
    holds = _historical_breakout_holds(
        bars, [(1, "H", 105.0)], "above", (1, 12),
    )

    assert holds == [2, 2, 3]
    assert math.ceil(median(holds)) == 2


def test_latest_unconfirmed_extreme_is_not_promoted_to_a_confirmed_swing():
    bars = _bars([100, 104, 99, 106, 101, 108, 103, 110, 105, 112, 107, 111, 118])
    reading = _readings(bars)["trend_following"]
    confirmed = reading.facts["confirmed_internal_swings"]

    assert all(item["index"] < len(bars) - 1 for item in confirmed)
    assert all(item["price"] != 118.4 for item in confirmed)


def test_mean_reversion_reports_distribution_dependence_and_unit_root_diagnostics():
    closes = [
        100.0 + 2.0 * math.sin(index * 0.35) + 0.5 * math.sin(index * 0.11)
        for index in range(120)
    ]
    reading = _readings(_bars(closes))["mean_reversion"]
    facts = reading.facts

    assert reading.applicable is True
    assert facts["rolling_zscore"] is not None
    assert facts["mean_distance"] is not None
    assert facts["absolute_zscore_own_history_percentile"] is not None
    assert "constant_only_adf0" in facts["unit_root_diagnostics"]
    assert "constant_plus_trend_adf0" in facts["unit_root_diagnostics"]
    assert "historical_reversion_episodes" in facts
    assert facts["historical_statistics_are_not_forward_probabilities"] is True
    assert facts["current_structural_regime"] in {"up", "down", "range_or_transition", "undetermined"}
    assert "reversion_history_by_structural_regime" in facts["historical_reversion_episodes"]
    assert facts["candidate_mean_reversion_references"]["rolling_mean"] is not None
    assert facts["candidate_mean_reversion_references"]["volume_weighted_average_price"] is not None
    assert "statistical_extreme_invalidation_context" in facts
    assert facts["directional_decision_emitted"] is False
    assert facts["entry_authorization_emitted"] is False
    assert reading.implications == ()


def test_short_history_and_zero_variance_remain_explicitly_unavailable():
    short = _readings(_bars([100, 101, 99, 102, 98, 101, 100, 102, 99, 101]))
    short_breakout = short["momentum_breakout"]
    assert short_breakout.facts["absolute_momentum_speed_acceleration"] is None
    assert "21 completed closes for adjacent-window momentum acceleration" in short_breakout.unknowns

    mean_reading = short["mean_reversion"]
    assert mean_reading.applicable is False
    assert mean_reading.facts["rolling_zscore"] is None
    assert mean_reading.unknowns

    constant = _readings(_bars([100.0] * 80))["mean_reversion"]
    assert constant.applicable is False
    assert constant.facts["rolling_zscore"] is None
    assert constant.unknowns


def test_unverified_source_time_is_not_promoted_by_reader():
    bars = _uptrend_bars()
    bars[-1]["source_timestamp_verified"] = False
    readings = _readings(bars)
    for reading in readings.values():
        assert reading.source_bar_epoch is None
        assert "source-verified timestamp for the latest completed bar" in reading.unknowns


def test_native_concept_pipeline_emits_neutral_method_evidence():
    evidence = run_concepts("XAUUSD", {"M5": _uptrend_bars()})
    selected = {
        item.source_module: item
        for item in evidence
        if item.source_module in {
            "concept.trend_following.m5",
            "concept.momentum_breakout.m5",
            "concept.mean_reversion.m5",
        }
    }

    assert set(selected) == {
        "concept.trend_following.m5",
        "concept.momentum_breakout.m5",
        "concept.mean_reversion.m5",
    }
    assert all(item.polarity == 0.0 for item in selected.values())
    assert all(item.measurements["source_kind"] == "concept_reading" for item in selected.values())


def test_market_lenses_live_ohlc_path_delivers_method_evidence_to_brain():
    bars = _uptrend_bars()
    now_epoch = float(int(time.time() // 300) * 300)
    rows = []
    for index, bar in enumerate(bars):
        rows.append({
            "time": now_epoch - (len(bars) - index) * 300,
            **{key: bar[key] for key in ("open", "high", "low", "close", "volume")},
            "volume_source": "provider_volume",
        })
    frame = pd.DataFrame(rows)
    frame.attrs.update({
        "source_symbol": "XAUUSD",
        "source_timestamp_origin": "broker_ohlcv",
        "source_broker": "mt5",
        "source_timestamp_verified": True,
        "source_timestamp_unit": "unix_seconds",
    })

    evidence = analyze_market_frames(
        "XAUUSD", {"M5": frame}, source_includes_forming_bar=False,
    )
    selected = {
        item.source_module: item
        for item in evidence
        if item.source_module in {
            "concept.trend_following.m5",
            "concept.momentum_breakout.m5",
            "concept.mean_reversion.m5",
        }
    }

    assert set(selected) == {
        "concept.trend_following.m5",
        "concept.momentum_breakout.m5",
        "concept.mean_reversion.m5",
    }
    assert all(item.polarity == 0.0 for item in selected.values())
    assert all(item.source_timestamp_verified is True for item in selected.values())


def test_trend_context_includes_higher_timeframe_structure_and_structural_geometry():
    m5 = _uptrend_bars()
    h1 = _bars(
        [200, 204, 199, 206, 201, 208, 203, 210, 205, 212, 207, 214, 209, 213],
        start_epoch=1_780_000_000.0,
    )
    reading = {
        item.concept: item
        for item in StrategyMethodReadings().read("XAUUSD", {"M5": m5, "H1": h1})
        if item.timeframe == "M5"
    }["trend_following"]

    assert reading.facts["higher_timeframe_structure"]["H1"] == "up"
    assert reading.facts["objective_basis"] in {
        "nearest_prior_confirmed_swing_beyond_price_only",
        "no_prior_confirmed_swing_objective",
    }
    assert isinstance(reading.facts["trail_anchor_levels"], list)
    assert reading.implications == ()
