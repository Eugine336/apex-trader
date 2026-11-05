from cognition.concepts.price_delivery import PriceDeliveryConcepts


def test_gap_read_requires_verified_timestamp_discontinuity():
    bars = []
    start = 1_700_000_000
    for i in range(24):
        bars.append({
            "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2,
            "timestamp_epoch": start + i * 3600,
            "source_timestamp_verified": True,
        })
    bars[20].update({
        "open": 102.0, "high": 103.0, "low": 101.8, "close": 102.5,
        "timestamp_epoch": start + 20 * 3600 + 3600,
    })
    for i in range(21, 24):
        bars[i].update({
            "open": 102.5, "high": 103.5, "low": 102.1, "close": 103.0,
            "timestamp_epoch": start + 21 * 3600 + (i-21) * 3600,
        })
    readings = PriceDeliveryConcepts().read("TEST", {"H1": bars})
    gap = next(item for item in readings if item.concept == "gap_analysis")
    assert gap.facts["direction"] == "LONG"
    assert gap.facts["filled"] is False
    assert gap.facts["accepted"] is True
    assert gap.implications[0].activated is True
