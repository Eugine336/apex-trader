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
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

from config import AppConfig, get_instrument, get_pip_size
from brain.instrument_profile import InstrumentProfile
from brain.market_data_utils import drop_forming_bar
from brain.structure_engine import StructureEngine
from brain.fvg_detector import FVGDetector, FairValueGap
from brain.order_block import OrderBlockDetector, OrderBlock, OBStatus
from brain.liquidity_mapper import LiquidityMapper
from brain.drawdown_guard import DrawdownGuard
from brain.session_engine import NewsGuard, SessionEngine
from trigger.entry_patterns import EntryPatternDetector


def _gate_quality_multiplier(measures: list, floor: float = 0.15) -> float:
    """Bounded quality multiplier for the softened conviction gate (inlined).

    Formerly ``brain.orchestrator.gate_quality_multiplier`` — kept inline so the
    entry engine carries no dependency on the retired legacy orchestrator. For
    each ``(value, threshold)`` a shortfall contributes ``value / threshold``
    (<1.0); the product is bounded to ``[floor, 1.0]``.
    """
    mult = 1.0
    for value, threshold in measures:
        try:
            threshold = float(threshold)
            value = float(value)
        except (TypeError, ValueError):
            continue
        if threshold <= 0:
            continue
        ratio = value / threshold
        if ratio < 1.0:
            mult *= max(0.0, ratio)
    return max(floor, min(1.0, mult))


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
    # Phase 9 gate softening: bounded [gate_floor, 1.0] quality multiplier set
    # when the entry-score floor was softened (orchestrator live) — the signal
    # is emitted carrying this factor instead of an EntryRejection, and the
    # orchestrator folds it into graded size. 1.0 = score floor passed cleanly.
    entry_quality_multiplier: float = 1.0
    # #20 — continuous market-readiness [0,1] behind the MARKET/PENDING choice.
    # The legacy decision is a hard threshold cascade (one of two modes); when
    # graded entry-mode is enabled this carries the smooth score so a 3.0-vs-3.1
    # pip near-miss is visible instead of a silent mode flip. Default 1.0.
    entry_mode_confidence: float = 1.0


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
        # Reusable liquidity mapper + memo for calculate_targets. The H1 liquidity
        # map and structure analysis depend only on (h1_df, pip_size) — not the
        # entry price — so when calculate_targets is invoked more than once per
        # candidate (e.g. a MARKET entry re-prices at the live tick) the heavy
        # map()/analyze() pass is computed once and reused.
        self._targets_liq_mapper = LiquidityMapper()
        self._targets_struct_cache: Optional[tuple] = None
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
        # Optional shadow-fed gate tuner (set by the trading loop). When present,
        # it can LOWER the entry score bar within a bounded envelope if the
        # engine's rejected setups keep winning. Neutral (None) by default.
        self.gate_tuner = None
        # Optional PairLearner (set by the trading loop). When present, it RAISES
        # the entry-score bar for cold-start (unproven) symbols so only
        # high-quality setups enter while a pair has no statistical edge yet.
        # Neutral (None) by default.
        self.pair_learner = None

    # ------------------------------------------------------------------
    # Main entry calculation
    # ------------------------------------------------------------------

    # ── Retired directional entry decider — REMOVED ──────────────────
    # ``calculate_entry`` (+ ``_htf_penalty_scale`` / ``_entry_gate_softening``
    # / ``_market_readiness`` / ``_decide_entry_mode``) was a directional
    # decider that graded a scan into a LONG/SHORT score and vetoed / penalised
    # counter-higher-timeframe setups — a higher-timeframe DIRECTION GATE the
    # constitution forbids (higher timeframe is context, not command). It had
    # no live call site (the single Cognitive Brain originates entries) and is
    # physically removed. The zone / stop / target / sizing helpers below
    # remain as live, non-directional execution utilities.

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
        atr_mult_override: Optional[float] = None,
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
            atr_mult_override=atr_mult_override,
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
        atr_mult_override: Optional[float] = None,
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
        # L5.5b: a profile may override the ATR stop multiple for its style
        # (e.g. a tight scalp at 1.0× vs a wide position at 3.0×). The clamp,
        # max-risk guard and structure fallback below are unchanged.
        if atr_mult_override is not None and atr_mult_override > 0.0:
            atr_distance = float(atr_value) * float(atr_mult_override)
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

    @staticmethod
    def _apply_sl_floor(
        *,
        direction: str,
        entry_price: float,
        stop_loss: float,
        risk_distance: float,
        pip_size: float,
        category: str,
        min_risk_distance: float,
        pair: str = "",
        atr: Optional[float] = None,
    ) -> tuple[float, float]:
        """Widen a real-but-too-tight stop up to the per-category minimum distance.

        A tight entry zone — or a low-volatility ATR stop that lands inside the
        minimum — can produce a sub-minimum SL that the risk-distance gate rejects
        outright (e.g. a 2.1-pip forex stop below the 5.0-pip floor), killing an
        otherwise valid setup. Rather than dropping it, we floor the stop to the
        minimum so the trade proceeds with a sane risk distance.

        Synthetics/crypto floor to a VOLATILITY-aware minimum (1×ATR) so the
        stop reflects what the instrument is actually doing rather than a fixed
        price percentage; forex, commodities and indices floor to the
        per-category ``min_risk_pips`` distance. Only a real-but-too-tight stop
        is widened — degenerate zones (< 1 pip) are left untouched so the
        caller's invalid-zone rejection still fires.

        Returns the (possibly widened) ``(stop_loss, risk_distance)`` pair.
        """
        if category in ("synthetic", "crypto"):
            if atr is not None and math.isfinite(atr) and atr > 0:
                # Volatility-aware minimum: a synthetic/crypto stop should be at
                # least one ATR wide instead of a hardcoded price percentage.
                floor_distance = float(atr)
            else:
                # Safety net only when ATR is unavailable (thin data).
                floor_distance = entry_price * (0.003 if category == "synthetic" else 0.0015)
        else:
            floor_distance = min_risk_distance

        if not (pip_size <= risk_distance < floor_distance):
            return stop_loss, risk_distance

        original_pips = risk_distance / pip_size if pip_size > 0 else 0.0
        floored_sl = (
            entry_price - floor_distance if direction.upper() in ("LONG", "BUY") else entry_price + floor_distance
        )
        logger.info(
            "[{}] SL floor applied — risk distance widened {:.1f} -> {:.1f} pips (min {:.1f} for {})",
            pair or "unknown",
            original_pips,
            floor_distance / pip_size if pip_size > 0 else 0.0,
            min_risk_distance / pip_size if pip_size > 0 else 0.0,
            category,
        )
        return floored_sl, floor_distance

    def _targets_market_structure(self, h1_df: pd.DataFrame, pip_size: float):
        """Return (liq_map, h1_analysis) for an H1 frame, memoized per frame.

        Both depend only on (h1_df, pip_size); caching them by frame identity +
        pip_size avoids rebuilding LiquidityMapper/StructureEngine on repeated
        calculate_targets calls for the same candidate within a cycle.
        """
        cache = self._targets_struct_cache
        if cache is not None and cache[0] is h1_df and cache[1] == pip_size:
            return cache[2], cache[3]
        liq_map = self._targets_liq_mapper.map(h1_df, pip_size)
        h1_analysis = StructureEngine(pip_size=pip_size).analyze(h1_df)
        self._targets_struct_cache = (h1_df, pip_size, liq_map, h1_analysis)
        return liq_map, h1_analysis

    def calculate_targets(
        self,
        pair: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        h1_df: pd.DataFrame,
        pip_size: float,
        *,
        tp1_rr: float = 1.5,
        tp2_rr: float = 3.0,
    ) -> tuple[float, float]:
        risk = abs(entry_price - stop_loss)
        # L5.5b: profile-supplied reward:risk targets (default 1.5 / 3.0 — the
        # pre-profile constants, so a None/disabled profile is identical). The
        # structure-target gate (>= 2.5R candidate) and TP1-floor logic below
        # are unchanged; only the fallback R multiples are parameterised.
        try:
            tp1_rr = float(tp1_rr) if float(tp1_rr) > 0 else 1.5
        except (TypeError, ValueError):
            tp1_rr = 1.5
        try:
            tp2_rr = float(tp2_rr) if float(tp2_rr) > 0 else 3.0
        except (TypeError, ValueError):
            tp2_rr = 3.0

        liq_map, h1_analysis = self._targets_market_structure(h1_df, pip_size)

        if direction == "LONG":
            tp1_liq = liq_map.nearest_buy_liq
            tp1 = tp1_liq.price if tp1_liq else entry_price + risk * tp1_rr
            if tp1 - entry_price < risk:
                tp1 = entry_price + risk * tp1_rr

            # Use correct pip_size so min_swing_size filters out noise swings.
            # Default pip_size=0.0001 on synthetics/indices gives near-zero threshold
            # and returns tiny intracandle highs as "swing highs" → TP2 too close.
            tp2_candidate = h1_analysis.swing_high
            if (
                tp2_candidate
                and tp2_candidate > tp1  # must be beyond TP1
                and tp2_candidate > entry_price  # SANITY: must be above entry on LONG
                and (tp2_candidate - entry_price) / risk >= 2.5
            ):  # must give at least 2.5R
                tp2 = tp2_candidate
            else:
                tp2 = max(entry_price + risk * tp2_rr, tp1 + risk * 1.5)

            # Final sanity: if tp1 or tp2 ended up on wrong side, force correct direction
            if tp1 <= entry_price:
                tp1 = entry_price + risk * tp1_rr
                logger.warning("TP1 sanity fix on LONG {} — was below entry, reset to 1.5R", pair)
            if tp2 <= tp1:
                tp2 = max(entry_price + risk * tp2_rr, tp1 + risk * 1.5)
                logger.warning("TP2 sanity fix on LONG {} — was below TP1, reset beyond TP1", pair)

        else:  # SHORT
            tp1_liq = liq_map.nearest_sell_liq
            tp1 = tp1_liq.price if tp1_liq else entry_price - risk * tp1_rr
            if entry_price - tp1 < risk:
                tp1 = entry_price - risk * tp1_rr

            tp2_candidate = h1_analysis.swing_low
            if (
                tp2_candidate
                and tp2_candidate < tp1  # must be beyond TP1
                and tp2_candidate < entry_price  # SANITY: must be below entry on SHORT
                and (entry_price - tp2_candidate) / risk >= 2.5
            ):  # must give at least 2.5R
                tp2 = tp2_candidate
            else:
                tp2 = min(entry_price - risk * tp2_rr, tp1 - risk * 1.5)

            # Final sanity: if tp1 or tp2 ended up on wrong side, force correct direction
            if tp1 >= entry_price:
                tp1 = entry_price - risk * tp1_rr
                logger.warning("TP1 sanity fix on SHORT {} — was above entry, reset to 1.5R", pair)
            if tp2 >= tp1:
                tp2 = min(entry_price - risk * tp2_rr, tp1 - risk * 1.5)
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
            # A zero/negative stop distance is invalid — refuse to size rather
            # than silently defaulting to the minimum lot (which would place a
            # real trade with an undefined risk). 0.0 signals "no valid size".
            logger.warning(
                "[entry_engine] {} risk_pips ≤ 0 (entry={}, sl={}) — refusing to size",
                pair or "trade", entry_price, stop_loss,
            )
            return 0.0

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
            ps = get_pip_size(symbol)
        except KeyError:
            return 0.0001
        # A misconfigured registry entry (pip_size <= 0) would crash every
        # downstream ``/ pip_size`` site with ZeroDivisionError. Clamp to a
        # safe positive default so risk/RR math degrades gracefully instead.
        return float(ps) if ps and ps > 0 else 0.0001

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
