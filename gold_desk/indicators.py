"""Small deterministic indicators with no third-party dependencies."""
from __future__ import annotations

from statistics import median, pstdev
from typing import Sequence

from gold_desk.models import Candle


def ema(values: Sequence[float], period: int) -> float | None:
    if period < 1 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(float(v) for v in values[:period]) / period
    for item in values[period:]:
        value = alpha * float(item) + (1.0 - alpha) * value
    return value


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    ranges: list[float] = []
    for i, bar in enumerate(candles):
        if i == 0:
            ranges.append(bar.high - bar.low)
        else:
            previous_close = candles[i - 1].close
            ranges.append(max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            ))
    return ranges


def atr(candles: Sequence[Candle], period: int = 14) -> float | None:
    if period < 1 or len(candles) < period + 1:
        return None
    values = true_ranges(candles)[-period:]
    value = sum(values) / len(values)
    return value if value > 0 else None


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    if period < 1 or len(values) < period + 1:
        return None
    changes = [float(values[i]) - float(values[i - 1]) for i in range(1, len(values))]
    sample = changes[-period:]
    gains = sum(max(change, 0.0) for change in sample) / period
    losses = sum(max(-change, 0.0) for change in sample) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def rolling_zscore(values: Sequence[float], window: int = 30) -> tuple[float, float, float] | None:
    if window < 2 or len(values) < window:
        return None
    sample = [float(v) for v in values[-window:]]
    mean = sum(sample) / len(sample)
    deviation = pstdev(sample)
    if deviation <= 0:
        return None
    return (sample[-1] - mean) / deviation, mean, deviation


def median_true_range(candles: Sequence[Candle], window: int = 20) -> float | None:
    if len(candles) < window + 1:
        return None
    value = median(true_ranges(candles)[-(window + 1):-1])
    return value if value > 0 else None
