"""System 4 probability and opportunity qualification contracts."""

import time

from cognition.brain import CognitiveBrain
from cognition.contracts import Evidence, EvidenceDomain, MarketState


def _calibration(*, direction="LONG", verified=True, symbol="SYNTHETIC"):
    return Evidence(
        source_module="validation.out_of_sample_probability_calibration",
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        symbol=symbol,
        observation="direction-specific out-of-sample calibration record",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "probability_calibration_verified": verified,
            "source_verified": True,
            "out_of_sample": True,
            "walk_forward": True,
            "untouched_holdout": True,
            "data_leakage_checked": True,
            "selection_bias_checked": True,
            "overfitting_checked": True,
            "calibration_side": direction,
            "win_probability": 0.61,
            "sample_count": 240,
            "brier_score": 0.17,
            "calibration_method": "isotonic",
            "calibration_id": "holdout-2026-q3",
        },
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=900,
    )


def test_uncalibrated_reasoning_confidence_is_not_a_win_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(Evidence(
        source_module="reasoning.confidence",
        domain=EvidenceDomain.REASONING,
        symbol="SYNTHETIC",
        observation="high native reasoning confidence",
        confidence=0.95,
        uncertainty=0.05,
        measurements={"confidence": 0.95},
        timestamp_epoch=time.time(),
        relevance_horizon_seconds=900,
    ))
    verified, probability, source = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is False
    assert probability == 0.0
    assert "no fresh verified" in source


def test_verified_direction_specific_calibration_supplies_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration())
    verified, probability, source = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is True
    assert probability == 0.61
    assert "isotonic:holdout-2026-q3" in source


def test_calibration_for_opposite_direction_cannot_qualify_candidate():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(direction="SHORT"))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is False


def test_unverified_calibration_record_cannot_supply_probability():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(verified=False))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is False


def test_calibration_from_another_symbol_cannot_qualify_candidate():
    state = MarketState(symbol="SYNTHETIC")
    state.add(_calibration(symbol="OTHER"))
    verified, _, _ = CognitiveBrain(
        reasoner=None,
    )._calibrated_win_probability(state, "LONG")
    assert verified is False
