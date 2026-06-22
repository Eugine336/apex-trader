"""APEX TRADER — Shared Setup-Quality Layer.

Single, shared computation of the second-order analysis signals that were
previously built but never wired into the trading planes:

* :class:`~brain.regime_detector.RegimeAnalysis` per symbol — feeds the
  cross-instrument :class:`~brain.regime_detector.SystemVolatilityMonitor`.
* Real **Opportunity Quality** (direction-free) and **Entry Quality**
  (direction-aware) scores via :mod:`brain.setup_quality`, replacing the thin
  ``confidence×10`` / ``coherence×10`` proxy.
* ATR-percentile volatility scoring via :func:`brain.atr_percentile.compute_atr_percentile`
  (folded into the OQ volatility component).
* RSI/MACD momentum-divergence penalty via
  :func:`brain.momentum_divergence.momentum_divergence_penalty` (folded into EQ).

This module is the parity backbone: BOTH the live analysis plane
(``scanner.candle_close_handler``) and the backtest plane
(``brain.decision_core.analyze_window``) call :func:`compute_quality_layer`
with the same inputs, so the live and backtest engines see identical quality
scores.  Pure/best-effort: every sub-computation is guarded so a failure
degrades to a neutral default and never breaks the publish or trading path.
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd
from loguru import logger

from brain.atr_percentile import compute_atr_percentile
from brain.momentum_divergence import momentum_divergence_penalty
from brain.regime_detector import RegimeAnalysis, RegimeDetector
from brain.setup_quality import compute_entry_quality, compute_opportunity_quality
from brain.volatility_stop import atr_series

# Shared, stateless detector instance (analyze() holds no per-call state).
_REGIME = RegimeDetector()


def compute_regime_analysis(df: Optional[pd.DataFrame]) -> Optional[RegimeAnalysis]:
    """Best-effort :class:`RegimeAnalysis` from an intraday candle frame.

    Returns ``None`` on missing data or analysis failure so the caller can
    skip the symbol when aggregating the system-wide volatility state.
    """
    if df is None or getattr(df, "empty", True):
        return None
    try:
        return _REGIME.analyze(df)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[quality] regime analyze failed: {}", exc)
        return None


def _latest_atr_pips(df: Optional[pd.DataFrame], pip_size: float, period: int = 14) -> Optional[float]:
    if df is None or getattr(df, "empty", True) or pip_size <= 0:
        return None
    try:
        series = atr_series(df, period)
        val = float(series.dropna().iloc[-1])
        if val <= 0:
            return None
        return val / pip_size
    except Exception:
        return None


def _nearest_distance_pips(levels: list[float], current_price: float, pip_size: float) -> Optional[float]:
    if not levels or current_price <= 0 or pip_size <= 0:
        return None
    try:
        return min(abs(current_price - lvl) for lvl in levels) / pip_size
    except Exception:
        return None


def _candidate_rr_ev(wm: Any) -> tuple[Optional[float], Optional[float]]:
    """Direction-free reward:risk and EV from the strongest ranked candidate."""
    try:
        cands = (
            wm.candidates_list()
            if hasattr(wm, "candidates_list")
            else list(getattr(wm, "candidates", ()) or [])
        )
    except Exception:
        return None, None
    best = None
    for c in cands:
        ev = getattr(c, "expected_value", None)
        if ev is None:
            continue
        if best is None or float(ev) > float(getattr(best, "expected_value", -1e9)):
            best = c
    if best is None:
        return None, None
    rr = getattr(best, "reward_risk", None)
    ev = getattr(best, "expected_value", None)
    return (
        float(rr) if rr is not None else None,
        float(ev) if ev is not None else None,
    )


def _volume_health(wm: Any) -> tuple[Optional[float], Optional[bool]]:
    """Volume ratio + climax flag from the freshest available volume analysis."""
    try:
        vol_by_tf = wm.volume_by_tf() if hasattr(wm, "volume_by_tf") else dict(getattr(wm, "volume", ()))
    except Exception:
        return None, None
    for tf in ("M5", "H1", "H4"):
        va = vol_by_tf.get(tf)
        if va is None:
            continue
        try:
            return float(getattr(va, "volume_ratio", 0.0) or 0.0), bool(
                getattr(va, "climax_detected", False)
            )
        except Exception:
            continue
    return None, None


def compute_quality_layer(
    symbol: str,
    wm: Any,
    *,
    m5_df: Optional[pd.DataFrame] = None,
    h1_df: Optional[pd.DataFrame] = None,
    current_price: float = 0.0,
) -> dict[str, Any]:
    """Compute the shared quality signals for a freshly built WorldModel.

    Returns a dict with ``opportunity_quality`` (0–10, direction-free),
    ``entry_quality_long`` / ``entry_quality_short`` (0–10, direction-aware),
    and ``regime_analysis`` (a :class:`RegimeAnalysis` or ``None``).  All values
    may be ``None`` when their inputs are unavailable — callers treat ``None`` as
    "not recomputed this cycle" and apply no quality pressure (matching the old
    proxy's neutral semantics).

    Inputs intentionally use ONLY data available identically in both the live
    and backtest planes (WorldModel analysis + M5/H1 candles).  Inputs that are
    not available in the backtest plane — broker spread, the news calendar, and
    the live session-liquidity label — are passed as ``None`` so the
    :mod:`brain.setup_quality` functions fall back to their documented neutral
    defaults in BOTH planes, preserving parity.
    """
    out: dict[str, Any] = {
        "opportunity_quality": None,
        "entry_quality_long": None,
        "entry_quality_short": None,
        "regime_analysis": None,
    }

    try:
        from config import get_pip_size

        try:
            pip_size = float(get_pip_size(symbol))
        except Exception:
            pip_size = 0.0001
        if pip_size <= 0:
            pip_size = 0.0001

        if current_price <= 0:
            for df in (m5_df, h1_df):
                if df is not None and not getattr(df, "empty", True):
                    try:
                        current_price = float(df["close"].iloc[-1])
                        break
                    except Exception:
                        continue

        # ── Regime analysis (for SystemVolatilityMonitor) ──────────────
        out["regime_analysis"] = compute_regime_analysis(h1_df)

        # ── Opportunity Quality (direction-free) ───────────────────────
        atr_pct = None
        if h1_df is not None and not getattr(h1_df, "empty", True):
            pct = compute_atr_percentile(h1_df)
            atr_pct = pct if pct >= 0 else None
        atr_pips = _latest_atr_pips(m5_df if m5_df is not None else h1_df, pip_size)
        rr, ev = _candidate_rr_ev(wm)
        vol_ratio, vol_climax = _volume_health(wm)

        oq = compute_opportunity_quality(
            atr_percentile=atr_pct,
            # spread / news / session are unavailable in the backtest plane —
            # pass None so both planes use the same neutral defaults (parity).
            spread_current=None,
            spread_typical=None,
            news_is_clear=None,
            session_liquidity=None,
            session_is_tradeable=None,
            reward_risk_magnitude=rr,
            ev_estimate=ev,
            volume_ratio=vol_ratio,
            volume_climax=vol_climax,
        )
        out["opportunity_quality"] = float(oq.score)

        # ── Entry Quality (direction-aware), per direction ─────────────
        ob_levels = [
            float(getattr(ob, "midpoint", 0.0))
            for ob in (wm.all_order_blocks() if hasattr(wm, "all_order_blocks") else [])
            if getattr(ob, "midpoint", 0.0)
        ]
        fvg_levels = [
            float(getattr(f, "midpoint", 0.0))
            for f in (wm.all_fvgs() if hasattr(wm, "all_fvgs") else [])
            if getattr(f, "midpoint", 0.0)
        ]
        liq_levels: list[float] = []
        try:
            for _tf, lm in (wm.liquidity if hasattr(wm, "liquidity") else ()):
                for zone in list(getattr(lm, "buy_side_liquidity", ()) or []) + list(
                    getattr(lm, "sell_side_liquidity", ()) or []
                ):
                    p = float(getattr(zone, "price", 0.0) or 0.0)
                    if p > 0:
                        liq_levels.append(p)
        except Exception:
            liq_levels = []

        ob_dist = _nearest_distance_pips(ob_levels, current_price, pip_size)
        fvg_dist = _nearest_distance_pips(fvg_levels, current_price, pip_size)
        liq_dist = _nearest_distance_pips(liq_levels, current_price, pip_size)

        for direction in ("LONG", "SHORT"):
            eq = compute_entry_quality(
                trade_dir=direction,
                nearest_ob_distance_pips=ob_dist,
                nearest_fvg_distance_pips=fvg_dist,
                nearest_liq_distance_pips=liq_dist,
                atr_pips=atr_pips,
                # entry/stop distance unknown at analysis time → neutral default.
                current_price=None,
                entry_price=None,
                stop_distance_pips=None,
            )
            score = float(eq.score)
            # Fold the RSI/MACD divergence penalty (0/7/15 points) into EQ on the
            # 0–10 scale: a divergence against this direction lowers entry quality.
            try:
                pen, _reason = momentum_divergence_penalty(m5_df, h1_df, direction)
                if pen > 0:
                    score = max(0.0, score - (pen / 10.0))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[quality] {} divergence penalty failed: {}", symbol, exc)
            key = "entry_quality_long" if direction == "LONG" else "entry_quality_short"
            out[key] = round(score, 2)

    except Exception as exc:  # noqa: BLE001
        logger.debug("[quality] {} quality layer failed: {}", symbol, exc)

    return out


def entry_quality_for(wm: Any, direction: str) -> Optional[float]:
    """Read the stored direction-aware entry quality off a WorldModel."""
    want = "LONG" if str(direction).upper() in ("BUY", "LONG") else "SHORT"
    attr = "entry_quality_long" if want == "LONG" else "entry_quality_short"
    val = getattr(wm, attr, None)
    return float(val) if val is not None else None
