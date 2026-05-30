"""
APEX TRADER — Entry Pattern Detector
Reads M1 candles like a veteran reads a room.
Detects engulfing, pin bars, inside bar breakouts,
and rejection wicks at institutional zones.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from loguru import logger


@dataclass
class PatternMatch:
    name: str
    description: str
    strength: int          # 1-5, higher = stronger confirmation
    candle_index: int      # Which candle triggered it


class EntryPatternDetector:
    """
    Micro-timeframe pattern recognition on M1.
    Every pattern returns (detected: bool, description: str).
    get_best_pattern() runs all detectors and returns the strongest match.
    """

    def detect_engulfing(
        self, df: pd.DataFrame, direction: str,
    ) -> tuple[bool, str]:
        if len(df) < 2:
            return False, ""
        curr = df.iloc[-1]
        prev = df.iloc[-2]

        curr_body = curr["close"] - curr["open"]
        prev_body = prev["close"] - prev["open"]

        if direction == "LONG":
            bullish = (
                curr_body > 0
                and prev_body < 0
                and curr["close"] > prev["open"]
                and curr["open"] < prev["close"]
                and abs(curr_body) > abs(prev_body)
            )
            if bullish:
                return True, "Bullish engulfing — buyers overwhelmed sellers"
        else:
            bearish = (
                curr_body < 0
                and prev_body > 0
                and curr["close"] < prev["open"]
                and curr["open"] > prev["close"]
                and abs(curr_body) > abs(prev_body)
            )
            if bearish:
                return True, "Bearish engulfing — sellers overwhelmed buyers"

        return False, ""

    def detect_pin_bar(
        self, df: pd.DataFrame, direction: str, pip_size: float,
    ) -> tuple[bool, str]:
        if len(df) < 1:
            return False, ""
        c = df.iloc[-1]

        total_range = c["high"] - c["low"]
        if total_range < pip_size:
            return False, ""

        body = abs(c["close"] - c["open"])
        body_ratio = body / total_range

        if direction == "LONG":
            lower_wick = min(c["open"], c["close"]) - c["low"]
            wick_ratio = lower_wick / total_range
            if wick_ratio >= 0.60 and body_ratio <= 0.30:
                return True, "Bullish pin bar — sharp rejection from below"
        else:
            upper_wick = c["high"] - max(c["open"], c["close"])
            wick_ratio = upper_wick / total_range
            if wick_ratio >= 0.60 and body_ratio <= 0.30:
                return True, "Bearish pin bar — sharp rejection from above"

        return False, ""

    def detect_inside_bar_breakout(
        self, df: pd.DataFrame, direction: str,
    ) -> tuple[bool, str]:
        if len(df) < 3:
            return False, ""
        mother = df.iloc[-3]
        inside = df.iloc[-2]
        breakout = df.iloc[-1]

        is_inside = inside["high"] <= mother["high"] and inside["low"] >= mother["low"]
        if not is_inside:
            return False, ""

        if direction == "LONG" and breakout["close"] > mother["high"]:
            return True, "Inside bar breakout to the upside"
        elif direction == "SHORT" and breakout["close"] < mother["low"]:
            return True, "Inside bar breakout to the downside"

        return False, ""

    def detect_rejection_wick(
        self,
        df: pd.DataFrame,
        zone_top: float,
        zone_bottom: float,
        direction: str,
        pip_size: float,
    ) -> tuple[bool, str]:
        if len(df) < 1:
            return False, ""
        c = df.iloc[-1]

        # Buffer scales to recent average candle range so it works across all
        # instruments — Gold M1 ranges $0.50-$2.00, FX ranges 0.0005-0.0020.
        # Using a fixed 2*pip_size was far too tight for Gold/indices.
        recent_range = (df["high"] - df["low"]).iloc[-5:].mean() if len(df) >= 5 else (c["high"] - c["low"])
        buffer = max(recent_range * 0.3, 2 * pip_size)

        if direction == "LONG":
            wick_pierced = c["low"] <= zone_top + buffer
            closed_above = c["close"] >= zone_top
            if wick_pierced and closed_above:
                return True, "Rejection wick into bullish zone — sharp reversal"
        else:
            wick_pierced = c["high"] >= zone_bottom - buffer
            closed_below = c["close"] <= zone_bottom
            if wick_pierced and closed_below:
                return True, "Rejection wick into bearish zone — sharp reversal"

        return False, ""

    def detect_volume_spike_at_zone(
        self, df: pd.DataFrame, zone_top: float, zone_bottom: float,
    ) -> tuple[bool, str]:
        if len(df) < 10 or "volume" not in df.columns:
            return False, ""

        avg_vol = df["volume"].iloc[-20:].mean() if len(df) >= 20 else df["volume"].mean()
        if avg_vol == 0:
            return False, ""

        recent = df.iloc[-3:]
        for _, candle in recent.iterrows():
            touches_zone = candle["low"] <= zone_top and candle["high"] >= zone_bottom
            if touches_zone and candle["volume"] > avg_vol * 1.5:
                ratio = round(candle["volume"] / avg_vol, 1)
                return True, f"Volume spike at zone ({ratio}x average)"

        return False, ""

    def get_best_pattern(
        self,
        df: pd.DataFrame,
        direction: str,
        zone_top: float,
        zone_bottom: float,
        pip_size: float,
    ) -> tuple[str, str]:
        """
        Run all detectors and return the best (strongest) match.
        Returns (pattern_name, description). Empty strings if nothing found.
        """
        checks: list[tuple[int, str, tuple[bool, str]]] = [
            (5, "engulfing", self.detect_engulfing(df, direction)),
            (4, "rejection_wick", self.detect_rejection_wick(df, zone_top, zone_bottom, direction, pip_size)),
            (3, "pin_bar", self.detect_pin_bar(df, direction, pip_size)),
            (2, "volume_spike", self.detect_volume_spike_at_zone(df, zone_top, zone_bottom)),
            (1, "inside_bar_breakout", self.detect_inside_bar_breakout(df, direction)),
        ]

        for _priority, name, (detected, desc) in checks:
            if detected:
                logger.debug(f"M1 pattern confirmed: {name} — {desc}")
                return name, desc

        return "", ""