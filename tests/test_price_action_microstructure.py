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
