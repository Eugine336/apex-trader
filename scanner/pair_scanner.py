"""
APEX TRADER — Pair Scanner
The Eyes. Scans every instrument, runs all brain modules, scores each
setup on 7 confluence factors, and classifies it as READY / WATCHLIST / WAITING.

Pip sizes come from the instrument registry — NEVER hardcoded.
"""

import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

from config import (
    AppConfig, get_instrument, get_pip_size,
    is_always_open, is_session_gated,
    ConfirmationPenaltyConfig,
    LayeredDecisionConfig,
)
from brain.structure_engine import StructureEngine
from brain.fvg_detector import FVGDetector
from brain.order_block import OrderBlockDetector, OBStatus
from brain.liquidity_mapper import LiquidityMapper
from brain.currency_strength import CurrencyStrengthMeter, CURRENCY_PAIRS
from brain.directional_consensus import (
    Vote, decide,
    vote_from_structure, vote_from_currency_strength,
    vote_from_volume, vote_from_wyckoff,
    vote_from_order_blocks, vote_from_fvg,
    vote_from_liquidity, vote_from_momentum, vote_from_vwap,
)
from brain.session_engine import SessionEngine, NewsGuard
from adaptive.ev_estimator import EVEstimator
from brain.volume_analyzer import VolumeAnalyzer
from brain.inducement_detector import InducementDetector
from brain.wyckoff_engine import WyckoffEngine
from brain.instrument_profile import get_profile
from brain.session_vwap import session_vwap_penalty
from brain.momentum_divergence import momentum_divergence_penalty
from brain.atr_percentile import atr_percentile_penalty
from brain.volume_profile import volume_profile_poc_penalty
from brain.setup_quality import (
    compute_opportunity_quality, compute_entry_quality,
)
from rl.bridge import RLBridge
from rl.obs_builder import ObservationBuilder
from rl.multi_tf_obs_builder import MultiTFObservationBuilder

_MT5_AVAILABLE = False
try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None


def _mt5_market_open(symbol: str, connector=None) -> bool:
    """
    Returns True if MT5 reports this symbol as currently tradeable.
    Checks symbol_info().trade_mode — no hardcoded hours, works for any
    instrument. Called once per scan cycle per instrument to skip closed
    markets before running the full brain stack.

    Trade modes: 0=disabled, 1=long-only, 2=short-only, 3=close-only, 4=full
    Modes 1/2/3 are treated as open enough to scan (signal may still be
    useful by the time the market fully opens).
    """
    if not _MT5_AVAILABLE or mt5 is None:
        return True  # can't check — don't block

    mapped = symbol
    if connector is not None:
        try:
            mapped = connector.symbol_map(symbol)
        except Exception as exc:
            logger.warning("[scanner] symbol_map lookup failed: {}", exc)
            pass

    try:
        mt5.symbol_select(mapped, True)
        info = mt5.symbol_info(mapped)
        if info is None:
            return True  # unknown symbol — don't block scan
        # trade_mode 0 = fully disabled, 3 = close-only (session ending)
        # Both mean no new entries are possible
        return info.trade_mode not in (0,)
    except Exception as exc:
        logger.warning("[scanner] symbol tradability check failed, allowing scan: {}", exc)
        return True  # on any error, don't block


@dataclass
class PairScanResult:
    pair: str
    direction: str                  # "LONG", "SHORT", "NEUTRAL"
    score: int                      # 0–100
    regime: str                     # market regime from bias engine
    trend_h4: str
    trend_h1: str
    bias_strength: str              # "STRONG", "MODERATE", "CONFLICTED", "NONE"
    has_fvg: bool
    has_order_block: bool
    has_liquidity_target: bool
    sweep_detected: bool
    inducement_detected: bool       # reserved for Phase 1.5 module
    wyckoff_phase: str              # reserved for Phase 1.5 module
    volume_confirmation: bool       # reserved for Phase 1.5 module
    session_active: bool
    currency_strength_aligned: bool
    status: str                     # "READY", "WATCHLIST", "WAITING"
    timestamp: datetime
    confluences: list[str] = field(default_factory=list)
    instrument_category: str = "forex"
    ev_estimate: float = 0.0
    opportunity_score: float = 0.0
    rl_action: int = 0
    rl_confidence: float = 0.0
    rl_expected_r: float = 0.0
    rl_stage: int = 1
    consensus_direction: str = ""
    consensus_net: float = 0.0
    consensus_agreement: float = 0.0
    opportunity_quality: float = 0.0
    entry_quality: float = 0.0


@dataclass
class ScanReport:
    timestamp: datetime
    session: str
    total_pairs_scanned: int
    ready_count: int
    watchlist_count: int
    results: list[PairScanResult]
    best_setup: Optional[PairScanResult]
    regime_distribution: dict[str, int] = field(default_factory=dict)


def _side_agnostic_rr(liq_map, atr_pips, pip_size: float) -> float | None:
    """Compute a direction-agnostic reward/risk magnitude from structural objectives."""
    from scanner.rr_helper import compute_side_agnostic_rr
    return compute_side_agnostic_rr(
        buy_price=liq_map.nearest_buy_liq.price if liq_map.nearest_buy_liq is not None else None,
        sell_price=liq_map.nearest_sell_liq.price if liq_map.nearest_sell_liq is not None else None,
        current_price=liq_map.current_price,
        atr_pips=atr_pips,
        pip_size=pip_size,
    )


class PairScanner:
    """
    Always watching. Scans every enabled instrument, runs the full brain
    stack, and surfaces only the setups worth pulling the trigger on.
    """

    def __init__(self, config: Optional[AppConfig] = None, mt5_connector=None,
                 scoring_weights: Optional[dict[str, int]] = None,
                 rl_checkpoint: Optional[str] = None):
        self.config = config or AppConfig()
        self.structure = StructureEngine()
        self.fvg_detector = FVGDetector()
        self.ob_detector = OrderBlockDetector()
        self.liquidity = LiquidityMapper()
        self.strength_meter = CurrencyStrengthMeter()
        self.session = SessionEngine()
        self._ev_estimator = EVEstimator()
        self._trade_history: list[dict] = []
        self.news = NewsGuard()
        self.volume = VolumeAnalyzer()
        self.last_report: Optional[ScanReport] = None
        self._mt5_connector = mt5_connector
        self._adaptive_weights = scoring_weights

        # ── Quality failure tracking ──────────────────────────────────
        self._quality_failures: int = 0
        self._quality_scans: int = 0

        # ── RL subsystem ──────────────────────────────────────────────
        self._obs_builders: dict[str, ObservationBuilder] = {}
        self._mtf_builders: dict[str, MultiTFObservationBuilder] = {}
        checkpoint = rl_checkpoint or "checkpoints/apex_rl_best.pt"
        try:
            self._rl = RLBridge(checkpoint=checkpoint)
            logger.info(f"[scanner] RL subsystem loaded — stage {self._rl.authority.stage_label}")
        except Exception as exc:
            logger.warning(f"[scanner] RL subsystem unavailable: {exc}")
            self._rl = RLBridge(checkpoint=checkpoint, enabled=False)

    # ------------------------------------------------------------------
    # Quality failure tracking
    # ------------------------------------------------------------------

    def get_quality_failure_stats(self) -> tuple[int, int]:
        """Return (failures, total_scans) since last reset."""
        return self._quality_failures, self._quality_scans

    def reset_quality_failure_stats(self) -> None:
        """Reset quality failure counters (called by main loop after each scan cycle)."""
        self._quality_failures = 0
        self._quality_scans = 0

    # ------------------------------------------------------------------
    # Single-pair scan
    # ------------------------------------------------------------------

    def scan_pair(
        self,
        pair: str,
        h4_df: pd.DataFrame,
        h1_df: pd.DataFrame,
        m15_df: pd.DataFrame,
        m5_df: pd.DataFrame,
        currency_data: Optional[dict[str, pd.DataFrame]] = None,
        utc_now: Optional[datetime] = None,
    ) -> PairScanResult:
        utc_now = utc_now or datetime.now(timezone.utc)
        pip_size = self._pip_size(pair)
        category = self._category(pair)
        profile = get_profile(pair)

        # ── Market hours gate ─────────────────────────────────────────
        if not is_always_open(pair):
            if not _mt5_market_open(pair, self._mt5_connector):
                logger.debug(f"{pair} — market closed (MT5 trade_mode=0), skipping scan")
                return PairScanResult(
                    pair=pair,
                    direction="NEUTRAL",
                    score=0,
                    regime="UNKNOWN",
                    ev_estimate=0.0,
                    trend_h4="UNKNOWN",
                    trend_h1="UNKNOWN",
                    bias_strength="NONE",
                    has_fvg=False,
                    has_order_block=False,
                    has_liquidity_target=False,
                    sweep_detected=False,
                    inducement_detected=False,
                    wyckoff_phase="N/A",
                    volume_confirmation=False,
                    session_active=False,
                    currency_strength_aligned=False,
                    status="MARKET_CLOSED",
                    timestamp=utc_now,
                    confluences=["Market closed — exchange hours"],
                    instrument_category=category,
                )

        # ── 1. Structure bias (H4 + H1) ──────────────────────────────
        bias = self.structure.get_bias(h4_df, h1_df)

        # ── Directional consensus voting ─────────────────────────────
        # Each brain module casts a direction-independent signed vote.
        # Direction is the weighted net; disagreement kills the trade.
        cc = self.config.consensus
        if cc.enabled:
            dir_votes: list[Vote] = []

            # Structure vote
            try:
                s_dir, s_conf = vote_from_structure(bias)
                dir_votes.append(Vote("structure", s_dir, s_conf, cc.weights.get("structure", 3.0)))
            except Exception as exc:
                logger.warning("[consensus] structure vote failed, abstaining: {}", exc)
                dir_votes.append(Vote("structure", "NEUTRAL", 0.0, 0.0))

            # Currency strength vote (direction-independent)
            try:
                if currency_data and pair in CURRENCY_PAIRS:
                    strength = self.strength_meter.calculate(currency_data)
                    cs_dir, cs_conf = vote_from_currency_strength(pair, strength, CURRENCY_PAIRS)
                    dir_votes.append(Vote("currency_strength", cs_dir, cs_conf, cc.weights.get("currency_strength", 2.0)))
                else:
                    strength = None
            except Exception as exc:
                logger.warning("[consensus] currency_strength vote failed, abstaining: {}", exc)
                strength = None

            # Volume vote
            vol_analysis = None
            try:
                vol_analysis = self.volume.analyze(m5_df)
                v_dir, v_conf = vote_from_volume(vol_analysis)
                dir_votes.append(Vote("volume", v_dir, v_conf, cc.weights.get("volume", 1.0)))
            except Exception as exc:
                logger.warning("[consensus] volume vote failed, abstaining: {}", exc)

            # Wyckoff vote
            wyckoff_analysis = None
            try:
                if profile.wyckoff_enabled:
                    wyck = WyckoffEngine(pip_size=pip_size)
                    wyckoff_analysis = wyck.analyze(h1_df)
                    w_dir, w_conf = vote_from_wyckoff(wyckoff_analysis)
                    dir_votes.append(Vote("wyckoff", w_dir, w_conf, cc.weights.get("wyckoff", 1.5)))
            except Exception as exc:
                logger.warning("[consensus] wyckoff vote failed, abstaining: {}", exc)

            # Order-block vote (direction-independent: score both sides)
            ob_det = OrderBlockDetector(
                pip_size=pip_size,
                min_impulse_pips=profile.ob_min_impulse_pips,
                buffer_pips=profile.ob_buffer_pips,
            )
            h1_obs = ob_det.detect(h1_df, timeframe="H1")
            m5_obs = ob_det.detect(m5_df, timeframe="M5")
            try:
                current_price = float(m5_df["close"].iloc[-1])
                all_obs = h1_obs + m5_obs
                ob_dir, ob_conf = vote_from_order_blocks(all_obs, current_price)
                dir_votes.append(Vote("order_block", ob_dir, ob_conf, cc.weights.get("order_block", 1.0)))
            except Exception as exc:
                logger.warning("[consensus] order_block vote failed, abstaining: {}", exc)
                current_price = float(m5_df["close"].iloc[-1])

            # FVG vote (direction-independent: score both sides)
            fvg_det = FVGDetector(
                pip_size=pip_size,
                proximity_pips=profile.fvg_proximity_pips,
                min_size_pips=profile.fvg_min_size_pips,
            )
            m5_fvgs = fvg_det.detect(m5_df, timeframe="M5")
            m15_fvgs = fvg_det.detect(m15_df, timeframe="M15")
            try:
                f_dir, f_conf = vote_from_fvg(m5_fvgs + m15_fvgs, current_price, fvg_det.proximity)
                dir_votes.append(Vote("fvg", f_dir, f_conf, cc.weights.get("fvg", 1.0)))
            except Exception as exc:
                logger.warning("[consensus] fvg vote failed, abstaining: {}", exc)

            # Liquidity vote
            liq_map = self.liquidity.map(h1_df, pip_size)
            try:
                l_dir, l_conf = vote_from_liquidity(self.liquidity, m5_df, pip_size)
                dir_votes.append(Vote("liquidity", l_dir, l_conf, cc.weights.get("liquidity", 1.0)))
            except Exception as exc:
                logger.warning("[consensus] liquidity vote failed, abstaining: {}", exc)

            # Momentum vote
            try:
                cp_cfg = self.config.confirmation_penalties
                mom_dir, mom_conf = vote_from_momentum(
                    m5_df, h1_df,
                    rsi_period=cp_cfg.rsi_period,
                    macd_fast=cp_cfg.macd_fast,
                    macd_slow=cp_cfg.macd_slow,
                    macd_signal=cp_cfg.macd_signal,
                )
                dir_votes.append(Vote("momentum", mom_dir, mom_conf, cc.weights.get("momentum", 1.0)))
            except Exception as exc:
                logger.warning("[consensus] momentum vote failed, abstaining: {}", exc)

            # VWAP vote
            try:
                session_status = self.session.get_status(utc_now)
                vwap_dir, vwap_conf = vote_from_vwap(
                    m5_df,
                    session_status.session_open_minutes,
                    current_price,
                    min_session_minutes=cp_cfg.vwap_min_session_minutes,
                )
                dir_votes.append(Vote("vwap", vwap_dir, vwap_conf, cc.weights.get("vwap", 1.0)))
            except Exception as exc:
                logger.warning("[consensus] vwap vote failed, abstaining: {}", exc)

            decision = decide(
                dir_votes,
                min_net_score=cc.min_net_score,
                min_agreement=cc.min_agreement,
                high_authority_modules=cc.high_authority_modules,
                high_authority_oppose_confidence=cc.high_authority_oppose_confidence,
                min_contributors=cc.min_contributors,
            )
            trade_dir = decision.direction
            logger.info("[consensus] {} — {}", pair, decision.summary)
        else:
            # Fallback: legacy single-module direction
            direction = bias["direction"]
            trade_dir = {"BULLISH": "LONG", "BEARISH": "SHORT"}.get(direction, "NEUTRAL")
            decision = None
            # Lazy-init analysis objects for the scoring section below
            strength = None
            vol_analysis = None
            wyckoff_analysis = None
            ob_det = OrderBlockDetector(
                pip_size=pip_size,
                min_impulse_pips=profile.ob_min_impulse_pips,
                buffer_pips=profile.ob_buffer_pips,
            )
            h1_obs = ob_det.detect(h1_df, timeframe="H1")
            m5_obs = ob_det.detect(m5_df, timeframe="M5")
            current_price = float(m5_df["close"].iloc[-1])
            fvg_det = FVGDetector(
                pip_size=pip_size,
                proximity_pips=profile.fvg_proximity_pips,
                min_size_pips=profile.fvg_min_size_pips,
            )
            m5_fvgs = fvg_det.detect(m5_df, timeframe="M5")
            m15_fvgs = fvg_det.detect(m15_df, timeframe="M15")
            liq_map = self.liquidity.map(h1_df, pip_size)

        score = 0
        confluences: list[str] = []
        scoring = self.config.scoring
        _w = self._adaptive_weights

        if decision is not None:
            confluences.append(decision.summary)

        if bias["tradeable"]:
            score += _w["structure"] if _w else scoring.structure_points
            confluences.append(f"Structure aligned ({bias['strength']})")

        # ── 2a. H1 Order blocks (directional bias) ───────────────────
        # h1_obs and ob_det already computed above (consensus or fallback)
        has_h1_ob = False
        if trade_dir != "NEUTRAL":
            entry_ob = ob_det.get_entry_ob(h1_obs, trade_dir, h1_df["close"].iloc[-1])
            if entry_ob and entry_ob.status in (OBStatus.FRESH, OBStatus.TESTED):
                has_h1_ob = True
                if _w:
                    _h1_max = _w["ob_h1"]
                    h1_ob_pts = _h1_max if entry_ob.strength == "STRONG" else (round(_h1_max * 0.7) if entry_ob.strength == "MODERATE" else round(_h1_max * 0.4))
                else:
                    h1_ob_pts = 10 if entry_ob.strength == "STRONG" else (7 if entry_ob.strength == "MODERATE" else 4)
                score += h1_ob_pts
                confluences.append(f"H1 OB bias ({entry_ob.strength}, +{h1_ob_pts})")

        # ── 2b. M5 Order blocks (entry zone) ─────────────────────────
        # m5_obs already computed above
        has_m5_ob = False
        if trade_dir != "NEUTRAL":
            m5_entry_ob = ob_det.get_entry_ob(m5_obs, trade_dir, m5_df["close"].iloc[-1])
            if m5_entry_ob and m5_entry_ob.status in (OBStatus.FRESH, OBStatus.TESTED):
                has_m5_ob = True
                if _w:
                    _m5_max = _w["ob_m5"]
                    m5_ob_pts = _m5_max if m5_entry_ob.strength == "STRONG" else (round(_m5_max * 0.7) if m5_entry_ob.strength == "MODERATE" else round(_m5_max * 0.4))
                else:
                    m5_ob_pts = 10 if m5_entry_ob.strength == "STRONG" else (7 if m5_entry_ob.strength == "MODERATE" else 4)
                score += m5_ob_pts
                confluences.append(f"M5 OB entry zone ({m5_entry_ob.strength}, +{m5_ob_pts})")

        has_ob = has_h1_ob or has_m5_ob

        # ── 3. Fair value gaps (strength-based) ──────────────────────
        # fvg_det, m5_fvgs, m15_fvgs already computed above
        has_fvg = False
        if trade_dir != "NEUTRAL":
            entry_fvg = fvg_det.get_entry_fvg(m5_fvgs + m15_fvgs, trade_dir, m5_df["close"].iloc[-1])
            if entry_fvg:
                has_fvg = True
                _fvg_base = _w["fvg"] if _w else scoring.fvg_points
                if entry_fvg.strength == "STRONG":
                    fvg_pts = _fvg_base
                elif entry_fvg.strength == "MODERATE":
                    fvg_pts = int(_fvg_base * 0.7)
                else:
                    fvg_pts = int(_fvg_base * 0.4)
                score += fvg_pts
                confluences.append(f"FVG entry zone ({entry_fvg.strength}, +{fvg_pts})")

        # ── 4. Multi-timeframe confluence ─────────────────────────────
        confluence = fvg_det.get_confluence_fvgs(
            m5_fvgs, m15_fvgs, trade_dir, m5_df["close"].iloc[-1], pip_size,
            overlap_threshold_pips=profile.mtf_overlap_threshold_pips,
        )
        if confluence["has_confluence"]:
            score += _w["mtf_confluence"] if _w else scoring.mtf_confluence_points
            confluences.append("Multi-TF FVG confluence")

        # ── 5. Session timing ─────────────────────────────────────────
        if not cc.enabled:
            session_status = self.session.get_status(utc_now)

        if is_always_open(pair):
            session_active = session_status.current_session not in ("DEAD", "WEEKEND")
        else:
            session_active = session_status.is_tradeable

        if session_active:
            score += _w["session"] if _w else scoring.session_points
            confluences.append(f"Session active ({session_status.current_session})")

        # ── 6. News filter ────────────────────────────────────────────
        news_status = self.news.check([pair], utc_now)
        if not profile.news_filter_enabled:
            score += _w["news"] if _w else scoring.news_points
            confluences.append("News filter N/A (synthetic)")
        elif news_status.is_clear:
            score += _w["news"] if _w else scoring.news_points
            confluences.append("News clear")

        # ── 7. Currency strength alignment ────────────────────────────
        cs_aligned = False
        if profile.currency_strength_enabled and currency_data and pair in CURRENCY_PAIRS:
            if strength is None:
                strength = self.strength_meter.calculate(currency_data)
            alignment = self.strength_meter.get_pair_alignment(pair, strength, trade_dir)
            if alignment["aligned"]:
                cs_aligned = True
                score += _w["currency_strength"] if _w else scoring.currency_strength_points
                confluences.append(f"Currency strength ({alignment['reason']})")

        # ── 8. Liquidity ──────────────────────────────────────────────
        # liq_map already computed above
        has_liq = liq_map.nearest_buy_liq is not None or liq_map.nearest_sell_liq is not None

        sweep = False
        if has_liq and trade_dir != "NEUTRAL":
            targets = (
                liq_map.sell_side_liquidity if trade_dir == "LONG"
                else liq_map.buy_side_liquidity
            )
            for zone in targets[:3]:
                if self.liquidity.detect_sweep(m5_df, zone, pip_size):
                    sweep = True
                    score += _w["liquidity_sweep"] if _w else 8
                    confluences.append(f"Liquidity sweep detected (+{_w['liquidity_sweep'] if _w else 8})")
                    break

        # ── 9. Volume confirmation ────────────────────────────────────
        volume_confirmed = False
        try:
            if vol_analysis is None:
                vol_analysis = self.volume.analyze(m5_df)
            if vol_analysis.has_spike and vol_analysis.confirmation_bias != "NEUTRAL":
                if (
                    (trade_dir == "LONG" and vol_analysis.confirmation_bias == "BULLISH")
                    or (trade_dir == "SHORT" and vol_analysis.confirmation_bias == "BEARISH")
                ):
                    volume_confirmed = True
                    _vol_pts = _w["volume"] if _w else 5
                    score += _vol_pts
                    confluences.append(
                        f"Volume confirmed ({vol_analysis.confirmation_bias}, "
                        f"ratio={vol_analysis.volume_ratio:.1f}x)"
                    )
                elif vol_analysis.climax_detected:
                    score = max(score - 5, 0)
                    confluences.append(
                        f"Volume climax WARNING ({vol_analysis.divergence_type})"
                    )
        except Exception as exc:
            logger.debug(f"Volume analysis error for {pair}: {exc}")

        # ── 10. Inducement detection ──────────────────────────────────
        inducement_detected = False
        try:
            ind_det = InducementDetector(pip_size=pip_size)
            inducement_analysis = ind_det.analyze(m5_df)
            if inducement_analysis.inducement_detected:
                inducement_detected = True
                _ind_pts = _w["inducement"] if _w else 5
                score += _ind_pts
                confluences.append(f"Inducement detected ({inducement_analysis.type}, +{_ind_pts})")
        except Exception as exc:
            logger.debug(f"Inducement detection error for {pair}: {exc}")

        # ── 11. Wyckoff phase ─────────────────────────────────────────
        wyckoff_phase = "N/A"
        if profile.wyckoff_enabled:
            try:
                if wyckoff_analysis is None:
                    wyck = WyckoffEngine(pip_size=pip_size)
                    wyckoff_analysis = wyck.analyze(h1_df)
                wyckoff_phase = wyckoff_analysis.phase
                if wyckoff_analysis.sub_phase in ("SPRING", "UPTHRUST"):
                    _wyck_pts = _w["wyckoff"] if _w else 5
                    score += _wyck_pts
                    confluences.append(f"Wyckoff {wyckoff_analysis.sub_phase} (+{_wyck_pts})")
            except Exception as exc:
                logger.debug(f"Wyckoff analysis error for {pair}: {exc}")
        else:
            logger.debug(f"{pair} — Wyckoff disabled for {category} instruments")

        # ── Confirmation-only penalties (subtract only, never add) ────
        # NOTE: when penalties go active (shadow_mode=False), the penalised
        # score feeds RL augment_score — RL may need a freeze/retrain.
        if not cc.enabled:
            cp_cfg = self.config.confirmation_penalties
        if cp_cfg.enabled and trade_dir in ("LONG", "SHORT"):
            total_penalty = 0
            penalty_notes: list[str] = []

            try:
                pts, reason = session_vwap_penalty(
                    m5_df, trade_dir,
                    session_status.session_open_minutes,
                    penalty_points=cp_cfg.vwap_wrong_side_penalty,
                    min_session_minutes=cp_cfg.vwap_min_session_minutes,
                )
                if pts > 0:
                    total_penalty += pts
                    penalty_notes.append(f"⚠️ VWAP wrong-side (−{pts})")
            except Exception as exc:
                logger.debug(f"[CONFIRM] VWAP error for {pair}: {exc}")

            try:
                pts, reason = momentum_divergence_penalty(
                    m5_df, h1_df, trade_dir,
                    both_tf_penalty=cp_cfg.divergence_both_tf_penalty,
                    single_tf_penalty=cp_cfg.divergence_single_tf_penalty,
                    rsi_period=cp_cfg.rsi_period,
                    macd_fast=cp_cfg.macd_fast,
                    macd_slow=cp_cfg.macd_slow,
                    macd_signal=cp_cfg.macd_signal,
                )
                if pts > 0:
                    total_penalty += pts
                    penalty_notes.append(f"⚠️ {reason} (−{pts})")
            except Exception as exc:
                logger.debug(f"[CONFIRM] Divergence error for {pair}: {exc}")

            try:
                pts, reason = atr_percentile_penalty(
                    m5_df,
                    penalty_points=cp_cfg.atr_dead_regime_penalty,
                    window=cp_cfg.atr_percentile_window,
                    dead_percentile=cp_cfg.atr_dead_percentile,
                )
                if pts > 0:
                    total_penalty += pts
                    penalty_notes.append(f"⚠️ {reason} (−{pts})")
            except Exception as exc:
                logger.debug(f"[CONFIRM] ATR-pctl error for {pair}: {exc}")

            try:
                pts, reason = volume_profile_poc_penalty(
                    h4_df, trade_dir, category,
                    penalty_points=cp_cfg.vp_poc_trap_penalty,
                    lookback=cp_cfg.vp_poc_lookback,
                    proximity_pct=cp_cfg.vp_poc_proximity_pct,
                )
                if pts > 0:
                    total_penalty += pts
                    penalty_notes.append(f"⚠️ {reason} (−{pts})")
            except Exception as exc:
                logger.debug(f"[CONFIRM] VP-POC error for {pair}: {exc}")

            if total_penalty > 0:
                if cp_cfg.shadow_mode:
                    logger.info(
                        f"[CONFIRM-SHADOW] {pair} would subtract {total_penalty} "
                        f"({', '.join(penalty_notes)})"
                    )
                else:
                    score = max(score - total_penalty, 0)
                    confluences.extend(penalty_notes)

        # ── Regime caps ───────────────────────────────────────────────
        regime = bias["h4_trend"]
        if regime == "RANGING":
            score = min(score, scoring.ranging_score_cap)
        if not session_active and score > 0 and is_session_gated(pair):
            score = max(score - 10, 0)

        # ── Expected Value estimate ───────────────────────────────────
        try:
            ev_est = self._ev_estimator.estimate(
                pair=pair,
                regime=regime,
                session=session_status.current_session if hasattr(session_status, "current_session") else "UNKNOWN",
                trade_history=self._trade_history,
            )
            ev_estimate = ev_est.expected_value
        except Exception:
            ev_estimate = 0.0

        # ── RL augmentation ───────────────────────────────────────────
        rl_action    = 0
        rl_confidence = 0.0
        rl_expected_r = 0.0
        rl_stage      = self._rl.authority.stage

        try:
            close_now = float(m5_df["close"].iloc[-1])
            tr = (m5_df["high"] - m5_df["low"]).abs().tail(14)
            atr_now = float(tr.mean()) if len(tr) > 0 else 0.0

            if pair not in self._mtf_builders:
                self._mtf_builders[pair] = MultiTFObservationBuilder()

            mtf_result = self._mtf_builders[pair].build_from_frames(
                frames={"M5": m5_df, "M15": m15_df, "H1": h1_df, "H4": h4_df},
                instrument=pair,
            )

            if mtf_result is not None:
                obs, ctx, sym_id = mtf_result
                rl_result = self._rl.augment_score(
                    pair=pair,
                    base_score=float(score),
                    obs=obs,
                    close=close_now,
                    atr=atr_now,
                    pip_size=pip_size,
                    context_vec=ctx,
                    symbol_id=sym_id,
                )

                if rl_result.vetoed:
                    logger.info(f"[RL] VETO {pair} — stage {rl_stage} "
                                f"conf={rl_result.rl_confidence:.2f}")
                    # Return WAITING — vetoed by RL
                    return PairScanResult(
                        pair=pair, direction=trade_dir,
                        score=int(score), regime=regime,
                        ev_estimate=ev_estimate,
                        trend_h4=bias["h4_trend"], trend_h1=bias["h1_trend"],
                        bias_strength=bias["strength"],
                        has_fvg=has_fvg, has_order_block=has_ob,
                        has_liquidity_target=has_liq, sweep_detected=sweep,
                        inducement_detected=inducement_detected,
                        wyckoff_phase=wyckoff_phase,
                        volume_confirmation=volume_confirmed,
                        session_active=session_active,
                        currency_strength_aligned=cs_aligned,
                        status="WAITING",
                        timestamp=utc_now,
                        confluences=confluences + [f"RL VETO (conf={rl_result.rl_confidence:.2f})"],
                        instrument_category=category,
                        rl_action=rl_result.rl_action,
                        rl_confidence=rl_result.rl_confidence,
                        rl_expected_r=rl_result.rl_expected_r,
                        rl_stage=rl_stage,
                        consensus_direction=decision.direction if decision else trade_dir,
                        consensus_net=decision.net_score if decision else 0.0,
                        consensus_agreement=decision.agreement if decision else 0.0,
                        opportunity_quality=0.0,
                        entry_quality=0.0,
                    )

                score        = int(rl_result.final_score)
                rl_action    = rl_result.rl_action
                rl_confidence = rl_result.rl_confidence
                rl_expected_r = rl_result.rl_expected_r

                if rl_result.rl_delta != 0:
                    confluences.append(
                        f"RL signal ({rl_result.rl_action} "
                        f"conf={rl_confidence:.2f} "
                        f"Δ{rl_result.rl_delta:+.1f})"
                    )

        except Exception as exc:
            logger.debug(f"[RL] augmentation error for {pair}: {exc}")

        # ── Layered quality gates (OQ + EQ) ───────────────────────────
        ld_cfg = self.config.layered_decision
        oq_score = 0.0
        eq_score = 0.0

        if ld_cfg.enabled and trade_dir in ("LONG", "SHORT"):
            self._quality_scans += 1
            try:
                tr_series = (m5_df["high"] - m5_df["low"]).abs().tail(14)
                _atr_pips = float(tr_series.mean()) / pip_size if len(tr_series) > 0 and pip_size > 0 else None

                _atr_pct = None
                if _atr_pips is not None:
                    try:
                        from brain.atr_percentile import compute_atr_percentile
                        _atr_pct = compute_atr_percentile(m5_df, window=100)
                    except Exception:
                        pass

                try:
                    _inst = get_instrument(pair)
                    _spread_typical = _inst.typical_spread_pips
                except KeyError:
                    _spread_typical = None

                _spread_current = None
                try:
                    _ask = float(m5_df["high"].iloc[-1])
                    _bid = float(m5_df["low"].iloc[-1])
                    if pip_size > 0:
                        _spread_current = (_ask - _bid) / pip_size
                except Exception:
                    pass

                _news_mins = None
                if hasattr(news_status, "next_high_impact") and news_status.next_high_impact:
                    _news_mins = news_status.next_high_impact.minutes_away

                _rr_magnitude = _side_agnostic_rr(liq_map, _atr_pips, pip_size)

                oq = compute_opportunity_quality(
                    atr_value=_atr_pips,
                    atr_percentile=_atr_pct,
                    spread_current=_spread_current,
                    spread_typical=_spread_typical,
                    news_is_clear=news_status.is_clear if news_status else None,
                    news_minutes_to_next_high=_news_mins,
                    session_liquidity=session_status.liquidity if session_status else None,
                    session_is_tradeable=session_active,
                    reward_risk_magnitude=_rr_magnitude,
                    ev_estimate=ev_estimate,
                    volume_ratio=vol_analysis.volume_ratio if vol_analysis else None,
                    volume_climax=vol_analysis.climax_detected if vol_analysis else None,
                    oq_weights=ld_cfg.oq_weights,
                )
                oq_score = oq.score

                _ob_dist = None
                entry_ob_ref = None
                if trade_dir != "NEUTRAL":
                    entry_ob_ref = ob_det.get_entry_ob(m5_obs + h1_obs, trade_dir, current_price)
                    if entry_ob_ref and pip_size > 0:
                        _ob_dist = abs(current_price - entry_ob_ref.midpoint) / pip_size

                _fvg_dist = None
                if trade_dir != "NEUTRAL":
                    _entry_fvg_ref = fvg_det.get_entry_fvg(m5_fvgs + m15_fvgs, trade_dir, current_price)
                    if _entry_fvg_ref and pip_size > 0:
                        fvg_mid = (_entry_fvg_ref.top + _entry_fvg_ref.bottom) / 2
                        _fvg_dist = abs(current_price - fvg_mid) / pip_size

                _liq_dist = None
                if trade_dir == "LONG" and liq_map.nearest_sell_liq and pip_size > 0:
                    _liq_dist = abs(current_price - liq_map.nearest_sell_liq.price) / pip_size
                elif trade_dir == "SHORT" and liq_map.nearest_buy_liq and pip_size > 0:
                    _liq_dist = abs(current_price - liq_map.nearest_buy_liq.price) / pip_size

                _stop_dist = _atr_pips * 1.5 if _atr_pips else None

                eq = compute_entry_quality(
                    trade_dir=trade_dir,
                    current_price=current_price,
                    entry_price=entry_ob_ref.midpoint if entry_ob_ref else current_price,
                    nearest_ob_distance_pips=_ob_dist,
                    nearest_fvg_distance_pips=_fvg_dist,
                    nearest_liq_distance_pips=_liq_dist,
                    atr_pips=_atr_pips,
                    stop_distance_pips=_stop_dist,
                    eq_weights=ld_cfg.eq_weights,
                )
                eq_score = eq.score

            except Exception as exc:
                logger.error("[quality] OQ/EQ computation failed for {}: {}", pair, exc)
                self._quality_failures += 1
                oq_score = 0.0
                eq_score = 0.0

        # ── Status ────────────────────────────────────────────────────
        if ld_cfg.enabled:
            if trade_dir not in ("LONG", "SHORT"):
                status = "WAITING"
            elif oq_score >= ld_cfg.opportunity_quality_min and eq_score >= ld_cfg.entry_quality_min:
                status = "READY"
            elif oq_score >= ld_cfg.opportunity_quality_min or eq_score >= ld_cfg.entry_quality_min:
                status = "WATCHLIST"
            else:
                status = "WAITING"
            logger.info(
                "[layered] {} — dir={} agree={:.2f} OQ={:.2f} EQ={:.2f} -> {}",
                pair, trade_dir,
                decision.agreement if decision else 0.0,
                oq_score, eq_score, status,
            )
        else:
            effective_min_score = profile.min_entry_score
            if score >= effective_min_score:
                status = "READY"
            elif score >= scoring.watchlist_score:
                status = "WATCHLIST"
            else:
                status = "WAITING"

        return PairScanResult(
            pair=pair,
            direction=trade_dir,
            score=score,
            regime=regime,
            ev_estimate=ev_estimate,
            trend_h4=bias["h4_trend"],
            trend_h1=bias["h1_trend"],
            bias_strength=bias["strength"],
            has_fvg=has_fvg,
            has_order_block=has_ob,
            has_liquidity_target=has_liq,
            sweep_detected=sweep,
            inducement_detected=inducement_detected,
            wyckoff_phase=wyckoff_phase,
            volume_confirmation=volume_confirmed,
            session_active=session_active,
            currency_strength_aligned=cs_aligned,
            status=status,
            timestamp=utc_now,
            confluences=confluences,
            instrument_category=category,
            rl_action=rl_action,
            rl_confidence=rl_confidence,
            rl_expected_r=rl_expected_r,
            rl_stage=rl_stage,
            consensus_direction=decision.direction if decision else trade_dir,
            consensus_net=decision.net_score if decision else 0.0,
            consensus_agreement=decision.agreement if decision else 0.0,
            opportunity_quality=oq_score,
            entry_quality=eq_score,
        )

    # ------------------------------------------------------------------
    # Full scan — all enabled instruments
    # ------------------------------------------------------------------

    def scan_all(
        self,
        market_data: dict[str, dict[str, pd.DataFrame]],
        currency_data: Optional[dict[str, pd.DataFrame]] = None,
        utc_now: Optional[datetime] = None,
    ) -> ScanReport:
        utc_now = utc_now or datetime.now(timezone.utc)
        session_status = self.session.get_status(utc_now)

        results: list[PairScanResult] = []
        regimes: dict[str, int] = {}

        for pair, frames in market_data.items():
            try:
                h4 = frames.get("H4")
                h1 = frames.get("H1")
                m15 = frames.get("M15")
                m5 = frames.get("M5")
                if h4 is None or h1 is None or m15 is None or m5 is None:
                    logger.warning(f"Skipping {pair} — missing timeframe data")
                    continue

                result = self.scan_pair(pair, h4, h1, m15, m5, currency_data, utc_now)
                results.append(result)
                regimes[result.regime] = regimes.get(result.regime, 0) + 1

                try:
                    close_val = float(m5["close"].iloc[-1])
                    high_val = float(m5["high"].iloc[-1])
                    low_val = float(m5["low"].iloc[-1])
                    tr_vals = (m5["high"] - m5["low"]).abs().tail(14)
                    atr_val = float(tr_vals.mean()) if len(tr_vals) > 0 else 0.0
                    self._rl.update_price(pair, high_val, low_val, close_val, atr_val, None)
                except Exception as exc:
                    logger.debug("[RL] update_price failed for {}: {}", pair, exc)
            except Exception as exc:
                logger.error(f"Error scanning {pair}: {exc}")

        if self.config.layered_decision.enabled:
            results.sort(
                key=lambda r: (
                    r.consensus_agreement + r.opportunity_quality + r.entry_quality
                ),
                reverse=True,
            )
        else:
            results.sort(key=lambda r: r.score, reverse=True)

        ready = [r for r in results if r.status == "READY"]
        watch = [r for r in results if r.status == "WATCHLIST"]
        closed = [r for r in results if r.status == "MARKET_CLOSED"]

        if closed:
            logger.debug(
                f"Skipped {len(closed)} closed markets: "
                f"{', '.join(r.pair for r in closed)}"
            )

        # Periodic authority evaluation
        try:
            self._rl.evaluate_authority()
        except Exception as exc:
            logger.debug("RL authority evaluation failed: {}", exc)

        report = ScanReport(
            timestamp=utc_now,
            session=session_status.current_session,
            total_pairs_scanned=len(results),
            ready_count=len(ready),
            watchlist_count=len(watch),
            results=results,
            best_setup=ready[0] if ready else None,
            regime_distribution=regimes,
        )
        self.last_report = report
        return report

    # ------------------------------------------------------------------
    # Convenience filters
    # ------------------------------------------------------------------

    @staticmethod
    def get_ready_setups(report: ScanReport) -> list[PairScanResult]:
        return [r for r in report.results if r.status == "READY"]

    @staticmethod
    def get_watchlist(report: ScanReport) -> list[PairScanResult]:
        return [r for r in report.results if r.status == "WATCHLIST"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
