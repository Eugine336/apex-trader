"""Tick microstructure reader: broker rates, quote changes and timing semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cognition.concepts import CONCEPT_MODULES
from cognition.concepts.tick_microstructure_readings import TickMicrostructureReadings
from cognition.market_lenses import analyze_market_frames


def _depth():
    levels = [
        {"price": 100.00, "volume": 10.0, "side": "bid"},
        {"price": 100.02, "volume": 5.0, "side": "ask"},
    ]
    return {
        "levels": [
            {
                **item,
                "source": "mt5.market_book_get",
                "source_symbol": "XAUUSD.a",
                "source_verified": True,
                "volume_basis": "volume_real",
            }
            for item in levels
        ],
        "source": "mt5.market_book_get",
        "source_symbol": "XAUUSD.a",
        "source_verified": True,
    }


def _tape(*, naive_time: bool = False):
    start = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
    times = [start + timedelta(seconds=index) for index in range(1, 6)]
    if naive_time:
        times = [stamp.replace(tzinfo=None) for stamp in times]
    quotes = [
        (100.00, 100.02, "buy"),
        (100.01, 100.02, "buy"),
        (100.01, 100.02, None),
        (100.01, 100.03, "sell"),
        (100.01, 100.03, None),
    ]
    rows = [
        {
            "time_msc_datetime": stamp,
            "time": stamp,
            "bid": bid,
            "ask": ask,
            "aggressor_side": aggressor,
        }
        for stamp, (bid, ask, aggressor) in zip(times, quotes)
    ]
    return {
        "rows": rows,
        "source": "mt5.copy_ticks_range",
        "source_verified": True,
        "source_symbol": "XAUUSD.a",
        "source_timezone": "UTC",
        "aggressor_side_basis": "broker_flag_when_present_otherwise_unknown",
        "window_start_epoch": start.timestamp(),
        "window_end_epoch": (start + timedelta(seconds=10)).timestamp(),
    }


def _reading(tape, depth=None):
    context = {"tick_history": tape}
    if depth is not None:
        context["market_depth"] = depth
    return TickMicrostructureReadings().read_with_context("XAUUSD", {}, context)[0]


def test_tick_microstructure_reader_is_registered_for_the_declared_method():
    modules = [
        module for module in CONCEPT_MODULES
        if module.key == "tick_microstructure_readings"
    ]
    assert len(modules) == 1
    assert modules[0].covers_methods == ("tick_microstructure",)
    assert modules[0].covers_lenses == ()


def test_raw_broker_ticks_produce_rate_quote_change_spread_and_microprice_readings():
    reading = _reading(_tape(), _depth())
    facts = reading.facts

    assert reading.applicable is True
    assert facts["status"] == "verified_tick_tape"
    assert facts["accepted_tick_count"] == 5
    assert facts["tick_rate_hz"] == 0.5
    assert facts["quote_update_count"] == 2
    assert facts["quote_rate_hz"] == 0.2
    assert facts["bid_quote_update_count"] == 1
    assert facts["ask_quote_update_count"] == 1
    assert facts["spread"]["change_event_count"] == 2
    assert facts["spread"]["change_price_units"] == 0.0
    assert facts["quote_intervals_milliseconds"]["median"] == 2000.0
    assert facts["microprice"]["available"] is True
    assert facts["microprice"]["value"] is not None
    assert facts["broker_aggressor_flag_counts"]["buy_flagged_ticks"] == 2
    assert facts["broker_aggressor_flag_counts"]["sell_flagged_ticks"] == 1
    assert facts["broker_aggressor_flag_counts"]["these_are_event_counts_not_executed_volume"] is True
    assert facts["tick_store_coalescing_used"] is False
    assert facts["source_timestamp_verified"] is True
    assert reading.source_bar_epoch == (datetime(2026, 11, 6, 12, 0, 5, tzinfo=timezone.utc).timestamp())
    assert reading.implications == ()


def test_missing_tick_feed_remains_unknown_not_a_zero_rate():
    reading = _reading({
        "rows": [],
        "source": None,
        "source_verified": False,
        "source_symbol": None,
        "source_timezone": None,
    })
    assert reading.applicable is False
    assert reading.facts["tick_rate_hz"] is None
    assert reading.facts["quote_rate_hz"] is None
    assert reading.facts["status"] == "unavailable_or_unverified"
    assert "source-verified broker tick history with UTC source timestamps" in reading.unknowns


def test_naive_tick_timestamps_cannot_be_promoted_to_verified_source_time():
    reading = _reading(_tape(naive_time=True))
    assert reading.facts["accepted_tick_count"] == 0
    assert reading.facts["invalid_tick_count"] == 5
    assert reading.facts["tick_rate_hz"] is None
    assert reading.facts["source_timestamp_verified"] is False
    assert reading.applicable is False
    assert reading.source_bar_epoch is None
    assert any("provider-originated timestamp verification" in item for item in reading.unknowns)


def test_microprice_remains_unknown_without_verified_two_sided_displayed_sizes():
    reading = _reading(_tape())
    assert reading.facts["microprice"]["available"] is False
    assert reading.facts["microprice"]["value"] is None
    assert any("verified two-sided Level-2 displayed sizes" in item for item in reading.unknowns)


def test_out_of_window_and_malformed_tick_rows_are_excluded():
    tape = _tape()
    tape["rows"] = list(tape["rows"]) + [
        {
            "time_msc_datetime": datetime(2026, 11, 6, 12, 1, 0, tzinfo=timezone.utc),
            "bid": 100.0,
            "ask": 100.02,
        },
        {
            "time_msc_datetime": datetime(2026, 11, 6, 12, 0, 6, tzinfo=timezone.utc),
            "bid": 0,
            "ask": 100.02,
        },
    ]
    reading = _reading(tape)
    assert reading.facts["accepted_tick_count"] == 5
    assert reading.facts["invalid_tick_count"] == 2


def test_tick_microstructure_reaches_native_brain_without_ohlc_history():
    evidence = analyze_market_frames(
        "XAUUSD",
        {},
        concept_context={
            "tick_history": _tape(),
            "market_depth": _depth(),
        },
    )
    item = next(
        row for row in evidence
        if row.measurements.get("source_kind") == "concept_reading"
        and row.measurements.get("concept") == "tick_microstructure"
    )
    assert item.polarity == 0.0
    assert item.data_basis == "broker_historical_tick_tape"
    assert item.independence_group == "broker_tick_tape:mt5.copy_ticks_range:XAUUSD.a"
    assert item.source_timestamp_verified is True
    assert item.source_timestamp_epoch is not None
    assert item.measurements["facts"]["tick_rate_hz"] == 0.5
    assert item.measurements["implications"] == []
