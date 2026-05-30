"""
APEX TRADER — Entry Engine
The sniper's trigger finger. Takes a READY scan result and computes
the exact entry price, stop loss, TP1, TP2, and position size.
Fires only when all micro-confirmations align on M1.
"""

import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from loguru import logger

from config import AppConfig, get_instrument, get_pip_size, InstrumentCategory, spread_open_guard_applies
from brain.structure_engine import StructureEngine
from brain.fvg_detector import FVGDetector, FairValueGap
from brain.order_block import OrderBlockDetector, OrderBlock, OBStatus
from brain.liquidity_mapper import LiquidityMapper
from brain.drawdown_guard import DrawdownGuard, DrawdownMode
from brain.session_engine import NewsGuard, SessionEngine
from trigger.entry_patterns import EntryPatternDetector


@dataclass
class EntrySignal:
    pair: str
    direction: str                      # "LONG" or "SHORT"
    entry_type: str                     # "FVG_MIDPOINT", "OB_MIDPOINT", "FVG_OB_OVERLAP", "SWEEP_REVERSAL"
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    risk_reward_1: float
    risk_reward_2: float
    risk_pips: float
    position_size_lots: float
    score: int
    confluences: list[str] = field(default_factory=list)
    entry_zone: str = ""
    micro_confirmation: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_until: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    instrument_category: str = "forex"


@dataclass
class EntryRejection:
    pair: str
    reason: str
    score: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EntryEngine:
    """
    APEX TRADER — The Trigger.
    Takes READY scan results and computes precise entries.
    Only fires when every micro-confirmation aligns on M1.
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()
        self.structure = StructureEngine()
        self.drawdown = DrawdownGuard()
        self.pattern_detector = EntryPatternDetector()
        self.news_guard = NewsGuard()
        self.session_engine = SessionEngine()

    # ------------------------------------------------------------------
    # Main entry calculation
    # ------------------------------------------------------------------

    def calculate_entry(
        self,
        pair: str,
        direction: str,
        m5_df: pd.DataFrame,
        m1_df: pd.DataFrame,
        h1_df: pd.DataFrame,
        scan_result=None,
        account_balance: float = 10000.0,
    ) -> Union[EntrySignal, EntryRejection]:
        now = datetime.now(timezone.utc)
        score = scan_result.score if scan_result else 85
        confluences = list(scan_result.confluences) if scan_result else []
        pip_size = self._pip_size(pair)
        category = self._category(pair)

        can_trade, reason = self.drawdown.can_trade(now)
        if not can_trade:
            logger.warning(f"[{pair}] Entry rejected — {reason}")
            return EntryRejection(pair=pair, reason=reason, score=score, timestamp=now)

        news_status = self.news_guard.check([pair], now)
        if not news_status.is_clear:
            return EntryRejection(
                pair=pair,
                reason=f"NewsGuard: {news_status.warning_message}",
                score=score,
                timestamp=now,
            )

        upcoming = self.news_guard.check([pair], now + timedelta(minutes=15))
        if not upcoming.is_clear:
            return EntryRejection(
                pair=pair,
                reason="High-impact news in <15min — holding off",
                score=score,
                timestamp=now,
            )

        session_status = self.session_engine.get_status(now)
        # Guard applies only to FX pairs — reads from instrument registry
        if (spread_open_guard_applies(pair)
                and session_status.current_session in ("LONDON", "NEW_YORK")
                and session_status.session_open_minutes <= 15):
            return EntryRejection(
                pair=pair,
                reason=f"Session {session_status.current_session} just opened ({session_status.session_open_minutes}min) — waiting for spread stabilization",
                score=score,
                timestamp=now,
            )

        status = self.drawdown.get_status(now)
        risk_pct = status.current_risk_pct

        zone = self.find_entry_zone(pair, direction, m5_df, pip_size)
        if zone["type"] == "NONE":
            return EntryRejection(
                pair=pair, reason="No valid entry zone (FVG or OB) found on M5",
                score=score, timestamp=now,
            )

        confirmed, pattern_desc = self.confirm_m1_entry(direction, m1_df, zone, pip_size)
        if not confirmed:
            return EntryRejection(
                pair=pair, reason="No micro-confirmation on M1",
                score=score, timestamp=now,
            )
        confluences.append(f"M1 confirmed: {pattern_desc}")

        if zone.get("has_sweep"):
            confluences.append("Liquidity sweep confirmed at entry zone")

        entry_price = zone["midpoint"]
        stop_loss = self.calculate_stop_loss(direction, zone, pip_size)
        tp1, tp2 = self.calculate_targets(pair, direction, entry_price, stop_loss, h1_df, pip_size)

        risk_distance = abs(entry_price - stop_loss)
        if risk_distance < pip_size:
            return EntryRejection(
                pair=pair, reason="Risk distance too small — invalid zone",
                score=score, timestamp=now,
            )

        rr1 = abs(tp1 - entry_price) / risk_distance
        rr2 = abs(tp2 - entry_price) / risk_distance

        if rr1 < 1.0:
            return EntryRejection(
                pair=pair, reason=f"Insufficient reward — R:R to TP1 is {rr1:.2f}",
                score=score, timestamp=now,
            )

        risk_pips = risk_distance / pip_size
        position_size = self.calculate_position_size(
            entry_price, stop_loss, risk_pct, account_balance, pip_size, pair,
        )

        zone_desc = self._describe_zone(zone, pip_size)
        valid_until = now + timedelta(minutes=25)

        signal = EntrySignal(
            pair=pair,
            direction=direction,
            entry_type=zone["type"],
            entry_price=round(entry_price, 5),
            stop_loss=round(stop_loss, 5),
            tp1=round(tp1, 5),
            tp2=round(tp2, 5),
            risk_reward_1=round(rr1, 2),
            risk_reward_2=round(rr2, 2),
            risk_pips=round(risk_pips, 1),
            position_size_lots=round(position_size, 2),
            score=score,
            confluences=confluences,
            entry_zone=zone_desc,
            micro_confirmation=pattern_desc,
            timestamp=now,
            valid_until=valid_until,
            instrument_category=category,
        )

        logger.info(
            f"[{pair}] ENTRY SIGNAL — {direction} @ {signal.entry_price} | "
            f"SL {signal.stop_loss} | TP1 {signal.tp1} | TP2 {signal.tp2} | "
            f"R:R {signal.risk_reward_1}/{signal.risk_reward_2} | "
            f"{signal.position_size_lots} lots | Score {score}"
        )
        return signal

    # ------------------------------------------------------------------
    # Entry zone discovery on M5
    # ------------------------------------------------------------------

    def find_entry_zone(
        self, pair: str, direction: str, m5_df: pd.DataFrame, pip_size: float,
    ) -> dict:
        current_price = m5_df["close"].iloc[-1]
        fvg_det = FVGDetector(pip_size=pip_size, proximity_pips=5.0)
        ob_det = OrderBlockDetector(pip_size=pip_size)

        fvgs = fvg_det.detect(m5_df, timeframe="M5")
        obs = ob_det.detect(m5_df, timeframe="M5")

        entry_fvg = fvg_det.get_entry_fvg(fvgs, direction, current_price)
        entry_ob = ob_det.get_entry_ob(obs, direction, current_price)

        has_sweep = self._check_sweep_near_zone(m5_df, entry_fvg, entry_ob, pip_size)

        if entry_fvg and entry_ob:
            if ob_det.get_confluence_with_fvg(entry_ob, entry_fvg, pip_size):
                overlap_top = min(entry_fvg.top, entry_ob.top)
                overlap_bottom = max(entry_fvg.bottom, entry_ob.bottom)
                return {
                    "type": "FVG_OB_OVERLAP",
                    "top": overlap_top,
                    "bottom": overlap_bottom,
                    "midpoint": (overlap_top + overlap_bottom) / 2,
                    "fvg": entry_fvg,
                    "ob": entry_ob,
                    "has_sweep": has_sweep,
                }

        if entry_fvg:
            return {
                "type": "FVG_MIDPOINT",
                "top": entry_fvg.top,
                "bottom": entry_fvg.bottom,
                "midpoint": entry_fvg.midpoint,
                "fvg": entry_fvg,
                "ob": None,
                "has_sweep": has_sweep,
            }

        if entry_ob and entry_ob.status in (OBStatus.FRESH, OBStatus.TESTED):
            return {
                "type": "OB_MIDPOINT",
                "top": entry_ob.top,
                "bottom": entry_ob.bottom,
                "midpoint": entry_ob.midpoint,
                "fvg": None,
                "ob": entry_ob,
                "has_sweep": has_sweep,
            }

        return {"type": "NONE", "top": 0, "bottom": 0, "midpoint": 0, "fvg": None, "ob": None, "has_sweep": False}

    # ------------------------------------------------------------------
    # M1 micro-confirmation
    # ------------------------------------------------------------------

    def confirm_m1_entry(
        self, direction: str, m1_df: pd.DataFrame, entry_zone: dict, pip_size: float,
    ) -> tuple[bool, str]:
        if len(m1_df) < 3:
            return False, ""

        zone_top = entry_zone["top"]
        zone_bottom = entry_zone["bottom"]

        pattern_name, pattern_desc = self.pattern_detector.get_best_pattern(
            m1_df, direction, zone_top, zone_bottom, pip_size,
        )
        if pattern_name:
            return True, pattern_desc

        choch = self._detect_m1_choch(m1_df, direction)
        if choch:
            return True, f"M1 Change of Character — {direction.lower()} shift"

        return False, ""

    # ------------------------------------------------------------------
    # Stop loss, targets, position sizing
    # ------------------------------------------------------------------

    def calculate_stop_loss(
        self, direction: str, entry_zone: dict, pip_size: float, buffer_pips: float = 2.0,
    ) -> float:
        buffer = buffer_pips * pip_size
        if direction == "LONG":
            return entry_zone["bottom"] - buffer
        else:
            return entry_zone["top"] + buffer

    def calculate_targets(
        self,
        pair: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        h1_df: pd.DataFrame,
        pip_size: float,
    ) -> tuple[float, float]:
        risk = abs(entry_price - stop_loss)

        liq = LiquidityMapper()
        liq_map = liq.map(h1_df, pip_size)

        if direction == "LONG":
            tp1_liq = liq_map.nearest_buy_liq
            tp1 = tp1_liq.price if tp1_liq else entry_price + risk * 1.5
            if tp1 - entry_price < risk:
                tp1 = entry_price + risk * 1.5

            structure = self.structure.analyze(h1_df)
            tp2 = structure.swing_high if structure.swing_high and structure.swing_high > tp1 else entry_price + risk * 2.5
        else:
            tp1_liq = liq_map.nearest_sell_liq
            tp1 = tp1_liq.price if tp1_liq else entry_price - risk * 1.5
            if entry_price - tp1 < risk:
                tp1 = entry_price - risk * 1.5

            structure = self.structure.analyze(h1_df)
            tp2 = structure.swing_low if structure.swing_low and structure.swing_low < tp1 else entry_price - risk * 2.5

        return tp1, tp2

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss: float,
        risk_pct: float,
        account_balance: float,
        pip_size: float,
        pair: str = "",
    ) -> float:
        risk_amount = account_balance * risk_pct
        risk_pips = abs(entry_price - stop_loss) / pip_size
        if risk_pips <= 0:
            return 0.01

        pip_value = self._pip_value(pair, pip_size)
        lots = risk_amount / (risk_pips * pip_value)
        return max(0.01, min(lots, 10.0))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_sweep_near_zone(
        self, df: pd.DataFrame, fvg: Optional[FairValueGap], ob: Optional[OrderBlock], pip_size: float,
    ) -> bool:
        if len(df) < 3:
            return False
        liq = LiquidityMapper()
        liq_map = liq.map(df, pip_size)

        zones = liq_map.sell_side_liquidity[:3] + liq_map.buy_side_liquidity[:3]
        for zone in zones:
            if liq.detect_sweep(df, zone, pip_size):
                return True
        return False

    def _detect_m1_choch(self, df: pd.DataFrame, direction: str) -> bool:
        if len(df) < 10:
            return False
        structure = StructureEngine(swing_lookback=3)
        analysis = structure.analyze(df)

        if direction == "LONG" and analysis.last_event.value == "CHOCH_BULLISH":
            return True
        if direction == "SHORT" and analysis.last_event.value == "CHOCH_BEARISH":
            return True
        return False

    def _pip_size(self, symbol: str) -> float:
        try:
            return get_pip_size(symbol)
        except KeyError:
            return 0.0001

    def _category(self, symbol: str) -> str:
        try:
            return get_instrument(symbol).category.value
        except KeyError:
            return "forex"

    def _pip_value(self, pair: str, pip_size: float) -> float:
        try:
            return get_instrument(pair).pip_value_per_lot
        except KeyError:
            return 10.0

    def _describe_zone(self, zone: dict, pip_size: float) -> str:
        kind = zone["type"]
        top = zone["top"]
        bottom = zone["bottom"]
        mid = zone["midpoint"]
        size_pips = round((top - bottom) / pip_size, 1)
        return f"{kind} {bottom:.5f}–{top:.5f}, midpoint {mid:.5f} ({size_pips} pips)"