"""Market geometry must come from fresh completed-bar structure, not constants."""

import time

import pytest

from cognition.contracts import Evidence, EvidenceDomain, MarketState, Opportunity
from cognition.opportunity_geometry import derive_opportunity_geometry
from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION


def _state(
    *,
    direction="LONG",
    timestamp=None,
    high=1.105,
    low=1.095,
    close=1.100,
    breakout="up",
    trend="uptrend",
    momentum=0.001,
    range_position=0.5,
    range_rotation=False,
):
    now = time.time()
    stamp = now if timestamp is None else timestamp
    state = MarketState(symbol="SYNTHETIC")
    state.add(Evidence(
        source_module="market_lenses.m5.structure",
        domain=EvidenceDomain.STRUCTURE,
        symbol="SYNTHETIC",
        observation="completed bar structural range",
        confidence=0.8,
        uncertainty=0.2,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": stamp,
            "timeframe": "M5",
            "current_close": close,
            "prior_range_high": high,
            "prior_range_low": low,
            "range_position": range_position,
            "breakout_direction": breakout,
            "trend_state": trend,
            "range_rotation": range_rotation,
            "trend_strength": 0.4,
        },
        timestamp_epoch=stamp,
        relevance_horizon_seconds=900,
    ))
    state.add(Evidence(
        source_module="market_lenses.m5.momentum",
        domain=EvidenceDomain.MOMENTUM,
        symbol="SYNTHETIC",
        observation="completed bar momentum",
        confidence=0.8,
        uncertainty=0.2,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": stamp,
            "timeframe": "M5",
            "close_change": momentum,
        },
        timestamp_epoch=stamp,
        relevance_horizon_seconds=900,
    ))
    return state


def test_long_geometry_comes_from_observed_structure():
    geometry = derive_opportunity_geometry(_state(), "LONG")

    assert geometry is not None
    assert geometry.activated is True
    assert geometry.entry_price == 1.100
    assert geometry.stop_price == 1.095
    assert geometry.target_price == 1.105
    assert geometry.risk_distance == pytest.approx(0.005)
    assert geometry.reward_distance == pytest.approx(0.005)
    assert geometry.reward_r == pytest.approx(1.0)
    assert geometry.evidence_ids
    assert "completed-bar structural breakout up" in geometry.activation_reason


def test_short_geometry_is_directionally_symmetric():
    geometry = derive_opportunity_geometry(
        _state(breakout="down", trend="downtrend", momentum=-0.001),
        "SHORT",
    )

    assert geometry is not None
    assert geometry.entry_price == 1.100
    assert geometry.stop_price == 1.105
    assert geometry.target_price == 1.095
    assert geometry.reward_r == pytest.approx(1.0)
    assert "breakout down" in geometry.activation_reason


def test_missing_structural_target_fails_closed():
    state = _state(high=1.100, close=1.100)

    assert derive_opportunity_geometry(state, "LONG") is None


def test_unconfirmed_direction_without_activation_fails_closed():
    state = _state(breakout="none", trend="range", momentum=0.0)

    assert derive_opportunity_geometry(state, "LONG") is None


def test_other_symbol_evidence_cannot_contaminate_geometry():
    state = _state()
    # MarketState objects can be contaminated by a misrouted adapter. Evidence
    # with a non-matching symbol cannot define a candidate's entry/stop/target.
    state.add(Evidence(
        source_module="market_lenses.m5.structure",
        domain=EvidenceDomain.STRUCTURE,
        symbol="OTHER",
        observation="foreign-symbol structural breakout",
        confidence=0.9, uncertainty=0.1,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": stamp,
            "timeframe": "M5",
            "current_close": 1.2,
            "prior_range_high": 1.25,
            "prior_range_low": 1.1,
            "range_position": 0.9,
            "breakout_direction": "up",
            "trend_state": "uptrend",
            "trend_strength": 0.8,
        },
        relevance_horizon_seconds=900,
    ))
    geometry = derive_opportunity_geometry(state, "LONG")
    assert geometry is not None
    assert geometry.entry_price == 1.100


def test_measured_range_rotation_can_activate_without_a_named_pattern():
    state = _state(
        breakout="none", trend="range", momentum=0.001,
        range_position=0.1, range_rotation=True,
    )
    geometry = derive_opportunity_geometry(state, "LONG")

    assert geometry is not None
    assert geometry.activated
    assert "measured range rotation" in geometry.activation_reason
    assert geometry.target_price > geometry.entry_price > geometry.stop_price


def test_stale_completed_bar_evidence_cannot_define_geometry():
    state = _state(timestamp=time.time() - 2000)

    assert derive_opportunity_geometry(state, "LONG") is None


def test_opportunity_serializes_explicit_geometry():
    opportunity = Opportunity.from_reply({
        "id": "geometry-test",
        "direction": "LONG",
        "state": "ACTIVE",
        "entry_price": 1.100,
        "stop_price": 1.095,
        "target_price": 1.105,
        "risk_distance": 0.005,
        "reward_distance": 0.005,
        "reward_r": 1.0,
        "geometry_evidence_ids": ["e1", "e2"],
    })

    payload = opportunity.to_dict()
    assert payload["identity_version"] == IDENTITY_VERSION
    assert payload["identity"] == IDENTITY_STATEMENT
    geometry = derive_opportunity_geometry(_state(), "LONG")
    assert geometry is not None
    geometry_payload = geometry.to_dict()
    assert geometry_payload["identity_version"] == IDENTITY_VERSION
    assert geometry_payload["identity"] == IDENTITY_STATEMENT
    assert payload["entry_price"] == 1.100
    assert payload["stop_price"] == 1.095
    assert payload["target_price"] == 1.105
    assert payload["reward_r"] == 1.0
    assert payload["geometry_evidence_ids"] == ["e1", "e2"]



def test_old_breakout_on_same_timeframe_cannot_activate_newer_non_breakout():
    state = _state(breakout="none", trend="range", momentum=0.0)
    old = Evidence(
        source_module="market_lenses.m5.structure.old",
        domain=EvidenceDomain.STRUCTURE,
        symbol="SYNTHETIC",
        observation="older completed structural breakout",
        confidence=0.8,
        uncertainty=0.2,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": stamp,
            "timeframe": "M5",
            "current_close": 1.100,
            "prior_range_high": 1.105,
            "prior_range_low": 1.095,
            "breakout_direction": "up",
            "trend_state": "range",
            "trend_strength": 0.0,
        },
        timestamp_epoch=time.time() - 30,
        relevance_horizon_seconds=900,
    )
    state.add(old)
    assert derive_opportunity_geometry(state, "LONG") is None


def test_momentum_from_another_timeframe_cannot_activate_trend():
    state = _state(breakout="none", trend="range", momentum=0.0)
    for item in state.evidence:
        if item.domain == EvidenceDomain.MOMENTUM:
            item.measurements["timeframe"] = "M1"
    state.evidence[0].measurements["trend_state"] = "uptrend"
    state.evidence[0].measurements["trend_strength"] = 0.5
    assert derive_opportunity_geometry(state, "LONG") is None


def test_conflicting_same_time_same_timeframe_structure_fails_closed():
    state = _state()
    original = state.evidence[0]
    conflicting = Evidence(
        source_module="market_lenses.m5.structure.conflict",
        domain=EvidenceDomain.STRUCTURE,
        symbol="SYNTHETIC",
        observation="conflicting structural close",
        confidence=0.8,
        uncertainty=0.2,
        measurements={
            **original.measurements,
            "current_close": 1.101,
            "breakout_direction": "none",
        },
        timestamp_epoch=original.timestamp_epoch,
        relevance_horizon_seconds=900,
    )
    state.add(conflicting)
    assert derive_opportunity_geometry(state, "LONG") is None


def test_missing_evidence_symbol_cannot_be_inherited_from_market_state():
    state = _state()
    state.evidence[0].symbol = ""
    state.evidence[1].symbol = ""
    assert derive_opportunity_geometry(state, "LONG") is None



def test_counter_side_sweep_requires_measured_reclaim_for_reversal_candidate():
    state = _state(breakout="none", trend="range", momentum=0.0)
    state.add(Evidence(
        source_module="market_lenses.m5.liquidity",
        domain=EvidenceDomain.LIQUIDITY,
        symbol="SYNTHETIC",
        observation="completed sell-side sweep and reclaim",
        confidence=0.8,
        uncertainty=0.2,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": stamp,
            "timeframe": "M5",
            "sweep_side": "sell",
            "swept_level": 1.095,
            "sweep_reclaimed": True,
            "current_close": 1.100,
        },
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=900,
    ))
    geometry = derive_opportunity_geometry(state, "LONG")
    assert geometry is not None
    assert geometry.activated is True
    assert "sweep/reclaim" in geometry.activation_reason


def test_sweep_side_without_reclaim_does_not_activate_reversal():
    state = _state(breakout="none", trend="range", momentum=0.0)
    state.add(Evidence(
        source_module="market_lenses.m5.liquidity",
        domain=EvidenceDomain.LIQUIDITY,
        symbol="SYNTHETIC",
        observation="sweep observed but reclaim not confirmed",
        confidence=0.8,
        uncertainty=0.2,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": stamp,
            "timeframe": "M5",
            "sweep_side": "sell",
            "swept_level": 1.095,
            "sweep_reclaimed": False,
            "current_close": 1.090,
        },
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=900,
    ))
    assert derive_opportunity_geometry(state, "LONG") is None


def test_low_liquidity_or_unfamiliar_only_market_state_has_no_geometry():
    state = MarketState(symbol="SYNTHETIC")
    state.add(Evidence(
        source_module="unknown_adapter",
        domain=EvidenceDomain.OTHER,
        symbol="SYNTHETIC",
        observation="unfamiliar market measurement",
        confidence=0.9,
        uncertainty=0.1,
        measurements={"unknown_microstructure_metric": 0.73},
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=900,
    ))
    assert derive_opportunity_geometry(state, "LONG") is None
