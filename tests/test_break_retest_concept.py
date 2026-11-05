from cognition.concepts.price_delivery import PriceDeliveryConcepts


def test_break_retest_requires_post_break_touch_and_completed_hold():
    bars = []
    for i in range(25):
        bars.append({
            "open": 100.0, "high": 102.0, "low": 99.0, "close": 100.2,
            "timestamp_epoch": 1_700_000_000 + i * 3600,
            "source_timestamp_verified": True,
        })
    bars[20].update({"open": 101.0, "high": 104.0, "low": 100.5, "close": 103.0})
    bars[21].update({"open": 103.0, "high": 103.5, "low": 101.8, "close": 102.5})
    for i in range(22, 25):
        bars[i].update({"open": 102.5, "high": 103.5, "low": 102.1, "close": 103.0})
    readings = PriceDeliveryConcepts().read("TEST", {"H1": bars})
    result = next(item for item in readings if item.concept == "break_retest")
    assert result.facts["direction"] == "LONG"
    assert result.facts["retest"] is True
    assert result.facts["accepted"] is True
    assert result.implications[0].activated is True
