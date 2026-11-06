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

def test_market_lenses_converts_each_broker_frame_once(monkeypatch):
    import pandas as pd
    import cognition.market_lenses as market_lenses

    original = market_lenses._rows
    calls = []

    def counted_rows(frame, limit=32, *, expected_symbol="", return_tail_limit=0):
        calls.append((limit, expected_symbol, return_tail_limit))
        return original(
            frame, limit=limit, expected_symbol=expected_symbol,
            return_tail_limit=return_tail_limit,
        )

    monkeypatch.setattr(market_lenses, "_rows", counted_rows)
    frame = pd.DataFrame({
        "time": [1_800_000_000 + index * 300 for index in range(40)],
        "open": [1.0 + index * 0.001 for index in range(40)],
        "high": [1.1 + index * 0.001 for index in range(40)],
        "low": [0.9 + index * 0.001 for index in range(40)],
        "close": [1.05 + index * 0.001 for index in range(40)],
        "volume": [100.0] * 40,
    })
    frame.attrs.update({
        "source_symbol": "EURUSD",
        "source_timestamp_origin": "broker_ohlcv",
        "source_broker": "test-broker",
        "source_timestamp_verified": True,
        "source_timestamp_unit": "unix_seconds",
    })

    market_lenses.analyze_market_frames(
        "EURUSD", {"M5": frame}, source_includes_forming_bar=True,
    )

    assert calls == [(1_000_000, "EURUSD", 32)]

def test_fractal_swings_incrementally_update_appended_and_rolling_history():
    from cognition.concepts.market_structure import (
        _fractal_swings_cached,
        clear_fractal_swing_cache,
        fractal_swing_cache_status,
        fractal_swings,
    )

    clear_fractal_swing_cache()
    base = [
        {"high": 2.0, "low": 0.0},
        {"high": 3.0, "low": 1.0},
        {"high": 2.0, "low": -1.0},
        {"high": 4.0, "low": 0.0},
        {"high": 3.0, "low": -2.0},
    ]
    key = "incremental-test:EURUSD:M5"
    first = fractal_swings(base, cache_key=key)
    appended = base + [{"high": 5.0, "low": -1.0}]
    second = fractal_swings(appended, cache_key=key)
    second_reference = list(_fractal_swings_cached(tuple(
        (bar["high"], bar["low"]) for bar in appended
    )))
    assert second == second_reference

    rolling = appended[1:] + [{"high": 4.5, "low": -3.0}]
    third = fractal_swings(rolling, cache_key=key)
    third_reference = list(_fractal_swings_cached(tuple(
        (bar["high"], bar["low"]) for bar in rolling
    )))
    assert third == third_reference
    assert first == list(_fractal_swings_cached(tuple(
        (bar["high"], bar["low"]) for bar in base
    )))
    assert fractal_swing_cache_status()["incremental_updates"] == 2

def test_prefix_swing_read_does_not_discard_full_history_incremental_state():
    from cognition.concepts.market_structure import (
        _fractal_swings_cached,
        clear_fractal_swing_cache,
        fractal_swing_cache_status,
        fractal_swings,
    )

    clear_fractal_swing_cache()
    bars = [
        {"high": 2.0, "low": 0.0},
        {"high": 3.0, "low": 1.0},
        {"high": 2.0, "low": -1.0},
        {"high": 4.0, "low": 0.0},
        {"high": 3.0, "low": -2.0},
    ]
    key = "shared-structure-liquidity:EURUSD:M5"
    full = fractal_swings(bars, cache_key=key)
    assert full == list(_fractal_swings_cached(tuple(
        (bar["high"], bar["low"]) for bar in bars
    )))
    prefix = fractal_swings(bars[:-1], cache_key=key)
    assert prefix == list(_fractal_swings_cached(tuple(
        (bar["high"], bar["low"]) for bar in bars[:-1]
    )))

    next_full = bars[1:] + [{"high": 5.0, "low": -3.0}]
    advanced = fractal_swings(next_full, cache_key=key)
    reference = list(_fractal_swings_cached(tuple(
        (bar["high"], bar["low"]) for bar in next_full
    )))
    assert advanced == reference
    status = fractal_swing_cache_status()
    assert status["prefix_hits"] == 1
    assert status["incremental_updates"] == 1

def test_single_pass_row_conversion_preserves_raw_tail_window_with_invalid_rows():
    import pandas as pd
    import cognition.market_lenses as market_lenses

    frame = pd.DataFrame({
        "time": [1_800_000_000 + index * 300 for index in range(40)],
        "open": [1.0] * 40,
        "high": [1.1] * 40,
        "low": [0.9] * 40,
        "close": [1.05] * 40,
    })
    frame.loc[38, "high"] = 0.5
    frame.loc[39, "low"] = 1.2

    full, tail_window = market_lenses._rows(
        frame, limit=1_000_000, expected_symbol="EURUSD",
        return_tail_limit=32,
    )
    prior_tail_result = market_lenses._rows(
        frame, limit=32, expected_symbol="EURUSD",
    )

    assert len(full) == 38
    assert tail_window == prior_tail_result
