"""
APEX TRADER — Multi-Timeframe Orchestrator
Used by the scanner/backtest path. The live executor (EntryEngine) receives
H1, M5, M1 as required inputs and H4, M15 as optional — H4 is used for the
bias gate (when enabled); M15 is accepted but currently unused.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from loguru import logger

from adaptive.score_optimizer import ScoringWeights
from brain.fvg_detector import FVGDetector
from brain.liquidity_mapper import LiquidityMapper
from brain.order_block import OrderBlockDetector
from brain.regime_detector import MarketRegime, RegimeDetector
from brain.session_engine import NewsGuard, SessionEngine
from brain.structure_engine import StructureEngine, StructureEvent
from config import session_score_floor


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

    When use_adaptive_weights=True, each factor's maximum point contribution
    is determined by the corresponding ScoringWeights value rather than
    hardcoded ceilings.  Two additional factors — mtf_confluence and
    currency_strength — become active.  When False (default), scoring is
    identical to the original hardcoded logic.
    """

    REQUIRED_TIMEFRAMES = ("H4", "H1", "M15", "M5", "M1")

    _STRUCTURE_PARTIAL_RATIO = 0.7
    _SESSION_MEDIUM_RATIO = 0.5

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
        use_adaptive_weights: bool = False,
        scoring_weights: Optional[ScoringWeights] = None,
        volatility_stop_mode: str = "off",
        atr_stop_period: int = 14,
        atr_stop_mult: float = 1.5,
        atr_stop_ratio_min: float = 1.0,
        atr_stop_ratio_max: float = 2.0,
        atr_stop_max_risk_mult: float = 4.0,
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
        self._use_adaptive_weights = use_adaptive_weights
        self._weights = scoring_weights or ScoringWeights()
        self._volatility_stop_mode = volatility_stop_mode
        self._atr_stop_period = atr_stop_period
        self._atr_stop_mult = atr_stop_mult
        self._atr_stop_ratio_min = atr_stop_ratio_min
        self._atr_stop_ratio_max = atr_stop_ratio_max
        self._atr_stop_max_risk_mult = atr_stop_max_risk_mult

    @staticmethod
    def load_saved_weights() -> ScoringWeights:
        """Load OOS-validated weights from the adaptive store, falling back
        to canonical defaults if the file is absent or corrupt. Handles
        old 9-factor → new 12-factor schema migration transparently."""
        from adaptive.score_optimizer import load_saved_weights as _load
        return _load()

    def build_setup(
        self,
        pair: str,
        data_by_timeframe: dict[str, pd.DataFrame],
        utc_now: Optional[datetime] = None,
        currency_strength_aligned: bool = False,
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

        m15_fvgs = self.fvg_detector.detect(m15_df, timeframe="M15")
        m5_fvgs = self.fvg_detector.detect(m5_df, timeframe="M5")
        fvg_confluence = self.fvg_detector.get_confluence_fvgs(
            m5_fvgs=m5_fvgs,
            m15_fvgs=m15_fvgs,
            direction=direction,
            current_price=current_price,
            pip_size=self.pip_size,
        )

        m5_obs = self.order_block_detector.detect(m5_df, timeframe="M5")
        entry_ob = self.order_block_detector.get_entry_ob(
            m5_obs, direction=direction, current_price=current_price
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

        liq_map = self.liquidity_mapper.map(m5_df, pip_size=self.pip_size)
        sweep_zone = (
            liq_map.nearest_sell_liq if direction == "LONG" else liq_map.nearest_buy_liq
        )
        sweep_confirmed = (
            self.liquidity_mapper.detect_sweep(m1_df, sweep_zone, self.pip_size)
            if sweep_zone
            else False
        )

        session_raw = self.session_engine.get_session_score(utc_now)
        session_raw = max(session_raw, session_score_floor(pair))

        if self._use_adaptive_weights:
            score, confluences = self._score_adaptive(
                bias=bias,
                session_raw=session_raw,
                fvg_confluence=fvg_confluence,
                entry_ob=entry_ob,
                choch_aligned=choch_aligned,
                m1_structure=m1_structure,
                sweep_confirmed=sweep_confirmed,
                currency_strength_aligned=currency_strength_aligned,
            )
        else:
            score, confluences = self._score_hardcoded(
                bias=bias,
                session_raw=session_raw,
                fvg_confluence=fvg_confluence,
                entry_ob=entry_ob,
                choch_aligned=choch_aligned,
                m1_structure=m1_structure,
                sweep_confirmed=sweep_confirmed,
            )

        adjusted_score = self.regime_detector.adjust_score(score, regime, max_score=100)
        if adjusted_score < self.min_entry_score:
            return None

        entry_price = self._resolve_entry_price(
            current_price=current_price,
            fvg_confluence=fvg_confluence,
            entry_ob=entry_ob,
        )
        stop_loss = self._resolve_stop_loss(direction, m1_df, entry_price, pair)
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

    def _score_hardcoded(
        self,
        bias: dict,
        session_raw: int,
        fvg_confluence: dict,
        entry_ob,
        choch_aligned: bool,
        m1_structure,
        sweep_confirmed: bool,
    ) -> tuple[int, list[Confluence]]:
        """Original hardcoded scoring — byte-for-byte identical to the
        pre-A4 implementation.  No mtf_confluence or currency_strength."""
        score = 0
        confluences: list[Confluence] = []

        structure_score = 20 if bias["strength"] == "STRONG" else 14
        score += structure_score
        confluences.append(
            Confluence(
                name="Market Structure",
                score=structure_score,
                details=f"H4={bias['h4_trend']}, H1={bias['h1_trend']}, strength={bias['strength']}",
            )
        )

        session_points = 10 if session_raw >= 7 else (5 if session_raw >= 4 else 0)
        score += session_points
        confluences.append(
            Confluence(
                name="Session Timing",
                score=session_points,
                details=f"session_score={session_raw}",
            )
        )

        score += 10
        confluences.append(
            Confluence(
                name="News Guard",
                score=10,
                details="No blocking high-impact event",
            )
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

        ob_points = 20 if entry_ob else 0
        score += ob_points
        confluences.append(
            Confluence(
                name="Order Block",
                score=ob_points,
                details=f"entry_ob={'YES' if entry_ob else 'NO'}",
            )
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

        sweep_points = 10 if sweep_confirmed else 0
        score += sweep_points
        confluences.append(
            Confluence(
                name="Liquidity Sweep",
                score=sweep_points,
                details=f"sweep_confirmed={sweep_confirmed}",
            )
        )

        return score, confluences

    def _score_adaptive(
        self,
        bias: dict,
        session_raw: int,
        fvg_confluence: dict,
        entry_ob,
        choch_aligned: bool,
        m1_structure,
        sweep_confirmed: bool,
        currency_strength_aligned: bool,
    ) -> tuple[int, list[Confluence]]:
        """Adaptive scoring — each factor's ceiling comes from ScoringWeights.
        Uses the 12-factor live-scanner taxonomy. Factors not available in
        the orchestrator context (ob_h1, volume, inducement, wyckoff) score 0."""
        w = self._weights.as_dict()
        score = 0
        confluences: list[Confluence] = []

        structure_pts = (
            w["structure"]
            if bias["strength"] == "STRONG"
            else round(w["structure"] * self._STRUCTURE_PARTIAL_RATIO)
        )
        score += structure_pts
        confluences.append(
            Confluence(
                name="Market Structure",
                score=structure_pts,
                details=f"H4={bias['h4_trend']}, H1={bias['h1_trend']}, strength={bias['strength']}",
            )
        )

        session_pts = (
            w["session"]
            if session_raw >= 7
            else (round(w["session"] * self._SESSION_MEDIUM_RATIO) if session_raw >= 4 else 0)
        )
        score += session_pts
        confluences.append(
            Confluence(
                name="Session Timing",
                score=session_pts,
                details=f"session_score={session_raw}",
            )
        )

        news_pts = w["news"]
        score += news_pts
        confluences.append(
            Confluence(
                name="News Guard",
                score=news_pts,
                details="No blocking high-impact event",
            )
        )

        fvg_pts = w["fvg"] if fvg_confluence["m5_fvg"] else 0
        score += fvg_pts
        confluences.append(
            Confluence(
                name="FVG Zone",
                score=fvg_pts,
                details=f"m5_fvg={'YES' if fvg_confluence['m5_fvg'] else 'NO'}",
            )
        )

        mtf_pts = w["mtf_confluence"] if fvg_confluence["has_confluence"] else 0
        score += mtf_pts
        confluences.append(
            Confluence(
                name="Multi-TF FVG",
                score=mtf_pts,
                details=f"confluence={fvg_confluence['has_confluence']}",
            )
        )

        ob_pts = w["ob_m5"] if entry_ob else 0
        score += ob_pts
        confluences.append(
            Confluence(
                name="Order Block",
                score=ob_pts,
                details=f"entry_ob={'YES' if entry_ob else 'NO'}",
            )
        )

        sweep_pts = w["liquidity_sweep"] if sweep_confirmed else 0
        score += sweep_pts
        confluences.append(
            Confluence(
                name="Liquidity Sweep",
                score=sweep_pts,
                details=f"sweep_confirmed={sweep_confirmed}",
            )
        )

        cs_pts = w["currency_strength"] if currency_strength_aligned else 0
        score += cs_pts
        confluences.append(
            Confluence(
                name="Currency Strength",
                score=cs_pts,
                details=f"aligned={currency_strength_aligned}",
            )
        )

        return score, confluences

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
        self, direction: str, m1_df: pd.DataFrame, entry_price: float,
        pair: str = "",
    ) -> float:
        buffer = 2 * self.pip_size
        if direction == "LONG":
            swing_low = float(m1_df["low"].tail(10).min())
            structure_sl = min(swing_low - buffer, entry_price - 5 * self.pip_size)
        else:
            swing_high = float(m1_df["high"].tail(10).max())
            structure_sl = max(swing_high + buffer, entry_price + 5 * self.pip_size)

        if self._volatility_stop_mode != "on":
            return structure_sl

        from brain.instrument_profile import get_profile
        from brain.volatility_stop import (
            atr_stop_price,
            clamped_atr_stop_distance,
            latest_atr,
        )

        atr_val = latest_atr(m1_df, self._atr_stop_period)
        structure_distance = abs(entry_price - structure_sl)
        inst_profile = get_profile(pair) if pair else None
        min_pips = inst_profile.min_risk_pips if inst_profile else 5.0
        max_pips = self._atr_stop_max_risk_mult * min_pips

        dist, status = clamped_atr_stop_distance(
            atr_val,
            structure_distance,
            mult=self._atr_stop_mult,
            min_pips=min_pips,
            max_pips=max_pips,
            pip_size=self.pip_size,
            ratio_min=self._atr_stop_ratio_min,
            ratio_max=self._atr_stop_ratio_max,
        )

        if status == "modeled" and dist is not None:
            return atr_stop_price(entry_price, direction, dist)

        logger.debug(
            "[ATR SL] unavailable for {} — falling back to structure SL {:.6f}",
            pair or "unknown", structure_sl,
        )
        return structure_sl

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
