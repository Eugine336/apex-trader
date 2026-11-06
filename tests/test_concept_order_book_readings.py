"""Order-book concept reader: exact snapshot metrics and provenance boundaries."""

from __future__ import annotations

import math
import time
from types import SimpleNamespace

import pandas as pd

from cognition.concepts import CONCEPT_MODULES
from cognition.concepts.order_book_readings import OrderBookReadings
from cognition.market_lenses import analyze_market_frames
from platforms.mt5 import mt5_connector as mt5_connector_module
from platforms.mt5.mt5_connector import MT5Connector


def _snapshot(levels=None, **overrides):
    raw = levels if levels is not None else [
        {"price": 100.00, "volume": 10.0, "side": "bid"},
        {"price": 99.99, "volume": 8.0, "side": "bid"},
        {"price": 100.02, "volume": 5.0, "side": "ask"},
        {"price": 100.03, "volume": 7.0, "side": "ask"},
    ]
    tagged = [
        {
            **level,
            "source": "mt5.market_book_get",
            "source_verified": True,
            "source_symbol": "XAUUSD.a",
            "volume_basis": "volume_real",
        }
        for level in raw
    ]
    snapshot = {
        "levels": tagged,
        "source": "mt5.market_book_get",
        "source_verified": True,
        "source_symbol": "XAUUSD.a",
        "snapshot_received_epoch": time.time(),
    }
    snapshot.update(overrides)
    return snapshot


def _spec():
    return {
        "source": "mt5.symbol_info",
        "source_verified": True,
        "specification_verified": True,
        "trade_tick_size": 0.01,
    }


def _read(snapshot, bars=None):
    readings = OrderBookReadings().read_with_context(
        "XAUUSD", {"M5": bars or []}, {"market_depth": snapshot, "symbol_spec": _spec()},
    )
    return readings[0]


def _frame():
    now = float(int(time.time() // 300) * 300)
    rows = []
    for index in range(20):
        close = 2300.0 + index * 0.1 + math.sin(index * 0.5) * 0.2
        rows.append({
            "time": now - (20 - index) * 300,
            "open": close - 0.05,
            "high": close + 0.15,
            "low": close - 0.15,
            "close": close,
            "volume": 100 + index,
            "tick_volume": 100 + index,
            "real_volume": 0,
        })
    frame = pd.DataFrame(rows)
    frame.attrs.update({
        "source_symbol": "XAUUSD",
        "source_timestamp_origin": "broker_ohlcv",
        "source_broker": "mt5",
        "source_timestamp_verified": True,
        "source_timestamp_unit": "unix_seconds",
        "volume_basis": "tick_volume_alias_with_real_volume_preserved",
    })
    return frame


def test_order_book_reader_is_registered_for_the_declared_method():
    modules = [
        module for module in CONCEPT_MODULES
        if module.key == "order_book_readings"
    ]
    assert len(modules) == 1
    assert modules[0].covers_methods == ("order_book",)
    assert modules[0].covers_lenses == ()


def test_order_book_computes_spread_microprice_queue_and_depth_imbalance():
    reading = _read(_snapshot())
    facts = reading.facts

    assert reading.applicable is True
    assert facts["status"] == "verified_two_sided_snapshot"
    assert facts["best_bid"] == {"price": 100.0, "volume": 10.0}
    assert facts["best_ask"] == {"price": 100.02, "volume": 5.0}
    assert math.isclose(facts["spread_price_units"], 0.02)
    assert math.isclose(facts["spread_ticks"], 2.0)
    assert math.isclose(facts["top_of_book"]["displayed_queue_imbalance"], 1.0 / 3.0)
    assert math.isclose(facts["top_of_book"]["microprice"], 100.01333333333334)
    assert math.isclose(facts["depth_by_levels_per_side"]["3"]["imbalance"], 0.2)
    assert facts["depth_by_levels_per_side"]["5"]["complete_for_requested_level_count"] is False
    assert facts["displayed_size_is_not_executed_volume"] is True
    assert facts["displayed_queue_imbalance_is_not_account_queue_position"] is True
    assert facts["provider_update_age_known"] is False
    assert reading.source_bar_epoch is None
    assert reading.implications == ()


def test_missing_or_unverified_book_is_unknown_not_a_zero_imbalance():
    reading = _read({
        "levels": [],
        "source": "mt5.market_book_get",
        "source_verified": False,
    })
    facts = reading.facts

    assert reading.applicable is False
    assert facts["top5_order_book_imbalance"] is None
    assert facts["spread_price_units"] is None
    assert "source-verified broker Level-2 bid/ask snapshot" in reading.unknowns
    assert "at least one valid displayed bid level and one valid displayed ask level" in reading.unknowns


def test_crossed_or_locked_book_is_not_used_for_spread_or_microprice():
    reading = _read(_snapshot([
        {"price": 100.02, "volume": 8.0, "side": "bid"},
        {"price": 100.01, "volume": 6.0, "side": "ask"},
    ]))
    assert reading.applicable is False
    assert reading.facts["locked_or_crossed"] is True
    assert reading.facts["spread_price_units"] is None
    assert reading.facts["top_of_book"]["microprice"] is None
    assert "valid uncrossed two-sided quote required for spread and microprice" in reading.unknowns


def test_malformed_and_unknown_side_levels_are_excluded_not_relabelled():
    levels = [
        {"price": 100.0, "volume": 10.0, "side": "bid"},
        {"price": 100.02, "volume": 5.0, "side": "ask"},
        {"price": 99.9, "volume": -3.0, "side": "bid"},
        {"price": 100.01, "volume": 99.0, "side": "unknown"},
        {"price": float("nan"), "volume": 4.0, "side": "ask"},
    ]
    reading = _read(_snapshot(levels))
    assert reading.applicable is True
    assert reading.facts["invalid_levels_excluded"] == 3
    assert reading.facts["valid_bid_levels"] == 1
    assert reading.facts["valid_ask_levels"] == 1
    assert any("invalid or unclassified book level(s)" in item for item in reading.unknowns)


def test_provider_time_is_not_inferred_from_local_snapshot_retrieval_time():
    reading = _read(_snapshot())
    assert reading.facts["snapshot_received_epoch"] is not None
    assert reading.facts["provider_timestamp_verified"] is False
    assert reading.facts["provider_update_age_known"] is False
    assert any("provider-originated book update timestamp" in item for item in reading.unknowns)


def test_live_market_lens_passes_book_snapshot_to_native_brain_evidence():
    evidence = analyze_market_frames(
        "XAUUSD",
        {"M5": _frame()},
        source_includes_forming_bar=False,
        concept_context={
            "symbol_spec": _spec(),
            "market_depth": _snapshot(),
        },
    )
    item = next(
        entry for entry in evidence
        if entry.measurements.get("source_kind") == "concept_reading"
        and entry.measurements.get("concept") == "order_book"
    )
    assert item.polarity == 0.0
    assert item.data_basis == "broker_order_book_snapshot"
    assert item.independence_group == "broker_order_book:mt5.market_book_get:XAUUSD"
    assert item.source_timestamp_verified is False
    assert item.source_timestamp_epoch is None
    assert item.measurements["facts"]["top5_order_book_imbalance"] is not None
    assert item.measurements["implications"] == []


def test_order_book_is_read_even_when_ohlc_history_is_absent():
    evidence = analyze_market_frames(
        "XAUUSD",
        {},
        concept_context={"market_depth": _snapshot()},
    )
    item = next(
        entry for entry in evidence
        if entry.measurements.get("source_kind") == "concept_reading"
        and entry.measurements.get("concept") == "order_book"
    )
    assert math.isclose(item.measurements["facts"]["spread_price_units"], 0.02, rel_tol=1e-12, abs_tol=1e-12)
    assert item.source_timestamp_verified is False


def test_mt5_adapter_marks_local_receipt_time_without_claiming_provider_time(monkeypatch):
    levels = [
        SimpleNamespace(price=100.0, volume=10, volume_real=10.0, type=2),
        SimpleNamespace(price=100.02, volume=5, volume_real=5.0, type=1),
    ]
    fake_mt5 = SimpleNamespace(
        market_book_add=lambda symbol: True,
        market_book_get=lambda symbol: levels,
        market_book_release=lambda symbol: True,
    )
    monkeypatch.setattr(mt5_connector_module, "mt5", fake_mt5)
    connector = object.__new__(MT5Connector)
    monkeypatch.setattr(connector, "_require_connection", lambda: None)
    monkeypatch.setattr(connector, "symbol_map", lambda symbol: "XAUUSD.a")

    result = connector.get_market_depth("XAUUSD")

    assert len(result) == 2
    assert all(level["source"] == "mt5.market_book_get" for level in result)
    assert all(level["source_symbol"] == "XAUUSD.a" for level in result)
    assert all(level["source_verified"] is True for level in result)
    assert all(level["snapshot_received_epoch"] > 0 for level in result)
    assert all("source_timestamp_epoch" not in level for level in result)
