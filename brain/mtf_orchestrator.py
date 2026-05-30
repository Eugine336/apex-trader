"""
APEX TRADER — Multi-Timeframe Orchestrator
This is the conductor: H4 bias, H1 confirmation, M15/M5 zone selection,
M1 trigger timing. The sniper fires only when the entire stack aligns.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from loguru import logger

from brain.fvg_detector import FVGDetector
from brain.liquidity_mapper import LiquidityMapper
from brain.order_block import OrderBlockDetector
from brain.regime_detector import MarketRegime, RegimeDetector
from brain.session_engine import NewsGuard, SessionEngine
from brain.structure_engine import StructureEngine, StructureEvent


@dataclass
class Confluence:
    name: str
    score: int
    details: str


@dataclass
class TradeSetup:
    pair: str
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    score: int
    confluences: list[Confluence]
    regime: str
    bias_strength: str
    timestamp: datetime


class MTFOrchestrator:
    """
    Coordinates all timing layers and emits a complete setup packet when ready.
    """

    REQUIRED_TIMEFRAMES = ("H4", "H1", "M15", "M5", "M1")

    def __init__(
        self,
        structure_engine: Optional[StructureEngine] = None,
        liquidity_mapper: Optional[LiquidityMapper] = None,
        fvg_detector: Optional[FVGDetector] = None,
        order_block_detector: Optional[OrderBlockDetector] = None,
        regime_detector: Optional[RegimeDetector] = None,
        session_engine: Optional[SessionEngine] = None,
        news_guard: Optional[NewsGuard] = None,
        min_entry_score: int = 65,
        pip_size: float = 0.0001,
    ):
        self.structure_engine = structure_engine or StructureEngine(swing_lookback=2)
        self.liquidity_mapper = liquidity_mapper or LiquidityMapper()
        self.fvg_detector = fvg_detector or FVGDetector(pip_size=pip_size)
        self.order_block_detector = order_block_detector or OrderBlockDetector(
            pip_size=pip_size
        )
        self.regime_detector = regime_detector or RegimeDetector()
        self.session_engine = session_engine or SessionEngine()
        self.news_guard = news_guard or NewsGuard()
        self.min_entry_score = min_entry_score
        self.pip_size = pip_size

    def build_setup(
        self,
        pair: str,
        data_by_timeframe: dict[str, pd.DataFrame],
        utc_now: Optional[datetime] = None,
    ) -> Optional[TradeSetup]:
        utc_now = utc_now or datetime.now(timezone.utc)
        missing = [tf for tf in self.REQUIRED_TIMEFRAMES if tf not in data_by_timeframe]
        if missing:
            logger.warning(f"Missing timeframes for setup: {missing}")
            return None

        if any(len(data_by_timeframe[tf]) < 25 for tf in self.REQUIRED_TIMEFRAMES):
            logger.warning("Insufficient candles in one or more required timeframes")
            return None

        h4_df = data_by_timeframe["H4"]
        h1_df = data_by_timeframe["H1"]
        m15_df = data_by_timeframe["M15"]
        m5_df = data_by_timeframe["M5"]
        m1_df = data_by_timeframe["M1"]
        current_price = float(m1_df["close"].iloc[-1])

        bias = self.structure_engine.get_bias(h4_df, h1_df)
        if bias["direction"] not in {"BULLISH", "BEARISH"} or bias["strength"] in {
            "NONE",
            "CONFLICTED",
        }:
            return None

        direction = "LONG" if bias["direction"] == "BULLISH" else "SHORT"
        confluences: list[Confluence] = []
        score = 0

        structure_score = 20 if bias["strength"] == "STRONG" else 14
        score += structure_score
        confluences.append(
            Confluence(
                name="Market Structure",
                score=structure_score,
                details=f"H4={bias['h4_trend']}, H1={bias['h1_trend']}, strength={bias['strength']}",
            )
        )

        regime = self.regime_detector.analyze(m15_df)
        if regime.regime == MarketRegime.VOLATILE:
            confluences.append(
                Confluence(
                    name="Regime Filter",
                    score=0,
                    details="VOLATILE regime detected — trading frozen",
                )
            )
            return None

        session_score = self.session_engine.get_session_score(utc_now)
        # Commodities (gold, indices, synthetics) trade 24/5 — don't zero their
        # session score during Asian/Transition hours. Treat any non-dead window
        # as at least a medium-quality session (score=4) for commodities.
        try:
            from config import get_instrument
            _cat = get_instrument(pair).category.value
        except (KeyError, Exception):
            _cat = "forex"
        if _cat in ("commodity", "synthetic"):
            session_score = max(session_score, 4)
        session_points = 10 if session_score >= 7 else (5 if session_score >= 4 else 0)
        score += session_points
        confluences.append(
            Confluence(
                name="Session Timing",
                score=session_points,
                details=f"session_score={session_score}",
            )
        )

        news = self.news_guard.check([pair], utc_now)
        if not news.is_clear:
            confluences.append(
                Confluence(
                    name="News Guard",
                    score=0,
                    details=news.warning_message or "High-impact event nearby",
                )
            )
            return None

        score += 10
        confluences.append(
            Confluence(
                name="News Guard",
                score=10,
                details="No blocking high-impact event",
            )
        )

        m15_fvgs = self.fvg_detector.detect(m15_df, timeframe="M15")
        m5_fvgs = self.fvg_detector.detect(m5_df, timeframe="M5")
        fvg_confluence = self.fvg_detector.get_confluence_fvgs(
            m5_fvgs=m5_fvgs,
            m15_fvgs=m15_fvgs,
            direction=direction,
            current_price=current_price,
            pip_size=self.pip_size,
        )

        fvg_points = (
            15
            if fvg_confluence["has_confluence"]
            else (8 if fvg_confluence["m5_fvg"] else 0)
        )
        score += fvg_points
        confluences.append(
            Confluence(
                name="FVG Zone",
                score=fvg_points,
                details=f"confluence={fvg_confluence['has_confluence']}",
            )
        )

        m5_obs = self.order_block_detector.detect(m5_df, timeframe="M5")
        entry_ob = self.order_block_detector.get_entry_ob(
            m5_obs, direction=direction, current_price=current_price
        )
        ob_points = 20 if entry_ob else 0
        score += ob_points
        confluences.append(
            Confluence(
                name="Order Block",
                score=ob_points,
                details=f"entry_ob={'YES' if entry_ob else 'NO'}",
            )
        )

        m1_structure = self.structure_engine.analyze(m1_df)
        choch_aligned = (
            direction == "LONG"
            and m1_structure.last_event
            in {StructureEvent.CHOCH_BULLISH, StructureEvent.BOS_BULLISH}
        ) or (
            direction == "SHORT"
            and m1_structure.last_event
            in {StructureEvent.CHOCH_BEARISH, StructureEvent.BOS_BEARISH}
        )
        trigger_points = 15 if choch_aligned else 0
        score += trigger_points
        confluences.append(
            Confluence(
                name="M1 Trigger",
                score=trigger_points,
                details=f"event={m1_structure.last_event.value}",
            )
        )

        liq_map = self.liquidity_mapper.map(m5_df, pip_size=self.pip_size)
        sweep_zone = (
            liq_map.nearest_sell_liq if direction == "LONG" else liq_map.nearest_buy_liq
        )
        sweep_confirmed = (
            self.liquidity_mapper.detect_sweep(m1_df, sweep_zone, self.pip_size)
            if sweep_zone
            else False
        )
        sweep_points = 10 if sweep_confirmed else 0
        score += sweep_points
        confluences.append(
            Confluence(
                name="Liquidity Sweep",
                score=sweep_points,
                details=f"sweep_confirmed={sweep_confirmed}",
            )
        )

        adjusted_score = self.regime_detector.adjust_score(score, regime, max_score=100)
        if adjusted_score < self.min_entry_score:
            return None

        entry_price = self._resolve_entry_price(
            current_price=current_price,
            fvg_confluence=fvg_confluence,
            entry_ob=entry_ob,
        )
        stop_loss = self._resolve_stop_loss(direction, m1_df, entry_price)
        tp1, tp2 = self._resolve_targets(direction, entry_price, stop_loss, liq_map)

        return TradeSetup(
            pair=pair,
            direction=direction,
            entry_price=round(entry_price, 6),
            stop_loss=round(stop_loss, 6),
            tp1=round(tp1, 6),
            tp2=round(tp2, 6),
            score=int(adjusted_score),
            confluences=confluences,
            regime=regime.regime.value,
            bias_strength=bias["strength"],
            timestamp=utc_now,
        )

    def _resolve_entry_price(
        self,
        current_price: float,
        fvg_confluence: dict,
        entry_ob,
    ) -> float:
        overlap_zone = fvg_confluence.get("overlap_zone")
        if overlap_zone:
            return float(overlap_zone["midpoint"])
        if fvg_confluence.get("m5_fvg"):
            return float(fvg_confluence["m5_fvg"].midpoint)
        if fvg_confluence.get("m15_fvg"):
            return float(fvg_confluence["m15_fvg"].midpoint)
        if entry_ob:
            return float(entry_ob.midpoint)
        return current_price

    def _resolve_stop_loss(
        self, direction: str, m1_df: pd.DataFrame, entry_price: float
    ) -> float:
        buffer = 2 * self.pip_size
        if direction == "LONG":
            swing_low = float(m1_df["low"].tail(10).min())
            return min(swing_low - buffer, entry_price - 5 * self.pip_size)
        swing_high = float(m1_df["high"].tail(10).max())
        return max(swing_high + buffer, entry_price + 5 * self.pip_size)

    def _resolve_targets(
        self,
        direction: str,
        entry_price: float,
        stop_loss: float,
        liq_map,
    ) -> tuple[float, float]:
        risk = abs(entry_price - stop_loss)
        if risk <= 0:
            risk = 8 * self.pip_size

        if direction == "LONG":
            buy_levels = [z.price for z in liq_map.buy_side_liquidity[:2]]
            tp1 = buy_levels[0] if buy_levels else entry_price + risk * 1.5
            tp2 = buy_levels[1] if len(buy_levels) > 1 else entry_price + risk * 2.5
            return max(tp1, entry_price + risk), max(tp2, entry_price + risk * 1.5)

        sell_levels = [z.price for z in liq_map.sell_side_liquidity[:2]]
        tp1 = sell_levels[0] if sell_levels else entry_price - risk * 1.5
        tp2 = sell_levels[1] if len(sell_levels) > 1 else entry_price - risk * 2.5
        return min(tp1, entry_price - risk), min(tp2, entry_price - risk * 1.5)