import math

from cognition.concepts.indicator_context import IndicatorContextConcepts


def _history():
    bars = []
    for i in range(90):
        close = 100.0 + i * 0.08 + math.sin(i / 3.0) * 0.7
        opening = close - math.sin(i) * 0.15
        bars.append({
            "open": opening, "high": max(opening, close) + 0.35 + (i % 4) * 0.02,
            "low": min(opening, close) - 0.32 - (i % 3) * 0.02,
            "close": close, "timestamp_epoch": 1_700_000_000 + i * 3600,
            "source_timestamp_verified": True,
        })
    return bars


def test_indicator_context_reads_all_registered_price_indicator_families():
    readings = IndicatorContextConcepts().read("TEST", {"H1": _history()})
    concepts = {item.concept for item in readings}
    assert concepts == {
        "moving_averages", "adx_directional", "aroon_trix", "divergence",
        "rsi", "macd", "stochastic", "cci_williams", "roc_momentum",
        "atr_true_range", "bollinger", "keltner_donchian",
        "realized_volatility", "zscore_mean",
    }
    assert all(item.applicable for item in readings)
    assert all(item.source_bar_epoch is not None for item in readings)


def test_indicator_context_does_not_invent_volume_weighted_average_without_volume():
    readings = IndicatorContextConcepts().read("TEST", {"H1": _history()})
    moving = next(item for item in readings if item.concept == "moving_averages")
    assert moving.facts["vwma20"] is None
    assert moving.facts["volume_available"] is False
