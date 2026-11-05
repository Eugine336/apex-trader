from cognition.concepts.pivot_points import PivotPointConcepts


def test_floor_pivots_use_last_completed_daily_bar():
    readings = PivotPointConcepts().read("TEST", {
        "D1": [{
            "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0,
            "timestamp_epoch": 1_700_000_000,
            "source_timestamp_verified": True,
        }],
        "H1": [{
            "open": 104.0, "high": 106.0, "low": 103.0, "close": 105.0,
            "timestamp_epoch": 1_700_086_400,
            "source_timestamp_verified": True,
        }],
    })
    pivot = readings[0]
    assert pivot.concept == "pivot_points"
    assert pivot.facts["pivot"] == 101.66666666666667
    assert pivot.facts["r1"] == 113.33333333333334
    assert pivot.facts["s1"] == 93.33333333333334
    assert pivot.implications[0].direction == "FLAT"
