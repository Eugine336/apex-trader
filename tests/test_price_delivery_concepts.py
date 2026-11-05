"""Regression tests for the full price-delivery concept module."""
from cognition.concepts.price_delivery import PriceDeliveryConcepts


def test_dealing_range_is_explicit_and_never_directional():
    bars = []
    for i in range(24):
        opening = 100.0 + (i % 3) * 0.1
        closing = opening + 0.05
        bars.append({
            "open": opening, "high": closing + 0.1, "low": opening - 0.1,
            "close": closing, "timestamp_epoch": 1_700_000_000 + i * 3600,
            "source_timestamp_verified": True,
        })
    readings = PriceDeliveryConcepts().read("TEST", {"H1": bars})
    dealing = next(item for item in readings if item.concept == "dealing_range")
    assert dealing.applicable
    assert dealing.facts["lookback_bars"] == 20
    assert dealing.implications[0].direction == "FLAT"
    assert "rolling reference range" in dealing.unknowns[0]


def test_fair_value_gap_is_read_from_three_completed_candles():
    bars = []
    for i in range(24):
        bars.append({
            "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.1,
            "timestamp_epoch": 1_700_000_000 + i * 3600,
            "source_timestamp_verified": True,
        })
    bars[-3].update({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2})
    bars[-2].update({"open": 100.6, "high": 101.8, "low": 100.5, "close": 101.7})
    bars[-1].update({"open": 102.0, "high": 103.0, "low": 102.0, "close": 102.8})
    readings = PriceDeliveryConcepts().read("TEST", {"H1": bars})
    fvg = next(item for item in readings if item.concept == "fair_value_gap")
    assert fvg.facts["direction"] == "LONG"
    assert fvg.facts["lower"] == 100.5
    assert fvg.facts["upper"] == 102.0
    assert fvg.implications[0].activated
