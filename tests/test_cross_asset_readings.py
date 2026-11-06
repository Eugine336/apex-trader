import math

from cognition.concepts.cross_asset_readings import CrossAssetReadings
from cognition.market_lenses import analyze_market_frames


def _bars_from_returns(returns, *, base_price=100.0, start_epoch=1_780_000_000.0, symbol="XAUUSD"):
    rows = []
    close = base_price
    for index, value in enumerate(returns):
        close *= math.exp(value)
        width = close * 0.001
        rows.append({
            "open": close,
            "high": close + width,
            "low": close - width,
            "close": close,
            "volume": 100.0 + index,
            "timestamp_epoch": start_epoch + index * 300.0,
            "source_timestamp_verified": True,
            "source_timestamp_origin": "broker_server_time",
            "source_symbol": symbol,
        })
    return rows


def _returns(count=90):
    return [
        0.0015 * math.sin(index * 0.31)
        + 0.0007 * math.cos(index * 0.17)
        + 0.0002 * math.sin(index * 0.071)
        for index in range(count)
    ]


def test_cross_asset_reader_ranks_measured_correlations_from_common_timestamps():
    target_returns = _returns()
    peer_returns = [value * 0.8 + 0.00003 * math.sin(i * 0.73) for i, value in enumerate(target_returns)]
    inverse_returns = [-value * 0.9 + 0.00002 * math.cos(i * 0.41) for i, value in enumerate(target_returns)]
    target = _bars_from_returns(target_returns)
    positive = _bars_from_returns(peer_returns, base_price=1.1, symbol="EURUSD")
    negative = _bars_from_returns(inverse_returns, base_price=150.0, symbol="USDJPY")
    misaligned = _bars_from_returns(peer_returns, base_price=0.9, start_epoch=1_780_000_150.0, symbol="GBPUSD")

    readings = CrossAssetReadings().read_with_context(
        "XAUUSD",
        {"M5": target},
        {"related_bars_by_symbol": {
            "EURUSD": {"M5": positive},
            "USDJPY": {"M5": negative},
            "GBPUSD": {"M5": misaligned},
        }},
    )

    assert len(readings) == 2
    assert readings[0].facts["measured_pair_rank"] == 1
    assert readings[0].facts["peer_symbol"] == "EURUSD"
    assert readings[0].facts["correlation"] > 0.95
    assert readings[1].facts["peer_symbol"] == "USDJPY"
    assert readings[1].facts["correlation"] < -0.95
    assert all(item.source_timestamp_verified is True for item in readings)
    assert all(item.data_basis == "synchronized_multi_symbol_completed_ohlc" for item in readings)
    assert all(item.implications == () for item in readings)
    assert all(item.facts["directional_decision_emitted"] is False for item in readings)


def test_cross_asset_reader_does_not_promote_missing_or_unsynchronized_peers():
    target = _bars_from_returns(_returns())
    misaligned = _bars_from_returns(_returns(), base_price=1.1, start_epoch=1_780_000_150.0, symbol="EURUSD")

    readings = CrossAssetReadings().read_with_context(
        "XAUUSD",
        {"M5": target},
        {"related_bars_by_symbol": {"EURUSD": {"M5": misaligned}}},
    )

    assert len(readings) == 1
    assert readings[0].applicable is False
    assert readings[0].source_bar_epoch is None
    assert readings[0].source_timestamp_verified is False
    assert readings[0].data_basis == "no_synchronized_peer_data"
    assert any("No fixed pair list" in item for item in readings[0].unknowns)


def test_cross_asset_reader_requires_a_minimum_common_return_sample():
    target = _bars_from_returns(_returns(20))
    peer = _bars_from_returns(_returns(20), base_price=1.2)

    readings = CrossAssetReadings().read_with_context(
        "XAUUSD",
        {"M5": target},
        {"related_bars_by_symbol": {"EURUSD": {"M5": peer}}},
    )

    assert len(readings) == 1
    assert readings[0].applicable is False
    assert readings[0].facts["minimum_return_samples"] == 30


def test_market_lenses_passes_only_completed_peer_bars_into_cross_asset_reader():
    target = _bars_from_returns(_returns())
    peer = _bars_from_returns(_returns(), base_price=1.2, symbol="EURUSD")
    forming_epoch = target[-1]["timestamp_epoch"]

    evidence = analyze_market_frames(
        "XAUUSD",
        {"M5": target},
        concept_context={
            "related_symbol_frames": {"EURUSD": {"M5": peer}},
        },
    )
    pair = next(
        item for item in evidence
        if item.measurements.get("concept") == "cross_asset"
        and item.measurements.get("peer_symbol") == "EURUSD"
    )

    assert pair.measurements["source_timestamp_known"] is True
    assert pair.measurements["source_timestamp_epoch"] == forming_epoch - 300.0
    assert pair.measurements["latest_common_epoch"] == forming_epoch - 300.0
    assert pair.measurements["directional_decision_emitted"] is False
    assert pair.source_module == "concept.cross_asset.xauusd.eurusd.m5"
