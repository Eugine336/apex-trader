"""Universal, non-authoritative analytical observations across market timeframes.

This module turns broker OHLC(V) frames into structured facts the native Brain can
use across instruments. It is deliberately not an entry strategy: it emits no
direction verdict, executable entry/stop/target, score, size, or order instruction.
Candle-derived reads are based on completed candles where the source exposes a
forming bar; the forming bar is excluded here and handled separately as provisional
price-action evidence.
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any

from cognition.contracts import Evidence, EvidenceDomain

_TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _rows(frame: Any, limit: int = 32) -> list[dict[str, float]]:
    """Read a pandas-like OHLC(V) frame without importing pandas."""
    if frame is None:
        return []
    try:
        if getattr(frame, "empty", False):
            return []
        tail = frame.tail(limit) if callable(getattr(frame, "tail", None)) else frame
        columns = {str(name).lower(): name for name in getattr(tail, "columns", [])}
        iterator = tail.iterrows() if callable(getattr(tail, "iterrows", None)) else enumerate(tail)
        result: list[dict[str, float]] = []
        for _, row in iterator:
            def value(*aliases: str) -> float | None:
                names = tuple(columns[name] for name in aliases if name in columns) + aliases
                for name in names:
                    try:
                        raw = row.get(name, None) if callable(getattr(row, "get", None)) else row[name]
                    except (KeyError, TypeError, IndexError, ValueError):
                        continue
                    parsed = _safe_float(raw)
                    if parsed is not None:
                        return parsed
                return None

            opening, high, low, close = (
                value("open", "o"), value("high", "h"),
                value("low", "l"), value("close", "c"),
            )
            if None in (opening, high, low, close):
                continue
            if high < low or high < max(opening, close) or low > min(opening, close):
                continue
            row_data = {
                "open": opening, "high": high, "low": low, "close": close,
            }
            volume = value("volume", "tick_volume", "real_volume")
            if volume is not None and volume >= 0:
                row_data["volume"] = volume
            result.append(row_data)
        return result
    except Exception:  # noqa: BLE001 — observer errors never break the cognition cycle
        return []


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _record(
    symbol: str,
    timeframe: str,
    domain: EvidenceDomain,
    kind: str,
    observation: str,
    measurements: dict[str, Any],
) -> Evidence:
    minutes = _TIMEFRAME_SECONDS.get(timeframe, 60)
    confidence = 0.72
    uncertainty = 0.28
    return Evidence(
        source_module=f"market_lenses.{timeframe.lower()}.{kind}",
        domain=domain,
        symbol=str(symbol or ""),
        observation=observation[:1400],
        confidence=confidence,
        uncertainty=uncertainty,
        polarity=0.0,
        measurements={
            **measurements,
            "source_kind": "market_lens",
            "timeframe": timeframe,
            "provisional": False,
            "data_basis": "completed_ohlc",
        },
        relevance_horizon_seconds=max(3 * minutes, 180.0),
    )


def analyze_market_frames(
    symbol: str,
    frames: dict[str, Any],
    *,
    source_includes_forming_bar: bool = True,
) -> list[Evidence]:
    """Emit cross-instrument structure, liquidity, momentum, volatility and volume facts.

    No direction is chosen. If the connector includes a forming bar, the last row
    is excluded from these confirmed-bar measurements; a separate provisional
    observer can still analyze it. Unsupported/short/invalid series simply omit
    the corresponding evidence rather than fabricate a conclusion.
    """
    output: list[Evidence] = []
    timeframe_signs: list[int] = []
    timeframe_scores: list[float] = []

    for raw_tf, frame in (frames or {}).items():
        timeframe = str(raw_tf or "").upper()
        if timeframe not in _TIMEFRAME_SECONDS:
            continue
        rows = _rows(frame)
        bars = rows[:-1] if source_includes_forming_bar and len(rows) > 1 else rows
        if len(bars) < 5:
            continue

        current = bars[-1]
        ranges = [max(0.0, bar["high"] - bar["low"]) for bar in bars]
        current_range = ranges[-1]
        baseline_ranges = ranges[-11:-1]
        baseline_range = _mean(baseline_ranges)
        if baseline_range <= 0:
            baseline_range = _mean([value for value in ranges[:-1] if value > 0])
        candle_body = current["close"] - current["open"]
        body_size = abs(candle_body)
        upper_wick = max(0.0, current["high"] - max(current["open"], current["close"]))
        lower_wick = max(0.0, min(current["open"], current["close"]) - current["low"])
        buffer = max(baseline_range * 0.05, current_range * 0.02, 1e-12)

        # Confirmed local structure: compare the latest completed close against
        # the ten preceding completed bars when sufficient data is available.
        lookback = min(10, len(bars) - 1)
        prior = bars[-lookback-1:-1]
        prior_high = max(bar["high"] for bar in prior)
        prior_low = min(bar["low"] for bar in prior)
        high_span = max(prior_high - prior_low, 1e-12)
        range_position = max(0.0, min(1.0, (current["close"] - prior_low) / high_span))
        breaks_high = current["close"] > prior_high + buffer
        breaks_low = current["close"] < prior_low - buffer
        breakout_direction = "up" if breaks_high else ("down" if breaks_low else "none")
        closes_for_trend = [bar["close"] for bar in bars]
        trend_change = (
            closes_for_trend[-1] - closes_for_trend[-6]
            if len(closes_for_trend) >= 6 else candle_body
        )
        trend_strength = (
            min(1.0, abs(trend_change) / (baseline_range * 5.0))
            if baseline_range > 0 else 0.0
        )
        trend_state = (
            "uptrend" if trend_change > baseline_range * 1.2 else
            ("downtrend" if trend_change < -baseline_range * 1.2 else "range")
        )
        range_rotation = bool(
            len(bars) >= 8
            and not (breaks_high or breaks_low)
            and abs(trend_change) <= baseline_range * 1.2
        )
        measurements: dict[str, Any] = {
            "lookback_bars": lookback,
            "prior_range_high": prior_high,
            "prior_range_low": prior_low,
            "range_position": round(range_position, 4),
            "near_range_boundary": bool(range_position >= 0.85 or range_position <= 0.15),
            "current_close": current["close"],
            "current_range": current_range,
            "current_body": candle_body,
            "body_to_range": round(body_size / current_range, 4) if current_range > 0 else 0.0,
            "upper_wick": upper_wick,
            "lower_wick": lower_wick,
            "bos": bool(breaks_high or breaks_low),
            "breakout_direction": breakout_direction,
            "trend_state": trend_state,
            "trend_strength": trend_strength,
            "range_rotation": range_rotation,
        }
        if breaks_high:
            measurements["displacement"] = current["close"] - prior_high
        elif breaks_low:
            measurements["displacement"] = current["close"] - prior_low
        location = (
            f"close broke above the preceding {lookback}-bar high"
            if breaks_high else (
                f"close broke below the preceding {lookback}-bar low"
                if breaks_low else
                f"close remains inside the preceding {lookback}-bar range"
            )
        )
        output.append(_record(
            symbol, timeframe, EvidenceDomain.STRUCTURE, "structure",
            f"{timeframe} completed-candle structure: {location}; "
            f"close location within prior range={range_position:.2f}.",
            measurements,
        ))

        # Sweep/reclaim is a boundary event, not proof that actual order flow was
        # absorbed. Preserve that distinction explicitly for later hypotheses.
        swept_high = current["high"] > prior_high + buffer and current["close"] < prior_high
        swept_low = current["low"] < prior_low - buffer and current["close"] > prior_low
        if swept_high or swept_low:
            side = "buy" if swept_high else "sell"
            boundary = "prior_range_high" if swept_high else "prior_range_low"
            output.append(_record(
                symbol, timeframe, EvidenceDomain.LIQUIDITY, "liquidity",
                f"{timeframe} completed candle swept {boundary} and closed back inside the prior range; this is a reclaim observation, not proof of absorption.",
                {
                    "sweep": True,
                    "sweep_side": side,
                    "swept_boundary": boundary,
                    "reclaimed_boundary": boundary,
                    "absorption_confirmed": False,
                    "current_close": current["close"],
                    "prior_range_high": prior_high,
                    "prior_range_low": prior_low,
                    "lookback_bars": lookback,
                },
            ))

        # Momentum is a measurement of recent price changes. Only assign
        # acceleration when the move's sign persists and its magnitude grows.
        closes = [bar["close"] for bar in bars]
        recent_change = closes[-1] - closes[-4] if len(closes) >= 4 else candle_body
        prior_change = closes[-4] - closes[-7] if len(closes) >= 7 else 0.0
        active_sign = 1 if recent_change > buffer else (-1 if recent_change < -buffer else 0)
        timeframe_signs.append(active_sign)
        timeframe_scores.append(abs(recent_change) / baseline_range if baseline_range > 0 else 0.0)
        same_sign = recent_change * prior_change > 0
        acceleration = bool(same_sign and abs(recent_change) > abs(prior_change) * 1.15)
        deceleration = bool(
            same_sign and abs(prior_change) > buffer
            and abs(recent_change) < abs(prior_change) * 0.60
        )
        momentum_state = "accelerating" if acceleration else (
            "decelerating" if deceleration else "mixed_or_stable"
        )
        output.append(_record(
            symbol, timeframe, EvidenceDomain.MOMENTUM, "momentum",
            f"{timeframe} completed-candle momentum: recent change={recent_change:.8g}; prior comparison={prior_change:.8g}; state={momentum_state}.",
            {
                "close_change": recent_change,
                "prior_close_change": prior_change,
                "momentum_state": momentum_state,
                "acceleration": acceleration,
                "deceleration": deceleration,
                "candle_body_sign": 1 if candle_body > 0 else (-1 if candle_body < 0 else 0),
                "body_to_range": measurements["body_to_range"],
            },
        ))

        # Volatility: current range vs earlier range baseline, plus short-window
        # contraction. These measurements are directionless.
        range_multiple = current_range / baseline_range if baseline_range > 0 else 0.0
        recent_ranges = ranges[-4:-1]
        earlier_ranges = ranges[-10:-4]
        recent_mean = _mean(recent_ranges)
        earlier_mean = _mean(earlier_ranges)
        expansion = bool(range_multiple >= 1.5)
        compression = bool(
            len(recent_ranges) >= 2 and earlier_mean > 0
            and recent_mean <= earlier_mean * 0.75
        )
        volatility_state = "expanding" if expansion else (
            "compressing" if compression else "stable_or_unknown"
        )
        output.append(_record(
            symbol, timeframe, EvidenceDomain.VOLATILITY, "volatility",
            f"{timeframe} completed-candle range multiple={range_multiple:.2f}; state={volatility_state} relative to its recent range baseline.",
            {
                "current_range": current_range,
                "baseline_range": baseline_range,
                "range_expansion_multiple": range_multiple,
                "expansion": expansion,
                "compression": compression,
                "volatility_state": volatility_state,
                "recent_mean_range": recent_mean,
                "earlier_mean_range": earlier_mean,
            },
        ))

        # Volume is included only when the selected broker actually supplies it.
        volumes = [bar["volume"] for bar in bars if "volume" in bar]
        if len(volumes) >= 5 and "volume" in current:
            baseline_volumes = volumes[-11:-1]
            volume_mean = _mean(baseline_volumes)
            volume_std = _stddev(baseline_volumes)
            volume_z = (
                (current["volume"] - volume_mean) / volume_std
                if volume_std > 0 else 0.0
            )
            volume_ratio = current["volume"] / volume_mean if volume_mean > 0 else 0.0
            output.append(_record(
                symbol, timeframe, EvidenceDomain.VOLUME, "volume",
                f"{timeframe} supplied broker-volume reading: ratio={volume_ratio:.2f} of its baseline; z-score={volume_z:.2f}.",
                {
                    "volume": current["volume"],
                    "volume_baseline": volume_mean,
                    "volume_z": volume_z,
                    "volume_ratio": volume_ratio,
                    "expansion": volume_ratio >= 1.25,
                    "volume_state": "expanding" if volume_ratio >= 1.25 else "baseline_or_lower",
                    "volume_source": "broker_frame",
                },
            ))

    # Non-directional cross-timeframe alignment. Individual timeframe facts remain
    # separate above; this summary does not emit a chosen LONG/SHORT/FLAT bias.
    active = [sign for sign in timeframe_signs if sign]
    if len(active) >= 2:
        bullish_n = sum(sign > 0 for sign in active)
        bearish_n = sum(sign < 0 for sign in active)
        agreement_n = max(bullish_n, bearish_n)
        conflict_score = 1.0 - (agreement_n / len(active))
        alignment_degree = agreement_n / len(active)
        output.append(_record(
            symbol, "MULTI", EvidenceDomain.MULTI_TIMEFRAME, "alignment",
            f"Independent timeframe measurements: {len(active)} active timeframe readings; alignment degree={alignment_degree:.2f}; conflict={conflict_score:.2f}. No directional verdict is emitted.",
            {
                "timeframes_observed": len(timeframe_signs),
                "active_timeframe_readings": len(active),
                "alignment_degree": alignment_degree,
                "conflict_score": conflict_score,
                "alignment": "aligned" if conflict_score <= 0.2 else ("conflict" if conflict_score >= 0.5 else "mixed"),
                "trend_strength": min(1.0, _mean(timeframe_scores)),
            },
        ))

    return output


__all__ = ["analyze_market_frames"]
