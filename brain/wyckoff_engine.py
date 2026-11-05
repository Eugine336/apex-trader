"""
APEX TRADER — Wyckoff Engine
This module translates market behavior into Wyckoff phases and highlights
the spring/upthrust traps where a sharp sniper usually gets paid.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd
from loguru import logger

from brain.market_data_utils import drop_forming_bar
from brain.structure_engine import StructureEngine, Trend
from brain.volume_analyzer import VolumeAnalysis, VolumeAnalyzer


class WyckoffPhase(Enum):
    PHASE_A = "PHASE_A"
    PHASE_B = "PHASE_B"
    PHASE_C = "PHASE_C"
    PHASE_D = "PHASE_D"
    PHASE_E = "PHASE_E"
    UNKNOWN = "UNKNOWN"


@dataclass
class WyckoffAnalysis:
    phase: str
    sub_phase: str
    spring_detected: bool
    upthrust_detected: bool
    phase_confidence: float


class WyckoffEngine:
    """
    Classifies accumulation/distribution development and extracts expected direction.
    """

    def __init__(
        self,
        lookback: int = 40,
        range_window: int = 24,
        pip_size: Optional[float] = None,
        volume_analyzer: Optional[VolumeAnalyzer] = None,
        structure_engine: Optional[StructureEngine] = None,
        swing_lookback: int = 2,
    ):
        if pip_size is None:
            raise ValueError(
                "WyckoffEngine requires an explicit pip_size; the 0.0001 "
                "FX-major default silently corrupts geometry on JPY/metals/"
                "indices/synthetics. Pass get_pip_size(symbol)."
            )
        self.lookback = lookback
        self.range_window = range_window
        self.pip_size = pip_size
        self.volume_analyzer = volume_analyzer or VolumeAnalyzer()
        # The inner StructureEngine's swing sensitivity is per-instrument, not a
        # hardcoded 2 — a synthetic index and a JPY cross do not confirm swings
        # on the same window. Callers pass the instrument profile's swing_lookback.
        self.structure_engine = structure_engine or StructureEngine(
            swing_lookback=swing_lookback, pip_size=pip_size
        )

    def analyze(self, df: pd.DataFrame) -> WyckoffAnalysis:
        # Closed-bar contract: spring/upthrust/range/trend are confirmed
        # structural artifacts and must read closed candles only. The inner
        # StructureEngine self-drops the forming bar in _detect_structure_event,
        # so it receives the live ``df`` to avoid a double-drop.
        closed = drop_forming_bar(df)
        if len(closed) < self.lookback:
            logger.warning("Not enough candles for Wyckoff analysis")
            return WyckoffAnalysis(
                phase=WyckoffPhase.UNKNOWN.value,
                sub_phase="INSUFFICIENT_DATA",
                spring_detected=False,
                upthrust_detected=False,
                phase_confidence=0.0,
            )

        volume = self.volume_analyzer.analyze(closed)
        structure = self.structure_engine.analyze(df)
        range_high, range_low, in_range = self._range_state(closed)
        spring = self._is_spring(closed, range_low)
        upthrust = self._is_upthrust(closed, range_high)
        trend_return = self._trend_return(closed)

        phase, sub_phase, confidence = self._classify(
            in_range=in_range,
            trend=structure.trend,
            trend_return=trend_return,
            spring=spring,
            upthrust=upthrust,
            volume=volume,
        )

        return WyckoffAnalysis(
            phase=phase.value,
            sub_phase=sub_phase,
            spring_detected=spring,
            upthrust_detected=upthrust,
            phase_confidence=round(confidence, 4),
        )

    def _range_state(self, df: pd.DataFrame) -> tuple[float, float, bool]:
        """Compare the latest completed auction with its own preceding history."""
        if len(df) <= self.range_window:
            return float("nan"), float("nan"), False
        reference = df.iloc[-self.range_window - 1 : -1]
        range_high = float(reference["high"].max())
        range_low = float(reference["low"].min())
        widths = []
        for end in range(self.range_window, len(df)):
            window = df.iloc[end - self.range_window : end]
            widths.append(float(window["high"].max() - window["low"].min()))
        current_width = range_high - range_low
        historical_midpoint = float(pd.Series(widths).median()) if widths else float("nan")
        in_range = bool(pd.notna(historical_midpoint) and current_width <= historical_midpoint)
        return range_high, range_low, in_range

    def _is_spring(self, df: pd.DataFrame, range_low: float) -> bool:
        last = df.iloc[-1]
        return (
            last["low"] < range_low
            and last["close"] > range_low
            and (min(last["open"], last["close"]) - last["low"])
            > abs(last["close"] - last["open"])
        )

    def _is_upthrust(self, df: pd.DataFrame, range_high: float) -> bool:
        last = df.iloc[-1]
        return (
            last["high"] > range_high
            and last["close"] < range_high
            and (last["high"] - max(last["open"], last["close"]))
            > abs(last["close"] - last["open"])
        )

    def _trend_return(self, df: pd.DataFrame) -> float:
        start = float(df["close"].iloc[-self.lookback])
        end = float(df["close"].iloc[-1])
        if start == 0:
            return 0.0
        return (end - start) / start

    def _classify(
        self,
        in_range: bool,
        trend: Trend,
        trend_return: float,
        spring: bool,
        upthrust: bool,
        volume: VolumeAnalysis,
    )-> tuple[WyckoffPhase, str, float]:
        # This reader reports phase facts only; it never recommends a side.
        if spring:
            return WyckoffPhase.PHASE_C, "SPRING", 0.82
        if upthrust:
            return WyckoffPhase.PHASE_C, "UPTHRUST", 0.82
        if in_range and volume.climax_detected:
            sub = "SELLING_CLIMAX" if trend_return < 0 else "BUYING_CLIMAX"
            return WyckoffPhase.PHASE_A, sub, 0.72
        if in_range:
            return WyckoffPhase.PHASE_B, "BUILDING_CAUSE", 0.60
        if trend in {Trend.BULLISH, Trend.BEARISH}:
            return WyckoffPhase.PHASE_E, "TREND_CONTINUATION", 0.70
        # No fixed percentage-return trigger: absent structural confirmation,
        # phase remains unresolved instead of inventing markup/markdown.
        return WyckoffPhase.UNKNOWN, "UNRESOLVED", 0.45
