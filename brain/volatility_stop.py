"""
APEX TRADER — Volatility Stop Model

Pure, deterministic ATR-based stop-loss distance calculator.
Computes a clamped candidate SL distance using ATR, bounded by both
absolute pip limits and a ratio envelope around the live structure stop.

This module has NO side effects, NO network access, and NO imports from
main_loop or trading_loop.  It reads no external files — rate tables or
instrument data are passed in by the caller.

Zero-vs-unknown discipline (matching F2 swap model):
  - ("unavailable", None) = ATR could not be computed (insufficient data,
    NaN, non-positive).  The caller must NOT fabricate a stop.
  - ("modeled", <float>) = a valid clamped distance was produced, even if
    it hit a clamp boundary.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute ATR using the exact same formula as RegimeDetector._calculate_atr.

    true_range = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR        = rolling(period).mean().bfill()
    """
    prev_close = df["close"].shift(1)
    tr_components = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    true_range = tr_components.max(axis=1)
    return true_range.rolling(period).mean().bfill()


def latest_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Return the last finite ATR value, or None if unavailable."""
    series = atr_series(df, period)
    if series.empty:
        return None
    val = float(series.iloc[-1])
    if math.isnan(val) or math.isinf(val) or val <= 0:
        return None
    return val


def clamped_atr_stop_distance(
    atr_value: Optional[float],
    structure_distance: float,
    *,
    mult: float,
    min_pips: float,
    max_pips: float,
    pip_size: float,
    ratio_min: float,
    ratio_max: float,
) -> tuple[Optional[float], str]:
    """Compute a clamped ATR-based stop distance in price units.

    Returns (distance, status) where status is "modeled" or "unavailable".
    A clamp-boundary hit still returns "modeled".
    """
    if atr_value is None or math.isnan(atr_value) or atr_value <= 0:
        return (None, "unavailable")

    raw = mult * atr_value

    floor = min_pips * pip_size
    ceiling = max_pips * pip_size
    clamped = max(floor, min(raw, ceiling))

    if structure_distance > 0:
        ratio_floor = ratio_min * structure_distance
        ratio_ceiling = ratio_max * structure_distance
        clamped = max(ratio_floor, min(clamped, ratio_ceiling))

    return (round(clamped, 6), "modeled")


def atr_stop_price(
    entry_price: float, direction: str, distance: float
) -> float:
    """Compute the ATR stop-loss price from entry and distance.

    LONG/BUY  -> stop below entry.
    SHORT/SELL -> stop above entry.
    """
    direction_upper = direction.upper()
    if direction_upper in ("LONG", "BUY"):
        return round(entry_price - distance, 6)
    return round(entry_price + distance, 6)
