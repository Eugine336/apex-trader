"""
APEX TRADER — Volume Profile POC Penalty

Computes H4 Point of Control (highest volume-at-price level) and
penalises entries that would push into the POC congestion zone.
Only computed on instruments with real exchange volume (commodity,
index, crypto). Forex and synthetics (tick-volume only) are skipped.

Pure function — no side effects, no broker/network access.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

_REAL_VOLUME_CATEGORIES = frozenset({"commodity", "index", "crypto"})


def has_real_volume(category: str) -> bool:
    """True if the category has real exchange volume (not tick-volume)."""
    return category.lower() in _REAL_VOLUME_CATEGORIES


def compute_poc(
    df: pd.DataFrame,
    lookback: int = 100,
) -> Optional[float]:
    """Return the Point of Control (price with highest traded volume)."""
    tail = df.tail(lookback)
    if len(tail) < 10:
        return None

    vol_col = None
    for candidate in ("tick_volume", "volume", "vol"):
        if candidate in tail.columns:
            vol_col = candidate
            break
    if vol_col is None:
        return None

    closes = tail["close"].values.astype(float)
    volumes = tail[vol_col].values.astype(float)

    if np.nansum(volumes) <= 0:
        return None

    bins = min(30, max(5, len(closes) // 3))
    hist, edges = np.histogram(closes, bins=bins, weights=volumes)
    poc_idx = int(np.argmax(hist))
    poc = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)
    return poc if math.isfinite(poc) and poc > 0 else None


def volume_profile_poc_penalty(
    h4_df: pd.DataFrame,
    trade_dir: str,
    category: str,
    penalty_points: int = 10,
    lookback: int = 100,
    proximity_pct: float = 0.5,
) -> tuple[int, str]:
    """Return (penalty, reason). Skips tick-volume instruments entirely."""
    if not has_real_volume(category):
        return 0, ""

    if trade_dir not in ("LONG", "SHORT"):
        return 0, ""

    poc = compute_poc(h4_df, lookback)
    if poc is None:
        return 0, ""

    current_price = float(h4_df["close"].iloc[-1])
    if not math.isfinite(current_price) or current_price <= 0:
        return 0, ""

    threshold = current_price * proximity_pct / 100.0

    at_poc = abs(current_price - poc) < threshold

    poc_ahead = False
    if trade_dir == "LONG" and poc > current_price:
        poc_ahead = (poc - current_price) < threshold * 3
    elif trade_dir == "SHORT" and poc < current_price:
        poc_ahead = (current_price - poc) < threshold * 3

    if at_poc or poc_ahead:
        return (
            penalty_points,
            f"VP-POC trap (POC={poc:.5f}, price={current_price:.5f})",
        )
    return 0, ""
