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
    AppConfig, get_instrument, get_pip_size, INSTRUMENT_REGISTRY,
)
from brain.structure_engine import StructureEngine, Trend
from brain.fvg_detector import FVGDetector
from brain.order_block import OrderBlockDetector, OBStatus
from brain.liquidity_mapper import LiquidityMapper
from brain.currency_strength import CurrencyStrengthMeter, CURRENCY_PAIRS
from brain.session_engine import SessionEngine, NewsGuard
from brain.inducement_detector import InducementDetector
from brain.wyckoff_engine import WyckoffEngine


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


class PairScanner:
    """
    Always watching. Scans every enabled instrument, runs the full brain
    stack, and surfaces only the setups worth pulling the trigger on.
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()
        self.structure = StructureEngine()
        self.fvg_detector = FVGDetector()
        self.ob_detector = OrderBlockDetector()
        self.liquidity = LiquidityMapper()
        self.strength_meter = CurrencyStrengthMeter()
        self.session = SessionEngine()
        self.news = NewsGuard()
        self.last_report: Optional[ScanReport] = None

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

        # ── 1. Structure bias (H4 + H1) ──────────────────────────────
        bias = self.structure.get_bias(h4_df, h1_df)
        direction = bias["direction"]       # "BULLISH" / "BEARISH" / "RANGING"
        trade_dir = {"BULLISH": "LONG", "BEARISH": "SHORT"}.get(direction, "NEUTRAL")

        score = 0
        confluences: list[str] = []
        scoring = self.config.scoring

        if bias["tradeable"]:
            score += scoring.structure_points
            confluences.append(f"Structure aligned ({bias['strength']})")

        # ── 2a. H1 Order blocks (directional bias) ───────────────────
        ob_det = OrderBlockDetector(pip_size=pip_size)
        h1_obs = ob_det.detect(h1_df, timeframe="H1")
        has_h1_ob = False
        if trade_dir != "NEUTRAL":
            entry_ob = ob_det.get_entry_ob(h1_obs, trade_dir, h1_df["close"].iloc[-1])
            if entry_ob and entry_ob.status in (OBStatus.FRESH, OBStatus.TESTED):
                has_h1_ob = True
                h1_ob_pts = 10 if entry_ob.strength == "STRONG" else (7 if entry_ob.strength == "MODERATE" else 4)
                score += h1_ob_pts
                confluences.append(f"H1 OB bias ({entry_ob.strength}, +{h1_ob_pts})")

        # ── 2b. M5 Order blocks (entry zone) ─────────────────────────
        m5_obs = ob_det.detect(m5_df, timeframe="M5")
        has_m5_ob = False
        if trade_dir != "NEUTRAL":
            m5_entry_ob = ob_det.get_entry_ob(m5_obs, trade_dir, m5_df["close"].iloc[-1])
            if m5_entry_ob and m5_entry_ob.status in (OBStatus.FRESH, OBStatus.TESTED):
                has_m5_ob = True
                m5_ob_pts = 10 if m5_entry_ob.strength == "STRONG" else (7 if m5_entry_ob.strength == "MODERATE" else 4)
                score += m5_ob_pts
                confluences.append(f"M5 OB entry zone ({m5_entry_ob.strength}, +{m5_ob_pts})")

        has_ob = has_h1_ob or has_m5_ob

        # ── 3. Fair value gaps (strength-based) ──────────────────────
        fvg_det = FVGDetector(pip_size=pip_size)
        m5_fvgs = fvg_det.detect(m5_df, timeframe="M5")
        m15_fvgs = fvg_det.detect(m15_df, timeframe="M15")
        has_fvg = False
        if trade_dir != "NEUTRAL":
            entry_fvg = fvg_det.get_entry_fvg(m5_fvgs + m15_fvgs, trade_dir, m5_df["close"].iloc[-1])
            if entry_fvg:
                has_fvg = True
                if entry_fvg.strength == "STRONG":
                    fvg_pts = scoring.fvg_points
                elif entry_fvg.strength == "MODERATE":
                    fvg_pts = int(scoring.fvg_points * 0.7)
                else:
                    fvg_pts = int(scoring.fvg_points * 0.4)
                score += fvg_pts
                confluences.append(f"FVG entry zone ({entry_fvg.strength}, +{fvg_pts})")

        # ── 4. Multi-timeframe confluence ─────────────────────────────
        confluence = fvg_det.get_confluence_fvgs(
            m5_fvgs, m15_fvgs, trade_dir, m5_df["close"].iloc[-1], pip_size,
        )
        if confluence["has_confluence"]:
            score += scoring.mtf_confluence_points
            confluences.append("Multi-TF FVG confluence")

        # ── 5. Session timing ─────────────────────────────────────────
        session_status = self.session.get_status(utc_now)
        session_active = session_status.is_tradeable
        if session_active:
            score += scoring.session_points
            confluences.append(f"Session active ({session_status.current_session})")

        # ── 6. News filter ────────────────────────────────────────────
        news_status = self.news.check([pair], utc_now)
        if news_status.is_clear:
            score += scoring.news_points
            confluences.append("News clear")

        # ── 7. Currency strength alignment ────────────────────────────
        cs_aligned = False
        if currency_data and pair in CURRENCY_PAIRS:
            strength = self.strength_meter.calculate(currency_data)
            alignment = self.strength_meter.get_pair_alignment(pair, strength, trade_dir)
            if alignment["aligned"]:
                cs_aligned = True
                score += scoring.currency_strength_points
                confluences.append(f"Currency strength ({alignment['reason']})")

        # ── 8. Liquidity ──────────────────────────────────────────────
        liq_map = self.liquidity.map(h1_df, pip_size)
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
                    score += 8
                    confluences.append("Liquidity sweep detected (+8)")
                    break

        # ── 9. Inducement detection ──────────────────────────────────
        inducement_detected = False
        try:
            ind_det = InducementDetector(pip_size=pip_size)
            inducement_analysis = ind_det.analyze(m5_df)
            if inducement_analysis.inducement_detected:
                inducement_detected = True
                score += 5
                confluences.append(f"Inducement detected ({inducement_analysis.type}, +5)")
        except Exception as exc:
            logger.debug(f"Inducement detection error for {pair}: {exc}")

        # ── 10. Wyckoff phase ────────────────────────────────────────
        wyckoff_phase = "N/A"
        try:
            wyck = WyckoffEngine(pip_size=pip_size)
            wyckoff_analysis = wyck.analyze(h1_df)
            wyckoff_phase = wyckoff_analysis.phase
            if wyckoff_analysis.sub_phase in ("SPRING", "UPTHRUST"):
                score += 5
                confluences.append(f"Wyckoff {wyckoff_analysis.sub_phase} (+5)")
        except Exception as exc:
            logger.debug(f"Wyckoff analysis error for {pair}: {exc}")

        # ── Regime caps ───────────────────────────────────────────────
        regime = bias["h4_trend"]
        if regime == "RANGING":
            score = min(score, scoring.ranging_score_cap)
        if not session_active and score > 0:
            score = max(score - 10, 0)

        # ── Status ────────────────────────────────────────────────────
        if score >= scoring.min_entry_score:
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
            trend_h4=bias["h4_trend"],
            trend_h1=bias["h1_trend"],
            bias_strength=bias["strength"],
            has_fvg=has_fvg,
            has_order_block=has_ob,
            has_liquidity_target=has_liq,
            sweep_detected=sweep,
            inducement_detected=inducement_detected,
            wyckoff_phase=wyckoff_phase,
            volume_confirmation=False,
            session_active=session_active,
            currency_strength_aligned=cs_aligned,
            status=status,
            timestamp=utc_now,
            confluences=confluences,
            instrument_category=category,
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
        """
        Scan every instrument in *market_data*.

        market_data structure::

            {
                "EURUSD": {"H4": df, "H1": df, "M15": df, "M5": df},
                "XAUUSD": {"H4": df, "H1": df, "M15": df, "M5": df},
                ...
            }
        """
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
            except Exception as exc:
                logger.error(f"Error scanning {pair}: {exc}")

        results.sort(key=lambda r: r.score, reverse=True)

        ready = [r for r in results if r.status == "READY"]
        watch = [r for r in results if r.status == "WATCHLIST"]

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
