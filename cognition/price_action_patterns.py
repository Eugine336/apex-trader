"""Context-aware M1 price-action observations for Apex's CognitiveBrain.

This observer preserves the useful legacy M1 pattern vocabulary without reviving
the legacy zone-gated entry pipeline. It emits measurements only: no trade
direction, order, entry price, stop, target, score or trigger authority. Patterns
from a forming candle are explicitly provisional and can repaint.
"""

from __future__ import annotations
import logging

from datetime import datetime, timezone
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
            logging.getLogger(__name__).debug("Exception handled by fallback in cognition/price_action_patterns.py:35", exc_info=True)
            continue
    return None


def _timestamp_epoch(value: Any) -> float | None:
    """Parse source timestamps without mistaking an ordinary row number for time."""
    if value is None:
        return None
    stamp = getattr(value, "timestamp", None)
    if callable(stamp):
        try:
            parsed = float(stamp())
            return parsed if isfinite(parsed) and parsed > 0 else None
        except (TypeError, ValueError, OverflowError, OSError):
            logging.getLogger(__name__).debug("Exception handled by fallback in cognition/price_action_patterns.py:49", exc_info=True)
            pass
    try:
        numeric = float(value)
        if isfinite(numeric) and numeric > 0:
            if numeric > 100_000_000_000:
                numeric /= 1000.0
            return numeric
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).debug("Exception handled by fallback in cognition/price_action_patterns.py:57", exc_info=True)
        pass
    try:
        text = str(value).strip()
        if not text:
            return None
        parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        parsed = parsed_dt.timestamp()
        return parsed if isfinite(parsed) and parsed > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _raw_row_value(row: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        try:
            value = row.get(key, None) if callable(getattr(row, "get", None)) else row[key]
            if value is not None:
                return value
        except (KeyError, TypeError, ValueError, IndexError):
            logging.getLogger(__name__).debug("Exception handled by fallback in cognition/price_action_patterns.py:78", exc_info=True)
            continue
    return None


def _rows(
    frame: Any, limit: int = 30, *, expected_symbol: str = "",
) -> list[dict[str, Any]]:
    """Read OHLC(V) with source-time attestation from the broker adapter."""
    if frame is None:
        return []
    attrs = getattr(frame, "attrs", {})
    attrs = attrs if isinstance(attrs, dict) else {}
    source_attested = bool(
        attrs.get("source_timestamp_verified") is True
        and str(attrs.get("source_symbol", "") or "").strip().upper()
            == str(expected_symbol or "").strip().upper()
        and attrs.get("source_timestamp_unit") == "unix_seconds"
        and attrs.get("source_timestamp_origin") == "broker_ohlcv"
        and bool(str(attrs.get("source_broker", "") or "").strip())
    )
    try:
        if getattr(frame, "empty", False):
            return []
        tail = frame.tail(limit) if callable(getattr(frame, "tail", None)) else frame
        iterator = tail.iterrows() if callable(getattr(tail, "iterrows", None)) else enumerate(tail)
        columns = {str(col).lower(): str(col) for col in getattr(tail, "columns", [])}
        out: list[dict[str, Any]] = []
        for row_index, row in iterator:
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
            item: dict[str, Any] = {
                "open": opening, "high": high, "low": low, "close": close,
            }
            time_keys = tuple(
                columns[name] for name in ("time", "timestamp", "datetime", "date")
                if name in columns
            ) + ("time", "timestamp", "datetime", "date")
            source_time = _raw_row_value(row, time_keys)
            source_epoch = _timestamp_epoch(source_time)
            if source_epoch is None:
                index_is_datetime = callable(getattr(row_index, "timestamp", None))
                index_is_iso_string = (
                    isinstance(row_index, str)
                    and any(marker in row_index for marker in ("-", "T", ":"))
                )
                if index_is_datetime or index_is_iso_string:
                    source_epoch = _timestamp_epoch(row_index)
            if source_epoch is not None:
                item["timestamp_epoch"] = source_epoch
            item["source_timestamp_verified"] = bool(
                source_attested and source_epoch is not None
            )
            item["source_timestamp_origin"] = (
                str(attrs.get("source_timestamp_origin", "") or "")
                if source_attested else None
            )
            item["source_broker"] = (
                str(attrs.get("source_broker", "") or "").strip().lower()
                if source_attested else None
            )
            volume = value("volume", "tick_volume", "real_volume")
            if volume is not None:
                item["volume"] = max(0.0, volume)
            out.append(item)
        if source_attested:
            source_epochs = [row.get("timestamp_epoch") for row in out]
            ordered = (
                len(source_epochs) == len(out)
                and all(
                    isinstance(epoch, (int, float)) and isfinite(float(epoch))
                    for epoch in source_epochs
                )
                and all(
                    float(right) > float(left)
                    for left, right in zip(source_epochs, source_epochs[1:])
                )
            )
            if not ordered:
                for row in out:
                    row["source_timestamp_verified"] = False
        return out
    except Exception:  # noqa: BLE001 — market observer must not break cognition
        return []


def detect_m1_price_action(
    frame: Any, *, expected_symbol: str = "",
) -> dict[str, Any]:
    """Return evidence measurements from recent M1 bars; never returns a trade."""
    bars = _rows(frame, expected_symbol=expected_symbol)
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
        "source_bar_epoch": (
            current.get("timestamp_epoch")
            if current.get("source_timestamp_verified") is True else None
        ),
        "source_timestamp_known": current.get("source_timestamp_verified") is True,
        "source_timestamp_origin": current.get("source_timestamp_origin"),
        "source_broker": current.get("source_broker"),
        "facts": facts,
    }


def evidence_from_m1_candles(
    symbol: str, frame: Any, *, provisional: bool = True,
) -> Evidence | None:
    """Convert M1 price-action measurements into one neutral Evidence record."""
    try:
        measurements = detect_m1_price_action(frame, expected_symbol=symbol)
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
        source_epoch = _number(measurements.get("source_bar_epoch"), 0.0)
        known_source_time = (
            bool(measurements.get("source_timestamp_known"))
            and source_epoch > 0
        )
        # Keep local evidence creation time separate from the broker candle time.
        sampled_at = datetime.now(timezone.utc).timestamp()
        timestamp_iso = datetime.fromtimestamp(
            sampled_at, tz=timezone.utc,
        ).isoformat()
        # Unknown source time is itself a material data-quality gap. Keep the
        # observation available for context, but substantially raise uncertainty
        # so it cannot masquerade as a timestamp-verified live M1 candle.
        confidence = (0.50 if provisional else 0.62) if known_source_time else 0.25
        uncertainty = (0.50 if provisional else 0.38) if known_source_time else 0.75
        return Evidence(
            source_module="price_action.m1",
            domain=EvidenceDomain.STRUCTURE,
            symbol=str(symbol or ""),
            observation=obs[:1600],
            confidence=confidence,
            uncertainty=uncertainty,
            polarity=0.0,
            measurements=measurements,
            timestamp_epoch=sampled_at,
            timestamp_iso=timestamp_iso,
            source_timestamp_epoch=source_epoch if known_source_time else None,
            source_timestamp_verified=known_source_time,
            relevance_horizon_seconds=60.0,
            data_basis="broker_ohlcv_m1" if known_source_time else "unverified_m1_context",
            provisional=bool(provisional),
        )
    except Exception:  # noqa: BLE001
        return None


__all__ = ["detect_m1_price_action", "evidence_from_m1_candles"]
