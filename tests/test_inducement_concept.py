from cognition.concepts.liquidity import LiquidityPools


def test_inducement_requires_internal_pool_sweep_and_external_range_preservation():
    bars = []
    for i in range(40):
        bars.append({
            "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0,
            "timestamp_epoch": 1_700_000_000 + i * 3600,
            "source_timestamp_verified": True,
        })
    for index, high, low in (
        (10, 110.0, 99.0), (15, 101.0, 95.0), (20, 108.0, 99.0),
        (25, 101.0, 96.0), (30, 112.0, 99.0), (35, 101.0, 90.0),
    ):
        bars[index].update({"high": high, "low": low})
    bars[-1].update({"open": 107.0, "high": 109.0, "low": 100.0, "close": 107.5})
    readings = LiquidityPools().read("TEST", {"H1": bars})
    inducement = next(item for item in readings if item.concept == "inducement")
    assert inducement.facts["internal_pool_kind"] == "buy_side"
    assert inducement.facts["internal_pool_price"] == 108.0
    assert inducement.facts["external_boundary_preserved"] is True
    assert inducement.implications[0].direction == "SHORT"
    assert inducement.implications[0].activated is True
