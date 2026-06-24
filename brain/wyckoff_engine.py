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
    expected_direction: str


class WyckoffEngine:
    """
    Classifies accumulation/distribution development and extracts expected direction.
    """

    def __init__(
        self,
        lookback: int = 40,
        range_window: int = 24,
        spring_buffer_pips: float = 3.0,
        pip_size: Optional[float] = None,
        volume_analyzer: Optional[VolumeAnalyzer] = None,
        structure_engine: Optional[StructureEngine] = None,
    ):
        if pip_size is None:
            raise ValueError(
                "WyckoffEngine requires an explicit pip_size; the 0.0001 "
                "FX-major default silently corrupts geometry on JPY/metals/"
                "indices/synthetics. Pass get_pip_size(symbol)."
            )
        self.lookback = lookback
        self.range_window = range_window
        self.spring_buffer = spring_buffer_pips * pip_size
        self.pip_size = pip_size
        self.volume_analyzer = volume_analyzer or VolumeAnalyzer()
        self.structure_engine = structure_engine or StructureEngine(
            swing_lookback=2, pip_size=pip_size
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
                expected_direction="NONE",
            )

        volume = self.volume_analyzer.analyze(closed)
        structure = self.structure_engine.analyze(df)
        range_high, range_low, in_range = self._range_state(closed)
        spring = self._is_spring(closed, range_low)
        upthrust = self._is_upthrust(closed, range_high)
        trend_return = self._trend_return(closed)

        phase, sub_phase, direction, confidence = self._classify(
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
            expected_direction=direction,
        )

    def _range_state(self, df: pd.DataFrame) -> tuple[float, float, bool]:
        recent = df.iloc[-self.range_window :]
        range_high = float(recent["high"].max())
        range_low = float(recent["low"].min())
        range_width = (range_high - range_low) / max(recent["close"].iloc[-1], 1e-9)
        in_range = range_width <= 0.015
        return range_high, range_low, in_range

    def _is_spring(self, df: pd.DataFrame, range_low: float) -> bool:
        last = df.iloc[-1]
        return (
            last["low"] < range_low - self.spring_buffer
            and last["close"] > range_low
            and (min(last["open"], last["close"]) - last["low"])
            > abs(last["close"] - last["open"])
        )

    def _is_upthrust(self, df: pd.DataFrame, range_high: float) -> bool:
        last = df.iloc[-1]
        return (
            last["high"] > range_high + self.spring_buffer
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
    ) -> tuple[WyckoffPhase, str, str, float]:
        if spring:
            return WyckoffPhase.PHASE_C, "SPRING", "LONG", 0.82
        if upthrust:
            return WyckoffPhase.PHASE_C, "UPTHRUST", "SHORT", 0.82

        if in_range and volume.climax_detected:
            sub = "SELLING_CLIMAX" if trend_return < 0 else "BUYING_CLIMAX"
            direction = "LONG" if trend_return < 0 else "SHORT"
            return WyckoffPhase.PHASE_A, sub, direction, 0.72

        if in_range:
            if volume.confirmation_bias in {"BULLISH", "BEARISH"}:
                direction = "LONG" if volume.confirmation_bias == "BULLISH" else "SHORT"
                return WyckoffPhase.PHASE_D, "RANGE_EXIT_PREP", direction, 0.68
            return WyckoffPhase.PHASE_B, "BUILDING_CAUSE", "NONE", 0.6

        if trend in {Trend.BULLISH, Trend.BEARISH}:
            direction = "LONG" if trend == Trend.BULLISH else "SHORT"
            return WyckoffPhase.PHASE_E, "TREND_CONTINUATION", direction, 0.7

        if trend_return > 0.01:
            return WyckoffPhase.PHASE_D, "MARKUP_START", "LONG", 0.64
        if trend_return < -0.01:
            return WyckoffPhase.PHASE_D, "MARKDOWN_START", "SHORT", 0.64

        return WyckoffPhase.UNKNOWN, "UNRESOLVED", "NONE", 0.45
