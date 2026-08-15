"""
APEX TRADER — Re-Entry Logic
Stopped at breakeven? That's not a loss — that's a reset.

V-010 — a breakeven exit means the ORIGINAL thesis did not play out, so re-entry
does NOT assume the original direction is still the trade. It asks a FRESH
opportunity question: does the CURRENT market structure present a setup right now?
The candidate direction is DERIVED from the live structure, not inherited from
the closed campaign — so a same-direction re-entry only happens when the setup
genuinely reformed on that side. No ego. Pure opportunity.
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
    After a breakeven stop, evaluates whether a FRESH opportunity exists in the
    CURRENT market structure (V-010) — the candidate direction is derived from the
    live structure, not assumed to be the closed trade's original direction.
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
        Determine if a FRESH opportunity is warranted after a breakeven stop.

        Only considers trades that were stopped at breakeven (re_entry_eligible).
        V-010 — the breakeven exit means the ORIGINAL thesis did not play out, so
        the original direction is NOT assumed to still be the trade. The candidate
        direction is DERIVED from the CURRENT M5 structure: a same-direction
        re-entry happens only when the setup genuinely reformed on that side; if
        the structure has flipped, the fresh (opposing) opportunity is surfaced
        instead of re-arming the invalidated original side.
        """
        pair = closed_trade.pair
        original_direction = closed_trade.direction

        if not getattr(closed_trade, "re_entry_eligible", False):
            return self._not_eligible(pair, original_direction, "Trade not eligible for re-entry")

        # Cooldown must be measured from when the triggering trade CLOSED, not
        # from when it was opened. Using candles_since_entry let any long-lived
        # trade bypass the cooldown instantly. Derive candles elapsed since the
        # close timestamp using the trade's own timeframe.
        close_time = getattr(closed_trade, "close_time", None)
        if close_time is not None:
            tf_minutes = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240}.get(
                getattr(closed_trade, "entry_timeframe", "M5"), 5,
            )
            elapsed_min = (datetime.now(timezone.utc) - close_time).total_seconds() / 60.0
            candles_since = int(elapsed_min // tf_minutes) if tf_minutes > 0 else 0
        else:
            candles_since = getattr(closed_trade, "candles_since_entry", 0)
        remaining_cooldown = max(0, self.cooldown_candles - candles_since)
        if remaining_cooldown > 0:
            return self._not_eligible(
                pair, original_direction,
                f"Cooldown active — wait {remaining_cooldown} more candle(s)",
                cooldown=remaining_cooldown,
            )

        # V-010 — derive the candidate direction from a FRESH read of the CURRENT
        # structure rather than inheriting the closed trade's direction. A
        # RANGING / undecided structure presents no fresh opportunity → not
        # eligible (we never force the original, invalidated side back on).
        analysis = self._structure.analyze(current_m5_df)
        fresh_direction = self._fresh_direction(analysis.trend)
        if fresh_direction is None:
            return self._not_eligible(
                pair, original_direction,
                f"No fresh directional opportunity — M5 structure is {analysis.trend.value}",
            )

        zone = self._find_new_zone(fresh_direction, current_m5_df)
        if zone is None:
            return self._not_eligible(pair, fresh_direction, "No new FVG or OB formed")

        same_side = fresh_direction == original_direction
        reason = (
            "Fresh setup reformed on the original side"
            if same_side else
            "Original thesis invalidated at breakeven — fresh opposing opportunity"
        )
        logger.info(
            f"RE-ENTRY ELIGIBLE: {pair} {fresh_direction} "
            f"(original {original_direction}, "
            f"{'same side' if same_side else 'flipped'}) — new zone at {zone}"
        )
        return ReEntryOpportunity(
            pair=pair,
            direction=fresh_direction,
            reason=reason,
            new_entry_zone=zone,
            cooldown_remaining=0,
            eligible=True,
            original_trade_id=getattr(closed_trade, "trade_id", ""),
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _fresh_direction(trend: Trend) -> Optional[str]:
        """Map the current M5 structure trend to a fresh candidate direction.

        A clear bull/bear structure yields LONG/SHORT; a RANGING / undecided
        structure yields None (no fresh directional opportunity), so re-entry is
        NOT armed on an ambiguous read."""
        if trend == Trend.BULLISH:
            return "LONG"
        if trend == Trend.BEARISH:
            return "SHORT"
        return None

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
