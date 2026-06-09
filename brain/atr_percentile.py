"""
APEX TRADER — ATR Percentile Rank

Classifies the current ATR within a rolling window to detect
dead/compressed volatility regimes where structure-based entries
are likely to chop. Used as a confirmation-only penalty.

Reuses ``brain.volatility_stop.atr_series`` for the ATR calculation.
Pure function — no side effects, no broker/network access.
"""

from __future__ import annotations

import math

import pandas as pd
from loguru import logger

from brain.volatility_stop import atr_series


def compute_atr_percentile(
    df: pd.DataFrame,
    period: int = 14,
    window: int = 100,
) -> float:
    """Return ATR percentile rank (0–100) within a rolling window.

    Returns -1.0 on insufficient data so the caller can distinguish
    "not computed" from "0th percentile."
    """
    atr = atr_series(df, period)
    usable = atr.tail(window).dropna()
    if len(usable) < window:
        return -1.0

    current_atr = float(usable.iloc[-1])
    if not math.isfinite(current_atr) or current_atr <= 0:
        return -1.0

    rank = float((usable < current_atr).sum()) / len(usable) * 100.0
    return rank


def atr_percentile_penalty(
    df: pd.DataFrame,
    penalty_points: int = 10,
    period: int = 14,
    window: int = 100,
    dead_percentile: float = 20.0,
) -> tuple[int, str]:
    """Return (penalty, reason). Penalty if ATR is in dead/compressed zone."""
    pctl = compute_atr_percentile(df, period, window)
    if pctl < 0:
        return 0, ""

    if pctl < dead_percentile:
        return (
            penalty_points,
            f"ATR dead regime (pctl={pctl:.0f}%, threshold={dead_percentile:.0f}%)",
        )
    return 0, ""
