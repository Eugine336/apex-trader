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
            "data_basis": "completed_ohlc",
            "source_bar_epoch": now,
            "timeframe": "M5",
            "opportunity_horizon": "M5",
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
            "data_basis": "completed_ohlc",
            "source_bar_epoch": now,
            "timeframe": "M5",
            "opportunity_horizon": "M5",
            "close_change": change,
        },
        timestamp_epoch=now,
        relevance_horizon_seconds=900.0,
    ))
    # Test-only artifact is registered through the producer registry. Raw
    # evidence flags alone are deliberately insufficient in the production Brain.
    from test_support.calibration import register_test_calibration
    # Register the exact horizons used by the M5 native fixtures and the
    # MTF advisor fixtures. Each artifact remains independently scoped by
    # symbol, side, family and horizon in the producer registry.
    for horizon_seconds in (300, 3600):
        artifact = register_test_calibration(
            symbol, side, horizon_seconds=horizon_seconds,
        )
        market_state.add(Evidence(
            source_module="calibration_producer.test_fixture",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            symbol=symbol,
            observation=(
                "producer-registered test calibration artifact "
                f"for {horizon_seconds}s"
            ),
            confidence=0.0,
            uncertainty=0.0,
            measurements=artifact.to_measurements(),
            timestamp_epoch=artifact.issued_at_epoch,
            source_timestamp_epoch=artifact.issued_at_epoch,
            relevance_horizon_seconds=3600.0,
            data_basis="test_fixture",
            independence_group=(
                f"test_calibration:{symbol}:{side}:{horizon_seconds}"
            ),
        ))
    return market_state
