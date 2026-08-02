"""
APEX TRADER — Session-Anchored VWAP

Computes intraday VWAP anchored to the current trading session start.
Used as a confirmation-only penalty: longs above VWAP (buying into overhead
supply) or shorts below VWAP (selling into demand) get penalised.

Pure function — no side effects, no broker/network access.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd


def compute_session_vwap(
    df: pd.DataFrame,
    session_open_minutes: int,
    min_bars: int = 6,
) -> Optional[float]:
    """Return session-anchored VWAP from an M5 DataFrame, or None."""
    if session_open_minutes <= 0 or len(df) < min_bars:
        return None

    bars_since_open = max(min_bars, session_open_minutes // 5)
    bars_since_open = min(bars_since_open, len(df))
    session_df = df.tail(bars_since_open)

    typical_price = (session_df["high"] + session_df["low"] + session_df["close"]) / 3

    vol_col = None
    for candidate in ("tick_volume", "volume", "vol"):
        if candidate in session_df.columns:
            vol_col = candidate
            break

    if vol_col is not None:
        volumes = session_df[vol_col].astype(float)
        total_vol = float(volumes.sum())
        if total_vol > 0:
            vwap = float((typical_price * volumes).sum() / total_vol)
            if math.isfinite(vwap) and vwap > 0:
                return vwap

    val = float(typical_price.mean())
    if math.isfinite(val) and val > 0:
        return val
    return None


def vwap_with_bands(
    df: pd.DataFrame,
    session_open_minutes: int,
    num_std: float = 1.5,
    min_bars: int = 6,
) -> Optional[tuple[float, float, float]]:
    """Return ``(vwap, upper_band, lower_band)`` from an M5 DataFrame, or None.

    Companion to :func:`compute_session_vwap` that also derives symmetric
    deviation bands: ``upper = vwap + num_std × std`` and
    ``lower = vwap - num_std × std``, where ``std`` is the (volume-weighted when
    volume is available) standard deviation of the session's typical price
    around the VWAP.  Uses the identical session window as
    :func:`compute_session_vwap` so the bands are anchored to the same bars.
    Returns None whenever the VWAP itself is unavailable or the deviation is
    not finite — callers treat that as "no VWAP zone this cycle".
    """
    vwap = compute_session_vwap(df, session_open_minutes, min_bars=min_bars)
    if vwap is None:
        return None

    bars_since_open = max(min_bars, session_open_minutes // 5)
    bars_since_open = min(bars_since_open, len(df))
    session_df = df.tail(bars_since_open)

    typical_price = (session_df["high"] + session_df["low"] + session_df["close"]) / 3

    vol_col = None
    for candidate in ("tick_volume", "volume", "vol"):
        if candidate in session_df.columns:
            vol_col = candidate
            break

    variance: Optional[float] = None
    if vol_col is not None:
        volumes = session_df[vol_col].astype(float)
        total_vol = float(volumes.sum())
        if total_vol > 0:
            variance = float(
                (volumes * (typical_price - vwap) ** 2).sum() / total_vol
            )

    if variance is None:
        variance = float(((typical_price - vwap) ** 2).mean())

    if variance is None or not math.isfinite(variance) or variance < 0:
        return None

    std = math.sqrt(variance)
    upper = vwap + num_std * std
    lower = vwap - num_std * std
    if not (math.isfinite(upper) and math.isfinite(lower)):
        return None
    return vwap, upper, lower


def session_vwap_penalty(
    df: pd.DataFrame,
    trade_dir: str,
    session_open_minutes: int,
    penalty_points: int = 15,
    min_session_minutes: int = 30,
) -> tuple[int, str]:
    """Return (penalty, reason). Penalty >= 0; 0 on agreement or bad data."""
    if trade_dir not in ("LONG", "SHORT"):
        return 0, ""

    if session_open_minutes < min_session_minutes:
        return 0, ""

    vwap = compute_session_vwap(df, session_open_minutes)
    if vwap is None:
        return 0, ""

    current_price = float(df["close"].iloc[-1])
    if not math.isfinite(current_price) or current_price <= 0:
        return 0, ""

    wrong_side = False
    if trade_dir == "LONG" and current_price > vwap:
        wrong_side = True
    elif trade_dir == "SHORT" and current_price < vwap:
        wrong_side = True

    if wrong_side:
        return (
            penalty_points,
            f"VWAP wrong-side (price={current_price:.5f} vs VWAP={vwap:.5f})",
        )
    return 0, ""
