"""
APEX TRADER — Inducement Detector
Institutions bait the crowd before the real move.
This detector hunts fake breaks, stop hunts, and turtle soup traps.
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd
from loguru import logger


@dataclass
class InducementAnalysis:
    inducement_detected: bool
    type: str
    trap_level: Optional[float]
    expected_direction: str
    confidence: float


class InducementDetector:
    """
    Detects manipulation signatures right before high-quality reversals.
    """

    def __init__(
        self,
        sweep_buffer_pips: float = 2.0,
        turtle_break_pips: float = 5.0,
        pip_size: float = 0.0001,
    ):
        self.sweep_buffer = sweep_buffer_pips * pip_size
        self.turtle_break = turtle_break_pips * pip_size
        self.pip_size = pip_size

    def analyze(self, df: pd.DataFrame) -> InducementAnalysis:
        if len(df) < 25:
            logger.warning("Not enough candles for inducement detection")
            return InducementAnalysis(False, "NONE", None, "NONE", 0.0)

        detections: list[InducementAnalysis] = []
        recent_high = float(df["high"].iloc[-22:-2].max())
        recent_low = float(df["low"].iloc[-22:-2].min())
        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]

        stop_hunt = self._detect_stop_hunt(last, recent_high, recent_low)
        if stop_hunt:
            detections.append(stop_hunt)

        fake_breakout = self._detect_fake_breakout(
            df, prev, last, recent_high, recent_low
        )
        if fake_breakout:
            detections.append(fake_breakout)

        turtle_soup = self._detect_turtle_soup(prev, last, recent_high, recent_low)
        if turtle_soup:
            detections.append(turtle_soup)

        inducement = self._detect_inducement_break(
            prev2, prev, last, recent_high, recent_low
        )
        if inducement:
            detections.append(inducement)

        if not detections:
            return InducementAnalysis(False, "NONE", None, "NONE", 0.0)

        detections.sort(key=lambda x: x.confidence, reverse=True)
        return detections[0]

    def _detect_stop_hunt(
        self, last: pd.Series, recent_high: float, recent_low: float
    ) -> Optional[InducementAnalysis]:
        body = abs(last["close"] - last["open"])
        upper_wick = last["high"] - max(last["open"], last["close"])
        lower_wick = min(last["open"], last["close"]) - last["low"]

        if (
            last["high"] >= recent_high + self.sweep_buffer
            and last["close"] < recent_high
            and upper_wick > body * 1.5
        ):
            return InducementAnalysis(
                inducement_detected=True,
                type="STOP_HUNT_BUY_SIDE",
                trap_level=recent_high,
                expected_direction="SHORT",
                confidence=0.82,
            )

        if (
            last["low"] <= recent_low - self.sweep_buffer
            and last["close"] > recent_low
            and lower_wick > body * 1.5
        ):
            return InducementAnalysis(
                inducement_detected=True,
                type="STOP_HUNT_SELL_SIDE",
                trap_level=recent_low,
                expected_direction="LONG",
                confidence=0.82,
            )
        return None

    def _detect_fake_breakout(
        self,
        df: pd.DataFrame,
        prev: pd.Series,
        last: pd.Series,
        recent_high: float,
        recent_low: float,
    ) -> Optional[InducementAnalysis]:
        volume_col = (
            "tick_volume"
            if "tick_volume" in df.columns
            else ("volume" if "volume" in df.columns else None)
        )
        low_momentum = False
        if volume_col:
            avg_volume = float(df[volume_col].iloc[-22:-2].mean())
            low_momentum = float(prev[volume_col]) < avg_volume * 0.9

        if prev["close"] > recent_high and last["close"] < recent_high and low_momentum:
            return InducementAnalysis(
                inducement_detected=True,
                type="FAKE_BREAKOUT_UP",
                trap_level=recent_high,
                expected_direction="SHORT",
                confidence=0.76,
            )

        if prev["close"] < recent_low and last["close"] > recent_low and low_momentum:
            return InducementAnalysis(
                inducement_detected=True,
                type="FAKE_BREAKOUT_DOWN",
                trap_level=recent_low,
                expected_direction="LONG",
                confidence=0.76,
            )
        return None

    def _detect_turtle_soup(
        self, prev: pd.Series, last: pd.Series, recent_high: float, recent_low: float
    ) -> Optional[InducementAnalysis]:
        break_up = recent_high < prev["high"] <= recent_high + self.turtle_break
        break_down = recent_low - self.turtle_break <= prev["low"] < recent_low

        if break_up and last["close"] < recent_high:
            return InducementAnalysis(
                inducement_detected=True,
                type="TURTLE_SOUP_BEARISH",
                trap_level=recent_high,
                expected_direction="SHORT",
                confidence=0.8,
            )

        if break_down and last["close"] > recent_low:
            return InducementAnalysis(
                inducement_detected=True,
                type="TURTLE_SOUP_BULLISH",
                trap_level=recent_low,
                expected_direction="LONG",
                confidence=0.8,
            )
        return None

    def _detect_inducement_break(
        self,
        prev2: pd.Series,
        prev: pd.Series,
        last: pd.Series,
        recent_high: float,
        recent_low: float,
    ) -> Optional[InducementAnalysis]:
        bearish_trap = prev["close"] < recent_low and last["close"] > prev2["high"]
        bullish_trap = prev["close"] > recent_high and last["close"] < prev2["low"]

        if bearish_trap:
            return InducementAnalysis(
                inducement_detected=True,
                type="INDUCEMENT_DOWN_BEFORE_LONG",
                trap_level=recent_low,
                expected_direction="LONG",
                confidence=0.74,
            )

        if bullish_trap:
            return InducementAnalysis(
                inducement_detected=True,
                type="INDUCEMENT_UP_BEFORE_SHORT",
                trap_level=recent_high,
                expected_direction="SHORT",
                confidence=0.74,
            )
        return None
