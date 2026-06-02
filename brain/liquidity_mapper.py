"""
APEX TRADER — Liquidity Mapper
Finds where buy/sell stops are resting.
Equal highs/lows, stop clusters, liquidity voids.
The bot sees the traps before price springs them.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional


@dataclass
class LiquidityZone:
    price: float
    kind: str           # "BUY_SIDE" or "SELL_SIDE"
    strength: str       # "WEAK", "MODERATE", "STRONG"
    touches: int        # How many times price touched this level
    equal_count: int    # How many equal highs/lows cluster here
    swept: bool         # Has this been swept already?
    timestamp: pd.Timestamp


@dataclass
class LiquidityMap:
    buy_side_liquidity: list[LiquidityZone]   # Above price — buy stops
    sell_side_liquidity: list[LiquidityZone]  # Below price — sell stops
    nearest_buy_liq: Optional[LiquidityZone]
    nearest_sell_liq: Optional[LiquidityZone]
    liquidity_bias: str   # "BUY_SIDE_SWEEP_LIKELY" | "SELL_SIDE_SWEEP_LIKELY" | "NEUTRAL"
    current_price: float


class LiquidityMapper:
    """
    Maps where institutional stop clusters are hiding.
    A professional trader always knows where the liquidity is
    before entering a trade — because price hunts liquidity.
    """

    def __init__(self, equal_threshold_pips: float = 3.0, min_touches: int = 2):
        self.equal_threshold = equal_threshold_pips * 0.0001  # Convert pips to price
        self.min_touches = min_touches

    def map(self, df: pd.DataFrame, pip_size: float = 0.0001) -> LiquidityMap:
        """
        Full liquidity mapping on OHLC data.
        Returns all buy/sell side liquidity zones.
        """
        self.equal_threshold = 3.0 * pip_size

        df = df.copy().reset_index(drop=True)
        current_price = df["close"].iloc[-1]

        # Find equal highs (sell side liquidity — stops above)
        equal_highs = self._find_equal_levels(df, "high")

        # Find equal lows (buy side liquidity — stops below)
        equal_lows = self._find_equal_levels(df, "low")

        # Find swing-based liquidity (more prominent levels)
        swing_highs = self._find_swing_liquidity(df, "high")
        swing_lows  = self._find_swing_liquidity(df, "low")

        # Build zones
        buy_side  = []  # Above price
        sell_side = []  # Below price

        for zone in equal_highs + swing_highs:
            if zone.price > current_price:
                buy_side.append(zone)

        for zone in equal_lows + swing_lows:
            if zone.price < current_price:
                sell_side.append(zone)

        # Sort by proximity
        buy_side.sort(key=lambda x: x.price)   # Nearest first (lowest above price)
        sell_side.sort(key=lambda x: x.price, reverse=True)  # Nearest first (highest below price)

        # Determine liquidity bias
        bias = self._determine_bias(df, buy_side, sell_side, current_price)

        return LiquidityMap(
            buy_side_liquidity=buy_side,
            sell_side_liquidity=sell_side,
            nearest_buy_liq=buy_side[0] if buy_side else None,
            nearest_sell_liq=sell_side[0] if sell_side else None,
            liquidity_bias=bias,
            current_price=current_price,
        )

    def _find_equal_levels(self, df: pd.DataFrame, column: str) -> list[LiquidityZone]:
        """
        Find equal highs or equal lows within threshold.
        These are the stop clusters institutions hunt.
        """
        values = df[column].values
        timestamps = df["time"].values if "time" in df.columns else [pd.Timestamp.now()] * len(df)
        zones = []
        used = set()

        for i in range(len(values)):
            if i in used:
                continue

            cluster = [i]
            for j in range(i + 1, len(values)):
                if j in used:
                    continue
                if abs(values[i] - values[j]) <= self.equal_threshold:
                    cluster.append(j)
                    used.add(j)

            if len(cluster) >= self.min_touches:
                avg_price = np.mean([values[k] for k in cluster])
                strength = (
                    "STRONG"   if len(cluster) >= 4 else
                    "MODERATE" if len(cluster) == 3 else
                    "WEAK"
                )
                kind = "BUY_SIDE" if column == "high" else "SELL_SIDE"
                zones.append(LiquidityZone(
                    price=avg_price,
                    kind=kind,
                    strength=strength,
                    touches=len(cluster),
                    equal_count=len(cluster),
                    swept=False,
                    timestamp=pd.Timestamp(timestamps[cluster[-1]])
                ))
            used.add(i)

        return zones

    def _find_swing_liquidity(self, df: pd.DataFrame, column: str) -> list[LiquidityZone]:
        """
        Find significant swing high/low liquidity pools.
        These are cleaner stop clusters from obvious swing points.
        """
        values = df[column].values
        timestamps = df["time"].values if "time" in df.columns else [pd.Timestamp.now()] * len(df)
        zones = []
        lookback = 5

        for i in range(lookback, len(values) - lookback):
            window = values[i - lookback: i + lookback + 1]

            if column == "high" and values[i] == max(window):
                # Significant swing high — buy stops rest above this
                touches = self._count_touches(df, values[i], column)
                zones.append(LiquidityZone(
                    price=values[i],
                    kind="BUY_SIDE",
                    strength="STRONG" if touches >= 3 else "MODERATE",
                    touches=touches,
                    equal_count=1,
                    swept=False,
                    timestamp=pd.Timestamp(timestamps[i])
                ))

            elif column == "low" and values[i] == min(window):
                # Significant swing low — sell stops rest below this
                touches = self._count_touches(df, values[i], column)
                zones.append(LiquidityZone(
                    price=values[i],
                    kind="SELL_SIDE",
                    strength="STRONG" if touches >= 3 else "MODERATE",
                    touches=touches,
                    equal_count=1,
                    swept=False,
                    timestamp=pd.Timestamp(timestamps[i])
                ))

        return zones

    def _count_touches(self, df: pd.DataFrame, level: float, column: str) -> int:
        """Count how many candles touched near this level."""
        return int(((df[column] - level).abs() <= self.equal_threshold * 2).sum())

    def _determine_bias(
        self,
        df: pd.DataFrame,
        buy_side: list,
        sell_side: list,
        current_price: float
    ) -> str:
        """
        Which side is more likely to get swept next?
        Price always moves toward the most liquidity.
        """
        if not buy_side and not sell_side:
            return "NEUTRAL"

        # Score each side by strength and proximity
        buy_score  = self._score_side(buy_side,  current_price, above=True)
        sell_score = self._score_side(sell_side, current_price, above=False)

        if buy_score > sell_score * 1.3:
            return "BUY_SIDE_SWEEP_LIKELY"    # Price likely to sweep above first
        elif sell_score > buy_score * 1.3:
            return "SELL_SIDE_SWEEP_LIKELY"   # Price likely to sweep below first
        else:
            return "NEUTRAL"

    def _score_side(self, zones: list, current_price: float, above: bool) -> float:
        """Score a liquidity side based on strength and proximity."""
        score = 0.0
        strength_map = {"STRONG": 3, "MODERATE": 2, "WEAK": 1}

        for zone in zones[:3]:  # Only look at 3 nearest
            strength_val = strength_map.get(zone.strength, 1)
            distance = abs(zone.price - current_price)
            proximity_bonus = 1.0 / (distance + 0.0001)  # Closer = higher score
            score += strength_val * proximity_bonus * zone.touches

        return score

    def detect_sweep(self, df: pd.DataFrame, zone: LiquidityZone, pip_size: float = 0.0001) -> bool:
        """
        Detect if a liquidity zone has just been swept.
        A sweep = price briefly pierces the level then reverses.
        This is the entry signal we've been waiting for.
        """
        if len(df) < 3:
            return False

        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]
        threshold = 2 * pip_size

        if zone.kind == "BUY_SIDE":
            # Sweep above: wick went above level, close came back below
            wick_pierced = last_candle["high"] >= zone.price - threshold
            closed_below = last_candle["close"] < zone.price
            was_below_before = prev_candle["close"] < zone.price
            return wick_pierced and closed_below and was_below_before

        elif zone.kind == "SELL_SIDE":
            # Sweep below: wick went below level, close came back above
            wick_pierced = last_candle["low"] <= zone.price + threshold
            closed_above = last_candle["close"] > zone.price
            was_above_before = prev_candle["close"] > zone.price
            return wick_pierced and closed_above and was_above_before

        return False

    def get_nearest_liquidity(
        self, liq_map: LiquidityMap, direction: str
    ) -> Optional[LiquidityZone]:
        """
        Get the nearest liquidity target in the trade direction.
        Used for setting Take Profit levels.
        direction: "LONG" or "SHORT"
        """
        if direction == "LONG":
            return liq_map.nearest_buy_liq   # TP targets buy-side liquidity above
        else:
            return liq_map.nearest_sell_liq  # TP targets sell-side liquidity below
