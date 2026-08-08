"""
APEX TRADER — Volume Analyzer
Forex gives us tick volume, and a sharp trader still extracts intent from it.
This module spots expansion, exhaustion, and divergence before the crowd reacts.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger


class VolumeDivergence(Enum):
    NONE = "NONE"
    BULLISH = "BULLISH_DIVERGENCE"
    BEARISH = "BEARISH_DIVERGENCE"


@dataclass
class VolumeAnalysis:
    volume_ratio: float
    has_spike: bool
    divergence_type: str
    climax_detected: bool
    poc_level: Optional[float]


class VolumeAnalyzer:
    """
    Reads volume intensity and compares it with price behavior.
    In forex we operate on tick volume — not perfect, but highly actionable.
    """

    def __init__(
        self,
        lookback: int = 20,
        spike_threshold: float = 1.5,
        climax_threshold: float = 2.5,
    ):
        self.lookback = lookback
        self.spike_threshold = spike_threshold
        self.climax_threshold = climax_threshold

    def analyze(
        self, df: pd.DataFrame, closed_df: Optional[pd.DataFrame] = None
    ) -> VolumeAnalysis:
        """Analyse volume from two views of the same series.

        ``df`` is the live frame (may include the still-forming bar) and is the
        source of *observational* reads — ``volume_ratio`` reflects how the
        current bar's volume is developing in real time, which is legitimate
        live awareness.

        ``closed_df`` (forming bar removed) is the source of *structural*
        verdicts — ``has_spike``, ``climax_detected``, ``divergence_type`` and
        ``confirmation_bias``. Deriving these from a partial bar divides an
        incomplete volume by a full-bar average and yields meaningless ratios,
        so they must come from confirmed, closed candles only.

        When ``closed_df`` is not supplied (or is too short) the method falls
        back to ``df`` for everything, preserving the original behaviour.
        """
        if len(df) < self.lookback + 2:
            logger.warning("Not enough candles for volume analysis")
            return VolumeAnalysis(
                volume_ratio=0.0,
                has_spike=False,
                divergence_type=VolumeDivergence.NONE.value,
                climax_detected=False,
                poc_level=None,
            )

        volume_col = self._resolve_volume_column(df)
        if volume_col is None:
            logger.warning(
                "No volume column found, volume analysis running in neutral mode"
            )
            return VolumeAnalysis(
                volume_ratio=0.0,
                has_spike=False,
                divergence_type=VolumeDivergence.NONE.value,
                climax_detected=False,
                poc_level=float(df["close"].iloc[-1]),
            )

        # Structural view: closed bars only. Fall back to the live frame when
        # closed_df is absent or too short to satisfy the lookback window.
        struct_df = df
        if (
            closed_df is not None
            and len(closed_df) >= self.lookback + 2
            and self._resolve_volume_column(closed_df) is not None
        ):
            struct_df = closed_df

        # ── Observational (live ``df``): how volume is developing right now ──
        volumes = df[volume_col].astype(float)
        current_volume = float(volumes.iloc[-1])
        avg_volume = float(volumes.iloc[-(self.lookback + 1) : -1].mean())
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0.0

        # ── Structural (closed bars): confirmed spike / climax / bias ──
        struct_col = self._resolve_volume_column(struct_df) or volume_col
        struct_volumes = struct_df[struct_col].astype(float)
        struct_current = float(struct_volumes.iloc[-1])
        struct_avg = float(struct_volumes.iloc[-(self.lookback + 1) : -1].mean())
        struct_ratio = struct_current / struct_avg if struct_avg > 0 else 0.0

        has_spike = struct_ratio >= self.spike_threshold
        divergence = self._detect_divergence(struct_df, struct_volumes)
        climax = self._detect_climax(struct_df, struct_ratio)
        poc_level = self._estimate_point_of_control(struct_df, struct_volumes)

        return VolumeAnalysis(
            volume_ratio=round(volume_ratio, 4),
            has_spike=has_spike,
            divergence_type=divergence.value,
            climax_detected=climax,
            poc_level=round(poc_level, 6) if poc_level is not None else None,
        )

    def _resolve_volume_column(self, df: pd.DataFrame) -> Optional[str]:
        for candidate in ("tick_volume", "volume", "vol"):
            if candidate in df.columns:
                return candidate
        return None

    def _detect_divergence(
        self, df: pd.DataFrame, volumes: pd.Series
    ) -> VolumeDivergence:
        window = self.lookback
        recent_high_idx = df["high"].iloc[-window:].idxmax()
        prior_high_idx = (
            df["high"].iloc[-(window * 2) : -window].idxmax()
            if len(df) >= window * 2
            else None
        )

        if prior_high_idx is not None:
            made_higher_high = (
                df.loc[recent_high_idx, "high"] > df.loc[prior_high_idx, "high"]
            )
            weaker_volume = volumes.loc[recent_high_idx] < volumes.loc[prior_high_idx]
            if made_higher_high and weaker_volume:
                return VolumeDivergence.BEARISH

        recent_low_idx = df["low"].iloc[-window:].idxmin()
        prior_low_idx = (
            df["low"].iloc[-(window * 2) : -window].idxmin()
            if len(df) >= window * 2
            else None
        )

        if prior_low_idx is not None:
            made_lower_low = (
                df.loc[recent_low_idx, "low"] < df.loc[prior_low_idx, "low"]
            )
            weaker_volume = volumes.loc[recent_low_idx] < volumes.loc[prior_low_idx]
            if made_lower_low and weaker_volume:
                return VolumeDivergence.BULLISH

        return VolumeDivergence.NONE

    def _detect_climax(self, df: pd.DataFrame, volume_ratio: float) -> bool:
        if volume_ratio < self.climax_threshold:
            return False

        candle = df.iloc[-1]
        body = abs(candle["close"] - candle["open"])
        upper_wick = candle["high"] - max(candle["open"], candle["close"])
        lower_wick = min(candle["open"], candle["close"]) - candle["low"]
        wick = max(upper_wick, lower_wick)
        return wick > body * 1.5

    def _estimate_point_of_control(
        self, df: pd.DataFrame, volumes: pd.Series
    ) -> Optional[float]:
        closes = df["close"].tail(self.lookback).values
        vols = volumes.tail(self.lookback).values
        if len(closes) == 0:
            return None
        bins = min(20, max(5, len(closes) // 2))
        hist, edges = np.histogram(closes, bins=bins, weights=vols)
        poc_idx = int(np.argmax(hist))
        return float((edges[poc_idx] + edges[poc_idx + 1]) / 2)

