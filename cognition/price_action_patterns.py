"""Context-aware M1 price-action observations for Apex's CognitiveBrain.

This observer preserves the useful legacy M1 pattern vocabulary without reviving
the legacy zone-gated entry pipeline. It emits measurements only: no trade
direction, order, entry price, stop, target, score or trigger authority. Patterns
from a forming candle are explicitly provisional and can repaint.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

from cognition.contracts import Evidence, EvidenceDomain


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _row_value(row: Any, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        try:
            if hasattr(row, "get"):
                value = row.get(key, None)
            else:
                value = row[key]
            if value is not None:
                return _number(value)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return None


def _rows(frame: Any, limit: int = 30) -> list[dict[str, float]]:
    """Read OHLC(V) from a pandas-like frame without importing pandas."""
    if frame is None:
        return []
    try:
        if getattr(frame, "empty", False):
            return []
        tail = frame.tail(limit) if callable(getattr(frame, "tail", None)) else frame
        iterator = tail.iterrows() if callable(getattr(tail, "iterrows", None)) else enumerate(tail)
        columns = {str(col).lower(): str(col) for col in getattr(tail, "columns", [])}
        out: list[dict[str, float]] = []
        for _, row in iterator:
            def value(*names: str) -> float | None:
                aliases = tuple(columns[name] for name in names if name in columns) + names
                return _row_value(row, aliases)
            opening, high, low, close = (
                value("open", "o"), value("high", "h"),
                value("low", "l"), value("close", "c"),
            )
            if None in (opening, high, low, close):
                continue
            if min(opening, high, low, close) < 0 or high < low:
                continue
            item = {"open": opening, "high": high, "low": low, "close": close}
            volume = value("volume", "tick_volume", "real_volume")
            if volume is not None:
                item["volume"] = max(0.0, volume)
            out.append(item)
        return out
    except Exception:  # noqa: BLE001 — market observer must not break cognition
        return []


def detect_m1_price_action(frame: Any) -> dict[str, Any]:
    """Return evidence measurements from recent M1 bars; never returns a trade."""
    bars = _rows(frame)
    if len(bars) < 3:
        return {}

    current = bars[-1]
    previous = bars[-2]
    mother, inside = bars[-3], bars[-2]
    candle_range = max(0.0, current["high"] - current["low"])
    body = abs(current["close"] - current["open"])
    if candle_range <= 0:
        return {}
    upper_wick = current["high"] - max(current["open"], current["close"])
    lower_wick = min(current["open"], current["close"]) - current["low"]
    prior = bars[-21:-1]
    average_range = sum(max(0.0, b["high"] - b["low"]) for b in prior) / max(1, len(prior))
    prior_high = max((b["high"] for b in prior), default=current["high"])
    prior_low = min((b["low"] for b in prior), default=current["low"])
    boundary_buffer = max(average_range * 0.3, candle_range * 0.05)
    near_high = abs(current["close"] - prior_high) <= boundary_buffer or current["high"] >= prior_high - boundary_buffer
    near_low = abs(current["close"] - prior_low) <= boundary_buffer or current["low"] <= prior_low + boundary_buffer
    tags: list[str] = []
    bullish: list[str] = []
    bearish: list[str] = []
    facts: list[str] = []

    current_body = current["close"] - current["open"]
    previous_body = previous["close"] - previous["open"]
    if (current_body > 0 and previous_body < 0
            and current["close"] > previous["open"]
            and current["open"] < previous["close"]
            and abs(current_body) > abs(previous_body)):
        tags.append("engulfing")
        bullish.append("bullish_engulfing")
        facts.append("latest M1 body fully engulfed the preceding bearish candle body")
    elif (current_body < 0 and previous_body > 0
          and current["close"] < previous["open"]
          and current["open"] > previous["close"]
          and abs(current_body) > abs(previous_body)):
        tags.append("engulfing")
        bearish.append("bearish_engulfing")
        facts.append("latest M1 body fully engulfed the preceding bullish candle body")

    body_ratio = body / candle_range
    lower_ratio = lower_wick / candle_range
    upper_ratio = upper_wick / candle_range
    if lower_ratio >= 0.60 and body_ratio <= 0.30 and lower_wick > upper_wick:
        tags.extend(["pin_bar", "rejection_wick"])
        bullish.append("bullish_pin_bar")
        facts.append("M1 candle has a lower rejection wick >=60% of range and body <=30%")
    elif upper_ratio >= 0.60 and body_ratio <= 0.30 and upper_wick > lower_wick:
        tags.extend(["pin_bar", "rejection_wick"])
        bearish.append("bearish_pin_bar")
        facts.append("M1 candle has an upper rejection wick >=60% of range and body <=30%")

    is_inside = inside["high"] <= mother["high"] and inside["low"] >= mother["low"]
    if is_inside and current["close"] > mother["high"]:
        tags.append("inside_bar_breakout")
        bullish.append("inside_bar_breakout_up")
        facts.append("M1 close broke above the mother candle after an inside bar")
    elif is_inside and current["close"] < mother["low"]:
        tags.append("inside_bar_breakout")
        bearish.append("inside_bar_breakout_down")
        facts.append("M1 close broke below the mother candle after an inside bar")

    if current["low"] < prior_low - boundary_buffer and current["close"] > prior_low:
        tags.append("range_rejection")
        bullish.append("sell_side_range_reclaim")
        facts.append("M1 traded below the prior 20-bar low and closed back above it")
    if current["high"] > prior_high + boundary_buffer and current["close"] < prior_high:
        tags.append("range_rejection")
        bearish.append("buy_side_range_reclaim")
        facts.append("M1 traded above the prior 20-bar high and closed back below it")

    volume_multiple = 0.0
    if "volume" in current:
        volumes = [b["volume"] for b in bars[-21:-1] if "volume" in b]
        average_volume = sum(volumes) / len(volumes) if volumes else 0.0
        if average_volume > 0:
            volume_multiple = current["volume"] / average_volume
            touches_boundary = (
                current["low"] <= prior_high + boundary_buffer
                and current["high"] >= prior_high - boundary_buffer
            ) or (
                current["low"] <= prior_low + boundary_buffer
                and current["high"] >= prior_low - boundary_buffer
            )
            if volume_multiple > 1.5 and touches_boundary:
                tags.append("volume_spike_at_boundary")
                facts.append(
                    f"M1 volume is {volume_multiple:.2f}x the preceding-bar average near a range boundary"
                )

    tags = list(dict.fromkeys(tags))
    if not tags:
        return {}
    return {
        "source_kind": "m1_price_action",
        "pattern_tags": tags,
        "bullish_patterns": list(dict.fromkeys(bullish)),
        "bearish_patterns": list(dict.fromkeys(bearish)),
        "near_range_boundary": bool(near_high or near_low),
        "near_prior_high": bool(near_high),
        "near_prior_low": bool(near_low),
        "prior_20bar_high": prior_high,
        "prior_20bar_low": prior_low,
        "last_close": current["close"],
        "current_range": candle_range,
        "average_prior_range": average_range,
        "volume_spike_multiple": volume_multiple,
        "facts": facts,
    }


def evidence_from_m1_candles(
    symbol: str, frame: Any, *, provisional: bool = True,
) -> Evidence | None:
    """Convert M1 price-action measurements into one neutral Evidence record."""
    try:
        measurements = detect_m1_price_action(frame)
        if not measurements:
            return None
        facts = list(measurements.pop("facts", []))
        suffix = " This is provisional because the source includes the forming bar." if provisional else ""
        obs = (
            "M1 price-action observations: " + "; ".join(facts)
            + ". Pattern observations are contextual evidence, not trade instructions."
            + suffix
        )
        measurements["provisional"] = bool(provisional)
        return Evidence(
            source_module="price_action.m1",
            domain=EvidenceDomain.STRUCTURE,
            symbol=str(symbol or ""),
            observation=obs[:1600],
            confidence=0.65 if provisional else 0.75,
            uncertainty=0.35 if provisional else 0.25,
            polarity=0.0,
            measurements=measurements,
            relevance_horizon_seconds=60.0,
        )
    except Exception:  # noqa: BLE001
        return None


__all__ = ["detect_m1_price_action", "evidence_from_m1_candles"]
