"""
APEX TRADER — Structure Engine
Reads market structure like a 100-year veteran.
Detects: trend direction, swing highs/lows, BOS, CHOCH
"""

import pandas as pd
from loguru import logger
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from brain.market_data_utils import drop_forming_bar


class Trend(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGING = "RANGING"


class StructureEvent(Enum):
    BOS_BULLISH   = "BOS_BULLISH"    # Break of structure — bullish continuation
    BOS_BEARISH   = "BOS_BEARISH"    # Break of structure — bearish continuation
    CHOCH_BULLISH = "CHOCH_BULLISH"  # Change of character — reversal to bullish
    CHOCH_BEARISH = "CHOCH_BEARISH"  # Change of character — reversal to bearish
    NONE          = "NONE"


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str  # "HH", "HL", "LH", "LL"
    timestamp: pd.Timestamp


@dataclass
class StructureAnalysis:
    trend: Trend
    last_event: StructureEvent
    swing_high: Optional[float]
    swing_low: Optional[float]
    last_bos_level: Optional[float]
    last_choch_level: Optional[float]
    structure_broken: bool
    bullish_swing_points: list
    bearish_swing_points: list
    confidence: float  # 0.0 to 1.0


class StructureEngine:
    """
    Reads market structure the way a professional trader does.
    Identifies HH/HL/LH/LL, detects BOS and CHOCH events.
    """

    def __init__(self, swing_lookback: int = 5, min_swing_size_pips: float = 3.0,
                 pip_size: float = 0.0001):
        self.swing_lookback = swing_lookback  # candles each side to confirm swing
        self.min_swing_size = min_swing_size_pips * pip_size

    def analyze(self, df: pd.DataFrame) -> StructureAnalysis:
        """
        Full structure analysis on a DataFrame.
        Expects columns: open, high, low, close, time
        Returns StructureAnalysis dataclass.
        """
        if len(df) < self.swing_lookback * 2 + 1:
            logger.warning("Not enough candles for structure analysis")
            return self._empty_analysis()

        df = df.copy().reset_index(drop=True)

        # Step 1: Find all swing highs and lows
        swings = self._find_swings(df)

        if len(swings) < 4:
            return self._empty_analysis()

        # Step 2: Label them HH, HL, LH, LL
        labeled = self._label_swings(swings)

        # Step 3: Determine trend
        trend = self._determine_trend(labeled)

        # Step 4: Detect BOS / CHOCH
        event, event_level = self._detect_structure_event(df, labeled, trend)

        # Step 5: Get current swing high/low
        highs = [s for s in labeled if s.kind in ["HH", "LH"]]
        lows  = [s for s in labeled if s.kind in ["HL", "LL"]]

        swing_high = highs[-1].price if highs else None
        swing_low  = lows[-1].price  if lows  else None

        # Step 6: Calculate confidence
        confidence = self._calculate_confidence(labeled, trend, event)

        return StructureAnalysis(
            trend=trend,
            last_event=event,
            swing_high=swing_high,
            swing_low=swing_low,
            last_bos_level=event_level if "BOS" in event.value else None,
            last_choch_level=event_level if "CHOCH" in event.value else None,
            structure_broken="BOS" in event.value or "CHOCH" in event.value,
            bullish_swing_points=[s for s in labeled if s.kind in ["HH", "HL"]],
            bearish_swing_points=[s for s in labeled if s.kind in ["LH", "LL"]],
            confidence=confidence,
        )

    def _find_swings(self, df: pd.DataFrame) -> list:
        """Find pivot highs and lows using lookback window."""
        swings = []
        n = len(df)
        lb = self.swing_lookback

        for i in range(lb, n - lb):
            # Swing High: highest point in window
            window_highs = df["high"].iloc[i - lb: i + lb + 1]
            if df["high"].iloc[i] == window_highs.max():
                # Filter by prominence: how far this pivot stands above neighbours
                nbr = list(window_highs.iloc[:lb]) + list(window_highs.iloc[lb + 1:])
                prominence = (df["high"].iloc[i] - max(nbr)) if nbr else 0.0
                if prominence >= self.min_swing_size:
                    swings.append({
                        "index": i,
                        "price": df["high"].iloc[i],
                        "type": "HIGH",
                        "timestamp": df["time"].iloc[i] if "time" in df.columns else pd.Timestamp.now()
                    })

            # Swing Low: lowest point in window
            window_lows = df["low"].iloc[i - lb: i + lb + 1]
            if df["low"].iloc[i] == window_lows.min():
                nbr = list(window_lows.iloc[:lb]) + list(window_lows.iloc[lb + 1:])
                prominence = (min(nbr) - df["low"].iloc[i]) if nbr else 0.0
                if prominence >= self.min_swing_size:
                    swings.append({
                        "index": i,
                        "price": df["low"].iloc[i],
                        "type": "LOW",
                        "timestamp": df["time"].iloc[i] if "time" in df.columns else pd.Timestamp.now()
                    })

        # Sort by index and remove duplicates
        swings = sorted(swings, key=lambda x: x["index"])
        return self._remove_duplicate_swings(swings)

    def _remove_duplicate_swings(self, swings: list) -> list:
        """Remove consecutive same-type swings, keep the more extreme one."""
        if not swings:
            return []
        cleaned = [swings[0]]
        for s in swings[1:]:
            if s["type"] == cleaned[-1]["type"]:
                # Keep the more extreme value
                if s["type"] == "HIGH" and s["price"] > cleaned[-1]["price"]:
                    cleaned[-1] = s
                elif s["type"] == "LOW" and s["price"] < cleaned[-1]["price"]:
                    cleaned[-1] = s
            else:
                cleaned.append(s)
        return cleaned

    def _label_swings(self, swings: list) -> list[SwingPoint]:
        """Label swings as HH, HL, LH, LL based on sequence."""
        labeled = []
        highs = [s for s in swings if s["type"] == "HIGH"]
        lows  = [s for s in swings if s["type"] == "LOW"]

        # Label highs
        for i, h in enumerate(highs):
            if i == 0:
                kind = "HH"  # First high — assume HH
            else:
                kind = "HH" if h["price"] > highs[i-1]["price"] else "LH"
            labeled.append(SwingPoint(
                index=h["index"], price=h["price"],
                kind=kind, timestamp=h["timestamp"]
            ))

        # Label lows
        for i, l in enumerate(lows):
            if i == 0:
                kind = "HL"  # First low — assume HL
            else:
                kind = "HL" if l["price"] > lows[i-1]["price"] else "LL"
            labeled.append(SwingPoint(
                index=l["index"], price=l["price"],
                kind=kind, timestamp=l["timestamp"]
            ))

        # Sort by index
        labeled.sort(key=lambda x: x.index)
        return labeled

    def _determine_trend(self, labeled: list[SwingPoint]) -> Trend:
        """Determine trend from the last 4+ swing points."""
        if len(labeled) < 4:
            return Trend.RANGING

        recent = labeled[-6:]  # Last 6 swing points
        highs = [s for s in recent if s.kind in ["HH", "LH"]]
        lows  = [s for s in recent if s.kind in ["HL", "LL"]]

        if not highs or not lows:
            return Trend.RANGING

        # Bullish: HH + HL pattern
        bullish_highs = sum(1 for s in highs if s.kind == "HH")
        bullish_lows  = sum(1 for s in lows  if s.kind == "HL")

        # Bearish: LH + LL pattern
        bearish_highs = sum(1 for s in highs if s.kind == "LH")
        bearish_lows  = sum(1 for s in lows  if s.kind == "LL")

        bullish_score = bullish_highs + bullish_lows
        bearish_score = bearish_highs + bearish_lows

        if bullish_score > bearish_score + 1:
            return Trend.BULLISH
        elif bearish_score > bullish_score + 1:
            return Trend.BEARISH
        else:
            return Trend.RANGING

    def _detect_structure_event(
        self, df: pd.DataFrame, labeled: list[SwingPoint], trend: Trend
    ) -> tuple[StructureEvent, Optional[float]]:
        """
        Detect the most recent BOS or CHOCH.
        BOS = continuation of trend breaking the last swing point
        CHOCH = counter-trend break signaling potential reversal

        Requires body confirmation: the bar's full body (open AND close)
        must be beyond the level, OR the previous bar also closed beyond it.
        Single-bar wicks that spike through a level and reverse are filtered.

        Uses the last CLOSED bar (not the forming bar) so that BOS/CHOCH
        signals cannot repaint mid-bar.
        """
        if len(labeled) < 3:
            return StructureEvent.NONE, None

        closed = drop_forming_bar(df)
        if len(closed) < 2:
            return StructureEvent.NONE, None

        last_close = float(closed["close"].iloc[-1])
        last_open = float(closed["open"].iloc[-1])
        prev_close = float(closed["close"].iloc[-2])
        recent_highs = [s for s in labeled if s.kind in ["HH", "LH"]]
        recent_lows  = [s for s in labeled if s.kind in ["HL", "LL"]]

        if not recent_highs or not recent_lows:
            return StructureEvent.NONE, None

        last_high = recent_highs[-1]
        last_low  = recent_lows[-1]

        # Check bullish break — close above swing high with body confirmation
        if last_close > last_high.price:
            body_confirmed = (
                last_open > last_high.price
                or prev_close > last_high.price
            )
            if body_confirmed:
                if trend == Trend.BULLISH:
                    return StructureEvent.BOS_BULLISH, last_high.price
                else:
                    return StructureEvent.CHOCH_BULLISH, last_high.price

        # Check bearish break — close below swing low with body confirmation
        if last_close < last_low.price:
            body_confirmed = (
                last_open < last_low.price
                or prev_close < last_low.price
            )
            if body_confirmed:
                if trend == Trend.BEARISH:
                    return StructureEvent.BOS_BEARISH, last_low.price
                else:
                    return StructureEvent.CHOCH_BEARISH, last_low.price

        return StructureEvent.NONE, None

    def _calculate_confidence(
        self,
        labeled: list[SwingPoint],
        trend: Trend,
        event: StructureEvent
    ) -> float:
        """Score the confidence of the structure reading (0.0 to 1.0)."""
        score = 0.0

        if trend != Trend.RANGING:
            score += 0.4  # Clear trend

        # Count consecutive aligned swing points
        if trend == Trend.BULLISH:
            aligned = sum(1 for s in labeled[-6:] if s.kind in ["HH", "HL"])
        elif trend == Trend.BEARISH:
            aligned = sum(1 for s in labeled[-6:] if s.kind in ["LH", "LL"])
        else:
            aligned = 0

        score += min(aligned * 0.1, 0.4)  # Up to 0.4 for alignment

        if event != StructureEvent.NONE:
            score += 0.2  # Recent structure event adds confidence

        return min(score, 1.0)

    def _empty_analysis(self) -> StructureAnalysis:
        return StructureAnalysis(
            trend=Trend.RANGING,
            last_event=StructureEvent.NONE,
            swing_high=None, swing_low=None,
            last_bos_level=None, last_choch_level=None,
            structure_broken=False,
            bullish_swing_points=[], bearish_swing_points=[],
            confidence=0.0,
        )

    def get_bias(
        self,
        h4_df: pd.DataFrame,
        h1_df: pd.DataFrame,
        d1_df: Optional[pd.DataFrame] = None,
    ) -> dict:
        """
        Combined D1 + H4 + H1 bias.
        D1 (when available) is the highest-priority directional authority.
        H4 gives the big picture direction. H1 gives precision.
        """
        h4 = self.analyze(h4_df)
        h1 = self.analyze(h1_df)
        d1 = self.analyze(d1_df) if d1_df is not None and len(d1_df) >= 5 else None

        d1_trend_val = d1.trend.value if d1 is not None else "UNKNOWN"
        d1_event_val = d1.last_event.value if d1 is not None else "NONE"
        d1_conf = d1.confidence if d1 is not None else 0.0

        if d1 is not None and d1.trend != Trend.RANGING:
            if h4.trend == d1.trend:
                bias_strength = "STRONG"
                direction = d1.trend
            elif h4.trend == Trend.RANGING:
                bias_strength = "STRONG"
                direction = d1.trend
            else:
                bias_strength = "MODERATE"
                direction = d1.trend
        elif h4.trend == h1.trend and h4.trend != Trend.RANGING:
            bias_strength = "STRONG"
            direction = h4.trend
        elif h4.trend != Trend.RANGING and h1.trend == Trend.RANGING:
            bias_strength = "MODERATE"
            direction = h4.trend
        elif h4.trend == Trend.RANGING and h1.trend != Trend.RANGING:
            bias_strength = "MODERATE"
            direction = h1.trend
        elif h4.trend != h1.trend:
            bias_strength = "CONFLICTED"
            direction = Trend.RANGING
        else:
            bias_strength = "NONE"
            direction = Trend.RANGING

        if d1 is not None:
            confidence = round(d1_conf * 0.4 + h4.confidence * 0.35 + h1.confidence * 0.25, 2)
        else:
            confidence = round((h4.confidence + h1.confidence) / 2, 2)

        return {
            "direction": direction.value,
            "d1_trend": d1_trend_val,
            "h4_trend": h4.trend.value,
            "h1_trend": h1.trend.value,
            "strength": bias_strength,
            "d1_event": d1_event_val,
            "h4_event": h4.last_event.value,
            "h1_event": h1.last_event.value,
            "swing_high": h1.swing_high,
            "swing_low": h1.swing_low,
            "confidence": confidence,
            "tradeable": bias_strength in ["STRONG", "MODERATE"],
        }
