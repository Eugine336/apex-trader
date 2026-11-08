from __future__ import annotations

import copy
import math

from cognition.concepts import CONCEPT_MODULES
from cognition.concepts.auction_utils import nearest_structural_level, swing_points


def _bars(count: int, *, offset: float = 0.0, step: int = 300, end_epoch: float = 1_800_000_000.0):
    result = []
    start = end_epoch - (count - 1) * step
    for index in range(count):
        centre = 100.0 + offset + math.sin(index / 4.0) * 2.0 + math.sin(index / 13.0)
        width = 0.2 + (index % 5) * 0.03
        result.append({
            "open": centre - 0.05, "high": centre + width, "low": centre - width,
            "close": centre + 0.03, "volume": 100.0 + index,
            "tick_volume": 100.0 + index, "timestamp_epoch": start + index * step,
            "source_timestamp_verified": True,
        })
    return result


def _mirror(bars, centre: float = 500.0):
    return [{
        **bar, "open": centre - bar["open"], "high": centre - bar["low"],
        "low": centre - bar["high"], "close": centre - bar["close"],
    } for bar in bars]


def _contains_lookahead_true(value):
    if isinstance(value, dict):
        return any(
            (str(key).lower() in {"lookahead", "uses_future_data"} and item is True)
            or _contains_lookahead_true(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_lookahead_true(item) for item in value)
    return False


def test_confirmed_swing_geometry_is_prefix_stable_after_confirmation():
    history = _bars(24)
    prefix = history[:16]
    future = _bars(8, offset=50.0, step=300, end_epoch=1_800_002_400.0)
    prefix_points = swing_points(prefix)
    extended_points = swing_points(prefix + future)
    extended_by_index = {(item["index"], item["kind"]): item for item in extended_points}
    for point in prefix_points:
        later = extended_by_index[(point["index"], point["kind"])]
        assert later["price"] == point["price"]
        assert later["epoch"] == point["epoch"]


def test_structural_geometry_is_mirror_symmetric():
    history = _bars(80)
    mirrored = _mirror(history)
    original_points = swing_points(history)
    mirrored_points = swing_points(mirrored)
    mirrored_by_index = {(p["index"], p["kind"]): p for p in mirrored_points}
    for point in original_points:
        opposite_kind = "low" if point["kind"] == "high" else "high"
        mirror_point = mirrored_by_index[(point["index"], opposite_kind)]
        assert math.isclose(mirror_point["price"], 500.0 - point["price"], abs_tol=1e-9)
        assert mirror_point["epoch"] == point["epoch"]
    price = history[-1]["close"]
    mirrored_price = 500.0 - price
    long_level = nearest_structural_level(history, "LONG", price)
    short_level = nearest_structural_level(mirrored, "SHORT", mirrored_price)
    if long_level is None:
        assert short_level is None
    else:
        assert short_level is not None
        assert math.isclose(short_level, 500.0 - long_level, abs_tol=1e-9)


def test_registered_concept_readers_keep_source_time_within_their_timeframe():
    bars_by_tf = {
        "M5": _bars(100, step=300),
        "M15": _bars(100, offset=0.4, step=900),
        "H1": _bars(100, offset=-0.3, step=3600),
    }
    original = copy.deepcopy(bars_by_tf)
    cutoff_by_timeframe = {tf: rows[-1]["timestamp_epoch"] for tf, rows in bars_by_tf.items()}
    for module in CONCEPT_MODULES:
        contextual_reader = getattr(module, "read_with_context", None)
        readings = (
            contextual_reader("EURUSD", bars_by_tf, {})
            if callable(contextual_reader) else module.read("EURUSD", bars_by_tf)
        )
        for reading in readings:
            timeframe = str(reading.timeframe or "").upper()
            if reading.source_bar_epoch is not None and timeframe in cutoff_by_timeframe:
                assert reading.source_bar_epoch <= cutoff_by_timeframe[timeframe], (
                    module.key, reading.concept, timeframe, reading.source_bar_epoch,
                )
            if reading.source_timestamp_verified:
                assert reading.source_timestamp_epoch is not None, module.key
                assert timeframe in cutoff_by_timeframe, (module.key, reading.concept, timeframe)
                assert reading.source_timestamp_epoch <= cutoff_by_timeframe[timeframe], (
                    module.key, reading.concept, timeframe, reading.source_timestamp_epoch,
                )
            assert not _contains_lookahead_true(reading.facts), (module.key, reading.concept, reading.facts)
    assert bars_by_tf == original
