"""Synthetic completed-bar market evidence for isolated cognition tests.

This fixture helper is test-only. It does not supply evidence to the production
runtime and cannot place orders.
"""

from __future__ import annotations

import time
from typing import Any

from cognition.contracts import Evidence, EvidenceDomain


def add_market_geometry(market_state: Any, direction: str = "LONG") -> Any:
    """Attach fresh, timestamped structural and momentum evidence for one side."""
    side = str(direction or "").strip().upper()
    if side in {"BUY", "LONG"}:
        side, breakout, trend, change = "LONG", "up", "uptrend", 0.001
    elif side in {"SELL", "SHORT"}:
        side, breakout, trend, change = "SHORT", "down", "downtrend", -0.001
    else:
        return market_state

    symbol = str(getattr(market_state, "symbol", "") or "").strip()
    now = time.time()
    market_state.add(Evidence(
        source_module="market_lenses.m5.structure.test_fixture",
        domain=EvidenceDomain.STRUCTURE,
        symbol=symbol,
        observation="synthetic confirmed M5 structural range for a unit test",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "timeframe": "M5",
            "current_close": 1.100,
            # The synthetic structure is oriented to provide positive payoff
            # geometry for whichever side a scenario is explicitly testing.
            "prior_range_high": 1.120 if side == "LONG" else 1.110,
            "prior_range_low": 1.090 if side == "LONG" else 1.080,
            "range_position": 0.5,
            "breakout_direction": breakout,
            "trend_state": trend,
            "trend_strength": 0.7,
            "range_rotation": False,
        },
        timestamp_epoch=now,
        relevance_horizon_seconds=900.0,
    ))
    market_state.add(Evidence(
        source_module="market_lenses.m5.momentum.test_fixture",
        domain=EvidenceDomain.MOMENTUM,
        symbol=symbol,
        observation="synthetic confirmed M5 directional response for a unit test",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "timeframe": "M5",
            "close_change": change,
        },
        timestamp_epoch=now,
        relevance_horizon_seconds=900.0,
    ))
    # Explicit synthetic calibration is supplied only by test fixtures. The
    # production Brain never promotes raw reasoning confidence to probability.
    market_state.add(Evidence(
        source_module="validation.out_of_sample_probability_calibration.test_fixture",
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        symbol=symbol,
        observation="synthetic direction-specific calibration for admission tests",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "probability_calibration_verified": True,
            "source_verified": True,
            "out_of_sample": True,
            "walk_forward": True,
            "untouched_holdout": True,
            "data_leakage_checked": True,
            "selection_bias_checked": True,
            "overfitting_checked": True,
            "calibration_side": side,
            "win_probability": 0.72,
            "sample_count": 240,
            "brier_score": 0.17,
            "reliability_gap": 0.08,
            "calibration_method": "synthetic_isotonic",
            "calibration_id": "unit-test-holdout",
        },
        timestamp_epoch=now,
        relevance_horizon_seconds=900.0,
    ))
    return market_state
