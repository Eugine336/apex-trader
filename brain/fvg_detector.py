"""
APEX TRADER — Fair Value Gap (FVG) Detector
Finds price imbalances where institutions left footprints.
Price always returns to fill these gaps — we trade from them.
"""

import pandas as pd
import numpy as np
from loguru import logger
from dataclasses import dataclass
from typing import Optional
from enum import Enum


class FVGStatus(Enum):
    OPEN      = "OPEN"       # Not yet filled
    PARTIALLY = "PARTIALLY"  # Partially filled
    FILLED    = "FILLED"     # Fully filled — no longer valid
    MITIGATED = "MITIGATED"  # Price entered the zone


@dataclass
class FairValueGap:
    kind: str           # "BULLISH" or "BEARISH"
    top: float          # Upper boundary
    bottom: float       # Lower boundary
    midpoint: float     # 50% level — ideal entry
    size_pips: float    # Size of the gap in pips
    strength: str       # "WEAK", "MODERATE", "STRONG"
    status: FVGStatus
    candle_index: int   # Which candle created it
    timestamp: pd.Timestamp
    timeframe: str      # Which TF this FVG is on


class FVGDetector:
    """
    Detects Fair Value Gaps (price imbalances) across timeframes.

    A Bullish FVG forms when:
    Candle 3 low > Candle 1 high (gap left between them)

    A Bearish FVG forms when:
    Candle 3 high < Candle 1 low (gap left between them)

    The midpoint of the FVG is the optimal entry zone.
    """

    def __init__(self, min_size_pips: float = 2.0, pip_size: float = 0.0001,
                 proximity_pips: float = 5.0):
        self.min_size_pips = min_size_pips
        self.pip_size = pip_size
        self.min_size = min_size_pips * pip_size
        # proximity_pips is supplied by InstrumentProfile — different per category.
        # Forex: 3.0 | Commodity: 8.0 | Index: 15.0 | Synthetic: 8.0
        self.proximity = proximity_pips * pip_size
    def detect(self, df: pd.DataFrame, timeframe: str = "M5") -> list[FairValueGap]:
        """
        Detect all open FVGs on the given dataframe.
        Returns list of FairValueGap objects, most recent first.
        """
        if len(df) < 3:
            return []

        df = df.copy().reset_index(drop=True)
        fvgs = []

        for i in range(1, len(df) - 1):
            c1 = df.iloc[i - 1]  # First candle
            c2 = df.iloc[i]      # Middle candle (the impulse)
            c3 = df.iloc[i + 1]  # Third candle

            ts = c2["time"] if "time" in df.columns else pd.Timestamp.now()

            # Bullish FVG: C3 low > C1 high (gap above C1, below C3)
            if c3["low"] > c1["high"]:
                gap_size = c3["low"] - c1["high"]
                if gap_size >= self.min_size:
                    midpoint = (c3["low"] + c1["high"]) / 2
                    strength = self._rate_strength(gap_size, c2)
                    fvgs.append(FairValueGap(
                        kind="BULLISH",
                        top=c3["low"],
                        bottom=c1["high"],
                        midpoint=midpoint,
                        size_pips=round(gap_size / self.pip_size, 1),
                        strength=strength,
                        status=FVGStatus.OPEN,
                        candle_index=i,
                        timestamp=pd.Timestamp(ts),
                        timeframe=timeframe,
                    ))

            # Bearish FVG: C3 high < C1 low (gap below C1, above C3)
            elif c3["high"] < c1["low"]:
                gap_size = c1["low"] - c3["high"]
                if gap_size >= self.min_size:
                    midpoint = (c1["low"] + c3["high"]) / 2
                    strength = self._rate_strength(gap_size, c2)
                    fvgs.append(FairValueGap(
                        kind="BEARISH",
                        top=c1["low"],
                        bottom=c3["high"],
                        midpoint=midpoint,
                        size_pips=round(gap_size / self.pip_size, 1),
                        strength=strength,
                        status=FVGStatus.OPEN,
                        candle_index=i,
                        timestamp=pd.Timestamp(ts),
                        timeframe=timeframe,
                    ))

        # Update status of all FVGs based on current price action
        fvgs = self._update_statuses(fvgs, df)

        # Return only OPEN and PARTIALLY filled, most recent first
        active = [f for f in fvgs if f.status in [FVGStatus.OPEN, FVGStatus.PARTIALLY]]
        active.sort(key=lambda x: x.candle_index, reverse=True)

        return active

    def _rate_strength(self, gap_size: float, impulse_candle: pd.Series) -> str:
        """
        Rate FVG strength based on gap size and impulse candle body.
        Strong FVG = large gap + strong impulse candle body.
        """
        body_size = abs(impulse_candle["close"] - impulse_candle["open"])
        gap_pips = gap_size / self.pip_size

        if gap_pips >= 10 and body_size >= gap_size * 2:
            return "STRONG"
        elif gap_pips >= 5:
            return "MODERATE"
        else:
            return "WEAK"

    def _update_statuses(self, fvgs: list[FairValueGap], df: pd.DataFrame) -> list[FairValueGap]:
        """Update each FVG's fill status based on subsequent price action."""
        for fvg in fvgs:
            # Check candles AFTER the FVG formed
            subsequent = df.iloc[fvg.candle_index + 2:]
            if subsequent.empty:
                continue

            for _, candle in subsequent.iterrows():
                if fvg.kind == "BULLISH":
                    if candle["low"] <= fvg.bottom:
                        fvg.status = FVGStatus.FILLED
                        break
                    elif candle["low"] <= fvg.midpoint:
                        fvg.status = FVGStatus.MITIGATED
                    elif candle["low"] <= fvg.top:
                        fvg.status = FVGStatus.PARTIALLY

                elif fvg.kind == "BEARISH":
                    if candle["high"] >= fvg.top:
                        fvg.status = FVGStatus.FILLED
                        break
                    elif candle["high"] >= fvg.midpoint:
                        fvg.status = FVGStatus.MITIGATED
                    elif candle["high"] >= fvg.bottom:
                        fvg.status = FVGStatus.PARTIALLY

        return fvgs

    def get_entry_fvg(
        self,
        fvgs: list[FairValueGap],
        direction: str,
        current_price: float
    ) -> Optional[FairValueGap]:
        """
        Get the best FVG to use as entry zone for a given direction.
        For LONG: find nearest BULLISH FVG below current price
        For SHORT: find nearest BEARISH FVG above current price
        """
        candidates = []

        for fvg in fvgs:
            if fvg.status == FVGStatus.FILLED:
                continue

            if direction == "LONG" and fvg.kind == "BULLISH":
                # FVG is below us (or price just entering from top) — allow proximity window
                if fvg.top < current_price + self.proximity:
                    candidates.append(fvg)

            elif direction == "SHORT" and fvg.kind == "BEARISH":
                # FVG is above us (or price just entering from bottom) — allow proximity window
                if fvg.bottom > current_price - self.proximity:
                    candidates.append(fvg)

        if not candidates:
            return None

        # Return the nearest one
        if direction == "LONG":
            return max(candidates, key=lambda x: x.top)    # Highest below price
        else:
            return min(candidates, key=lambda x: x.bottom) # Lowest above price

    def is_price_in_fvg(self, price: float, fvg: FairValueGap) -> bool:
        """Check if a given price is currently inside an FVG zone."""
        return fvg.bottom <= price <= fvg.top

    def get_confluence_fvgs(
        self,
        m5_fvgs: list[FairValueGap],
        m15_fvgs: list[FairValueGap],
        direction: str,
        current_price: float,
        pip_size: float = 0.0001,
        overlap_threshold_pips: float = 5.0,
    ) -> dict:
        """
        Check if M5 and M15 FVGs overlap — this is extremely high confluence.
        Overlapping FVGs from multiple timeframes = institutional zone.
        overlap_threshold_pips supplied by InstrumentProfile.mtf_overlap_threshold_pips.
        """
        overlap_threshold = overlap_threshold_pips * pip_size

        m5_entry  = self.get_entry_fvg(m5_fvgs, direction, current_price)
        m15_entry = self.get_entry_fvg(m15_fvgs, direction, current_price)

        has_confluence = False
        overlap_zone = None

        if m5_entry and m15_entry:
            # Check if zones overlap
            overlap_top    = min(m5_entry.top, m15_entry.top)
            overlap_bottom = max(m5_entry.bottom, m15_entry.bottom)

            if overlap_top >= overlap_bottom - overlap_threshold:
                has_confluence = True
                overlap_zone = {
                    "top": overlap_top,
                    "bottom": overlap_bottom,
                    "midpoint": (overlap_top + overlap_bottom) / 2,
                }

        return {
            "has_confluence": has_confluence,
            "m5_fvg": m5_entry,
            "m15_fvg": m15_entry,
            "overlap_zone": overlap_zone,
            "strength": "VERY_STRONG" if has_confluence else (
                "MODERATE" if m5_entry else "NONE"
            )
        }
