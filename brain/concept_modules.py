"""APEX TRADER — Concept Modules (non-ICT signal generators).

The legacy/ICT brain modules (FVG, order blocks, structure, liquidity, Wyckoff,
inducement) cover one school of trading.  This module adds *concept* generators
from other schools so the WorldModel — and the data-driven combiner that weights
them — is not biased to a single methodology:

  * ``trend_momentum``   — classical trend-following (fast/slow SMA + slope).
  * ``mean_reversion``   — statistical pullback (z-score of price vs its mean).
  * ``volatility_regime``— classifies the market as TREND / RANGE / VOLATILE so
    the learned edge can be scoped per regime.

Each generator returns a uniform :class:`ConceptSignal` (non-directional strength only)
or NEUTRAL.  Everything is defensively guarded and returns a neutral result on
any error or insufficient data, so a concept can never break the analysis plane
(the caller also wraps each module in try/except).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


# Regime labels (also used as edge-tracker keys).
REGIME_TREND = "TREND"
REGIME_RANGE = "RANGE"
REGIME_VOLATILE = "VOLATILE"
REGIME_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ConceptSignal:
    """A single non-ICT concept's NON-DIRECTIONAL strength read.

    ``strength`` is 0.0-1.0 — the magnitude of the concept's signal, carrying no
    trade direction (Part XXV: analytical modules are measurement instruments,
    never voters).
    """

    name: str
    strength: float

    @property
    def is_active(self) -> bool:
        return self.strength > 0.0


_NEUTRAL_TREND = ConceptSignal("trend_momentum", 0.0)
_NEUTRAL_MR = ConceptSignal("mean_reversion", 0.0)


def _closes(df: pd.DataFrame) -> Any:
    if df is None or "close" not in getattr(df, "columns", []):
        return None
    return df["close"]


def trend_momentum(
    df: pd.DataFrame, fast: int = 20, slow: int = 50,
) -> ConceptSignal:
    """Trend-following: fast SMA vs slow SMA, confirmed by price location."""
    try:
        close = _closes(df)
        if close is None or len(close) < slow + 1:
            return _NEUTRAL_TREND
        sma_fast = float(close.rolling(fast).mean().iloc[-1])
        sma_slow = float(close.rolling(slow).mean().iloc[-1])
        last = float(close.iloc[-1])
        if sma_slow == 0:
            return _NEUTRAL_TREND
        sep = abs(sma_fast - sma_slow) / abs(sma_slow)
        strength = max(0.0, min(1.0, sep * 50.0))  # ~2% separation → full
        if (sma_fast > sma_slow and last >= sma_fast) or (
            sma_fast < sma_slow and last <= sma_fast
        ):
            return ConceptSignal("trend_momentum", round(strength, 4))
        return _NEUTRAL_TREND
    except Exception:
        return _NEUTRAL_TREND


def mean_reversion(
    df: pd.DataFrame, window: int = 20, z_threshold: float = 1.5,
) -> ConceptSignal:
    """Statistical pullback: extreme z-score of price vs its rolling mean.

    Overbought (z high) → SHORT bias; oversold (z low) → LONG bias.
    """
    try:
        close = _closes(df)
        if close is None or len(close) < window + 1:
            return _NEUTRAL_MR
        mean = float(close.rolling(window).mean().iloc[-1])
        std = float(close.rolling(window).std().iloc[-1])
        last = float(close.iloc[-1])
        if std <= 0:
            return _NEUTRAL_MR
        z = (last - mean) / std
        if abs(z) < z_threshold:
            return _NEUTRAL_MR
        strength = max(0.0, min(1.0, abs(z) / 3.0))
        return ConceptSignal("mean_reversion", round(strength, 4))
    except Exception:
        return _NEUTRAL_MR


def volatility_regime(
    df: pd.DataFrame, window: int = 14, baseline: int = 50,
) -> str:
    """Classify the market: TREND / RANGE / VOLATILE (best-effort)."""
    try:
        if df is None or not {"high", "low", "close"}.issubset(getattr(df, "columns", [])):
            return REGIME_UNKNOWN
        if len(df) < baseline + 1:
            return REGIME_UNKNOWN
        rng = (df["high"] - df["low"]).abs()
        atr_recent = float(rng.rolling(window).mean().iloc[-1])
        atr_base = float(rng.rolling(baseline).mean().iloc[-1])
        if atr_base <= 0:
            return REGIME_UNKNOWN
        if atr_recent / atr_base >= 1.5:
            return REGIME_VOLATILE
        trend = trend_momentum(df)
        return REGIME_TREND if trend.is_active else REGIME_RANGE
    except Exception:
        return REGIME_UNKNOWN


def run_concepts(df: pd.DataFrame) -> tuple[list[ConceptSignal], str]:
    """Run all concept generators; return (signals, regime_label)."""
    signals = [trend_momentum(df), mean_reversion(df)]
    regime = volatility_regime(df)
    return signals, regime
