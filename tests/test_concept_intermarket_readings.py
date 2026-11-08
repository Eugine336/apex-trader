"""Tests for broker-neutral, point-in-time intermarket readings."""
import pandas as pd

from cognition.concepts.intermarket_readings import IntermarketReadings


def _frame(symbol, closes, base_epoch, *, source_symbol=None):
    rows = []
    for index, close in enumerate(closes):
        rows.append({
            "time": base_epoch + index * 300,
            "open": close,
            "high": close + 0.8,
            "low": close - 0.8,
            "close": close,
            "tick_volume": 10 + index,
            "real_volume": 0,
            "spread": 1,
        })
    frame = pd.DataFrame(rows)
    frame.attrs.update({
        "source_timestamp_verified": True,
        "source_symbol": source_symbol or symbol,
        "source_timestamp_unit": "unix_seconds",
        "source_timestamp_origin": "broker_ohlcv",
        "source_broker": "test-provider",
        "volume_basis": "tick_volume_alias_with_real_volume_preserved",
    })
    return frame


def _bars(symbol, closes, base_epoch, cutoff):
    # The target's market-lens path has already removed the forming row.
    return [
        {
            "timestamp_epoch": base_epoch + index * 300,
            "open": close, "high": close + 0.8, "low": close - 0.8, "close": close,
            "source_timestamp_verified": True,
            "source_timestamp_origin": "broker_ohlcv",
            "source_broker": "test-provider",
        }
        for index, close in enumerate(closes)
        if base_epoch + index * 300 + 300 <= cutoff
    ]


def test_intermarket_reader_emits_broker_neutral_pair_measurements():
    base = 1_800_000_000
    cutoff = base + 11 * 300 + 100
    target_closes = [100 + index * 0.5 for index in range(12)]
    peer_closes = [50 + index * 0.25 for index in range(12)]
    module = IntermarketReadings()
    context = {
        "multi_symbol_frames": {
            "EURUSD": {"M5": _frame("EURUSD", target_closes, base)},
            "GBPUSD": {"M5": _frame("GBPUSD", peer_closes, base)},
        },
        "multi_symbol_snapshot_cutoff_epoch": cutoff,
        "multi_symbol_specs": {
            "EURUSD": {
                "source_verified": True, "specification_verified": True,
                "currency_base": "EUR", "currency_profit": "USD",
            },
            "GBPUSD": {
                "source_verified": True, "specification_verified": True,
                "currency_base": "GBP", "currency_profit": "USD",
            },
        },
    }
    readings = module.read_with_context(
        "EURUSD", {"M5": _bars("EURUSD", target_closes, base, cutoff)}, context,
    )
    by_concept = {reading.concept: reading for reading in readings}

    assert {"cross_asset", "smt_divergence", "relative_value", "currency_strength"} <= set(by_concept)
    cross_asset = by_concept["cross_asset"]
    assert cross_asset.facts["peer_relationships"][0]["symbol"] == "GBPUSD"
    assert cross_asset.facts["peer_relationships"][0]["return_correlation"] > 0.99
    assert cross_asset.source_timestamp_verified is True
    assert cross_asset.source_timestamp_epoch <= cutoff
    assert cross_asset.implications[0].direction == "FLAT"
    assert by_concept["relative_value"].facts["stationarity_test_performed"] is False
    assert by_concept["currency_strength"].facts["ranked_currencies"]


def test_intermarket_reader_rejects_peer_symbol_provenance_mismatch():
    base = 1_800_000_000
    cutoff = base + 11 * 300 + 100
    closes = [100 + index for index in range(12)]
    module = IntermarketReadings()
    context = {
        "multi_symbol_frames": {
            "GBPUSD": {"M5": _frame("GBPUSD", closes, base, source_symbol="EURUSD")},
        },
        "multi_symbol_snapshot_cutoff_epoch": cutoff,
    }
    readings = module.read_with_context(
        "EURUSD", {"M5": _bars("EURUSD", closes, base, cutoff)}, context,
    )
    assert readings == []


def test_intermarket_reader_does_not_use_forming_or_future_peer_bar():
    base = 1_800_000_000
    cutoff = base + 11 * 300 + 100
    closes = [100 + index * 0.5 for index in range(12)]
    module = IntermarketReadings()
    context = {
        "multi_symbol_frames": {
            "GBPUSD": {"M5": _frame("GBPUSD", closes, base)},
        },
        "multi_symbol_snapshot_cutoff_epoch": cutoff,
    }
    readings = module.read_with_context(
        "EURUSD", {"M5": _bars("EURUSD", closes, base, cutoff)}, context,
    )
    cross_asset = next(item for item in readings if item.concept == "cross_asset")
    assert cross_asset.facts["snapshot_cutoff_epoch"] == cutoff
    assert cross_asset.source_timestamp_epoch <= cutoff
    assert cross_asset.facts["peer_relationships"][0]["common_bar_count"] == 11
