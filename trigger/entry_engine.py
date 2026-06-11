"""
APEX TRADER — Entry Engine
The sniper's trigger finger. Takes a READY scan result and computes
the exact entry price, stop loss, TP1, TP2, and position size.
Uses M1 micro-confirmation as a graduated score contributor.

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
    direction: str  # "LONG" or "SHORT"
    entry_type: str  # "FVG_MIDPOINT", "OB_MIDPOINT", "FVG_OB_OVERLAP", "SWEEP_REVERSAL"
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
    # ── Entry mode decision ─────────────────────────────────────────────
    # Set by EntryEngine._decide_entry_mode(). Tells execution layer whether
    # to fire a market order immediately or place a limit/stop pending order.
    # "MARKET"  — price is inside or right at the zone; enter now
    # "PENDING" — price has not yet reached the zone; wait for retrace
    entry_mode: str = "PENDING"  # default conservative; engine overrides this


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
    Uses M1 micro-confirmation as a graded score adjustment.
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        volatility_stop_mode: Optional[str] = None,
        atr_stop_period: Optional[int] = None,
        atr_stop_mult: Optional[float] = None,
        atr_stop_ratio_min: Optional[float] = None,
        atr_stop_ratio_max: Optional[float] = None,
        atr_stop_max_risk_mult: Optional[float] = None,
    ):
        self.config = config or AppConfig()
        self.structure = StructureEngine()
        self.drawdown = DrawdownGuard()
        self.pattern_detector = EntryPatternDetector()
        self.news_guard = NewsGuard()
        self.session_engine = SessionEngine()
        risk_cfg = self.config.risk
        self._volatility_stop_mode = (
            volatility_stop_mode if volatility_stop_mode is not None else risk_cfg.volatility_stop_mode
        ).lower()
        self._atr_stop_period = int(atr_stop_period if atr_stop_period is not None else risk_cfg.atr_stop_period)
        self._atr_stop_mult = float(atr_stop_mult if atr_stop_mult is not None else risk_cfg.atr_stop_mult)
        self._atr_stop_ratio_min = float(
            atr_stop_ratio_min if atr_stop_ratio_min is not None else risk_cfg.atr_stop_ratio_min
        )
        self._atr_stop_ratio_max = float(
            atr_stop_ratio_max if atr_stop_ratio_max is not None else risk_cfg.atr_stop_ratio_max
        )
        self._atr_stop_max_risk_mult = float(
            atr_stop_max_risk_mult if atr_stop_max_risk_mult is not None else risk_cfg.atr_stop_max_risk_mult
        )

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
                f"[{pair}] Entry rejected — unknown instrument, not in registry, refusing to assume economics"
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
        if (
            spread_open_guard_applies(pair)
            and session_status.current_session in ("LONDON", "NEW_YORK")
            and session_status.session_open_minutes <= 15
        ):
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
                score=score,
                timestamp=now,
            )

        # ── H4 bias gate (default OFF) ───────────────────────────────────
        # When enabled, rejects entries where H4 trend directly contradicts
        # the trade direction. Can only reject, never widen risk.
        if self.config.risk.h4_bias_gate_enabled and h4_df is not None and len(h4_df) >= 20:
            from brain.structure_engine import StructureEngine as _SE

            h4_structure = _SE(pip_size=pip_size).analyze(h4_df)
            h4_trend = h4_structure.trend.value
            if direction == "LONG" and h4_trend == "BEARISH":
                return EntryRejection(
                    pair=pair,
                    reason="H4 bias gate — LONG entry rejected, H4 trend is BEARISH",
                    score=score,
                    timestamp=now,
                    direction=direction,
                )
            if direction == "SHORT" and h4_trend == "BULLISH":
                return EntryRejection(
                    pair=pair,
                    reason="H4 bias gate — SHORT entry rejected, H4 trend is BULLISH",
                    score=score,
                    timestamp=now,
                    direction=direction,
                )

        zone = self.find_entry_zone(pair, direction, m5_df, pip_size, profile)
        if zone["type"] == "NONE":
            return EntryRejection(
                pair=pair,
                reason="No valid entry zone (FVG or OB) found on M5",
                score=score,
                timestamp=now,
            )

        if len(m1_df) < 3:
            return EntryRejection(
                pair=pair,
                reason=f"Insufficient M1 bars ({len(m1_df)}) for momentum scoring",
                score=score,
                timestamp=now,
                entry_price=zone.get("midpoint"),
                direction=direction,
            )

        recent_closes = m1_df["close"].iloc[-5:].values if len(m1_df) >= 5 else []
        stale = len(recent_closes) > 0 and len(set(round(float(c), 5) for c in recent_closes)) == 1
        if stale:
            return EntryRejection(
                pair=pair,
                reason="Stale M1 feed — broker not sending new ticks",
                score=score,
                timestamp=now,
                entry_price=zone.get("midpoint"),
                direction=direction,
            )

        zone_top = zone["top"]
        zone_bottom = zone["bottom"]
        pattern_name, _pattern_desc = self.pattern_detector.get_best_pattern(
            drop_forming_bar(m1_df),
            direction,
            zone_top,
            zone_bottom,
            pip_size,
        )
        choch_or_bos = self._detect_m1_choch(
            m1_df,
            direction,
            profile=profile,
            entry_zone=zone,
            pip_size=pip_size,
        )

        pattern_scores = {
            "engulfing": 3,
            "pin_bar": 2,
            "rejection_wick": 2,
            "volume_spike": 1,
            "inside_bar_breakout": 1,
        }
        pattern_score = pattern_scores.get(pattern_name, 0)
        pattern_label = pattern_name or "none"
        if choch_or_bos and pattern_score < 5:
            pattern_score = 5
            pattern_label = "choch_bos"

        recent = m1_df.iloc[-5:] if len(m1_df) >= 5 else m1_df.iloc[-3:]
        n = len(recent)
        if direction == "LONG":
            aligned = sum(1 for _, row in recent.iterrows() if float(row["close"]) > float(row["open"]))
        else:
            aligned = sum(1 for _, row in recent.iterrows() if float(row["close"]) < float(row["open"]))

        ratio = aligned / n if n else 0.0
        if ratio >= 1.0:
            momentum_score = 5
        elif ratio >= 0.8:
            momentum_score = 3
        elif ratio >= 0.6:
            momentum_score = 0
        elif ratio >= 0.4:
            momentum_score = -5
        elif ratio >= 0.2:
            momentum_score = -10
        else:
            momentum_score = -15

        m1_adjustment = max(-15, min(10, pattern_score + momentum_score))
        score += m1_adjustment

        aligned_on_five = int(round(ratio * 5))
        confluences.append(f"M1 pattern: {pattern_label} ({pattern_score:+d})")
        confluences.append(f"M1 momentum: {aligned_on_five}/5 ({momentum_score:+d})")
        confluences.append(f"M1 net adjustment: {m1_adjustment:+d}")

        if score < effective_min_score:
            return EntryRejection(
                pair=pair,
                reason=(f"Score {score} dropped below {effective_min_score} after M1 adjustment ({m1_adjustment:+d})"),
                score=score,
                timestamp=now,
                entry_price=zone.get("midpoint"),
                direction=direction,
            )

        if pattern_label != "none":
            micro_confirmation = pattern_label
        elif momentum_score > 0:
            micro_confirmation = "momentum_only"
        else:
            micro_confirmation = "no_confirmation"

        if zone.get("has_sweep"):
            confluences.append("Liquidity sweep confirmed at entry zone")

        min_risk_distance = profile.min_risk_pips * pip_size

        def _compute_pricing(target_entry_price: float):
            stop_loss_local = self.calculate_stop_loss(
                direction,
                zone,
                pip_size,
                profile.sl_buffer_pips,
                entry_price=target_entry_price,
                pair=pair,
                m5_df=m5_df,
            )
            risk_distance_local = abs(target_entry_price - stop_loss_local)

            # ── SL floor for synthetics and crypto — MUST run before calculate_targets ──
            # calculate_targets uses risk_distance to validate the 2.5R minimum for TP2.
            # If the floor widens the SL AFTER targets are set, the effective R:R collapses
            # and the validator rejects a perfectly good setup with "R:R to TP2 below minimum".
            # Fix: apply the floor here so calculate_targets sees the real risk distance.
            if category == "synthetic":
                pct_floor = target_entry_price * 0.003  # 0.3%
                if risk_distance_local < pct_floor:
                    stop_loss_local = (
                        target_entry_price - pct_floor if direction == "LONG" else target_entry_price + pct_floor
                    )
                    risk_distance_local = pct_floor
            elif category == "crypto":
                pct_floor = target_entry_price * 0.0015  # 0.15%
                if risk_distance_local < pct_floor:
                    stop_loss_local = (
                        target_entry_price - pct_floor if direction == "LONG" else target_entry_price + pct_floor
                    )
                    risk_distance_local = pct_floor

            tp1_local, tp2_local = self.calculate_targets(
                pair, direction, target_entry_price, stop_loss_local, h1_df, pip_size
            )

            # Percentage-based SL floor already applied above — skip duplicate block.
            if risk_distance_local < pip_size:
                return EntryRejection(
                    pair=pair,
                    reason="Risk distance too small — invalid zone",
                    score=score,
                    timestamp=now,
                    entry_price=target_entry_price,
                    stop_loss=stop_loss_local,
                    direction=direction,
                )
            if risk_distance_local < min_risk_distance:
                return EntryRejection(
                    pair=pair,
                    reason=f"Risk distance {risk_distance_local / pip_size:.1f} pips below minimum {profile.min_risk_pips} for {category}",
                    score=score,
                    timestamp=now,
                    entry_price=target_entry_price,
                    stop_loss=stop_loss_local,
                    direction=direction,
                )

            rr1_local = abs(tp1_local - target_entry_price) / risk_distance_local
            rr2_local = abs(tp2_local - target_entry_price) / risk_distance_local

            if rr1_local < 1.0:
                return EntryRejection(
                    pair=pair,
                    reason=f"Insufficient reward — R:R to TP1 is {rr1_local:.2f}",
                    score=score,
                    timestamp=now,
                    entry_price=target_entry_price,
                    stop_loss=stop_loss_local,
                    direction=direction,
                )

            risk_pips_local = risk_distance_local / pip_size
            position_size_local = self.calculate_position_size(
                target_entry_price,
                stop_loss_local,
                risk_pct,
                account_balance,
                pip_size,
                pair,
            )
            return (
                target_entry_price,
                stop_loss_local,
                tp1_local,
                tp2_local,
                risk_distance_local,
                rr1_local,
                rr2_local,
                risk_pips_local,
                position_size_local,
            )

        zone_midpoint = float(zone["midpoint"])
        pricing = _compute_pricing(zone_midpoint)
        if isinstance(pricing, EntryRejection):
            return pricing
        (
            entry_price,
            stop_loss,
            tp1,
            tp2,
            risk_distance,
            rr1,
            rr2,
            risk_pips,
            position_size,
        ) = pricing

        zone_desc = self._describe_zone(zone, pip_size)
        entry_timeframe = self._determine_entry_timeframe(zone)

        expiry_minutes = {
            "M1": 10,
            "M5": 20,
            "M15": 45,
            "H1": 120,
            "H4": 240,
        }.get(entry_timeframe, 25)
        valid_until = now + timedelta(minutes=expiry_minutes)

        # ── Intelligent entry mode decision ──────────────────────────────
        # Decide HERE in the brain — not blindly in the execution layer.
        # The execution layer reads signal.entry_mode and honours it.
        try:
            current_tick = m1_df["close"].iloc[-1]
        except Exception:
            current_tick = zone_midpoint  # fallback: treat as at-price

        entry_mode = self._decide_entry_mode(
            direction=direction,
            current_price=float(current_tick),
            entry_price=zone_midpoint,
            zone=zone,
            micro_confirmation=micro_confirmation,
            has_sweep=bool(zone.get("has_sweep")),
            pip_size=pip_size,
            score=score,
            risk_reward=rr1,
            momentum_score=momentum_score,
        )

        if entry_mode == "MARKET":
            market_entry = float(current_tick)
            if math.isfinite(market_entry):
                market_pricing = _compute_pricing(market_entry)
                if isinstance(market_pricing, EntryRejection):
                    return market_pricing
                (
                    entry_price,
                    stop_loss,
                    tp1,
                    tp2,
                    risk_distance,
                    rr1,
                    rr2,
                    risk_pips,
                    position_size,
                ) = market_pricing

        # ── Non-finite price guard ───────────────────────────────────────
        _price_fields = {
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "risk_distance": risk_distance,
            "rr1": rr1,
            "rr2": rr2,
        }
        for _name, _val in _price_fields.items():
            if not math.isfinite(_val):
                logger.error(
                    "[{}] Non-finite {} ({}) in signal — rejecting",
                    pair,
                    _name,
                    _val,
                )
                return EntryRejection(
                    pair=pair,
                    reason=f"Non-finite {_name} ({_val}) in signal — rejecting",
                    score=score,
                    timestamp=now,
                    entry_price=entry_price if math.isfinite(entry_price) else None,
                    stop_loss=stop_loss if math.isfinite(stop_loss) else None,
                    direction=direction,
                )

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
            micro_confirmation=micro_confirmation,
            timestamp=now,
            valid_until=valid_until,
            instrument_category=category,
            entry_timeframe=entry_timeframe,
            entry_mode=entry_mode,
        )

        logger.info(
            f"[{pair}] ENTRY SIGNAL — {direction} @ {signal.entry_price} | "
            f"SL {signal.stop_loss} | TP1 {signal.tp1} | TP2 {signal.tp2} | "
            f"R:R {signal.risk_reward_1}/{signal.risk_reward_2} | "
            f"{signal.position_size_lots} lots | Score {score} | "
            f"entry_mode={entry_mode}"
        )
        return signal

    # ------------------------------------------------------------------
    # Intelligent entry mode decision
    # ------------------------------------------------------------------

    def _decide_entry_mode(
        self,
        direction: str,
        current_price: float,
        entry_price: float,
        zone: dict,
        micro_confirmation: str,
        has_sweep: bool,
        pip_size: float,
        *,
        score: int = 0,
        risk_reward: float = 0.0,
        momentum_score: int = 0,
    ) -> str:
        """
        Decide whether to enter at market NOW or place a pending limit/stop order.

        Returns "MARKET" or "PENDING".

        Rules (priority order):
        1. MARKET — Price is already inside the zone. A limit would never fill cleanly.
        2. MARKET — High-score momentum continuation:
                    score >= 80, momentum_only, momentum score >= +3, and <= 5 pips away.
        3. MARKET — Good at-market economics:
                    R:R >= 2.5 and <= 3 pips from zone midpoint.
        4. MARKET — Price within 3 pips AND strong M1 confirmation
                    (choch_bos, engulfing, pin_bar, rejection_wick).
        5. MARKET — Price within 2 pips AND moderate confirmation (momentum_only).
        6. MARKET — Liquidity sweep confirmed at zone within 5 pips.
        7. MARKET — Sweep + any confirmation within 7 pips.
        8. PENDING — Otherwise, wait for retrace to zone.
        """
        zone_top = zone.get("top", entry_price)
        zone_bottom = zone.get("bottom", entry_price)
        distance_pips = abs(current_price - entry_price) / pip_size

        # Rule 1: price already inside zone
        if zone_bottom <= current_price <= zone_top:
            logger.debug(
                "[entry_mode] MARKET — price {:.5f} inside zone [{:.5f}-{:.5f}]",
                current_price,
                zone_bottom,
                zone_top,
            )
            return "MARKET"

        moderate_confirmation = micro_confirmation == "momentum_only"
        strong_momentum = moderate_confirmation and momentum_score >= 3
        effective_rr = risk_reward if math.isfinite(risk_reward) else 0.0

        # Rule 2: high-score momentum continuation near the zone
        if score >= 80 and strong_momentum and distance_pips <= 5.0:
            logger.debug(
                "[entry_mode] MARKET — high-score momentum continuation "
                "(score={}, momentum_score={}, distance={:.1f}p)",
                score,
                momentum_score,
                distance_pips,
            )
            return "MARKET"

        # Rule 3: strong R:R even at market
        if effective_rr >= 2.5 and distance_pips <= 3.0:
            logger.debug(
                "[entry_mode] MARKET — at-market R:R {:.2f} with distance {:.1f}p",
                effective_rr,
                distance_pips,
            )
            return "MARKET"

        strong_confirmation = micro_confirmation in ("choch_bos", "engulfing", "pin_bar", "rejection_wick")

        # Rule 4: close to zone + strong confirmation
        if distance_pips <= 3.0 and strong_confirmation:
            logger.debug(
                "[entry_mode] MARKET — {:.1f} pips from zone, confirmation={}",
                distance_pips,
                micro_confirmation,
            )
            return "MARKET"

        # Rule 5: close to zone + moderate confirmation
        if distance_pips <= 2.0 and moderate_confirmation:
            logger.debug(
                "[entry_mode] MARKET — {:.1f} pips from zone, moderate confirmation={}",
                distance_pips,
                micro_confirmation,
            )
            return "MARKET"

        # Rule 6: sweep confirmed at zone
        if has_sweep and distance_pips <= 5.0:
            logger.debug(
                "[entry_mode] MARKET — sweep confirmed at zone, {:.1f} pips from midpoint",
                distance_pips,
            )
            return "MARKET"

        # Rule 7: sweep + any confirmation can still justify market execution
        if has_sweep and micro_confirmation != "no_confirmation" and distance_pips <= 7.0:
            logger.debug(
                "[entry_mode] MARKET — sweep+confirmation, {:.1f} pips from midpoint",
                distance_pips,
            )
            return "MARKET"

        # Rule 8: price already moved past zone in trade direction — pending useless
        price_past_short = direction == "SHORT" and current_price < zone_bottom
        price_past_long = direction == "LONG" and current_price > zone_top
        if price_past_short or price_past_long:
            logger.debug(
                "[entry_mode] MARKET — price {:.5f} already past zone "
                "[{:.5f}-{:.5f}] in {} direction",
                current_price,
                zone_bottom,
                zone_top,
                direction,
            )
            return "MARKET"

        # Rule 9: place pending order, wait for retrace
        logger.debug(
            "[entry_mode] PENDING — {:.1f} pips from zone, confirmation={}, "
            "sweep={}, score={}, rr={:.2f}, momentum_score={:+d}",
            distance_pips,
            micro_confirmation,
            has_sweep,
            score,
            effective_rr,
            momentum_score,
        )
        return "PENDING"

    # ------------------------------------------------------------------
    # Entry zone discovery on M5
    # ------------------------------------------------------------------

    def find_entry_zone(
        self,
        pair: str,
        direction: str,
        m5_df: pd.DataFrame,
        pip_size: float,
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
        self,
        direction: str,
        m1_df: pd.DataFrame,
        entry_zone: dict,
        pip_size: float,
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
            drop_forming_bar(m1_df),
            direction,
            zone_top,
            zone_bottom,
            pip_size,
        )
        logger.debug(f"M1 pattern result — name='{pattern_name}' desc='{pattern_desc}'")
        if pattern_name:
            return True, pattern_desc

        choch = self._detect_m1_choch(m1_df, direction, profile=profile, entry_zone=entry_zone, pip_size=pip_size)
        logger.debug(f"M1 CHoCH result — {choch}")
        if choch:
            return True, f"M1 Change of Character — {direction.lower()} shift"

        return False, ""

    # ------------------------------------------------------------------
    # Stop loss, targets, position sizing
    # ------------------------------------------------------------------

    def calculate_stop_loss(
        self,
        direction: str,
        entry_zone: dict,
        pip_size: float,
        buffer_pips: float = 2.0,
        *,
        entry_price: Optional[float] = None,
        pair: str = "",
        m5_df: Optional[pd.DataFrame] = None,
    ) -> float:
        structure_sl = self._calculate_structure_stop_loss(
            direction,
            entry_zone,
            pip_size,
            buffer_pips,
        )
        if self._volatility_stop_mode != "on":
            return structure_sl
        if entry_price is None or m5_df is None:
            return structure_sl

        return self._calculate_volatility_stop_loss(
            direction=direction,
            entry_price=float(entry_price),
            structure_sl=structure_sl,
            m5_df=m5_df,
            pair=pair,
            pip_size=pip_size,
        )

    def _calculate_structure_stop_loss(
        self,
        direction: str,
        entry_zone: dict,
        pip_size: float,
        buffer_pips: float = 2.0,
    ) -> float:
        buffer = buffer_pips * pip_size
        if direction == "LONG":
            return entry_zone["bottom"] - buffer
        return entry_zone["top"] + buffer

    def _calculate_volatility_stop_loss(
        self,
        *,
        direction: str,
        entry_price: float,
        structure_sl: float,
        m5_df: pd.DataFrame,
        pair: str,
        pip_size: float,
    ) -> float:
        from brain.volatility_stop import latest_atr

        if len(m5_df) < self._atr_stop_period:
            logger.debug(
                "[ATR SL] {} has {} M5 bars (< {}) — using structure SL {:.6f}",
                pair or "unknown",
                len(m5_df),
                self._atr_stop_period,
                structure_sl,
            )
            return structure_sl

        atr_value = latest_atr(m5_df, self._atr_stop_period)
        if atr_value is None:
            logger.debug(
                "[ATR SL] ATR unavailable for {} — using structure SL {:.6f}",
                pair or "unknown",
                structure_sl,
            )
            return structure_sl

        structure_distance = abs(entry_price - structure_sl)
        if not math.isfinite(structure_distance) or structure_distance <= 0:
            return structure_sl

        atr_distance = float(atr_value) * self._atr_stop_mult
        if not math.isfinite(atr_distance) or atr_distance <= 0:
            return structure_sl

        raw_ratio = atr_distance / structure_distance
        clamped_ratio = max(
            self._atr_stop_ratio_min,
            min(raw_ratio, self._atr_stop_ratio_max),
        )
        final_distance = structure_distance * clamped_ratio
        max_allowed_distance = structure_distance * self._atr_stop_max_risk_mult
        if final_distance > max_allowed_distance:
            logger.debug(
                "[ATR SL] {} ATR distance {:.6f} exceeds max {:.6f} (x{:.2f}) — using structure SL {:.6f}",
                pair or "unknown",
                final_distance,
                max_allowed_distance,
                self._atr_stop_max_risk_mult,
                structure_sl,
            )
            return structure_sl

        if not math.isfinite(final_distance) or final_distance <= 0:
            return structure_sl

        atr_sl = entry_price - final_distance if direction.upper() in ("LONG", "BUY") else entry_price + final_distance
        if not math.isfinite(atr_sl):
            return structure_sl
        if direction.upper() in ("LONG", "BUY") and atr_sl >= entry_price:
            return structure_sl
        if direction.upper() in ("SHORT", "SELL") and atr_sl <= entry_price:
            return structure_sl

        if abs(atr_sl - structure_sl) > 1e-12:
            risk_delta_pips = 0.0
            if pip_size > 0:
                risk_delta_pips = (abs(entry_price - atr_sl) - abs(entry_price - structure_sl)) / pip_size
            logger.info(
                "[{}] ATR stop override — structure {:.5f} -> ATR {:.5f} (risk delta {:+.1f} pips)",
                pair or "unknown",
                structure_sl,
                atr_sl,
                risk_delta_pips,
            )

        return atr_sl

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
            if (
                tp2_candidate
                and tp2_candidate > tp1  # must be beyond TP1
                and tp2_candidate > entry_price  # SANITY: must be above entry on LONG
                and (tp2_candidate - entry_price) / risk >= 2.5
            ):  # must give at least 2.5R
                tp2 = tp2_candidate
            else:
                tp2 = max(entry_price + risk * 3.0, tp1 + risk * 1.5)

            # Final sanity: if tp1 or tp2 ended up on wrong side, force correct direction
            if tp1 <= entry_price:
                tp1 = entry_price + risk * 1.5
                logger.warning("TP1 sanity fix on LONG {} — was below entry, reset to 1.5R", pair)
            if tp2 <= tp1:
                tp2 = max(entry_price + risk * 3.0, tp1 + risk * 1.5)
                logger.warning("TP2 sanity fix on LONG {} — was below TP1, reset beyond TP1", pair)

        else:  # SHORT
            tp1_liq = liq_map.nearest_sell_liq
            tp1 = tp1_liq.price if tp1_liq else entry_price - risk * 1.5
            if entry_price - tp1 < risk:
                tp1 = entry_price - risk * 1.5

            structure = StructureEngine(pip_size=pip_size)
            h1_analysis = structure.analyze(h1_df)
            tp2_candidate = h1_analysis.swing_low
            if (
                tp2_candidate
                and tp2_candidate < tp1  # must be beyond TP1
                and tp2_candidate < entry_price  # SANITY: must be below entry on SHORT
                and (entry_price - tp2_candidate) / risk >= 2.5
            ):  # must give at least 2.5R
                tp2 = tp2_candidate
            else:
                tp2 = min(entry_price - risk * 3.0, tp1 - risk * 1.5)

            # Final sanity: if tp1 or tp2 ended up on wrong side, force correct direction
            if tp1 >= entry_price:
                tp1 = entry_price - risk * 1.5
                logger.warning("TP1 sanity fix on SHORT {} — was above entry, reset to 1.5R", pair)
            if tp2 >= tp1:
                tp2 = min(entry_price - risk * 3.0, tp1 - risk * 1.5)
                logger.warning("TP2 sanity fix on SHORT {} — was above TP1, reset beyond TP1", pair)

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
        self,
        df: pd.DataFrame,
        fvg: Optional[FairValueGap],
        ob: Optional[OrderBlock],
        pip_size: float,
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

    def _detect_m1_choch(
        self,
        df: pd.DataFrame,
        direction: str,
        profile: Optional["InstrumentProfile"] = None,
        entry_zone: Optional[dict] = None,
        pip_size: float = 0.0001,
    ) -> bool:
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

    def _detect_momentum_confirmation(
        self,
        df: pd.DataFrame,
        direction: str,
        entry_zone: Optional[dict] = None,
        pip_size: float = 0.0001,
        profile: Optional["InstrumentProfile"] = None,
    ) -> bool:
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
        opens = recent["open"].values

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
            has_vol_candle = any(m and float(v) > avg_vol for m, v in zip(momentum_mask, recent_vols))
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
