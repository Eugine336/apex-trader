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
    # Observation only: which side currently holds the greater resting-liquidity
    # pressure (strength × proximity × touches). It does NOT predict the next
    # move or imply a trade direction — the Brain interprets it.
    dominant_liquidity_side: str   # "BUY_SIDE" | "SELL_SIDE" | "BALANCED"
    current_price: float


class LiquidityMapper:
    """
    Maps where institutional stop clusters are hiding.
    A professional trader always knows where the liquidity is
    before entering a trade — because price hunts liquidity.
    """

    def __init__(self, equal_threshold_pips: float = 3.0, min_touches: int = 2):
        # Store the threshold in PIPS, not a pre-converted price. The price
        # threshold is derived per call from the instrument's real pip_size
        # (passed to map()/classify_sweep_reaction()) — the old
        # ``equal_threshold_pips * 0.0001`` baked an FX-major pip in, silently
        # mis-scaling sweep/equal-level geometry on JPY pairs, metals, indices
        # and Deriv synthetics.
        self.equal_threshold_pips = float(equal_threshold_pips)
        self.min_touches = min_touches

    def map(self, df: pd.DataFrame, pip_size: float) -> LiquidityMap:
        """
        Full liquidity mapping on OHLC data.
        Returns all buy/sell side liquidity zones.
        """
        # Per-call threshold from the instrument's real pip_size (do NOT mutate
        # self — keeps map() thread-safe when different-pip-size symbols are
        # scanned concurrently).
        threshold = self.equal_threshold_pips * pip_size

        df = df.copy().reset_index(drop=True)
        current_price = df["close"].iloc[-1]

        # Find equal highs (sell side liquidity — stops above)
        equal_highs = self._find_equal_levels(df, "high", threshold)

        # Find equal lows (buy side liquidity — stops below)
        equal_lows = self._find_equal_levels(df, "low", threshold)

        # Find swing-based liquidity (more prominent levels)
        swing_highs = self._find_swing_liquidity(df, "high", threshold)
        swing_lows  = self._find_swing_liquidity(df, "low", threshold)

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

        # Observe which side currently holds the greater resting liquidity.
        dominant_side = self._dominant_liquidity_side(df, buy_side, sell_side, current_price)

        return LiquidityMap(
            buy_side_liquidity=buy_side,
            sell_side_liquidity=sell_side,
            nearest_buy_liq=buy_side[0] if buy_side else None,
            nearest_sell_liq=sell_side[0] if sell_side else None,
            dominant_liquidity_side=dominant_side,
            current_price=current_price,
        )

    def _find_equal_levels(self, df: pd.DataFrame, column: str, threshold: float | None = None) -> list[LiquidityZone]:
        """
        Find equal highs or equal lows within threshold.
        These are the stop clusters institutions hunt.

        The greedy clustering is identical to the original nested loop, but the
        inner scan over candidate candles is vectorized: for each unused anchor
        ``i`` we mask every later unused candle within ``thr`` in one numpy op
        instead of a Python ``for j`` loop.
        """
        if threshold is None:
            raise ValueError(
                "LiquidityMapper._find_equal_levels requires an explicit "
                "pip-derived threshold; call via map()/classify_sweep_reaction() "
                "with the instrument's real pip_size."
            )
        thr = threshold
        values = df[column].values
        timestamps = df["time"].values if "time" in df.columns else [pd.Timestamp.now()] * len(df)
        zones = []
        n = len(values)
        used = np.zeros(n, dtype=bool)

        for i in range(n):
            if used[i]:
                continue

            # Later, still-unused candles within threshold of the anchor.
            mask = (~used) & (np.abs(values - values[i]) <= thr)
            mask[: i + 1] = False
            js = np.nonzero(mask)[0]
            cluster = np.concatenate(([i], js)) if js.size else np.array([i])

            if len(cluster) >= self.min_touches:
                avg_price = float(np.mean(values[cluster]))
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

            used[js] = True
            used[i] = True

        return zones

    def _find_swing_liquidity(self, df: pd.DataFrame, column: str, threshold: float | None = None) -> list[LiquidityZone]:
        """
        Find significant swing high/low liquidity pools.
        These are cleaner stop clusters from obvious swing points.
        """
        values = df[column].values
        timestamps = df["time"].values if "time" in df.columns else [pd.Timestamp.now()] * len(df)
        zones = []
        lookback = 5
        # Touch counts reuse the cached numpy column instead of a per-swing
        # pandas reduction (the original called _count_touches(df, ...) which
        # rebuilt a Series each time — the dominant cost in liquidity mapping).
        if threshold is None:
            raise ValueError(
                "LiquidityMapper._find_swing_liquidity requires an explicit "
                "pip-derived threshold; pass pip_size via map()."
            )
        thr2 = threshold * 2

        for i in range(lookback, len(values) - lookback):
            window = values[i - lookback: i + lookback + 1]

            if column == "high" and values[i] == max(window):
                # Significant swing high — buy stops rest above this
                touches = int((np.abs(values - values[i]) <= thr2).sum())
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
                touches = int((np.abs(values - values[i]) <= thr2).sum())
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

    def _count_touches(self, df: pd.DataFrame, level: float, column: str, threshold: float | None = None) -> int:
        """Count how many candles touched near this level."""
        if threshold is None:
            raise ValueError(
                "LiquidityMapper._count_touches requires an explicit "
                "pip-derived threshold; pass pip_size via map()."
            )
        thr = threshold
        return int(((df[column] - level).abs() <= thr * 2).sum())

    def _dominant_liquidity_side(
        self,
        df: pd.DataFrame,
        buy_side: list,
        sell_side: list,
        current_price: float
    ) -> str:
        """
        Observe which side currently holds the greater resting liquidity.

        Measures each side by strength × proximity × touches and reports the
        dominant side as an observation. This is a measurement of where stops
        are resting — not a prediction of the next move and not a trade
        direction; the Brain interprets it.
        """
        if not buy_side and not sell_side:
            return "BALANCED"

        # Score each side by strength and proximity
        buy_score  = self._score_side(buy_side,  current_price, above=True)
        sell_score = self._score_side(sell_side, current_price, above=False)

        if buy_score > sell_score * 1.3:
            return "BUY_SIDE"     # Greater resting liquidity above price
        elif sell_score > buy_score * 1.3:
            return "SELL_SIDE"    # Greater resting liquidity below price
        else:
            return "BALANCED"

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

    def classify_sweep_reaction(
        self, df: pd.DataFrame, pip_size: float,
        reaction_window: int = 3,
    ) -> tuple[str, str, float]:
        """
        Classify the most recent liquidity interaction from observed price action.

        Maps zones from bars BEFORE the reaction window (so zones reflect
        pre-reaction state), then inspects the last *reaction_window* bars for
        a pierce-then-reaction sequence across multiple candles.

        Returns (event, reaction, confidence) — all observations, never a trade
        call:
            event:      "REVERSAL" | "CONTINUATION" | "NONE"
                        (the observed liquidity-interaction event)
            reaction:   "UP" | "DOWN" | "NONE"
                        (the observed direction of the price reaction — a
                         measurement of what price did, NOT a LONG/SHORT vote;
                         the Brain decides any trade direction)
            confidence: 0.0 .. 1.0 (reaction magnitude relative to ATR)
        """
        try:
            if reaction_window < 1:
                reaction_window = 3

            min_bars = max(5, reaction_window + 3)
            if df is None or len(df) < min_bars:
                return ("NONE", "NONE", 0.0)

            close_vals = df["close"].values
            if np.isnan(close_vals[-1]):
                return ("NONE", "NONE", 0.0)

            pre_reaction = df.iloc[:-reaction_window]
            if len(pre_reaction) < 3:
                return ("NONE", "NONE", 0.0)

            threshold = self.equal_threshold_pips * pip_size
            equal_highs = self._find_equal_levels(pre_reaction, "high", threshold)
            equal_lows = self._find_equal_levels(pre_reaction, "low", threshold)
            swing_highs = self._find_swing_liquidity(pre_reaction, "high", threshold)
            swing_lows = self._find_swing_liquidity(pre_reaction, "low", threshold)
            all_zones = equal_highs + swing_highs + equal_lows + swing_lows

            if not all_zones:
                return ("NONE", "NONE", 0.0)

            atr = self._recent_atr(df, pip_size)
            if atr <= 0:
                return ("NONE", "NONE", 0.0)

            window_df = df.iloc[-reaction_window:]
            anchor_close = df.iloc[-(reaction_window + 1)]["close"]

            best_kind = "NONE"
            best_reaction = "NONE"
            best_conf = 0.0

            for zone in all_zones:
                kind, reaction, conf = self._classify_zone_reaction(
                    window_df, zone, pip_size, atr, anchor_close,
                )
                if conf > best_conf:
                    best_kind = kind
                    best_reaction = reaction
                    best_conf = conf

            return (best_kind, best_reaction, best_conf)

        except Exception as exc:
            from loguru import logger
            logger.warning("[liquidity] classify_sweep_reaction failed, returning NONE: {}", exc)
            return ("NONE", "NONE", 0.0)

    def _classify_zone_reaction(
        self,
        window_df: pd.DataFrame,
        zone: LiquidityZone,
        pip_size: float,
        atr: float,
        anchor_close: float,
    ) -> tuple[str, str, float]:
        """Classify the reaction to a single zone across a multi-bar window.

        Scans the window for: (a) a pierce of the zone, then (b) on a
        subsequent or same bar, a reversal (close back on original side)
        or continuation (close beyond with displacement).  Returns the
        highest-confidence ``(event, reaction, confidence)`` observation, where
        ``reaction`` is the observed price-reaction direction ("UP"/"DOWN"/
        "NONE") — a measurement of what price did, not a trade direction.
        """
        threshold = 2 * pip_size
        highs = window_df["high"].values
        lows = window_df["low"].values
        opens = window_df["open"].values
        closes = window_df["close"].values
        n = len(window_df)

        best = ("NONE", "NONE", 0.0)

        if zone.kind == "BUY_SIDE":
            anchor_below = anchor_close < zone.price
            if not anchor_below:
                return best

            pierce_idx = -1
            for i in range(n):
                if highs[i] >= zone.price - threshold:
                    pierce_idx = i
                    break

            if pierce_idx < 0:
                return best

            wick_above = float(max(highs[pierce_idx:]) - zone.price)
            last_close = float(closes[-1])

            if last_close < zone.price:
                conf = min(max(wick_above / atr, 0.0), 1.0) if atr > 0 else 0.0
                bars_to_reclaim = n - pierce_idx
                if bars_to_reclaim > 1:
                    speed_bonus = max(0.0, 0.1 * (3 - bars_to_reclaim))
                    conf = min(conf + speed_bonus, 1.0)
                conf = max(conf, 0.3)
                best = ("REVERSAL", "DOWN", conf)
            else:
                displacement = last_close - zone.price
                body = abs(last_close - float(opens[-1]))
                if displacement > threshold and body > threshold:
                    conf = min(displacement / atr, 1.0) if atr > 0 else 0.0
                    conf = max(conf, 0.3)
                    if conf > best[2]:
                        best = ("CONTINUATION", "UP", conf)

        elif zone.kind == "SELL_SIDE":
            anchor_above = anchor_close > zone.price
            if not anchor_above:
                return best

            pierce_idx = -1
            for i in range(n):
                if lows[i] <= zone.price + threshold:
                    pierce_idx = i
                    break

            if pierce_idx < 0:
                return best

            wick_below = float(zone.price - min(lows[pierce_idx:]))
            last_close = float(closes[-1])

            if last_close > zone.price:
                conf = min(max(wick_below / atr, 0.0), 1.0) if atr > 0 else 0.0
                bars_to_reclaim = n - pierce_idx
                if bars_to_reclaim > 1:
                    speed_bonus = max(0.0, 0.1 * (3 - bars_to_reclaim))
                    conf = min(conf + speed_bonus, 1.0)
                conf = max(conf, 0.3)
                best = ("REVERSAL", "UP", conf)
            else:
                displacement = zone.price - last_close
                body = abs(last_close - float(opens[-1]))
                if displacement > threshold and body > threshold:
                    conf = min(displacement / atr, 1.0) if atr > 0 else 0.0
                    conf = max(conf, 0.3)
                    if conf > best[2]:
                        best = ("CONTINUATION", "DOWN", conf)

        return best

    @staticmethod
    def _recent_atr(df: pd.DataFrame, pip_size: float, period: int = 14) -> float:
        """Simple ATR from the last N candles."""
        if len(df) < period + 1:
            return 0.0
        highs = df["high"].values[-(period + 1):]
        lows = df["low"].values[-(period + 1):]
        closes = df["close"].values[-(period + 1):]
        trs = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        return float(np.mean(trs)) if trs else 0.0

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
