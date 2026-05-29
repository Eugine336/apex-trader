"""
APEX TRADER — Re-Entry Logic
Stopped at breakeven? That's not a loss — that's a reset.
If the setup is still valid, we go again. No ego. Pure logic.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from loguru import logger

from brain.structure_engine import StructureEngine, Trend
from brain.fvg_detector import FVGDetector, FVGStatus
from brain.order_block import OrderBlockDetector, OBStatus


@dataclass
class ReEntryOpportunity:
    pair: str
    direction: str
    reason: str
    new_entry_zone: str
    cooldown_remaining: int
    eligible: bool
    original_trade_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ReEntryManager:
    """
    After a breakeven stop, evaluates whether the original setup
    is still valid and a new entry zone exists.
    """

    def __init__(self, cooldown_candles: int = 3):
        self.cooldown_candles = cooldown_candles
        self._structure = StructureEngine()
        self._fvg = FVGDetector()
        self._ob = OrderBlockDetector()

    def get_cooldown_candles(self) -> int:
        return self.cooldown_candles

    def check_re_entry(
        self,
        closed_trade,
        current_m5_df: pd.DataFrame,
        current_m1_df: Optional[pd.DataFrame] = None,
    ) -> ReEntryOpportunity:
        """
        Determine if a re-entry is warranted after a breakeven stop.
        Only considers trades that were stopped at breakeven (re_entry_eligible).
        """
        pair = closed_trade.pair
        direction = closed_trade.direction

        if not getattr(closed_trade, "re_entry_eligible", False):
            return self._not_eligible(pair, direction, "Trade not eligible for re-entry")

        candles_since = getattr(closed_trade, "candles_since_entry", 0)
        remaining_cooldown = max(0, self.cooldown_candles - candles_since)
        if remaining_cooldown > 0:
            return self._not_eligible(
                pair, direction,
                f"Cooldown active — wait {remaining_cooldown} more candle(s)",
                cooldown=remaining_cooldown,
            )

        analysis = self._structure.analyze(current_m5_df)
        bias_valid = (
            (direction == "LONG" and analysis.trend == Trend.BULLISH)
            or (direction == "SHORT" and analysis.trend == Trend.BEARISH)
        )
        if not bias_valid:
            return self._not_eligible(
                pair, direction,
                f"Bias no longer valid — M5 trend is {analysis.trend.value}",
            )

        zone = self._find_new_zone(direction, current_m5_df)
        if zone is None:
            return self._not_eligible(pair, direction, "No new FVG or OB formed")

        logger.info(
            f"RE-ENTRY ELIGIBLE: {pair} {direction} — new zone at {zone}"
        )
        return ReEntryOpportunity(
            pair=pair,
            direction=direction,
            reason="Stopped at breakeven, setup still valid",
            new_entry_zone=zone,
            cooldown_remaining=0,
            eligible=True,
            original_trade_id=getattr(closed_trade, "trade_id", ""),
        )

    # ------------------------------------------------------------------

    def _find_new_zone(
        self, direction: str, df_m5: pd.DataFrame,
    ) -> Optional[str]:
        fvgs = self._fvg.detect(df_m5)
        active_fvgs = [
            f for f in fvgs
            if f.status == FVGStatus.OPEN
            and ((direction == "LONG" and f.kind == "BULLISH")
                 or (direction == "SHORT" and f.kind == "BEARISH"))
        ]
        if active_fvgs:
            f = active_fvgs[-1]
            return f"FVG {f.top:.5f}-{f.bottom:.5f}"

        obs = self._ob.detect(df_m5)
        active_obs = [
            o for o in obs
            if o.status == OBStatus.FRESH
            and ((direction == "LONG" and o.kind == "BULLISH")
                 or (direction == "SHORT" and o.kind == "BEARISH"))
        ]
        if active_obs:
            o = active_obs[-1]
            return f"OB {o.top:.5f}-{o.bottom:.5f}"

        return None

    def _not_eligible(
        self,
        pair: str,
        direction: str,
        reason: str,
        cooldown: int = 0,
    ) -> ReEntryOpportunity:
        return ReEntryOpportunity(
            pair=pair,
            direction=direction,
            reason=reason,
            new_entry_zone="",
            cooldown_remaining=cooldown,
            eligible=False,
        )
