from cognition.concepts.price_action import PriceActionConcepts


def test_candle_microstructure_reads_anatomy_even_without_named_pattern():
    bars = []
    for i in range(15):
        bars.append({
            "open": 100.0, "high": 100.8, "low": 99.2, "close": 100.1 + (i % 2) * 0.01,
            "timestamp_epoch": 1_700_000_000 + i * 3600,
            "source_timestamp_verified": True,
        })
    readings = PriceActionConcepts().read("TEST", {"H1": bars})
    candle = next(item for item in readings if item.concept == "candle_microstructure")
    assert 0.0 <= candle.facts["body_ratio"] <= 1.0
    assert candle.implications[0].direction == "FLAT"
    assert "not bid/ask delta" in candle.unknowns[0]


def test_failed_auction_uses_an_intact_previously_confirmed_swing_beyond_ten_bars():
    rows = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(25)]
    rows[2]["low"] = 95
    rows[-1]["low"] = 94
    readings = PriceActionConcepts().read("TEST", {"H1": rows})
    reclaim = next(r for r in readings if r.concept == "failed_auction")
    assert reclaim.facts["swept_level"] == 95
    assert reclaim.facts["swept_pivot_index"] == 2
    assert reclaim.facts["lookback_bars"] == len(rows) - 1 - 2
    assert reclaim.implications[0].invalidation_price == 94

    # A prior candle traded below that boundary. The current low did not
    # sweep the newer low and must not trigger on the old boundary again.
    rows[5]["low"] = 93
    assert all(r.concept != "failed_auction" for r in PriceActionConcepts().read("TEST", {"H1": rows}))
