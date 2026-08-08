"""
APEX TRADER — Order Block Detector
Finds where institutions placed their orders.
Order blocks are the last opposing candle before a strong move.
Price returns to these zones to fill remaining orders.
"""

import pandas as pd
from dataclasses import dataclass
from typing import Optional
from enum import Enum


class OBStatus(Enum):
    FRESH     = "FRESH"      # Never been tapped
    TESTED    = "TESTED"     # Tapped once, still valid
    MITIGATED = "MITIGATED"  # Price entered the zone
    BROKEN    = "BROKEN"     # Price closed through it — invalid


@dataclass
class OrderBlock:
    kind: str           # "BULLISH" or "BEARISH"
    top: float          # Upper boundary
    bottom: float       # Lower boundary
    midpoint: float     # 50% level — optimal entry
    origin_index: int   # Candle that created this OB
    strength: str       # "WEAK", "MODERATE", "STRONG"
    status: OBStatus
    impulse_size: float # Size of the move it caused (pips)
    timestamp: pd.Timestamp
    timeframe: str
    breaker: bool       # True if this is a breaker block (failed OB)


class OrderBlockDetector:
    """
    Detects Order Blocks — the footprints of institutional trading.

    Bullish Order Block:
    The last bearish candle before a strong bullish impulse move.
    Institutions left buy orders here. Price returns to fill them.

    Bearish Order Block:
    The last bullish candle before a strong bearish impulse move.
    Institutions left sell orders here. Price returns to fill them.

    Breaker Block:
    A failed order block that price broke through.
    Now acts as the OPPOSITE type — strong reversal zone.
    """

    def __init__(
        self,
        min_impulse_pips: float = 10.0,
        pip_size: float = 0.0001,
        lookback: int = 50,
        buffer_pips: float = 2.0,
    ):
        self.min_impulse = min_impulse_pips * pip_size
        self.pip_size = pip_size
        self.lookback = lookback
        # buffer_pips supplied by InstrumentProfile — wider for indices/synthetics
        self.buffer = buffer_pips * pip_size

    def detect(self, df: pd.DataFrame, timeframe: str = "H1") -> list[OrderBlock]:
        """
        Detect all valid order blocks on the dataframe.
        Returns list sorted by recency (most recent first).
        """
        if len(df) < 5:
            return []

        df = df.copy().reset_index(drop=True)
        obs = []

        # Cache columns as numpy arrays — scalar numpy indexing is far cheaper
        # than df.iloc[] row access. Detection logic is identical.
        opens = df["open"].values
        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        has_time = "time" in df.columns
        times = df["time"].values if has_time else None
        n = len(df)

        # Only look at recent candles
        start = max(0, n - self.lookback)

        for i in range(start + 1, n - 1):
            c_open = opens[i]
            c_close = closes[i]
            ts = times[i] if has_time else pd.Timestamp.now()

            # Bullish OB: bearish candle followed by strong bullish move
            if c_close < c_open:  # Bearish candle
                impulse = self._measure_impulse_values(closes, highs, lows, i, "up")
                if impulse >= self.min_impulse:
                    strength = self._rate_strength_values(
                        c_open, c_close, highs[i], lows[i], impulse
                    )
                    ob = OrderBlock(
                        kind="BULLISH",
                        top=c_open,         # Top of bearish candle body
                        bottom=c_close,     # Bottom of bearish candle body
                        midpoint=(c_open + c_close) / 2,
                        origin_index=i,
                        strength=strength,
                        status=OBStatus.FRESH,
                        impulse_size=round(impulse / self.pip_size, 1),
                        timestamp=pd.Timestamp(ts),
                        timeframe=timeframe,
                        breaker=False,
                    )
                    obs.append(ob)

            # Bearish OB: bullish candle followed by strong bearish move
            elif c_close > c_open:  # Bullish candle
                impulse = self._measure_impulse_values(closes, highs, lows, i, "down")
                if impulse >= self.min_impulse:
                    strength = self._rate_strength_values(
                        c_open, c_close, highs[i], lows[i], impulse
                    )
                    ob = OrderBlock(
                        kind="BEARISH",
                        top=c_close,        # Top of bullish candle body
                        bottom=c_open,      # Bottom of bullish candle body
                        midpoint=(c_close + c_open) / 2,
                        origin_index=i,
                        strength=strength,
                        status=OBStatus.FRESH,
                        impulse_size=round(impulse / self.pip_size, 1),
                        timestamp=pd.Timestamp(ts),
                        timeframe=timeframe,
                        breaker=False,
                    )
                    obs.append(ob)

        # Update statuses
        obs = self._update_statuses(obs, df)

        # Detect breaker blocks from broken OBs
        breakers = self._detect_breakers(obs, df, timeframe)
        obs.extend(breakers)

        # Return only valid ones, most recent first
        valid = [o for o in obs if o.status not in [OBStatus.BROKEN]]
        valid.sort(key=lambda x: x.origin_index, reverse=True)

        return valid

    def _measure_impulse(self, df: pd.DataFrame, origin: int, direction: str) -> float:
        """
        Measure the size of the move caused by this candle.
        Look forward up to 5 candles for the extent of the move.
        """
        return self._measure_impulse_values(
            df["close"].values, df["high"].values, df["low"].values, origin, direction
        )

    def _measure_impulse_values(self, closes, highs, lows, origin: int, direction: str) -> float:
        """Value-based core of :meth:`_measure_impulse` (no df.iloc access)."""
        n = len(closes)
        if origin >= n - 1:
            return 0.0

        origin_price = closes[origin]
        end = min(origin + 6, n)

        if direction == "up":
            peak = highs[origin + 1: end].max()
            return max(peak - origin_price, 0)
        else:
            trough = lows[origin + 1: end].min()
            return max(origin_price - trough, 0)

    def _rate_strength(self, candle: pd.Series, impulse: float) -> str:
        """Rate order block strength based on candle and impulse."""
        return self._rate_strength_values(
            candle["open"], candle["close"], candle["high"], candle["low"], impulse
        )

    def _rate_strength_values(self, c_open, c_close, c_high, c_low, impulse: float) -> str:
        """Value-based core of :meth:`_rate_strength` (no pd.Series access)."""
        body = abs(c_close - c_open)
        wick = (c_high - c_low) - body
        impulse_pips = impulse / self.pip_size

        if impulse_pips >= 30 and wick < body:
            return "STRONG"
        elif impulse_pips >= 15:
            return "MODERATE"
        else:
            return "WEAK"

    def _update_statuses(self, obs: list[OrderBlock], df: pd.DataFrame) -> list[OrderBlock]:
        """Update each OB status based on subsequent price action.

        Vectorized equivalent of the original per-candle loop. Semantics are
        preserved exactly —
          * if any subsequent candle closes through the block it is BROKEN;
          * otherwise the touch count of the zone decides the status:
            0 touches → FRESH (unchanged), 1 touch → TESTED, 2+ → MITIGATED.
        """
        lows = df["low"].values
        highs = df["high"].values

        for ob in obs:
            start = ob.origin_index + 1
            if start >= len(df):
                continue

            if ob.kind == "BULLISH":
                sub_lows = lows[start:]
                if (sub_lows <= ob.bottom).any():
                    ob.status = OBStatus.BROKEN
                    continue
                touches = int((sub_lows <= ob.top).sum())
                if touches >= 2:
                    ob.status = OBStatus.MITIGATED
                elif touches == 1:
                    ob.status = OBStatus.TESTED

            elif ob.kind == "BEARISH":
                sub_highs = highs[start:]
                if (sub_highs >= ob.top).any():
                    ob.status = OBStatus.BROKEN
                    continue
                touches = int((sub_highs >= ob.bottom).sum())
                if touches >= 2:
                    ob.status = OBStatus.MITIGATED
                elif touches == 1:
                    ob.status = OBStatus.TESTED

        return obs

    def _detect_breakers(
        self, obs: list[OrderBlock], df: pd.DataFrame, timeframe: str
    ) -> list[OrderBlock]:
        """
        Detect breaker blocks — failed OBs that flip polarity.
        A broken bullish OB becomes a bearish breaker (resistance).
        A broken bearish OB becomes a bullish breaker (support).
        These are extremely powerful reversal zones.
        """
        breakers = []

        for ob in obs:
            if ob.status != OBStatus.BROKEN:
                continue

            # Flip the kind
            new_kind = "BEARISH" if ob.kind == "BULLISH" else "BULLISH"

            breakers.append(OrderBlock(
                kind=new_kind,
                top=ob.top,
                bottom=ob.bottom,
                midpoint=ob.midpoint,
                origin_index=ob.origin_index,
                strength="STRONG",  # Breakers are always strong
                status=OBStatus.FRESH,
                impulse_size=ob.impulse_size,
                timestamp=ob.timestamp,
                timeframe=timeframe,
                breaker=True,
            ))

        return breakers

    def get_entry_ob(
        self,
        obs: list[OrderBlock],
        direction: str,
        current_price: float
    ) -> Optional[OrderBlock]:
        """
        Get the best order block for entry.
        For LONG: nearest bullish OB below current price
        For SHORT: nearest bearish OB above current price
        """
        candidates = []

        for ob in obs:
            if ob.status == OBStatus.BROKEN:
                continue

            if direction == "LONG" and ob.kind == "BULLISH":
                if ob.top < current_price:
                    candidates.append(ob)

            elif direction == "SHORT" and ob.kind == "BEARISH":
                if ob.bottom > current_price:
                    candidates.append(ob)

        if not candidates:
            return None

        # Prefer FRESH > TESTED > MITIGATED
        status_priority = {OBStatus.FRESH: 3, OBStatus.TESTED: 2, OBStatus.MITIGATED: 1}
        candidates.sort(key=lambda x: (
            status_priority.get(x.status, 0),
            -abs(x.midpoint - current_price)  # Closer to price = better
        ), reverse=True)

        return candidates[0]

    def is_price_at_ob(self, price: float, ob: OrderBlock, pip_size: float = 0.0001) -> bool:
        """Check if price is currently at/inside an order block."""
        # Uses self.buffer set from InstrumentProfile.ob_buffer_pips at construction
        return (ob.bottom - self.buffer) <= price <= (ob.top + self.buffer)

    def get_confluence_with_fvg(
        self,
        ob: OrderBlock,
        fvg,  # FairValueGap
        pip_size: float = 0.0001
    ) -> bool:
        """
        Check if an OB and FVG overlap — extremely high confluence zone.
        OB + FVG overlap = institutional confirmation area.
        """
        overlap_threshold = 3 * pip_size
        overlap_top    = min(ob.top, fvg.top)
        overlap_bottom = max(ob.bottom, fvg.bottom)
        return overlap_top >= overlap_bottom - overlap_threshold
