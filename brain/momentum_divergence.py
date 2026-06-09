"""
APEX TRADER — Momentum Divergence Detector

Detects RSI/MACD divergence against the intended trade direction on
M5 and H1 timeframes. Used as a confirmation-only penalty: if momentum
opposes the trade, the setup is penalised.

Pure function — no side effects, no broker/network access.
RSI formula reused from brain.currency_strength._calculate_rsi pattern.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd
from loguru import logger


def calculate_rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder-style RSI. Returns None on insufficient data or NaN."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = float(gains.rolling(period).mean().iloc[-1])
    avg_loss = float(losses.rolling(period).mean().iloc[-1])
    if not math.isfinite(avg_gain) or not math.isfinite(avg_loss):
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi if math.isfinite(rsi) else None


def calculate_macd(
    closes: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Optional[tuple[float, float]]:
    """Return (macd_line, signal_line) or None on insufficient data."""
    if len(closes) < slow + signal:
        return None
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    ml = float(macd_line.iloc[-1])
    sl = float(signal_line.iloc[-1])
    if not math.isfinite(ml) or not math.isfinite(sl):
        return None
    return ml, sl


def _tf_diverges_against(
    df: pd.DataFrame,
    trade_dir: str,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> bool:
    """True if this timeframe's momentum diverges against trade_dir."""
    closes = df["close"]

    rsi = calculate_rsi(closes, rsi_period)
    rsi_against = False
    if rsi is not None:
        if trade_dir == "LONG" and rsi > 70:
            rsi_against = True
        elif trade_dir == "SHORT" and rsi < 30:
            rsi_against = True

    macd = calculate_macd(closes, macd_fast, macd_slow, macd_signal)
    macd_against = False
    if macd is not None:
        macd_line, signal_line = macd
        if trade_dir == "LONG" and macd_line < signal_line:
            macd_against = True
        elif trade_dir == "SHORT" and macd_line > signal_line:
            macd_against = True

    return rsi_against or macd_against


def momentum_divergence_penalty(
    m5_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    trade_dir: str,
    both_tf_penalty: int = 15,
    single_tf_penalty: int = 7,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> tuple[int, str]:
    """Return (penalty, reason). Both TFs diverge → full; one → half; zero → 0."""
    if trade_dir not in ("LONG", "SHORT"):
        return 0, ""

    m5_against = _tf_diverges_against(
        m5_df, trade_dir, rsi_period, macd_fast, macd_slow, macd_signal,
    )
    h1_against = _tf_diverges_against(
        h1_df, trade_dir, rsi_period, macd_fast, macd_slow, macd_signal,
    )

    if m5_against and h1_against:
        return both_tf_penalty, "RSI/MACD divergence M5+H1"
    if m5_against:
        return single_tf_penalty, "RSI/MACD divergence M5 only"
    if h1_against:
        return single_tf_penalty, "RSI/MACD divergence H1 only"
    return 0, ""
