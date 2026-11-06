from __future__ import annotations

from cognition.concepts import clear_concept_reading_cache, read_concept_readings
from cognition.concepts.base import ConceptReading
from cognition.contracts import EvidenceDomain


def _bars():
    return {
        "M5": [
            {
                "open": 1.0 + index,
                "high": 1.2 + index,
                "low": 0.8 + index,
                "close": 1.1 + index,
                "volume": 10.0,
                "timestamp_epoch": 300.0 * (index + 1),
                "source_timestamp_verified": True,
                "provisional": False,
            }
            for index in range(5)
        ]
    }


def _reading(concept):
    return ConceptReading(
        concept=concept,
        timeframe="M5",
        domain=EvidenceDomain.STRUCTURE,
        applicable=True,
        applicability_reason="test fixture",
        observation="non-directional test reading",
        implications=(),
        facts={},
        source_bar_epoch=1500.0,
        source_timestamp_verified=True,
    )


class _PureOHLCReader:
    key = "test.pure_ohlc"
    calls = 0

    def read(self, symbol, bars_by_tf):
        self.calls += 1
        return [_reading("pure")]


class _ContextReader:
    key = "test.contextual"

    def __init__(self):
        self.calls = 0

    def read(self, symbol, bars_by_tf):
        raise AssertionError("contextual modules must use read_with_context")

    def read_with_context(self, symbol, bars_by_tf, context):
        self.calls += 1
        return [_reading("contextual")]


def test_pure_readers_cache_by_full_completed_bar_history_context_readers_do_not():
    clear_concept_reading_cache()
    pure = _PureOHLCReader()
    pure.calls = 0
    contextual = _ContextReader()
    bars = _bars()

    first_context = {}
    first, first_faults = read_concept_readings(
        "EURUSD", bars, first_context, modules=(pure, contextual),
    )
    second_context = {}
    second, second_faults = read_concept_readings(
        "EURUSD", bars, second_context, modules=(pure, contextual),
    )

    assert first_faults == second_faults == []
    assert [item.concept for item in first] == ["pure", "contextual"]
    assert [item.concept for item in second] == ["pure", "contextual"]
    assert pure.calls == 1
    assert contextual.calls == 2
    assert first_context["pipeline_metrics"]["cache_misses"] == 1
    assert second_context["pipeline_metrics"]["cache_hits"] == 1
    assert second_context["pipeline_metrics"]["module_count"] == 2
    assert second_context["pipeline_metrics"]["elapsed_ms"] >= 0.0
    assert second_context["pipeline_metrics"]["modules"]["test.pure_ohlc"]["cache_hit"] is True
    assert second_context["pipeline_metrics"]["modules"]["test.contextual"]["cacheable"] is False


def test_pure_reader_cache_invalidates_on_historical_correction_not_only_latest_bar():
    clear_concept_reading_cache()
    pure = _PureOHLCReader()
    pure.calls = 0
    bars = _bars()
    read_concept_readings("EURUSD", bars, modules=(pure,))

    corrected = _bars()
    corrected["M5"][0]["high"] += 0.01
    # The latest completed bar is unchanged; the complete history digest changes.
    assert corrected["M5"][-1] == bars["M5"][-1]
    read_concept_readings("EURUSD", corrected, modules=(pure,))

    assert pure.calls == 2


def test_pure_reader_cache_is_symbol_scoped_and_explicitly_clearable():
    clear_concept_reading_cache()
    pure = _PureOHLCReader()
    pure.calls = 0
    bars = _bars()
    read_concept_readings("EURUSD", bars, modules=(pure,))
    read_concept_readings("GBPUSD", bars, modules=(pure,))
    assert pure.calls == 2

    clear_concept_reading_cache()
    read_concept_readings("EURUSD", bars, modules=(pure,))
    assert pure.calls == 3


def test_fractal_swings_are_shared_for_identical_completed_high_low_history():
    from cognition.concepts.market_structure import (
        _fractal_swings_cached,
        fractal_swings,
    )

    _fractal_swings_cached.cache_clear()
    bars = [
        {"high": 2.0, "low": 0.0},
        {"high": 3.0, "low": 1.0},
        {"high": 2.0, "low": -1.0},
        {"high": 4.0, "low": 0.0},
        {"high": 3.0, "low": -2.0},
    ]
    first = fractal_swings(bars)
    after_first = _fractal_swings_cached.cache_info()
    second = fractal_swings(bars)
    after_second = _fractal_swings_cached.cache_info()

    assert first == second
    assert after_first.misses == 1
    assert after_second.hits == 1
