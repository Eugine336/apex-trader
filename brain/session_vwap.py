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


def session_open_minutes_from_df(df: Optional[pd.DataFrame]) -> int:
    """Rough session age in minutes from an M5 series.

    Uses the span of the ``time`` column when present, else ``(bars - 1) × 5``.
    Best-effort — returns 0 on any failure or when fewer than two bars exist.
    Shared by the live handler and the backtest so both derive the VWAP-zone
    session window identically.
    """
    if df is None:
        return 0
    try:
        if getattr(df, "empty", False) or len(df) < 2:
            return 0
        if "time" in df.columns:
            t0 = pd.to_datetime(df["time"].iloc[0], utc=True, errors="coerce")
            t1 = pd.to_datetime(df["time"].iloc[-1], utc=True, errors="coerce")
            if pd.notna(t0) and pd.notna(t1):
                mins = int((t1 - t0).total_seconds() / 60.0)
                if mins > 0:
                    return mins
        return max(0, int((len(df) - 1) * 5))
    except Exception:  # noqa: BLE001 — a derivation miss just yields 0
        return 0


def vwap_with_bands(
    df: pd.DataFrame,
    session_open_minutes: int,
    num_std: float = 1.5,
    min_bars: int = 6,
) -> Optional[tuple[float, float, float]]:
    """Return ``(vwap, upper_band, lower_band)`` from an M5 DataFrame, or None.

    Companion to :func:`compute_session_vwap`.  The bands are the session VWAP
    ± ``num_std`` × the (volume-weighted where volume is available) standard
    deviation of typical price around the VWAP over the same session window.
    Returns None when the VWAP itself cannot be computed or the band width is
    non-finite / degenerate (zero-variance session), so callers can treat the
    absence of bands as "no VWAP zone this cycle".
    """
    vwap = compute_session_vwap(df, session_open_minutes, min_bars=min_bars)
    if vwap is None:
        return None

    bars_since_open = max(min_bars, session_open_minutes // 5)
    bars_since_open = min(bars_since_open, len(df))
    session_df = df.tail(bars_since_open)

    typical_price = (session_df["high"] + session_df["low"] + session_df["close"]) / 3
    deviations_sq = (typical_price - vwap) ** 2

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
            variance = float((deviations_sq * volumes).sum() / total_vol)
    if variance is None:
        variance = float(deviations_sq.mean())

    if not math.isfinite(variance) or variance <= 0:
        return None

    std = math.sqrt(variance)
    upper = vwap + num_std * std
    lower = vwap - num_std * std
    if not (math.isfinite(upper) and math.isfinite(lower)) or upper <= lower:
        return None
    return (vwap, upper, lower)


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
