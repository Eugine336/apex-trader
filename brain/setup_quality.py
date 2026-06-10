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
        atr_pct = _safe_finite(atr_percentile, -1.0)
        if atr_pct < 0:
            components["volatility"] = 3.0
            reasons.append("volatility: no ATR data, degraded")
        elif atr_pct < 10:
            components["volatility"] = 1.0
            reasons.append("volatility: dead regime (<10th pctl)")
        elif atr_pct < 25:
            components["volatility"] = 5.0
            reasons.append("volatility: low-moderate")
        elif atr_pct <= 75:
            components["volatility"] = 10.0
            reasons.append("volatility: healthy regime")
        elif atr_pct <= 90:
            components["volatility"] = 6.0
            reasons.append("volatility: elevated")
        else:
            components["volatility"] = 2.0
            reasons.append("volatility: extreme spike (>90th pctl)")

        # --- Spread (0-10): current vs typical ---
        s_cur = _safe_finite(spread_current, -1.0)
        s_typ = _safe_finite(spread_typical, -1.0)
        if s_cur < 0 or s_typ <= 0:
            components["spread"] = 3.0
            reasons.append("spread: no data, degraded")
        else:
            ratio = s_cur / s_typ
            if ratio <= 1.0:
                components["spread"] = 10.0
                reasons.append(f"spread: tight ({ratio:.1f}x)")
            elif ratio <= 1.5:
                components["spread"] = 8.0
                reasons.append(f"spread: normal ({ratio:.1f}x)")
            elif ratio <= 2.5:
                components["spread"] = 5.0
                reasons.append(f"spread: widened ({ratio:.1f}x)")
            elif ratio <= 4.0:
                components["spread"] = 2.0
                reasons.append(f"spread: wide ({ratio:.1f}x)")
            else:
                components["spread"] = 0.0
                reasons.append(f"spread: extreme ({ratio:.1f}x)")

        # --- News proximity (0-10) ---
        if news_is_clear is None:
            components["news"] = 5.0
            reasons.append("news: unknown, neutral")
        elif news_is_clear:
            components["news"] = 10.0
            reasons.append("news: clear")
        else:
            mins = _safe_finite(news_minutes_to_next_high, 0)
            if mins <= 5:
                components["news"] = 0.0
                reasons.append(f"news: imminent high-impact ({mins}m)")
            elif mins <= 15:
                components["news"] = 3.0
                reasons.append(f"news: nearby high-impact ({mins}m)")
            elif mins <= 30:
                components["news"] = 6.0
                reasons.append(f"news: approaching ({mins}m)")
            else:
                components["news"] = 8.0
                reasons.append(f"news: distant ({mins}m)")

        # --- Session quality (0-10) ---
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

        # --- Reward/risk magnitude (0-10, direction-free) ---
        rr = _safe_finite(reward_risk_magnitude, -1.0)
        if rr < 0:
            components["reward_risk"] = 2.0
            reasons.append("rr: no data, degraded")
        elif rr < 1.0:
            components["reward_risk"] = 1.0
            reasons.append(f"rr: poor ({rr:.1f})")
        elif rr < 1.5:
            components["reward_risk"] = 5.0
            reasons.append(f"rr: acceptable ({rr:.1f})")
        elif rr < 2.5:
            components["reward_risk"] = 8.0
            reasons.append(f"rr: good ({rr:.2f})")
        else:
            components["reward_risk"] = 10.0
            reasons.append(f"rr: excellent ({rr:.2f})")

        # --- Historical edge / EV (0-10) ---
        ev = _safe_finite(ev_estimate, -999.0)
        if ev < -900:
            components["historical_ev"] = 5.0
            reasons.append("ev: no data, neutral")
        elif ev < 0:
            components["historical_ev"] = 2.0
            reasons.append(f"ev: negative ({ev:.2f})")
        elif ev < 0.3:
            components["historical_ev"] = 5.0
            reasons.append(f"ev: marginal ({ev:.2f})")
        elif ev < 0.8:
            components["historical_ev"] = 8.0
            reasons.append(f"ev: positive ({ev:.2f})")
        else:
            components["historical_ev"] = 10.0
            reasons.append(f"ev: strong ({ev:.2f})")

        # --- Volume health (0-10, direction-free) ---
        vratio = _safe_finite(volume_ratio, -1.0)
        climax = volume_climax if volume_climax is not None else False
        if vratio < 0:
            components["volume_health"] = 5.0
            reasons.append("volume: no data, neutral")
        elif climax:
            components["volume_health"] = 1.0
            reasons.append(f"volume: climax warning ({vratio:.1f}x)")
        elif vratio < 0.5:
            components["volume_health"] = 3.0
            reasons.append(f"volume: thin ({vratio:.1f}x)")
        elif vratio <= 2.0:
            components["volume_health"] = 10.0
            reasons.append(f"volume: healthy ({vratio:.1f}x)")
        else:
            components["volume_health"] = 6.0
            reasons.append(f"volume: elevated ({vratio:.1f}x)")

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

        # --- OB proximity (0-10): closer is better ---
        ob_dist = _safe_finite(nearest_ob_distance_pips, -1.0)
        if ob_dist < 0 or atr <= 0:
            components["ob_proximity"] = 3.0
            reasons.append("ob: no data, degraded")
        else:
            ratio = ob_dist / atr if atr > 0 else 999.0
            if ratio <= 0.3:
                components["ob_proximity"] = 10.0
                reasons.append(f"ob: very close ({ratio:.1f}x ATR)")
            elif ratio <= 0.7:
                components["ob_proximity"] = 8.0
                reasons.append(f"ob: near ({ratio:.1f}x ATR)")
            elif ratio <= 1.5:
                components["ob_proximity"] = 5.0
                reasons.append(f"ob: moderate ({ratio:.1f}x ATR)")
            elif ratio <= 3.0:
                components["ob_proximity"] = 2.0
                reasons.append(f"ob: far ({ratio:.1f}x ATR)")
            else:
                components["ob_proximity"] = 0.0
                reasons.append(f"ob: too far ({ratio:.1f}x ATR)")

        # --- FVG proximity (0-10): closer is better ---
        fvg_dist = _safe_finite(nearest_fvg_distance_pips, -1.0)
        if fvg_dist < 0 or atr <= 0:
            components["fvg_proximity"] = 3.0
            reasons.append("fvg: no data, degraded")
        else:
            ratio = fvg_dist / atr if atr > 0 else 999.0
            if ratio <= 0.3:
                components["fvg_proximity"] = 10.0
                reasons.append(f"fvg: very close ({ratio:.1f}x ATR)")
            elif ratio <= 0.7:
                components["fvg_proximity"] = 8.0
                reasons.append(f"fvg: near ({ratio:.1f}x ATR)")
            elif ratio <= 1.5:
                components["fvg_proximity"] = 5.0
                reasons.append(f"fvg: moderate ({ratio:.1f}x ATR)")
            else:
                components["fvg_proximity"] = 1.0
                reasons.append(f"fvg: far ({ratio:.1f}x ATR)")

        # --- Liquidity proximity (0-10): closer is better ---
        liq_dist = _safe_finite(nearest_liq_distance_pips, -1.0)
        if liq_dist < 0 or atr <= 0:
            components["liquidity_proximity"] = 5.0
            reasons.append("liquidity: no data, neutral")
        else:
            ratio = liq_dist / atr if atr > 0 else 999.0
            if ratio <= 0.5:
                components["liquidity_proximity"] = 10.0
                reasons.append(f"liquidity: very close ({ratio:.1f}x ATR)")
            elif ratio <= 1.5:
                components["liquidity_proximity"] = 7.0
                reasons.append(f"liquidity: near ({ratio:.1f}x ATR)")
            elif ratio <= 3.0:
                components["liquidity_proximity"] = 4.0
                reasons.append(f"liquidity: moderate ({ratio:.1f}x ATR)")
            else:
                components["liquidity_proximity"] = 1.0
                reasons.append(f"liquidity: far ({ratio:.1f}x ATR)")

        # --- ATR extension (0-10): prefer un-extended entries ---
        c_price = _safe_finite(current_price, -1.0)
        e_price = _safe_finite(entry_price, -1.0)
        if c_price <= 0 or e_price <= 0 or atr <= 0:
            components["atr_extension"] = 5.0
            reasons.append("extension: no data, neutral")
        else:
            pip_ext = abs(c_price - e_price)
            atr_ratio = pip_ext / atr if atr > 0 else 999.0
            if atr_ratio <= 0.3:
                components["atr_extension"] = 10.0
                reasons.append(f"extension: minimal ({atr_ratio:.1f}x ATR)")
            elif atr_ratio <= 0.7:
                components["atr_extension"] = 8.0
                reasons.append(f"extension: moderate ({atr_ratio:.1f}x ATR)")
            elif atr_ratio <= 1.5:
                components["atr_extension"] = 4.0
                reasons.append(f"extension: extended ({atr_ratio:.1f}x ATR)")
            else:
                components["atr_extension"] = 1.0
                reasons.append(f"extension: over-extended ({atr_ratio:.1f}x ATR)")

        # --- Stop placement quality (0-10) ---
        stop_dist = _safe_finite(stop_distance_pips, -1.0)
        if stop_dist <= 0 or atr <= 0:
            components["stop_quality"] = 3.0
            reasons.append("stop: no data, degraded")
        else:
            ratio = stop_dist / atr
            if 0.5 <= ratio <= 1.5:
                components["stop_quality"] = 10.0
                reasons.append(f"stop: well-placed ({ratio:.1f}x ATR)")
            elif 0.3 <= ratio < 0.5:
                components["stop_quality"] = 6.0
                reasons.append(f"stop: tight ({ratio:.1f}x ATR)")
            elif 1.5 < ratio <= 2.5:
                components["stop_quality"] = 5.0
                reasons.append(f"stop: wide ({ratio:.1f}x ATR)")
            elif ratio < 0.3:
                components["stop_quality"] = 2.0
                reasons.append(f"stop: too tight ({ratio:.1f}x ATR)")
            else:
                components["stop_quality"] = 1.0
                reasons.append(f"stop: too wide ({ratio:.1f}x ATR)")

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
