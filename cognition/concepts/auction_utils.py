"""Shared helpers for session and auction concepts; preserve unknown provenance."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cognition.concepts.base import TIMEFRAME_SECONDS, finite, percentile_rank


FRACTAL_RADIUS = 1
VALUE_AREA_FRACTION = 0.70


def source_epoch(bar: dict[str, Any]) -> float | None:
    if bar.get("source_timestamp_verified") is not True:
        return None
    epoch = finite(bar.get("timestamp_epoch"))
    return epoch if epoch is not None and epoch > 0 else None


def volume_of(bar: dict[str, Any]) -> tuple[float | None, str]:
    value = finite(bar.get("volume"))
    basis = str(bar.get("volume_source") or "").strip().lower()
    if value is not None and value >= 0:
        return value, basis or "provider_volume"
    for key, label in (("tick_volume", "tick_volume_proxy"), ("real_volume", "real_volume")):
        value = finite(bar.get(key))
        if value is not None and value >= 0:
            return value, label
    return None, "unavailable"


def clean_bars(raw: list[dict] | None) -> list[dict[str, Any]]:
    cleaned = []
    previous_epoch = None
    for source in raw or []:
        if not isinstance(source, dict):
            continue
        values = {key: finite(source.get(key)) for key in ("open", "high", "low", "close")}
        if any(value is None for value in values.values()):
            continue
        opening, high, low, close = (values[key] for key in ("open", "high", "low", "close"))
        if high < low or high < max(opening, close) or low > min(opening, close):
            continue
        bar = {**source, **values}
        epoch = source_epoch(bar)
        if epoch is not None:
            if previous_epoch is not None and epoch <= previous_epoch:
                continue
            previous_epoch = epoch
        volume, basis = volume_of(bar)
        if volume is not None:
            bar["volume"], bar["volume_basis"] = volume, basis
        cleaned.append(bar)
    return cleaned


def reference_close(bars_by_tf: dict[str, list[dict]]) -> tuple[float | None, str]:
    for timeframe in sorted(bars_by_tf, key=lambda tf: TIMEFRAME_SECONDS.get(tf, 10**9)):
        bars = clean_bars(bars_by_tf.get(timeframe))
        if bars:
            return float(bars[-1]["close"]), timeframe
    return None, ""


def last_source_epoch(bars: list[dict[str, Any]]) -> float | None:
    return source_epoch(bars[-1]) if bars else None


def timeframe_seconds(timeframe: str) -> int:
    return TIMEFRAME_SECONDS.get(str(timeframe or "").upper(), 0)


def session_info(bar: dict[str, Any]) -> dict[str, Any]:
    open_epoch = finite(bar.get("session_open_epoch"))
    close_epoch = finite(bar.get("session_close_epoch"))
    timezone_name = str(bar.get("session_timezone") or bar.get("timezone") or "").strip()
    session_id = str(bar.get("session_id") or bar.get("session_name") or bar.get("market_session") or "").strip()
    verified = (
        bar.get("session_calendar_verified") is True
        and source_epoch(bar) is not None
        and open_epoch is not None and close_epoch is not None and open_epoch < close_epoch
        and bool(session_id) and bool(timezone_name)
    )
    if verified:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(timezone_name)
        except (ImportError, KeyError, ValueError):
            verified = False
    return {
        "verified": verified, "session_id": session_id or None,
        "open_epoch": open_epoch, "close_epoch": close_epoch,
        "timezone": timezone_name or None, "dst_status": bar.get("dst_status"),
        "holiday_status": bar.get("holiday_status"), "rollover_window": bar.get("rollover_window"),
        "rollover_start_epoch": finite(bar.get("rollover_start_epoch")),
        "rollover_end_epoch": finite(bar.get("rollover_end_epoch")),
        "liquidity": bar.get("liquidity"), "is_tradeable": bar.get("is_tradeable"),
        "minutes_to_next_session": finite(bar.get("minutes_to_next_session")),
    }


def swing_points(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points = []
    for index in range(FRACTAL_RADIUS, len(bars) - FRACTAL_RADIUS):
        previous, current, following = bars[index - 1], bars[index], bars[index + 1]
        if current["high"] > previous["high"] and current["high"] > following["high"]:
            points.append({"kind": "high", "price": current["high"], "index": index, "epoch": source_epoch(current)})
        if current["low"] < previous["low"] and current["low"] < following["low"]:
            points.append({"kind": "low", "price": current["low"], "index": index, "epoch": source_epoch(current)})
    return points


def nearest_structural_level(bars: list[dict[str, Any]], direction: str, price: float) -> float | None:
    points = swing_points(bars)
    if direction == "LONG":
        levels = [p["price"] for p in points if p["kind"] == "high" and p["price"] > price]
        return min(levels) if levels else None
    if direction == "SHORT":
        levels = [p["price"] for p in points if p["kind"] == "low" and p["price"] < price]
        return max(levels) if levels else None
    return None


def weighted_vwap(bars: list[dict[str, Any]]) -> tuple[float | None, str]:
    numerator = denominator = 0.0
    bases = set()
    for bar in bars:
        volume, basis = volume_of(bar)
        if volume is None or volume <= 0:
            continue
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        numerator += typical * volume
        denominator += volume
        bases.add(basis)
    if denominator <= 0:
        return None, "unavailable"
    basis = "tick_volume_proxy" if any("tick" in item for item in bases) else (
        "mixed_or_provider_volume" if len(bases) > 1 else next(iter(bases))
    )
    return numerator / denominator, basis


def profile_from_bars(bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """OHLCV proxy only; never infer intrabar traded volume."""
    nodes = {}
    bases = set()
    tick_size = None
    if bars and bars[-1].get("tick_size_verified") is True:
        tick_size = finite(bars[-1].get("tick_size"))
        if tick_size is not None and tick_size <= 0:
            tick_size = None
    for bar in bars:
        volume, basis = volume_of(bar)
        if volume is None or volume <= 0:
            continue
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        price = round(typical / tick_size) * tick_size if tick_size else typical
        nodes[price] = nodes.get(price, 0.0) + volume
        bases.add(basis)
    if not nodes:
        return None
    ordered = sorted(nodes.items())
    prices, volumes = [item[0] for item in ordered], [item[1] for item in ordered]
    total = sum(volumes)
    poc_index = max(range(len(ordered)), key=lambda index: (volumes[index], -prices[index]))
    low_index = high_index = poc_index
    included = volumes[poc_index]
    target = total * VALUE_AREA_FRACTION
    while included < target and (low_index > 0 or high_index < len(ordered) - 1):
        left = volumes[low_index - 1] if low_index > 0 else -1.0
        right = volumes[high_index + 1] if high_index < len(ordered) - 1 else -1.0
        if left > right:
            low_index -= 1
            included += volumes[low_index]
        elif right > left:
            high_index += 1
            included += volumes[high_index]
        else:
            if low_index > 0:
                low_index -= 1
                included += volumes[low_index]
            if high_index < len(ordered) - 1 and included < target:
                high_index += 1
                included += volumes[high_index]
    hvn, lvn = [], []
    for index in range(1, len(ordered) - 1):
        left, current, right = volumes[index - 1:index + 2]
        if current >= left and current >= right and (current > left or current > right):
            hvn.append(prices[index])
        if current <= left and current <= right and (current < left or current < right):
            lvn.append(prices[index])
    basis = "tick_volume_proxy" if any("tick" in item for item in bases) else (
        "mixed_or_provider_volume" if len(bases) > 1 else next(iter(bases))
    )
    return {
        "poc": prices[poc_index], "vah": prices[high_index], "val": prices[low_index],
        "hvn": tuple(hvn), "lvn": tuple(lvn), "total_volume": total,
        "poc_volume": volumes[poc_index], "poc_concentration": volumes[poc_index] / total,
        "volume_basis": basis,
        "price_assignment": "verified_tick_size" if tick_size else "bar_typical_price_exact_nodes",
        "node_count": len(ordered),
    }


def utc_week_key(epoch: float) -> tuple[int, int]:
    value = datetime.fromtimestamp(epoch, tz=timezone.utc).isocalendar()
    return value.year, value.week


def percentile_strength(value: float | None, sample: list[float]) -> float | None:
    return percentile_rank(value, sample) if value is not None else None
