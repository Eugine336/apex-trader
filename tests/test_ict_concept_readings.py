from cognition.concepts.price_delivery import PriceDeliveryConcepts


def _bars():
    return [{
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.1,
        "timestamp_epoch": 1_700_000_000 + i * 3600,
        "source_timestamp_verified": True,
    } for i in range(35)]


def test_displacement_requires_relative_range_and_dominant_body():
    bars = _bars()
    bars[-1].update({"open": 100.0, "high": 103.0, "low": 99.5, "close": 102.8})
    readings = PriceDeliveryConcepts().read("TEST", {"H1": bars})
    displacement = next(x for x in readings if x.concept == "displacement")
    assert displacement.facts["displacement"] is True
    assert displacement.facts["direction"] == "LONG"
    assert displacement.implications[0].activated is True


def test_breaker_block_requires_original_block_failure_and_opposite_side_retest():
    bars = _bars()
    bars[20].update({"open": 100.0, "high": 101.0, "low": 98.0, "close": 99.0})
    bars[21].update({"open": 99.0, "high": 105.0, "low": 99.0, "close": 104.5})
    for i in range(22, 25):
        bars[i].update({"open": 104.0, "high": 105.0, "low": 103.0, "close": 104.0})
    bars[25].update({"open": 100.0, "high": 101.0, "low": 97.0, "close": 97.5})
    bars[26].update({"open": 97.5, "high": 100.0, "low": 97.0, "close": 97.8})
    for i in range(27, 35):
        bars[i].update({"open": 97.8, "high": 98.0, "low": 97.0, "close": 97.5})
    readings = PriceDeliveryConcepts().read("TEST", {"H1": bars})
    breaker = next(x for x in readings if x.concept == "breaker_block")
    assert breaker.facts["original_block_direction"] == "LONG"
    assert breaker.facts["retested"] is True
    assert breaker.facts["accepted"] is True
    assert breaker.implications[0].direction == "SHORT"
    assert breaker.implications[0].activated is True


def test_imbalance_ranks_its_impulse_against_earlier_bars_only():
    rows = _bars()
    for row in rows[:-3]:
        row.update(open=100, high=100.5, low=99.5, close=100.4)
    # The centre candle's body is smaller than every earlier one.
    rows[-3].update(open=100, high=100.5, low=99.5, close=100.4)
    rows[-2].update(open=100, high=101.5, low=99.5, close=100.1)
    rows[-1].update(open=102, high=103, low=101.5, close=102.5)
    fvg = next(r for r in PriceDeliveryConcepts().read("TEST", {"H1": rows}) if r.concept == "fair_value_gap")
    assert fvg.facts["body_baseline_samples"] == len(rows) - 2
    assert fvg.facts["displacement_body_percentile"] == 0.0


def test_order_block_remains_identifiable_beyond_the_old_search_window():
    rows = _bars()
    rows[5].update(open=100, high=101, low=98, close=99)
    rows[6].update(open=99, high=105, low=99, close=104.5)
    for row in rows[7:]:
        row.update(open=104, high=105, low=103, close=104)
    reading = next(r for r in PriceDeliveryConcepts().read("TEST", {"H1": rows}) if r.concept == "order_blocks")
    assert reading.facts["origin_index"] == 5
    assert reading.facts["impulse_index"] == 6
    assert reading.implications[0].activated is True
