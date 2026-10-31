"""Universal, non-authoritative analytical observations across market timeframes.

This module turns broker OHLC(V) frames into structured facts the native Brain can
use across instruments. It is deliberately not an entry strategy: it emits no
direction verdict, executable entry/stop/target, score, size, or order instruction.
Candle-derived reads are based on completed candles where the source exposes a
forming bar; the forming bar is excluded here and handled separately as provisional
price-action evidence.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
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


def _timestamp_epoch(value: Any) -> float | None:
    """Convert broker time values or a datetime-like index to Unix seconds."""
    if value is None:
        return None
    stamp = getattr(value, "timestamp", None)
    if callable(stamp):
        try:
            tzinfo = getattr(value, "tzinfo", None)
            # datetime-like values with no timezone cannot be treated as UTC.
            if hasattr(value, "tzinfo") and tzinfo is None:
                return None
            parsed = float(stamp())
            return parsed if isfinite(parsed) and parsed > 0 else None
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    numeric = _safe_float(value)
    if numeric is not None and numeric > 0:
        # A few providers expose milliseconds since epoch instead of seconds.
        if numeric > 100_000_000_000:
            numeric /= 1000.0
        return numeric if numeric > 0 else None
    try:
        text = str(value).strip()
        if not text:
            return None
        parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        # Naive timestamps have no timezone provenance and cannot support
        # synchronized multi-timeframe claims.
        if parsed_dt.tzinfo is None:
            return None
        parsed = parsed_dt.astimezone(timezone.utc).timestamp()
        return parsed if isfinite(parsed) and parsed > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _rows(frame: Any, limit: int = 32) -> list[dict[str, Any]]:
    """Read a pandas-like OHLC(V) frame without importing pandas."""
    if frame is None:
        return []
    try:
        if getattr(frame, "empty", False):
            return []
        tail = frame.tail(limit) if callable(getattr(frame, "tail", None)) else frame
        columns = {str(name).lower(): name for name in getattr(tail, "columns", [])}
        iterator = tail.iterrows() if callable(getattr(tail, "iterrows", None)) else enumerate(tail)
        result: list[dict[str, Any]] = []
        for row_index, row in iterator:
            def raw_value(*aliases: str) -> Any:
                names = tuple(columns[name] for name in aliases if name in columns) + aliases
                for name in names:
                    try:
                        return row.get(name, None) if callable(getattr(row, "get", None)) else row[name]
                    except (KeyError, TypeError, IndexError, ValueError):
                        continue
                return None

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
            row_data: dict[str, Any] = {
                "open": opening, "high": high, "low": low, "close": close,
            }
            source_time = raw_value("time", "timestamp", "datetime", "date")
            epoch = _timestamp_epoch(source_time)
            if epoch is None:
                # DataFrame RangeIndex/ordinary integer indexes are row numbers,
                # not timestamps. Only trust a datetime-like or ISO-string index.
                index_is_datetime_like = callable(getattr(row_index, "timestamp", None))
                index_is_string_time = (
                    isinstance(row_index, str)
                    and any(marker in row_index for marker in ("-", "T", ":"))
                )
                if index_is_datetime_like or index_is_string_time:
                    epoch = _timestamp_epoch(row_index)
            if epoch is not None:
                row_data["timestamp_epoch"] = epoch
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


def _sma(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return _mean(values[-period:])


def _ema_series(values: list[float], period: int) -> list[float | None]:
    if period <= 0 or len(values) < period:
        return []
    alpha = 2.0 / (period + 1.0)
    seed = _mean(values[:period])
    result: list[float | None] = [None] * (period - 1) + [seed]
    current = seed
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
        result.append(current)
    return result


def _true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    result: list[float] = []
    for index, (high, low) in enumerate(zip(highs, lows)):
        previous_close = closes[index - 1] if index else closes[index]
        result.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return result


def _technical_indicator_evidence(
    symbol: str,
    timeframe: str,
    bars: list[dict[str, Any]],
    source_epoch: float | None,
) -> list[Evidence]:
    """Calculate causal, completed-bar technical measurements without signals."""
    if len(bars) < 15:
        return []
    closes = [float(row["close"]) for row in bars]
    highs = [float(row["high"]) for row in bars]
    lows = [float(row["low"]) for row in bars]
    opens = [float(row["open"]) for row in bars]
    true_range = _true_ranges(highs, lows, closes)
    atr14 = _sma(true_range, 14)
    atr20 = _sma(true_range, 20)
    momentum: dict[str, Any] = {}
    volatility: dict[str, Any] = {}
    volume: dict[str, Any] = {}

    # Trend and momentum indicators. Values are descriptive; no threshold
    # converts them into a trade direction or permission.
    for period in (10, 20, 50, 100, 200):
        sma = _sma(closes, period)
        ema_values = _ema_series(closes, period)
        ema = ema_values[-1] if ema_values else None
        if sma is not None:
            momentum[f"sma_{period}"] = sma
        if ema is not None:
            momentum[f"ema_{period}"] = ema
    ema12 = _ema_series(closes, 12)
    ema20 = _ema_series(closes, 20)
    ema26 = _ema_series(closes, 26)
    ema50 = _ema_series(closes, 50)
    if ema20 and ema50 and atr14 and atr14 > 0:
        momentum["ema_spread_atr"] = (ema20[-1] - ema50[-1]) / atr14
    if ema20 and len(ema20) >= 6 and ema20[-6] is not None:
        momentum["ema_slope"] = (ema20[-1] - ema20[-6]) / 5.0

    if len(closes) >= 15:
        changes = [closes[i] - closes[i - 1] for i in range(len(closes) - 14, len(closes))]
        gains = [max(change, 0.0) for change in changes]
        losses = [max(-change, 0.0) for change in changes]
        average_gain = _mean(gains)
        average_loss = _mean(losses)
        momentum["rsi"] = (
            50.0 if average_gain == 0.0 and average_loss == 0.0
            else 100.0 if average_loss == 0.0
            else 100.0 - (100.0 / (1.0 + average_gain / average_loss))
        )

    if ema12 and ema26 and len(closes) >= 35:
        macd_values = [
            fast - slow for fast, slow in zip(ema12, ema26)
            if fast is not None and slow is not None
        ]
        signal_values = _ema_series(macd_values, 9)
        if macd_values:
            momentum["macd"] = macd_values[-1]
        if signal_values:
            momentum["macd_signal"] = signal_values[-1]
            momentum["macd_histogram"] = macd_values[-1] - signal_values[-1]

    if len(bars) >= 14:
        low14, high14 = min(lows[-14:]), max(highs[-14:])
        span14 = high14 - low14
        if span14 > 0:
            k_values = [
                100.0 * (closes[index] - min(lows[index - 13:index + 1]))
                / max(max(highs[index - 13:index + 1]) - min(lows[index - 13:index + 1]), 1e-12)
                for index in range(len(bars) - 3, len(bars))
            ]
            momentum["stochastic_k"] = k_values[-1]
            momentum["stochastic_d"] = _mean(k_values)

    if len(bars) >= 20:
        typical = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(bars))]
        typical_mean = _mean(typical[-20:])
        mean_deviation = _mean([abs(value - typical_mean) for value in typical[-20:]])
        if mean_deviation > 0:
            momentum["cci"] = (typical[-1] - typical_mean) / (0.015 * mean_deviation)
        high14, low14 = max(highs[-14:]), min(lows[-14:])
        if high14 > low14:
            momentum["williams_r"] = -100.0 * (high14 - closes[-1]) / (high14 - low14)
        if len(closes) >= 6 and closes[-6] != 0:
            momentum["roc_5"] = (closes[-1] / closes[-6] - 1.0) * 100.0
        if len(closes) >= 11 and closes[-11] != 0:
            momentum["roc_10"] = (closes[-1] / closes[-11] - 1.0) * 100.0

    # ADX / directional movement using rolling 14-bar directional movement and
    # true-range sums. The smoothing convention is explicitly versioned below.
    if len(bars) >= 28:
        dx_values = []
        for end in range(14, len(bars)):
            plus_values = []
            minus_values = []
            tr_values = []
            for index in range(end - 13, end + 1):
                up = highs[index] - highs[index - 1]
                down = lows[index - 1] - lows[index]
                plus_values.append(up if up > down and up > 0 else 0.0)
                minus_values.append(down if down > up and down > 0 else 0.0)
                tr_values.append(true_range[index])
            tr_sum = sum(tr_values)
            if tr_sum <= 0:
                continue
            plus_di = 100.0 * sum(plus_values) / tr_sum
            minus_di = 100.0 * sum(minus_values) / tr_sum
            denom = plus_di + minus_di
            if denom > 0:
                dx_values.append(100.0 * abs(plus_di - minus_di) / denom)
        if dx_values:
            momentum["adx"] = _mean(dx_values[-14:])
            momentum["plus_di"] = plus_di
            momentum["minus_di"] = minus_di

    if momentum:
        momentum.update({
            "atr": atr14,
            "indicator_periods": {"rsi": 14, "adx": 14, "macd": [12, 26, 9]},
            "calculation_method": "causal_completed_ohlc_v1",
        })

    # Volatility and statistical location indicators.
    if atr14 is not None:
        volatility["atr"] = atr14
        if closes[-1] > 0:
            volatility["atr_percent"] = 100.0 * atr14 / closes[-1]
    atr_history = [
        _sma(true_range[max(0, index - 13):index + 1], min(14, index + 1))
        for index in range(max(0, len(true_range) - 100), len(true_range))
    ]
    atr_history = [value for value in atr_history if value is not None]
    if atr_history:
        volatility["atr_percentile"] = sum(value <= atr_history[-1] for value in atr_history) / len(atr_history)
        volatility["realized_volatility"] = _stddev([
            (closes[i] / closes[i - 1] - 1.0)
            for i in range(max(1, len(closes) - 20), len(closes))
            if closes[i - 1] > 0
        ])
    if len(closes) >= 20:
        mean20 = _mean(closes[-20:])
        std20 = _stddev(closes[-20:])
        if std20 > 0:
            upper, lower = mean20 + 2.0 * std20, mean20 - 2.0 * std20
            volatility.update({
                "bollinger_upper": upper,
                "bollinger_lower": lower,
                "bollinger_width": (upper - lower) / max(abs(mean20), 1e-12),
                "bollinger_percent_b": (closes[-1] - lower) / max(upper - lower, 1e-12),
                "rolling_zscore": (closes[-1] - mean20) / std20,
            })
        if atr20 is not None and ema20:
            volatility["keltner_upper"] = ema20[-1] + 2.0 * atr20
            volatility["keltner_lower"] = ema20[-1] - 2.0 * atr20
        volatility["donchian_high"] = max(highs[-21:-1]) if len(highs) >= 21 else max(highs[-20:])
        volatility["donchian_low"] = min(lows[-21:-1]) if len(lows) >= 21 else min(lows[-20:])
    if volatility:
        volatility["indicator_periods"] = {"atr": 14, "bollinger": [20, 2.0], "keltner": [20, 2.0]}
        volatility["calculation_method"] = "causal_completed_ohlc_v1"

    # Volume indicators are calculated only when the provider supplied a
    # volume field. The exact source is retained; tick volume is never described
    # as consolidated exchange volume or actual bid/ask order flow.
    if len(bars) >= 20 and all("volume" in row for row in bars[-20:]):
        volumes = [float(row.get("volume", 0.0)) for row in bars]
        volume_sources = {str(row.get("volume_source", "provider_volume")) for row in bars[-20:]}
        source_label = (
            next(iter(volume_sources)) if len(volume_sources) == 1 else "mixed_provider_volume"
        )
        obv = 0.0
        for index in range(1, len(closes)):
            if closes[index] > closes[index - 1]:
                obv += volumes[index]
            elif closes[index] < closes[index - 1]:
                obv -= volumes[index]
        typical = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(bars))]
        raw_flow = [typical[i] * volumes[i] for i in range(len(bars))]
        positive_flow = sum(raw_flow[i] for i in range(max(1, len(bars) - 14), len(bars))
                            if typical[i] > typical[i - 1])
        negative_flow = sum(raw_flow[i] for i in range(max(1, len(bars) - 14), len(bars))
                            if typical[i] < typical[i - 1])
        money_ratio = positive_flow / negative_flow if negative_flow > 0 else None
        mfi = (
            100.0 - 100.0 / (1.0 + money_ratio)
            if money_ratio is not None else (100.0 if positive_flow > 0 else 50.0)
        )
        mf_multiplier = [
            ((2.0 * closes[i] - highs[i] - lows[i]) / (highs[i] - lows[i]))
            if highs[i] > lows[i] else 0.0
            for i in range(len(bars))
        ]
        volume_window = volumes[-20:]
        flow_window = [mf_multiplier[i] * volumes[i] for i in range(len(bars) - 20, len(bars))]
        volume_sum = sum(volume_window)
        if volume_sum > 0:
            volume["cmf"] = sum(flow_window) / volume_sum
        volume["obv"] = obv
        volume["mfi"] = mfi
        volume["accumulation_distribution"] = sum(flow_window)
        if len(volumes) >= 11 and volumes[-11] > 0:
            volume["volume_roc"] = (volumes[-1] / volumes[-11] - 1.0) * 100.0
        if volume_sum > 0:
            volume["vwap"] = sum(typical[i] * volumes[i] for i in range(len(bars) - 20, len(bars))) / volume_sum
        volume["volume_source"] = source_label
        volume["volume_is_tick_proxy"] = "tick" in source_label.lower()
        volume["indicator_periods"] = {"mfi": 14, "cmf": 20, "vwap": 20}
        volume["calculation_method"] = "causal_completed_ohlc_v1"

    output: list[Evidence] = []
    if momentum:
        output.append(_record(
            symbol, timeframe, EvidenceDomain.MOMENTUM, "technical_momentum",
            f"{timeframe} completed-bar technical momentum and trend measurements.",
            momentum, source_epoch=source_epoch,
        ))
    if volatility:
        output.append(_record(
            symbol, timeframe, EvidenceDomain.VOLATILITY, "technical_volatility",
            f"{timeframe} completed-bar volatility and statistical-location measurements.",
            volatility, source_epoch=source_epoch,
        ))
    if volume:
        output.append(_record(
            symbol, timeframe, EvidenceDomain.VOLUME, "technical_volume",
            f"{timeframe} provider-volume indicators; source={volume.get('volume_source')}.",
            volume, source_epoch=source_epoch,
        ))
    return output


def _record(
    symbol: str,
    timeframe: str,
    domain: EvidenceDomain,
    kind: str,
    observation: str,
    measurements: dict[str, Any],
    *,
    source_epoch: float | None = None,
) -> Evidence:
    seconds = _TIMEFRAME_SECONDS.get(timeframe, 60)
    known_source_time = bool(source_epoch and source_epoch > 0)
    sampled_at = source_epoch if known_source_time else datetime.now(timezone.utc).timestamp()
    try:
        timestamp_iso = datetime.fromtimestamp(sampled_at, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        sampled_at = datetime.now(timezone.utc).timestamp()
        timestamp_iso = datetime.fromtimestamp(sampled_at, tz=timezone.utc).isoformat()
        known_source_time = False
    confidence = 0.72 if known_source_time else 0.55
    uncertainty = 0.28 if known_source_time else 0.45
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
            "source_timestamp_known": known_source_time,
            "source_bar_epoch": source_epoch,
        },
        timestamp_epoch=sampled_at,
        timestamp_iso=timestamp_iso,
        relevance_horizon_seconds=max(3 * seconds, 180.0),
        source_timestamp_epoch=source_epoch if known_source_time else None,
        data_basis="completed_ohlc",
        provisional=False,
        independence_group=f"ohlc:{str(symbol or '').upper()}:{timeframe}",
        measurement_version="market-lens-v1",
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
    timeframe_observations: list[dict[str, Any]] = []
    snapshot_cutoff_epoch = time.time()

    for raw_tf, frame in (frames or {}).items():
        timeframe = str(raw_tf or "").upper()
        if timeframe not in _TIMEFRAME_SECONDS:
            continue
        rows = _rows(frame)
        bars = rows[:-1] if source_includes_forming_bar and len(rows) > 1 else rows
        if len(bars) < 5:
            continue

        current = bars[-1]
        source_epoch = _safe_float(current.get("timestamp_epoch"))
        timeframe_observations.append({
            "timeframe": timeframe,
            "bar_open_epoch": source_epoch,
            "bar_close_epoch": (
                source_epoch + _TIMEFRAME_SECONDS[timeframe]
                if source_epoch is not None else None
            ),
            "timeframe_seconds": _TIMEFRAME_SECONDS[timeframe],
        })
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
            source_epoch=source_epoch,
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
                source_epoch=source_epoch,
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
            source_epoch=source_epoch,
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
            source_epoch=source_epoch,
        ))

        # Broad technical measurements are emitted as evidence only. They
        # never select a side or authorize an opportunity.
        output.extend(_technical_indicator_evidence(
            symbol, timeframe, bars, source_epoch,
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
                source_epoch=source_epoch,
            ))

    # Non-directional alignment is synchronized only when the latest completed
    # bars are point-in-time eligible at one shared snapshot cutoff, have source
    # timestamps, and intraday candle opens align to their declared epoch grids.
    # D1 is excluded from this assertion because broker daily boundaries can
    # depend on server timezone/DST. Unsynchronized alignment remains descriptive.
    active = [sign for sign in timeframe_signs if sign]
    if len(active) >= 2:
        bullish_n = sum(sign > 0 for sign in active)
        bearish_n = sum(sign < 0 for sign in active)
        agreement_n = max(bullish_n, bearish_n)
        conflict_score = 1.0 - (agreement_n / len(active))
        alignment_degree = agreement_n / len(active)
        intraday = [
            item for item in timeframe_observations
            if item["timeframe"] in {"M1", "M5", "M15", "H1", "H4"}
        ]
        timestamped = [
            item for item in intraday
            if item["bar_open_epoch"] is not None
            and item["bar_close_epoch"] is not None
        ]
        grid_aligned = bool(timestamped) and all(
            abs(
                (item["bar_open_epoch"] / item["timeframe_seconds"])
                - round(item["bar_open_epoch"] / item["timeframe_seconds"])
            ) < 1e-6
            for item in timestamped
        )
        eligible = bool(timestamped) and all(
            item["bar_close_epoch"] <= snapshot_cutoff_epoch
            for item in timestamped
        )
        synchronized = (
            len(timestamped) >= 2
            and len(timestamped) == len(intraday)
            and grid_aligned
            and eligible
        )
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
                "synchronized_timestamps": synchronized,
                "source_verified": synchronized,
                "synchronization_method": "common_snapshot_cutoff_and_intraday_epoch_boundaries",
                "snapshot_cutoff_epoch": snapshot_cutoff_epoch,
                "timeframe_observations": timeframe_observations,
                "daily_boundary_included": False,
                "synchronization_limitation": (
                    None if synchronized else
                    "source timestamps, intraday boundaries, or common-cutoff eligibility not fully verified"
                ),
                "causation_established": False,
            },
            source_epoch=snapshot_cutoff_epoch if synchronized else None,
        ))

    return output


__all__ = ["analyze_market_frames"]
