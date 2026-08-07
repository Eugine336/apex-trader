"""
APEX TRADER — Directional Consensus

Replaces single-module direction with weighted signed voting across all
brain modules.  Each module casts a direction-independent vote; direction
is the weighted net; disagreement lowers confidence and can kill the trade.

Pure functions — no side effects, no broker/network access, no torch/pandas
dependency in the math itself.
"""

from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger


# ── Throttled extractor-failure visibility ───────────────────────────────
# Vote extractors run per candle-close for every symbol/TF. A failing
# extractor must be *visible* (no silent ``except: pass`` — a broken analyst
# is otherwise indistinguishable from "no signal"), but a per-cycle WARNING
# would flood the log. Log the first failure of each (module, stage) at
# WARNING with the exception, then throttle repeats to once per interval.
_WARN_THROTTLE_S = 300.0
_last_extractor_warn: dict[str, float] = {}


def _warn_extractor_failure(module: str, exc: Exception, *, stage: str = "") -> None:
    key = f"{module}:{stage}" if stage else module
    now = _time.monotonic()
    if now - _last_extractor_warn.get(key, 0.0) >= _WARN_THROTTLE_S:
        _last_extractor_warn[key] = now
        logger.warning(
            "[consensus] vote_from_{} extractor failed{}: {} — {}",
            module,
            f" ({stage})" if stage else "",
            type(exc).__name__,
            exc,
        )


class VoteResult(tuple):
    """A ``(direction, confidence)`` pair that also carries an ``evidence`` dict.

    Subclasses ``tuple`` so every existing ``direction, confidence = vote_from_x(...)``
    unpack, ``result[0]`` index, and ``len(result)`` keep working unchanged. The
    ``.evidence`` mapping exposes the richer per-module read (#26) — the secondary
    measurements (RSI level, MACD histogram, zone stacking, sweep type, …) that
    used to be discarded the instant the analysis object was collapsed to a bare
    2-tuple. Nothing's behaviour depends on it unless a consumer opts in to read it,
    so it is fully backward compatible.
    """

    def __new__(cls, direction: str, confidence: float, evidence: Optional[dict] = None):
        self = super().__new__(cls, (direction, confidence))
        self._evidence = dict(evidence) if evidence else {}
        return self

    @property
    def direction(self) -> str:
        return self[0]

    @property
    def confidence(self) -> float:
        return self[1]

    @property
    def evidence(self) -> dict:
        return dict(self._evidence)


@dataclass(frozen=True)
class Vote:
    module: str
    direction: str          # "LONG", "SHORT", "NEUTRAL"
    confidence: float       # 0.0 .. 1.0
    weight: float           # from ConsensusConfig.weights
    # Optional: the timeframe label this vote was actually computed on
    # (e.g. "M5", "H1", "H4"). Empty when the producer does not record it.
    # The ranker uses it to classify the opportunity's real horizon instead
    # of inferring SCALP/SWING from the module name alone (collapse #12).
    timeframe: str = ""
    # ── Per-module evidence (#26) ─────────────────────────────────────────
    # The richer read behind this vote: secondary measurements the module
    # computed (RSI level, MACD histogram, zone stacking, sweep type, …) that the
    # (direction, confidence) collapse used to discard. Optional and excluded from
    # equality/hash so existing Vote comparisons keep working — purely additive
    # context for the ranker / orchestrator / dashboard to read if they want it.
    evidence: dict = field(default_factory=dict, compare=False)

    @property
    def signed(self) -> float:
        """LONG=+1, SHORT=-1, NEUTRAL=0, scaled by confidence and weight."""
        sign = {"LONG": 1.0, "SHORT": -1.0}.get(self.direction, 0.0)
        return self.weight * self.confidence * sign


@dataclass
class DirectionDecision:
    direction: str              # "LONG", "SHORT", "NEUTRAL"
    net_score: float            # raw weighted sum
    agreement: float            # 0.0 .. 1.0 — how strongly the panel agrees
    contributors: list[str]     # modules voting WITH the net direction
    opposed_by: list[str]       # modules that triggered a high-authority veto
    votes: list[Vote] = field(default_factory=list)
    # ── Suppressed minority cluster (PR10 Phase 0) ───────────────────────
    # When the panel collapses to NEUTRAL because agreement fell below the
    # threshold, the coherent minority cluster that opposed the net direction is
    # captured here (direction, the modules voting it, and the fraction of the
    # weighted vote magnitude it carried). Lets the scanner emit a counterfactual
    # shadow so "how many suppressed counter-trend setups would have won?" can be
    # measured. ``suppressed_direction`` stays NEUTRAL when nothing was suppressed.
    suppressed_direction: str = "NEUTRAL"
    suppressed_modules: list[str] = field(default_factory=list)
    suppressed_strength: float = 0.0

    @property
    def summary(self) -> str:
        for_str = ", ".join(self.contributors) if self.contributors else "none"
        against_str = ", ".join(self.opposed_by) if self.opposed_by else "none"
        return (
            f"Consensus {self.direction} "
            f"(net={self.net_score:+.2f} agree={self.agreement:.0%}; "
            f"for: {for_str}, against: {against_str})"
        )


def vote_from_structure(bias: dict) -> VoteResult:
    """Extract a signed vote from StructureEngine.get_bias() output."""
    direction = bias.get("direction", "RANGING")
    conf = float(bias.get("confidence", 0.0))
    ev = {"raw_direction": direction, "raw_confidence": conf}
    if direction == "BULLISH":
        return VoteResult("LONG", max(0.0, min(1.0, conf)), ev)
    elif direction == "BEARISH":
        return VoteResult("SHORT", max(0.0, min(1.0, conf)), ev)
    return VoteResult("NEUTRAL", 0.0, ev)


def vote_from_currency_strength(
    pair: str,
    analysis,
    currency_pairs: dict,
) -> VoteResult:
    """
    Extract a direction-independent vote from StrengthAnalysis.rankings.
    base stronger → LONG, quote stronger → SHORT, require rank_diff >= 2.
    """
    if pair not in currency_pairs:
        return VoteResult("NEUTRAL", 0.0)

    base, quote = currency_pairs[pair]
    base_cs = next((r for r in analysis.rankings if r.currency == base), None)
    quote_cs = next((r for r in analysis.rankings if r.currency == quote), None)

    if not base_cs or not quote_cs:
        return VoteResult("NEUTRAL", 0.0, {"base": base, "quote": quote})

    rank_diff = abs(base_cs.rank - quote_cs.rank)
    ev = {
        "base": base, "quote": quote,
        "base_rank": base_cs.rank, "quote_rank": quote_cs.rank,
        "rank_diff": rank_diff,
    }
    if rank_diff < 2:
        return VoteResult("NEUTRAL", 0.0, ev)

    conf = min(rank_diff / 7.0, 1.0)

    if base_cs.rank < quote_cs.rank:
        return VoteResult("LONG", conf, ev)
    elif quote_cs.rank < base_cs.rank:
        return VoteResult("SHORT", conf, ev)
    return VoteResult("NEUTRAL", 0.0, ev)


def vote_from_volume(vol_analysis) -> VoteResult:
    """Extract a signed vote from VolumeAnalysis.confirmation_bias."""
    bias = getattr(vol_analysis, "confirmation_bias", "NEUTRAL")
    ratio = float(getattr(vol_analysis, "volume_ratio", 0.0))
    has_spike = bool(getattr(vol_analysis, "has_spike", False))
    ev = {"bias": bias, "volume_ratio": ratio, "has_spike": has_spike}

    if not has_spike or bias == "NEUTRAL":
        return VoteResult("NEUTRAL", 0.0, ev)

    conf = min(ratio / 3.0, 1.0) if ratio > 0 else 0.0

    if bias == "BULLISH":
        return VoteResult("LONG", conf, ev)
    elif bias == "BEARISH":
        return VoteResult("SHORT", conf, ev)
    return VoteResult("NEUTRAL", 0.0, ev)


def vote_from_wyckoff(wyckoff_analysis) -> VoteResult:
    """SPRING → LONG, UPTHRUST → SHORT."""
    sub = getattr(wyckoff_analysis, "sub_phase", "")
    phase_conf = float(getattr(wyckoff_analysis, "phase_confidence", 0.0))
    conf = max(0.0, min(1.0, phase_conf))
    ev = {"sub_phase": sub, "phase_confidence": phase_conf}

    if sub == "SPRING":
        return VoteResult("LONG", conf, ev)
    elif sub == "UPTHRUST":
        return VoteResult("SHORT", conf, ev)
    return VoteResult("NEUTRAL", 0.0, ev)


def _zone_confidence(scores: list[float], confluence_bonus: bool, step: float) -> float:
    """Combine same-side zone strengths into one confidence.

    ``confluence_bonus=False`` reproduces the legacy ``max()`` (best-per-side
    only). When on, additional stacked zones each add a diminishing ``step`` ×
    their strength on top of the best, so three stacked bullish OBs read stronger
    than one (#25 — zone stacking/confluence was previously discarded). Result is
    clamped to ``[0, 1]``.
    """
    if not scores:
        return 0.0
    ordered = sorted(scores, reverse=True)
    best = ordered[0]
    if not confluence_bonus:
        return min(best, 1.0)
    total = best + step * sum(ordered[1:])
    return min(total, 1.0)


def vote_from_order_blocks(
    obs: list,
    current_price: float,
    confluence_bonus: bool = True,
    confluence_step: float = 0.15,
) -> VoteResult:
    """
    Direction-independent: score nearby FRESH/TESTED OBs per side.
    Side with the stronger zone confluence wins the vote — stacked zones on the
    same side reinforce each other (#25) instead of only the single best counting.
    """
    bull_scores: list[float] = []
    bear_scores: list[float] = []
    strength_map = {"STRONG": 1.0, "MODERATE": 0.7, "WEAK": 0.4}

    for ob in obs:
        status_name = ob.status.value if hasattr(ob.status, "value") else str(ob.status)
        if status_name == "BROKEN":
            continue

        s = strength_map.get(ob.strength, 0.3)
        if status_name == "FRESH":
            s *= 1.0
        elif status_name == "TESTED":
            s *= 0.8
        else:
            s *= 0.5

        if ob.kind == "BULLISH" and ob.top < current_price:
            bull_scores.append(s)
        elif ob.kind == "BEARISH" and ob.bottom > current_price:
            bear_scores.append(s)

    best_bull = _zone_confidence(bull_scores, confluence_bonus, confluence_step)
    best_bear = _zone_confidence(bear_scores, confluence_bonus, confluence_step)
    ev = {
        "bull_zone_count": len(bull_scores),
        "bear_zone_count": len(bear_scores),
        "bull_confidence": round(best_bull, 4),
        "bear_confidence": round(best_bear, 4),
        "confluence_bonus": confluence_bonus,
    }

    if best_bull > best_bear and best_bull > 0:
        return VoteResult("LONG", best_bull, ev)
    elif best_bear > best_bull and best_bear > 0:
        return VoteResult("SHORT", best_bear, ev)
    return VoteResult("NEUTRAL", 0.0, ev)


def vote_from_fvg(
    fvgs: list,
    current_price: float,
    proximity: float,
    confluence_bonus: bool = True,
    confluence_step: float = 0.15,
) -> VoteResult:
    """
    Direction-independent: score nearby unfilled FVGs per side.
    Stacked same-side gaps reinforce each other (#25) rather than only the
    single strongest one counting.
    """
    bull_scores: list[float] = []
    bear_scores: list[float] = []
    strength_map = {"STRONG": 1.0, "MODERATE": 0.7, "WEAK": 0.4}

    for fvg in fvgs:
        status_name = fvg.status.value if hasattr(fvg.status, "value") else str(fvg.status)
        if status_name == "FILLED":
            continue

        s = strength_map.get(fvg.strength, 0.3)

        if fvg.kind == "BULLISH" and fvg.top < current_price + proximity:
            bull_scores.append(s)
        elif fvg.kind == "BEARISH" and fvg.bottom > current_price - proximity:
            bear_scores.append(s)

    best_bull = _zone_confidence(bull_scores, confluence_bonus, confluence_step)
    best_bear = _zone_confidence(bear_scores, confluence_bonus, confluence_step)
    ev = {
        "bull_zone_count": len(bull_scores),
        "bear_zone_count": len(bear_scores),
        "bull_confidence": round(best_bull, 4),
        "bear_confidence": round(best_bear, 4),
        "confluence_bonus": confluence_bonus,
    }

    if best_bull > best_bear and best_bull > 0:
        return VoteResult("LONG", best_bull, ev)
    elif best_bear > best_bull and best_bear > 0:
        return VoteResult("SHORT", best_bear, ev)
    return VoteResult("NEUTRAL", 0.0, ev)


def vote_from_liquidity(liq_mapper, m5_df, pip_size: float = 0.0001) -> VoteResult:
    """
    Vote from the observed post-sweep price reaction — no static assumption.

    Calls liq_mapper.classify_sweep_reaction to determine whether the most
    recent interaction with a liquidity zone was a REVERSAL, CONTINUATION,
    or NONE, and votes accordingly.
    """
    try:
        kind, direction, conf = liq_mapper.classify_sweep_reaction(m5_df, pip_size)
    except Exception as exc:
        _warn_extractor_failure("liquidity", exc)
        return VoteResult("NEUTRAL", 0.0)

    ev = {"sweep_kind": kind, "sweep_direction": direction, "sweep_confidence": conf}
    if kind == "NONE" or direction == "NEUTRAL":
        return VoteResult("NEUTRAL", 0.0, ev)

    return VoteResult(direction, max(0.0, min(conf, 1.0)), ev)


def vote_from_momentum(
    m5_df,
    h1_df,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    continuous_confidence: bool = True,
) -> VoteResult:
    """
    Direction-independent momentum vote from RSI + MACD.
    RSI overbought (>70) → SHORT bias; oversold (<30) → LONG bias.
    MACD above signal → LONG bias; below → SHORT bias.

    ``continuous_confidence=True`` (#11) derives confidence from *how* overbought/
    oversold RSI is and the MACD histogram magnitude, instead of the legacy
    hardcoded 0.8 (both agree) / 0.4 (one neutral). The continuous values land in
    the same neighbourhood (both-agree → [0.6, 1.0], single → [0.3, 0.6]) so net
    behaviour is preserved while the gradient (a barely-overbought 71 vs a pinned
    99) is no longer flattened. Set ``False`` to restore the constant confidences.
    """
    from brain.momentum_divergence import calculate_rsi, calculate_macd

    rsi_dir = "NEUTRAL"
    macd_dir = "NEUTRAL"
    rsi_val: Optional[float] = None
    ml: Optional[float] = None
    sl: Optional[float] = None

    try:
        rsi = calculate_rsi(m5_df["close"], rsi_period)
        if rsi is not None:
            rsi_val = rsi
            if rsi > 70:
                rsi_dir = "SHORT"
            elif rsi < 30:
                rsi_dir = "LONG"
    except Exception as exc:
        _warn_extractor_failure("momentum", exc, stage="rsi")

    try:
        result = calculate_macd(m5_df["close"], macd_fast, macd_slow, macd_signal)
        if result is not None:
            ml, sl = result
            if ml > sl:
                macd_dir = "LONG"
            elif ml < sl:
                macd_dir = "SHORT"
    except Exception as exc:
        _warn_extractor_failure("momentum", exc, stage="macd")

    # ── Continuous component strengths in [0, 1] ──────────────────────────
    # RSI: distance past the 70/30 extreme (71 → ~0.03, 100/0 → 1.0).
    rsi_strength = 0.0
    if rsi_val is not None:
        if rsi_dir == "SHORT":
            rsi_strength = max(0.0, min(1.0, (rsi_val - 70.0) / 30.0))
        elif rsi_dir == "LONG":
            rsi_strength = max(0.0, min(1.0, (30.0 - rsi_val) / 30.0))
    # MACD: histogram size relative to the line magnitude (scale-invariant).
    macd_strength = 0.0
    if ml is not None and sl is not None:
        denom = max(abs(ml), abs(sl), 1e-9)
        macd_strength = max(0.0, min(1.0, abs(ml - sl) / denom))

    ev = {
        "rsi": round(rsi_val, 2) if rsi_val is not None else None,
        "rsi_dir": rsi_dir,
        "rsi_strength": round(rsi_strength, 4),
        "macd_line": round(ml, 6) if ml is not None else None,
        "macd_signal": round(sl, 6) if sl is not None else None,
        "macd_hist": round(ml - sl, 6) if (ml is not None and sl is not None) else None,
        "macd_dir": macd_dir,
        "macd_strength": round(macd_strength, 4),
    }

    if rsi_dir == "NEUTRAL" and macd_dir == "NEUTRAL":
        return VoteResult("NEUTRAL", 0.0, ev)

    if rsi_dir == macd_dir and rsi_dir != "NEUTRAL":
        ev["agree"] = True
        if continuous_confidence:
            conf = 0.6 + 0.4 * ((rsi_strength + macd_strength) / 2.0)
        else:
            conf = 0.8
        return VoteResult(rsi_dir, max(0.0, min(1.0, conf)), ev)

    if rsi_dir != "NEUTRAL" and macd_dir != "NEUTRAL" and rsi_dir != macd_dir:
        ev["agree"] = False
        return VoteResult("NEUTRAL", 0.0, ev)

    # Exactly one source non-neutral.
    ev["agree"] = False
    non_neutral = rsi_dir if rsi_dir != "NEUTRAL" else macd_dir
    single_strength = rsi_strength if rsi_dir != "NEUTRAL" else macd_strength
    if continuous_confidence:
        conf = 0.3 + 0.3 * single_strength
    else:
        conf = 0.4
    return VoteResult(non_neutral, max(0.0, min(1.0, conf)), ev)


def vote_from_vwap(
    m5_df,
    session_open_minutes: int,
    current_price: float,
    min_session_minutes: int = 30,
) -> VoteResult:
    """
    Direction-independent VWAP vote.
    Price above VWAP → LONG bias; below → SHORT bias.
    """
    from brain.session_vwap import compute_session_vwap

    if session_open_minutes < min_session_minutes:
        return VoteResult("NEUTRAL", 0.0, {"session_open_minutes": session_open_minutes})

    try:
        vwap = compute_session_vwap(m5_df, session_open_minutes)
        if vwap is None or vwap <= 0:
            return VoteResult("NEUTRAL", 0.0)
    except Exception as exc:
        _warn_extractor_failure("vwap", exc)
        return VoteResult("NEUTRAL", 0.0)

    pip_size = abs(current_price - vwap)
    if pip_size < 1e-8:
        return VoteResult("NEUTRAL", 0.0, {"vwap": vwap, "price": current_price})

    pct_away = abs(current_price - vwap) / vwap
    conf = min(pct_away * 100, 1.0)
    ev = {
        "vwap": round(vwap, 6),
        "price": current_price,
        "pct_away": round(pct_away, 6),
    }

    if current_price > vwap:
        return VoteResult("LONG", conf, ev)
    else:
        return VoteResult("SHORT", conf, ev)


def vote_from_inducement(inducement_analysis) -> VoteResult:
    """Extract a signed vote from InducementAnalysis.

    Inducement (stop hunts / fake breakouts / turtle soup / liquidity grabs)
    is a reversal signal: the detector already resolves the post-trap
    ``expected_direction`` (LONG after a sell-side sweep, SHORT after a
    buy-side sweep) and its own confidence. We pass that through directly.
    NEUTRAL/0 when nothing was detected or no direction resolved.
    """
    detected = bool(getattr(inducement_analysis, "inducement_detected", False))
    direction = str(
        getattr(inducement_analysis, "expected_direction", "NONE") or "NONE"
    ).upper()
    conf = float(getattr(inducement_analysis, "confidence", 0.0) or 0.0)
    trap_type = getattr(inducement_analysis, "type", "NONE")
    ev = {
        "detected": detected,
        "type": trap_type,
        "expected_direction": direction,
        "raw_confidence": conf,
    }

    if not detected or direction not in ("LONG", "SHORT"):
        return VoteResult("NEUTRAL", 0.0, ev)

    return VoteResult(direction, max(0.0, min(1.0, conf)), ev)


def vote_from_volatility(
    m5_df,
    bias_direction: str,
    *,
    atr_window: int = 14,
    baseline_window: int = 50,
    expansion_ratio: float = 1.2,
    max_expansion: float = 2.5,
) -> VoteResult:
    """Volatility-regime vote: expansion confirms the structural bias direction.

    Volatility carries no direction of its own — it measures expansion vs
    compression. So this analyst only votes when ATR is *expanding* AND the
    structural bias already has a direction, reading the expansion as conviction
    behind that move (continuation). In compression (coiling) it abstains —
    NEUTRAL — because a quiet market has no directional edge to confirm.

    ``expansion_ratio`` is the recent-vs-baseline ATR threshold above which the
    market is treated as expanding; confidence scales linearly from there up to
    ``max_expansion``. NEUTRAL/0 when ATR is unavailable or the bias has no
    direction (fail-safe).
    """
    bd = str(bias_direction or "").upper()
    ev: dict = {"bias_direction": bd}

    if bd not in ("LONG", "SHORT"):
        return VoteResult("NEUTRAL", 0.0, ev)

    if m5_df is None or len(m5_df) < baseline_window + 1:
        return VoteResult("NEUTRAL", 0.0, ev)

    try:
        from brain.volatility_stop import atr_series

        recent = atr_series(m5_df, atr_window)
        base = atr_series(m5_df, baseline_window)
        if recent.empty or base.empty:
            return VoteResult("NEUTRAL", 0.0, ev)
        atr_recent = float(recent.iloc[-1])
        atr_base = float(base.iloc[-1])
    except Exception as exc:
        _warn_extractor_failure("volatility", exc)
        return VoteResult("NEUTRAL", 0.0, ev)

    if (
        not math.isfinite(atr_recent)
        or not math.isfinite(atr_base)
        or atr_base <= 0
        or atr_recent <= 0
    ):
        return VoteResult("NEUTRAL", 0.0, ev)

    ratio = atr_recent / atr_base
    ev.update({
        "atr_recent": round(atr_recent, 6),
        "atr_baseline": round(atr_base, 6),
        "expansion_ratio": round(ratio, 4),
    })

    if ratio < expansion_ratio:
        # Compression / normal range — market coiling, no directional edge.
        ev["regime"] = "COMPRESSION" if ratio < 1.0 else "NORMAL"
        return VoteResult("NEUTRAL", 0.0, ev)

    ev["regime"] = "EXPANSION"
    span = max(1e-9, max_expansion - expansion_ratio)
    conf = max(0.0, min(1.0, (ratio - expansion_ratio) / span))
    return VoteResult(bd, conf, ev)


def vote_from_correlation(correlation_signal) -> VoteResult:
    """Intermarket-confirmation vote.

    Confirms a direction when correlated instruments agree (e.g. a risk-on
    basket rising together, or a USD-strength read aligning across the complex).
    This is intermarket *confirmation* — distinct from exposure management,
    which belongs to the Portfolio Division.

    Expects a mapping ``{"direction": "LONG"|"SHORT", "confidence": float,
    "agreement": float, "detail": str}`` (or any object exposing those
    attributes). Returns NEUTRAL/0 when no intermarket read is supplied — the
    per-symbol WorldModel carries none today, so this abstains until a
    cross-pair confirmation feed is wired (fail-safe, behaviour-neutral).
    """
    if not correlation_signal:
        return VoteResult("NEUTRAL", 0.0)

    def _get(key: str, default):
        if isinstance(correlation_signal, dict):
            return correlation_signal.get(key, default)
        return getattr(correlation_signal, key, default)

    direction = str(_get("direction", "NEUTRAL") or "NEUTRAL").upper()
    conf = float(_get("confidence", 0.0) or 0.0)
    ev = {
        "direction": direction,
        "agreement": _get("agreement", None),
        "detail": _get("detail", ""),
    }

    if direction not in ("LONG", "SHORT"):
        return VoteResult("NEUTRAL", 0.0, ev)

    return VoteResult(direction, max(0.0, min(1.0, conf)), ev)
