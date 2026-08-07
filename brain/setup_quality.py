"""
APEX TRADER — Setup Quality (Layers 2 & 3)

Layer 2: Opportunity Quality — direction-FREE.
  Answers: "Is this market moment worth trading at all?"
  A perfect BUY and a perfect SELL with mirror-image inputs
  produce the SAME score.

Layer 3: Entry Quality — direction-AWARE (for location judgment only).
  Answers: "Is the entry location good for the already-decided direction?"
  Never feeds direction back out.

Pure functions — no torch, no pandas, no side effects.
Fail-closed: missing/bad inputs → LOW score + warning.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from brain.smoothing import piecewise_linear


_DEFAULT_OQ_WEIGHTS: dict[str, float] = {
    "volatility": 1.5,
    "spread": 1.5,
    "news": 1.0,
    "session": 1.5,
    "reward_risk": 2.0,
    "historical_ev": 1.0,
    "volume_health": 1.0,
}

_DEFAULT_EQ_WEIGHTS: dict[str, float] = {
    "ob_proximity": 2.0,
    "fvg_proximity": 1.5,
    "liquidity_proximity": 1.0,
    "atr_extension": 1.5,
    "stop_quality": 2.0,
}


@dataclass(frozen=True)
class OpportunityQuality:
    score: float
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EntryQuality:
    score: float
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _clamp(val: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, val))


def _safe_finite(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def compute_opportunity_quality(
    *,
    atr_value: Optional[float] = None,
    atr_percentile: Optional[float] = None,
    spread_current: Optional[float] = None,
    spread_typical: Optional[float] = None,
    news_is_clear: Optional[bool] = None,
    news_minutes_to_next_high: Optional[int] = None,
    session_liquidity: Optional[str] = None,
    session_is_tradeable: Optional[bool] = None,
    reward_risk_magnitude: Optional[float] = None,
    ev_estimate: Optional[float] = None,
    volume_ratio: Optional[float] = None,
    volume_climax: Optional[bool] = None,
    oq_weights: Optional[dict[str, float]] = None,
) -> OpportunityQuality:
    """Direction-FREE opportunity quality.

    Every input is a market-condition scalar — none is directional.
    Returns a score in [0, 10] and component breakdown.
    On failure: returns LOW score (0.0) + warning.
    """
    weights = oq_weights if oq_weights is not None else _DEFAULT_OQ_WEIGHTS
    components: dict[str, float] = {}
    reasons: list[str] = []

    try:
        # --- Volatility (0-10): prefer moderate ATR percentile ---
        # Continuous curve anchored on the old tier values (peak in the healthy
        # mid-band, decaying toward dead-low and extreme-high). No cliffs.
        atr_pct = _safe_finite(atr_percentile, -1.0)
        if atr_pct < 0:
            components["volatility"] = 3.0
            reasons.append("volatility: no ATR data, degraded")
        else:
            components["volatility"] = round(piecewise_linear(
                atr_pct,
                ((5.0, 1.0), (17.0, 5.0), (50.0, 10.0), (82.0, 6.0), (95.0, 2.0)),
            ), 2)
            reasons.append(f"volatility: {atr_pct:.0f}th pctl → {components['volatility']:.1f}")

        # --- Spread (0-10): current vs typical, smooth decay as it widens ---
        s_cur = _safe_finite(spread_current, -1.0)
        s_typ = _safe_finite(spread_typical, -1.0)
        if s_cur < 0 or s_typ <= 0:
            components["spread"] = 3.0
            reasons.append("spread: no data, degraded")
        else:
            ratio = s_cur / s_typ
            components["spread"] = round(piecewise_linear(
                ratio,
                ((1.0, 10.0), (1.5, 8.0), (2.5, 5.0), (4.0, 2.0), (6.0, 0.0)),
            ), 2)
            reasons.append(f"spread: {ratio:.1f}x → {components['spread']:.1f}")

        # --- News proximity (0-10) ---
        if news_is_clear is None:
            components["news"] = 5.0
            reasons.append("news: unknown, neutral")
        elif news_is_clear:
            components["news"] = 10.0
            reasons.append("news: clear")
        else:
            mins = _safe_finite(news_minutes_to_next_high, 0)
            # Smooth ramp away from an imminent high-impact event.
            components["news"] = round(piecewise_linear(
                mins,
                ((5.0, 0.0), (15.0, 3.0), (30.0, 6.0), (60.0, 8.0)),
            ), 2)
            reasons.append(f"news: {mins:.0f}m to high-impact → {components['news']:.1f}")

        # --- Session quality (0-10) — discrete liquidity label ---
        liq = (session_liquidity or "").upper()
        tradeable = session_is_tradeable if session_is_tradeable is not None else True
        if not tradeable:
            components["session"] = 1.0
            reasons.append("session: not tradeable")
        elif liq == "HIGH":
            components["session"] = 10.0
            reasons.append("session: high liquidity")
        elif liq == "MEDIUM":
            components["session"] = 7.0
            reasons.append("session: medium liquidity")
        elif liq == "LOW":
            components["session"] = 3.0
            reasons.append("session: low liquidity")
        elif liq == "DEAD":
            components["session"] = 0.0
            reasons.append("session: dead")
        else:
            components["session"] = 5.0
            reasons.append("session: unknown, neutral")

        # --- Reward/risk magnitude (0-10, direction-free), smooth rising ---
        rr = _safe_finite(reward_risk_magnitude, -1.0)
        if rr < 0:
            components["reward_risk"] = 2.0
            reasons.append("rr: no data, degraded")
        else:
            components["reward_risk"] = round(piecewise_linear(
                rr,
                ((0.0, 1.0), (1.0, 3.0), (1.5, 6.0), (2.0, 8.0), (2.5, 9.0), (3.0, 10.0)),
            ), 2)
            reasons.append(f"rr: {rr:.2f} → {components['reward_risk']:.1f}")

        # --- Historical edge / EV (0-10), smooth rising ---
        ev = _safe_finite(ev_estimate, -999.0)
        if ev < -900:
            components["historical_ev"] = 5.0
            reasons.append("ev: no data, neutral")
        else:
            components["historical_ev"] = round(piecewise_linear(
                ev,
                ((-1.0, 1.0), (0.0, 3.0), (0.3, 5.0), (0.8, 8.0), (1.2, 10.0)),
            ), 2)
            reasons.append(f"ev: {ev:.2f} → {components['historical_ev']:.1f}")

        # --- Volume health (0-10, direction-free), peak in the healthy band ---
        vratio = _safe_finite(volume_ratio, -1.0)
        climax = volume_climax if volume_climax is not None else False
        if vratio < 0:
            components["volume_health"] = 5.0
            reasons.append("volume: no data, neutral")
        elif climax:
            components["volume_health"] = 1.0
            reasons.append(f"volume: climax warning ({vratio:.1f}x)")
        else:
            components["volume_health"] = round(piecewise_linear(
                vratio,
                ((0.0, 2.0), (0.5, 4.0), (1.0, 10.0), (2.0, 10.0), (3.5, 6.0)),
            ), 2)
            reasons.append(f"volume: {vratio:.1f}x → {components['volume_health']:.1f}")

        # --- Weighted aggregate ---
        total_w = sum(weights.get(k, 0.0) for k in components)
        if total_w <= 0:
            logger.warning("[OQ] all weights zero — returning LOW score")
            return OpportunityQuality(score=0.0, components=components, reasons=reasons)

        weighted_sum = sum(
            components[k] * weights.get(k, 0.0) for k in components
        )
        final = _clamp(weighted_sum / total_w)
        return OpportunityQuality(score=round(final, 2), components=components, reasons=reasons)

    except Exception as exc:
        logger.warning("[OQ] computation failed ({}), returning LOW score", exc)
        return OpportunityQuality(score=0.0, components={}, reasons=[f"computation failed: {exc}"])


def compute_entry_quality(
    *,
    trade_dir: str,
    current_price: Optional[float] = None,
    entry_price: Optional[float] = None,
    nearest_ob_distance_pips: Optional[float] = None,
    nearest_fvg_distance_pips: Optional[float] = None,
    nearest_liq_distance_pips: Optional[float] = None,
    atr_pips: Optional[float] = None,
    stop_distance_pips: Optional[float] = None,
    eq_weights: Optional[dict[str, float]] = None,
) -> EntryQuality:
    """Direction-AWARE entry-location quality.

    Uses trade_dir to judge whether the entry location is favorable
    (near support for LONG, near resistance for SHORT). Never feeds
    direction back out — it only scores the location.
    On failure: returns LOW score (0.0) + warning.
    """
    weights = eq_weights if eq_weights is not None else _DEFAULT_EQ_WEIGHTS
    components: dict[str, float] = {}
    reasons: list[str] = []

    if trade_dir not in ("LONG", "SHORT"):
        return EntryQuality(
            score=0.0, components={}, reasons=["no direction — cannot evaluate entry"]
        )

    try:
        atr = _safe_finite(atr_pips, -1.0)

        # --- OB proximity (0-10): closer is better, smooth decay ---
        ob_dist = _safe_finite(nearest_ob_distance_pips, -1.0)
        if ob_dist < 0 or atr <= 0:
            components["ob_proximity"] = 3.0
            reasons.append("ob: no data, degraded")
        else:
            ratio = ob_dist / atr if atr > 0 else 999.0
            components["ob_proximity"] = round(piecewise_linear(
                ratio,
                ((0.3, 10.0), (0.7, 8.0), (1.5, 5.0), (3.0, 2.0), (5.0, 0.0)),
            ), 2)
            reasons.append(f"ob: {ratio:.1f}x ATR → {components['ob_proximity']:.1f}")

        # --- FVG proximity (0-10): closer is better, smooth decay ---
        fvg_dist = _safe_finite(nearest_fvg_distance_pips, -1.0)
        if fvg_dist < 0 or atr <= 0:
            components["fvg_proximity"] = 3.0
            reasons.append("fvg: no data, degraded")
        else:
            ratio = fvg_dist / atr if atr > 0 else 999.0
            components["fvg_proximity"] = round(piecewise_linear(
                ratio,
                ((0.3, 10.0), (0.7, 8.0), (1.5, 5.0), (3.0, 1.0)),
            ), 2)
            reasons.append(f"fvg: {ratio:.1f}x ATR → {components['fvg_proximity']:.1f}")

        # --- Liquidity proximity (0-10): closer is better, smooth decay ---
        liq_dist = _safe_finite(nearest_liq_distance_pips, -1.0)
        if liq_dist < 0 or atr <= 0:
            components["liquidity_proximity"] = 5.0
            reasons.append("liquidity: no data, neutral")
        else:
            ratio = liq_dist / atr if atr > 0 else 999.0
            components["liquidity_proximity"] = round(piecewise_linear(
                ratio,
                ((0.5, 10.0), (1.5, 7.0), (3.0, 4.0), (5.0, 1.0)),
            ), 2)
            reasons.append(f"liquidity: {ratio:.1f}x ATR → {components['liquidity_proximity']:.1f}")

        # --- ATR extension (0-10): prefer un-extended entries, smooth decay ---
        c_price = _safe_finite(current_price, -1.0)
        e_price = _safe_finite(entry_price, -1.0)
        if c_price <= 0 or e_price <= 0 or atr <= 0:
            components["atr_extension"] = 5.0
            reasons.append("extension: no data, neutral")
        else:
            pip_ext = abs(c_price - e_price)
            atr_ratio = pip_ext / atr if atr > 0 else 999.0
            components["atr_extension"] = round(piecewise_linear(
                atr_ratio,
                ((0.3, 10.0), (0.7, 8.0), (1.5, 4.0), (3.0, 1.0)),
            ), 2)
            reasons.append(f"extension: {atr_ratio:.1f}x ATR → {components['atr_extension']:.1f}")

        # --- Stop placement quality (0-10): plateau in the well-placed band ---
        stop_dist = _safe_finite(stop_distance_pips, -1.0)
        if stop_dist <= 0 or atr <= 0:
            components["stop_quality"] = 3.0
            reasons.append("stop: no data, degraded")
        else:
            ratio = stop_dist / atr
            components["stop_quality"] = round(piecewise_linear(
                ratio,
                ((0.3, 2.0), (0.5, 10.0), (1.5, 10.0), (2.5, 5.0), (4.0, 1.0)),
            ), 2)
            reasons.append(f"stop: {ratio:.1f}x ATR → {components['stop_quality']:.1f}")

        # --- Weighted aggregate ---
        total_w = sum(weights.get(k, 0.0) for k in components)
        if total_w <= 0:
            logger.warning("[EQ] all weights zero — returning LOW score")
            return EntryQuality(score=0.0, components=components, reasons=reasons)

        weighted_sum = sum(
            components[k] * weights.get(k, 0.0) for k in components
        )
        final = _clamp(weighted_sum / total_w)
        return EntryQuality(score=round(final, 2), components=components, reasons=reasons)

    except Exception as exc:
        logger.warning("[EQ] computation failed ({}), returning LOW score", exc)
        return EntryQuality(score=0.0, components={}, reasons=[f"computation failed: {exc}"])
