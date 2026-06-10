"""
APEX TRADER — Entry Engine
The sniper's trigger finger. Takes a READY scan result and computes
the exact entry price, stop loss, TP1, TP2, and position size.
Fires only when all micro-confirmations align on M1.

Live path inputs: H1, M5, M1 are required. H4 and M15 are optional —
passed through for the H4 bias gate (when enabled) and future use.
"""

import math
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from loguru import logger

from config import AppConfig, get_instrument, get_pip_size, spread_open_guard_applies
from brain.instrument_profile import get_profile, InstrumentProfile
from brain.market_data_utils import drop_forming_bar
from brain.structure_engine import StructureEngine
from brain.fvg_detector import FVGDetector, FairValueGap
from brain.order_block import OrderBlockDetector, OrderBlock, OBStatus
from brain.liquidity_mapper import LiquidityMapper
from brain.drawdown_guard import DrawdownGuard
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
    entry_timeframe: str = "M5"


@dataclass
class EntryRejection:
    pair: str
    reason: str
    score: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    direction: Optional[str] = None


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
        scan_result,
        account_balance: float,
        h4_df: Optional[pd.DataFrame] = None,
        m15_df: Optional[pd.DataFrame] = None,
    ) -> Union[EntrySignal, EntryRejection]:
        if scan_result is None:
            raise ValueError("calculate_entry requires a scan_result; refusing to fabricate a score")
        now = datetime.now(timezone.utc)
        score = scan_result.score
        confluences = list(scan_result.confluences)
        try:
            get_instrument(pair)
        except KeyError:
            logger.warning(
                f"[{pair}] Entry rejected — unknown instrument, not in registry, "
                "refusing to assume economics"
            )
            return EntryRejection(
                pair=pair,
                reason="Unknown instrument — not in registry, refusing to assume economics",
                score=scan_result.score,
                timestamp=now,
            )
        pip_size = self._pip_size(pair)
        category = self._category(pair)
        profile = get_profile(pair)

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

        effective_min_score = max(
            self.config.scoring.min_entry_score,
            status.current_score_threshold,
        )
        if score < effective_min_score:
            return EntryRejection(
                pair=pair,
                reason=f"score {score} < drawdown floor {effective_min_score} (mode={status.mode})",
                score=score, timestamp=now,
            )

        # ── H4 bias gate (default OFF) ───────────────────────────────────
        # When enabled, rejects entries where H4 trend directly contradicts
        # the trade direction. Can only reject, never widen risk.
        if (self.config.risk.h4_bias_gate_enabled
                and h4_df is not None
                and len(h4_df) >= 20):
            from brain.structure_engine import StructureEngine as _SE
            h4_structure = _SE(pip_size=pip_size).analyze(h4_df)
            h4_trend = h4_structure.trend.value
            if direction == "LONG" and h4_trend == "BEARISH":
                return EntryRejection(
                    pair=pair,
                    reason=f"H4 bias gate — LONG entry rejected, H4 trend is BEARISH",
                    score=score, timestamp=now, direction=direction,
                )
            if direction == "SHORT" and h4_trend == "BULLISH":
                return EntryRejection(
                    pair=pair,
                    reason=f"H4 bias gate — SHORT entry rejected, H4 trend is BULLISH",
                    score=score, timestamp=now, direction=direction,
                )

        zone = self.find_entry_zone(pair, direction, m5_df, pip_size, profile)
        if zone["type"] == "NONE":
            return EntryRejection(
                pair=pair, reason="No valid entry zone (FVG or OB) found on M5",
                score=score, timestamp=now,
            )

        confirmed, pattern_desc = self.confirm_m1_entry(direction, m1_df, zone, pip_size, profile)
        if not confirmed:
            recent_closes = m1_df["close"].iloc[-5:].values if len(m1_df) >= 5 else []
            stale = len(recent_closes) > 0 and len(set(round(float(c), 5) for c in recent_closes)) == 1
            reason = "Stale M1 feed — broker not sending new ticks" if stale else "No micro-confirmation on M1"
            return EntryRejection(
                pair=pair, reason=reason,
                score=score, timestamp=now,
                entry_price=zone.get("midpoint"),
                direction=direction,
            )
        confluences.append(f"M1 confirmed: {pattern_desc}")

        if zone.get("has_sweep"):
            confluences.append("Liquidity sweep confirmed at entry zone")

        entry_price = zone["midpoint"]
        stop_loss = self.calculate_stop_loss(direction, zone, pip_size, profile.sl_buffer_pips)
        risk_distance = abs(entry_price - stop_loss)

        # ── SL floor for synthetics and crypto — MUST run before calculate_targets ──
        # calculate_targets uses risk_distance to validate the 2.5R minimum for TP2.
        # If the floor widens the SL AFTER targets are set, the effective R:R collapses
        # and the validator rejects a perfectly good setup with "R:R to TP2 below minimum".
        # Fix: apply the floor here so calculate_targets sees the real risk distance.
        if category == "synthetic":
            pct_floor = entry_price * 0.003   # 0.3%
            if risk_distance < pct_floor:
                stop_loss = (
                    entry_price - pct_floor if direction == "LONG"
                    else entry_price + pct_floor
                )
                risk_distance = pct_floor
        elif category == "crypto":
            pct_floor = entry_price * 0.0015  # 0.15%
            if risk_distance < pct_floor:
                stop_loss = (
                    entry_price - pct_floor if direction == "LONG"
                    else entry_price + pct_floor
                )
                risk_distance = pct_floor

        tp1, tp2 = self.calculate_targets(pair, direction, entry_price, stop_loss, h1_df, pip_size)

        min_risk_distance = profile.min_risk_pips * pip_size

        # Percentage-based SL floor already applied above — skip duplicate block.

        if risk_distance < pip_size:
            return EntryRejection(
                pair=pair, reason="Risk distance too small — invalid zone",
                score=score, timestamp=now,
                entry_price=entry_price, stop_loss=stop_loss, direction=direction,
            )
        if risk_distance < min_risk_distance:
            return EntryRejection(
                pair=pair,
                reason=f"Risk distance {risk_distance/pip_size:.1f} pips below minimum {profile.min_risk_pips} for {category}",
                score=score, timestamp=now,
                entry_price=entry_price, stop_loss=stop_loss, direction=direction,
            )

        rr1 = abs(tp1 - entry_price) / risk_distance
        rr2 = abs(tp2 - entry_price) / risk_distance

        if rr1 < 1.0:
            return EntryRejection(
                pair=pair, reason=f"Insufficient reward — R:R to TP1 is {rr1:.2f}",
                score=score, timestamp=now,
                entry_price=entry_price, stop_loss=stop_loss, direction=direction,
            )

        risk_pips = risk_distance / pip_size
        position_size = self.calculate_position_size(
            entry_price, stop_loss, risk_pct, account_balance, pip_size, pair,
        )

        # ── Non-finite price guard ───────────────────────────────────────
        _price_fields = {
            "entry_price": entry_price, "stop_loss": stop_loss,
            "tp1": tp1, "tp2": tp2, "risk_distance": risk_distance,
            "rr1": rr1, "rr2": rr2,
        }
        for _name, _val in _price_fields.items():
            if not math.isfinite(_val):
                logger.error(
                    "[{}] Non-finite {} ({}) in signal — rejecting", pair, _name, _val,
                )
                return EntryRejection(
                    pair=pair,
                    reason=f"Non-finite {_name} ({_val}) in signal — rejecting",
                    score=score, timestamp=now,
                    entry_price=entry_price if math.isfinite(entry_price) else None,
                    stop_loss=stop_loss if math.isfinite(stop_loss) else None,
                    direction=direction,
                )

        zone_desc = self._describe_zone(zone, pip_size)
        entry_timeframe = self._determine_entry_timeframe(zone)

        expiry_minutes = {
            "M1": 10, "M5": 20, "M15": 45, "H1": 120, "H4": 240,
        }.get(entry_timeframe, 25)
        valid_until = now + timedelta(minutes=expiry_minutes)

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
            entry_timeframe=entry_timeframe,
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
        profile: Optional["InstrumentProfile"] = None,
    ) -> dict:
        from brain.instrument_profile import get_profile as _gp
        profile = profile or _gp(pair)
        current_price = m5_df["close"].iloc[-1]
        fvg_det = FVGDetector(
            pip_size=pip_size,
            proximity_pips=profile.fvg_proximity_pips,
            min_size_pips=profile.fvg_min_size_pips,
        )
        ob_det = OrderBlockDetector(
            pip_size=pip_size,
            min_impulse_pips=profile.ob_min_impulse_pips,
            buffer_pips=profile.ob_buffer_pips,
        )

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
        profile: Optional["InstrumentProfile"] = None,
    ) -> tuple[bool, str]:
        if len(m1_df) < 3:
            logger.debug(f"M1 confirm — not enough bars ({len(m1_df)})")
            return False, ""

        # ── Stale data guard ────────────────────────────────────────────────
        # If the last 5 closes are all identical the MT5/Deriv feed has frozen
        # (common on demo accounts outside trading hours). Skip confirmation so
        # we don't spin endlessly trying to detect structure on flat data.
        recent_closes = m1_df["close"].iloc[-5:].values
        if len(set(round(float(c), 5) for c in recent_closes)) == 1:
            logger.debug("M1 confirm — stale feed detected (all closes identical), skipping")
            return False, ""

        zone_top = entry_zone["top"]
        zone_bottom = entry_zone["bottom"]

        logger.debug(
            f"M1 confirm | direction={direction} | bars={len(m1_df)} | "
            f"zone={zone_bottom:.3f}–{zone_top:.3f} | "
            f"last_close={m1_df['close'].iloc[-1]:.3f} | "
            f"last_low={m1_df['low'].iloc[-1]:.3f} | "
            f"last_high={m1_df['high'].iloc[-1]:.3f}"
        )

        pattern_name, pattern_desc = self.pattern_detector.get_best_pattern(
            drop_forming_bar(m1_df), direction, zone_top, zone_bottom, pip_size,
        )
        logger.debug(f"M1 pattern result — name='{pattern_name}' desc='{pattern_desc}'")
        if pattern_name:
            return True, pattern_desc

        choch = self._detect_m1_choch(m1_df, direction, profile=profile,
                                      entry_zone=entry_zone, pip_size=pip_size)
        logger.debug(f"M1 CHoCH result — {choch}")
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

            # Use correct pip_size so min_swing_size filters out noise swings.
            # Default pip_size=0.0001 on synthetics/indices gives near-zero threshold
            # and returns tiny intracandle highs as "swing highs" → TP2 too close.
            structure = StructureEngine(pip_size=pip_size)
            h1_analysis = structure.analyze(h1_df)
            tp2_candidate = h1_analysis.swing_high
            if (tp2_candidate
                    and tp2_candidate > tp1                          # must be beyond TP1
                    and tp2_candidate > entry_price                  # SANITY: must be above entry on LONG
                    and (tp2_candidate - entry_price) / risk >= 2.5): # must give at least 2.5R
                tp2 = tp2_candidate
            else:
                tp2 = entry_price + risk * 3.0   # guaranteed 3R fallback

            # Final sanity: if tp1 or tp2 ended up on wrong side, force correct direction
            if tp1 <= entry_price:
                tp1 = entry_price + risk * 1.5
                logger.warning("TP1 sanity fix on LONG {} — was below entry, reset to 1.5R", pair)
            if tp2 <= tp1:
                tp2 = entry_price + risk * 3.0
                logger.warning("TP2 sanity fix on LONG {} — was below TP1, reset to 3R", pair)

        else:  # SHORT
            tp1_liq = liq_map.nearest_sell_liq
            tp1 = tp1_liq.price if tp1_liq else entry_price - risk * 1.5
            if entry_price - tp1 < risk:
                tp1 = entry_price - risk * 1.5

            structure = StructureEngine(pip_size=pip_size)
            h1_analysis = structure.analyze(h1_df)
            tp2_candidate = h1_analysis.swing_low
            if (tp2_candidate
                    and tp2_candidate < tp1                          # must be beyond TP1
                    and tp2_candidate < entry_price                  # SANITY: must be below entry on SHORT
                    and (entry_price - tp2_candidate) / risk >= 2.5): # must give at least 2.5R
                tp2 = tp2_candidate
            else:
                tp2 = entry_price - risk * 3.0   # guaranteed 3R fallback

            # Final sanity: if tp1 or tp2 ended up on wrong side, force correct direction
            if tp1 >= entry_price:
                tp1 = entry_price - risk * 1.5
                logger.warning("TP1 sanity fix on SHORT {} — was above entry, reset to 1.5R", pair)
            if tp2 >= tp1:
                tp2 = entry_price - risk * 3.0
                logger.warning("TP2 sanity fix on SHORT {} — was above TP1, reset to 3R", pair)

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
        """
        Returns lot size for MT5, or 0.0 for Deriv (stake is sized by RiskEngine).
        Lazy-imports build_context_for_symbol to avoid the circular import:
          entry_engine → platforms/__init__ → main_loop → entry_engine
        """
        # Lazy import — must stay inside the function, not at module level
        from platform_context import build_context_for_symbol  # noqa: PLC0415
        ctx = build_context_for_symbol(pair)
        if ctx.uses_stake:
            # Deriv: sizing is handled downstream by RiskEngine.calculate_stake()
            # Return 0.0 so the signal carries a clear sentinel — main_loop
            # ignores position_size_lots for Deriv and uses assessment.stake_usd.
            return 0.0

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

    def _detect_m1_choch(self, df: pd.DataFrame, direction: str,
                          profile: Optional["InstrumentProfile"] = None,
                          entry_zone: Optional[dict] = None,
                          pip_size: float = 0.0001) -> bool:
        if len(df) < 10:
            return False
        # M1 CHoCH uses a FIXED small lookback regardless of instrument category.
        # profile.swing_lookback (8–10) is calibrated for H4/H1 scanner use —
        # applying it to M1 with 30–50 bars leaves almost no pivot candidates
        # (each needs to be the extreme in a 21-bar window on only 30 bars).
        # M1 structure needs lookback=3 (7-bar window) to surface enough swings.
        M1_SWING_LOOKBACK = 3
        bars = profile.m1_confirmation_bars if profile else 50
        # Use more bars for structure engine — 100 gives richer swing history
        # while still being recent enough to be relevant.
        bars = max(bars, 100)
        df_slice = df.iloc[-bars:] if len(df) > bars else df
        # Use the actual instrument pip_size — never fabricate it from fvg_min_size_pips.
        # Synthetics/indices have pip_size >> 0.0001; wrong pip_size → min_swing_size
        # is near-zero so StructureEngine detects no events on M1.
        pip_sz = pip_size if pip_size > 0 else 0.0001
        structure = StructureEngine(swing_lookback=M1_SWING_LOOKBACK, pip_size=pip_sz)
        analysis = structure.analyze(df_slice)

        logger.debug(
            f"M1 CHoCH check | direction={direction} | "
            f"last_event={analysis.last_event.value} | "
            f"trend={analysis.trend.value} | "
            f"last_choch_level={analysis.last_choch_level}"
        )

        # CHoCH just happened — strongest confirmation
        if direction == "LONG" and analysis.last_event.value == "CHOCH_BULLISH":
            return True
        if direction == "SHORT" and analysis.last_event.value == "CHOCH_BEARISH":
            return True

        # BOS in trade direction — structure already broken, price retesting zone.
        # This IS micro-confirmation. Waiting for a CHoCH here means waiting for
        # something that has already happened one level up.
        if direction == "SHORT" and analysis.last_event.value == "BOS_BEARISH":
            return True
        if direction == "LONG" and analysis.last_event.value == "BOS_BULLISH":
            return True

        # CHoCH level set but overwritten by subsequent event — still valid
        if direction == "LONG" and analysis.last_choch_level is not None:
            if analysis.trend.value in ("BULLISH", "RANGING"):
                return True
        if direction == "SHORT" and analysis.last_choch_level is not None:
            if analysis.trend.value in ("BEARISH", "RANGING"):
                return True

        # ── Momentum fallback ────────────────────────────────────────────────
        # When structure engine doesn't fire an event (common on M1 with few
        # candles), use raw price momentum as micro-confirmation.
        # A professional trader reading a 1-minute chart sees this instantly:
        # 3 consecutive candles closing in the trade direction = momentum shift.
        return self._detect_momentum_confirmation(df, direction, entry_zone, pip_size, profile=profile)

    def _detect_momentum_confirmation(self, df: pd.DataFrame, direction: str,
                                       entry_zone: Optional[dict] = None,
                                       pip_size: float = 0.0001,
                                       profile: Optional["InstrumentProfile"] = None) -> bool:
        """
        Fallback micro-confirmation using momentum candles.
        Requires price to be near the entry zone (zone proximity gate)
        and at least one momentum candle backed by above-average volume.
        """
        if len(df) < 5:
            return False

        # ── Zone proximity gate ──────────────────────────────────────────
        # Momentum far from any entry zone is noise — real momentum that
        # matters happens AT or NEAR the zone.
        if entry_zone and entry_zone.get("type", "NONE") != "NONE":
            zone_top = entry_zone["top"]
            zone_bottom = entry_zone["bottom"]
            last_low = float(df["low"].iloc[-1])
            last_high = float(df["high"].iloc[-1])
            # Use profile proximity pips — synthetics/indices need much wider
            # windows than forex (15–20 pips vs 3 pips).
            proximity_pips = profile.fvg_proximity_pips if profile else 3.0
            proximity = proximity_pips * pip_size

            if direction == "LONG":
                if last_low > zone_top + proximity:
                    return False
            elif direction == "SHORT":
                if last_high < zone_bottom - proximity:
                    return False

        recent = df.iloc[-5:]
        closes = recent["close"].values
        opens  = recent["open"].values

        # ── Volume filter ────────────────────────────────────────────────
        # At least one of the momentum candles should have tick_volume above
        # the 20-bar average — real momentum is backed by participation.
        volume_ok = True
        if "tick_volume" in df.columns and len(df) >= 20:
            avg_vol = float(df["tick_volume"].iloc[-20:].mean())
            recent_vols = recent["tick_volume"].values
            if direction == "LONG":
                momentum_mask = [c > o for c, o in zip(closes, opens)]
            else:
                momentum_mask = [c < o for c, o in zip(closes, opens)]
            has_vol_candle = any(
                m and float(v) > avg_vol
                for m, v in zip(momentum_mask, recent_vols)
            )
            volume_ok = has_vol_candle

        if not volume_ok:
            return False

        if direction == "LONG":
            bullish_count = sum(1 for c, o in zip(closes, opens) if c > o)
            if bullish_count >= 3:
                return True
            if closes[-1] > opens[-1] and closes[-2] > opens[-2] and closes[-1] > closes[-2]:
                return True
            lows = recent["low"].values
            if lows[-1] > lows[-3] and closes[-1] > closes[-3]:
                return True

        elif direction == "SHORT":
            bearish_count = sum(1 for c, o in zip(closes, opens) if c < o)
            if bearish_count >= 3:
                return True
            if closes[-1] < opens[-1] and closes[-2] < opens[-2] and closes[-1] < closes[-2]:
                return True
            highs = recent["high"].values
            if highs[-1] < highs[-3] and closes[-1] < closes[-3]:
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

    @staticmethod
    def _determine_entry_timeframe(zone: dict) -> str:
        """Derive entry timeframe from the zone's FVG or OB origin."""
        fvg = zone.get("fvg")
        if fvg and hasattr(fvg, "timeframe") and fvg.timeframe:
            return fvg.timeframe
        ob = zone.get("ob")
        if ob and hasattr(ob, "timeframe") and ob.timeframe:
            return ob.timeframe
        return "M5"
