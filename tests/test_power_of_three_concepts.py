from cognition.concepts.power_of_three import PowerOfThreeConcepts


def test_power_of_three_requires_sweep_then_distribution_in_complete_utc_day():
    start = 1_700_006_400  # 2023-11-15 00:00 UTC
    bars = []
    for i in range(24):
        opening = 100.0
        high, low, close = 101.0, 99.0, 100.0
        if i == 9:
            low, close = 98.0, 99.5  # sell-side manipulation and reclaim
        if i >= 16:
            opening = 100.0 + (i-16) * 0.9
            close = opening + 0.8
            high, low = close + 0.2, opening - 0.2
        bars.append({
            "open": opening, "high": high, "low": low, "close": close,
            "timestamp_epoch": start + i * 3600,
            "source_timestamp_verified": True,
        })
    readings = PowerOfThreeConcepts().read("TEST", {"H1": bars})
    reading = readings[0]
    assert reading.applicable is True
    assert reading.facts["manipulation_sweep_side"] == "sell_side"
    assert reading.facts["distribution_confirmed"] is True
    assert reading.implications[0].direction == "LONG"
    assert reading.implications[0].activated is True


def test_power_of_three_is_unknown_without_complete_verified_utc_day():
    bars = [{
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "timestamp_epoch": 1_700_006_400 + i * 3600,
        "source_timestamp_verified": False,
    } for i in range(12)]
    reading = PowerOfThreeConcepts().read("TEST", {"H1": bars})[0]
    assert reading.applicable is False
    assert reading.implications[0].activated is False
