from cognition.concepts.structure_patterns import StructurePatternConcepts


def _flat_history():
    return [{
        "open": 101.0, "high": 102.0, "low": 100.5, "close": 101.2,
        "timestamp_epoch": 1_700_000_000 + i * 3600,
        "source_timestamp_verified": True,
    } for i in range(30)]


def test_double_top_requires_confirmed_neckline_break():
    bars = _flat_history()
    bars[20].update({"open": 108.0, "high": 110.0, "low": 107.0, "close": 109.0})
    bars[22].update({"open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0})
    bars[24].update({"open": 108.0, "high": 110.0, "low": 107.0, "close": 109.0})
    bars[-1].update({"open": 99.5, "high": 101.0, "low": 98.5, "close": 99.0})
    readings = StructurePatternConcepts().read("TEST", {"H1": bars})
    pattern = next(x for x in readings if x.concept == "chart_patterns")
    assert "double_top" in pattern.facts["patterns"]
    assert pattern.facts["breakout_confirmed"] is True
    assert pattern.implications[0].direction == "SHORT"
    assert pattern.implications[0].activated is True


def test_supply_demand_zone_is_an_ohlc_proxy_and_requires_retest():
    bars = _flat_history()
    bars[-3].update({"open": 100.0, "high": 100.4, "low": 99.6, "close": 100.1})
    bars[-2].update({"open": 100.1, "high": 100.5, "low": 99.7, "close": 100.0})
    bars[-1].update({"open": 100.0, "high": 104.0, "low": 100.0, "close": 103.5})
    readings = StructurePatternConcepts().read("TEST", {"H1": bars})
    zone = next(x for x in readings if x.concept == "supply_demand")
    assert zone.facts["direction"] == "LONG"
    assert zone.implications[0].activated is True
    assert "retest and reaction evidence" in zone.implications[0].requires
    assert "does not prove hidden limit orders" in zone.unknowns[0]
