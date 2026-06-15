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
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


@dataclass(frozen=True)
class Vote:
    module: str
    direction: str          # "LONG", "SHORT", "NEUTRAL"
    confidence: float       # 0.0 .. 1.0
    weight: float           # from ConsensusConfig.weights

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


def decide(
    votes: list[Vote],
    min_net_score: float,
    min_agreement: float,
    high_authority_modules: list[str],
    high_authority_oppose_confidence: float,
    min_contributors: int = 1,
    log_suppressed_minorities: bool = True,
) -> DirectionDecision:
    """
    Compute consensus direction from a list of weighted signed votes.

    Returns NEUTRAL (no trade) when:
    - |net| < min_net_score
    - agreement < min_agreement
    - a high-authority module opposes the net direction with high confidence
    - fewer than min_contributors modules cast a non-NEUTRAL vote

    When agreement is what dissolves an otherwise-directional panel, the
    suppressed minority cluster is captured on the returned decision (and logged
    under ``CONSENSUS_MINORITY_SUPPRESSED`` when ``log_suppressed_minorities``)
    so the scanner can shadow it for counterfactual measurement (PR10 Phase 0).
    """
    non_neutral = [v for v in votes if v.direction != "NEUTRAL"]
    neutral_modules = [v.module for v in votes if v.direction == "NEUTRAL"]
    non_neutral_modules = [v.module for v in non_neutral]

    if not non_neutral:
        logger.info(
            "[consensus] participation: {}/{} voted | non-neutral: [none] | "
            "abstained: [{}] | net=0.00 agree=0% → NEUTRAL",
            0, len(votes), ", ".join(v.module for v in votes),
        )
        return DirectionDecision(
            direction="NEUTRAL",
            net_score=0.0,
            agreement=0.0,
            contributors=[],
            opposed_by=[],
            votes=list(votes),
        )

    net = sum(v.signed for v in votes)
    raw_dir = "LONG" if net > 0 else ("SHORT" if net < 0 else "NEUTRAL")

    total_abs = sum(abs(v.signed) for v in non_neutral)
    if total_abs > 0:
        with_net = sum(
            abs(v.signed) for v in non_neutral
            if (v.direction == raw_dir)
        )
        agreement = with_net / total_abs
    else:
        agreement = 0.0

    contributors = [
        v.module for v in non_neutral if v.direction == raw_dir
    ]
    opposed_by: list[str] = []

    if raw_dir != "NEUTRAL":
        for v in non_neutral:
            if (
                v.module in high_authority_modules
                and v.direction != raw_dir
                and v.confidence >= high_authority_oppose_confidence
            ):
                opposed_by.append(v.module)

    direction = raw_dir
    suppressed_direction = "NEUTRAL"
    suppressed_modules: list[str] = []
    suppressed_strength = 0.0

    if direction == "NEUTRAL":
        pass
    elif abs(net) < min_net_score:
        logger.info(
            "[consensus] NEUTRAL — net {:.2f} below min_net_score {:.2f}",
            abs(net), min_net_score,
        )
        direction = "NEUTRAL"
    elif agreement < min_agreement:
        logger.info(
            "[consensus] NEUTRAL — agreement {:.0%} below min {:.0%}",
            agreement, min_agreement,
        )
        direction = "NEUTRAL"
        # Capture the coherent minority cluster that opposed the net direction
        # (the suppressed counter-trend opportunity) for counterfactual shadows.
        minority_dir = "SHORT" if raw_dir == "LONG" else "LONG"
        minority_votes = [
            v for v in non_neutral if v.direction == minority_dir
        ]
        if minority_votes and total_abs > 0:
            suppressed_direction = minority_dir
            suppressed_modules = [v.module for v in minority_votes]
            suppressed_strength = (
                sum(abs(v.signed) for v in minority_votes) / total_abs
            )
            if log_suppressed_minorities:
                cluster_desc = ", ".join(
                    f"{v.module}({v.confidence:.2f})" for v in minority_votes
                )
                logger.info(
                    "CONSENSUS_MINORITY_SUPPRESSED: dir={} cluster=[{}] "
                    "strength={:.2f} majority_dir={} agreement={:.2f}",
                    minority_dir, cluster_desc, suppressed_strength,
                    raw_dir, agreement,
                )
    elif opposed_by:
        logger.info(
            "[consensus] NEUTRAL — high-authority opposition from: {}",
            ", ".join(opposed_by),
        )
        direction = "NEUTRAL"
    elif len(non_neutral) < min_contributors:
        logger.info(
            "[consensus] NEUTRAL — only {} non-neutral voter(s), "
            "min_contributors requires {}",
            len(non_neutral), min_contributors,
        )
        direction = "NEUTRAL"

    logger.info(
        "[consensus] participation: {}/{} voted | non-neutral: [{}] | "
        "abstained: [{}] | net={:+.2f} agree={:.0%} → {}",
        len(non_neutral), len(votes),
        ", ".join(non_neutral_modules),
        ", ".join(neutral_modules) if neutral_modules else "none",
        net, agreement, direction,
    )

    return DirectionDecision(
        direction=direction,
        net_score=net,
        agreement=agreement,
        contributors=contributors,
        opposed_by=opposed_by,
        votes=list(votes),
        suppressed_direction=suppressed_direction,
        suppressed_modules=suppressed_modules,
        suppressed_strength=suppressed_strength,
    )


# ── Per-module vote extractors ───────────────────────────────────────────
# Each returns (direction, confidence) or ("NEUTRAL", 0.0) on failure.
# They are pure — they read the analysis objects already computed upstream.

def vote_from_structure(bias: dict) -> tuple[str, float]:
    """Extract a signed vote from StructureEngine.get_bias() output."""
    direction = bias.get("direction", "RANGING")
    conf = float(bias.get("confidence", 0.0))
    if direction == "BULLISH":
        return "LONG", max(0.0, min(1.0, conf))
    elif direction == "BEARISH":
        return "SHORT", max(0.0, min(1.0, conf))
    return "NEUTRAL", 0.0


def vote_from_currency_strength(
    pair: str,
    analysis,
    currency_pairs: dict,
) -> tuple[str, float]:
    """
    Extract a direction-independent vote from StrengthAnalysis.rankings.
    base stronger → LONG, quote stronger → SHORT, require rank_diff >= 2.
    """
    if pair not in currency_pairs:
        return "NEUTRAL", 0.0

    base, quote = currency_pairs[pair]
    base_cs = next((r for r in analysis.rankings if r.currency == base), None)
    quote_cs = next((r for r in analysis.rankings if r.currency == quote), None)

    if not base_cs or not quote_cs:
        return "NEUTRAL", 0.0

    rank_diff = abs(base_cs.rank - quote_cs.rank)
    if rank_diff < 2:
        return "NEUTRAL", 0.0

    conf = min(rank_diff / 7.0, 1.0)

    if base_cs.rank < quote_cs.rank:
        return "LONG", conf
    elif quote_cs.rank < base_cs.rank:
        return "SHORT", conf
    return "NEUTRAL", 0.0


def vote_from_volume(vol_analysis) -> tuple[str, float]:
    """Extract a signed vote from VolumeAnalysis.confirmation_bias."""
    bias = getattr(vol_analysis, "confirmation_bias", "NEUTRAL")
    ratio = float(getattr(vol_analysis, "volume_ratio", 0.0))
    has_spike = bool(getattr(vol_analysis, "has_spike", False))

    if not has_spike or bias == "NEUTRAL":
        return "NEUTRAL", 0.0

    conf = min(ratio / 3.0, 1.0) if ratio > 0 else 0.0

    if bias == "BULLISH":
        return "LONG", conf
    elif bias == "BEARISH":
        return "SHORT", conf
    return "NEUTRAL", 0.0


def vote_from_wyckoff(wyckoff_analysis) -> tuple[str, float]:
    """SPRING → LONG, UPTHRUST → SHORT."""
    sub = getattr(wyckoff_analysis, "sub_phase", "")
    phase_conf = float(getattr(wyckoff_analysis, "phase_confidence", 0.0))
    conf = max(0.0, min(1.0, phase_conf))

    if sub == "SPRING":
        return "LONG", conf
    elif sub == "UPTHRUST":
        return "SHORT", conf
    return "NEUTRAL", 0.0


def vote_from_order_blocks(obs: list, current_price: float) -> tuple[str, float]:
    """
    Direction-independent: score the strongest FRESH/TESTED OB per side.
    Side with the stronger nearby OB wins the vote.
    """
    best_bull = 0.0
    best_bear = 0.0
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
            best_bull = max(best_bull, s)
        elif ob.kind == "BEARISH" and ob.bottom > current_price:
            best_bear = max(best_bear, s)

    if best_bull > best_bear and best_bull > 0:
        return "LONG", min(best_bull, 1.0)
    elif best_bear > best_bull and best_bear > 0:
        return "SHORT", min(best_bear, 1.0)
    return "NEUTRAL", 0.0


def vote_from_fvg(fvgs: list, current_price: float, proximity: float) -> tuple[str, float]:
    """
    Direction-independent: score best unfilled FVG per side.
    """
    best_bull = 0.0
    best_bear = 0.0
    strength_map = {"STRONG": 1.0, "MODERATE": 0.7, "WEAK": 0.4}

    for fvg in fvgs:
        status_name = fvg.status.value if hasattr(fvg.status, "value") else str(fvg.status)
        if status_name == "FILLED":
            continue

        s = strength_map.get(fvg.strength, 0.3)

        if fvg.kind == "BULLISH" and fvg.top < current_price + proximity:
            best_bull = max(best_bull, s)
        elif fvg.kind == "BEARISH" and fvg.bottom > current_price - proximity:
            best_bear = max(best_bear, s)

    if best_bull > best_bear and best_bull > 0:
        return "LONG", min(best_bull, 1.0)
    elif best_bear > best_bull and best_bear > 0:
        return "SHORT", min(best_bear, 1.0)
    return "NEUTRAL", 0.0


def vote_from_liquidity(liq_mapper, m5_df, pip_size: float = 0.0001) -> tuple[str, float]:
    """
    Vote from the observed post-sweep price reaction — no static assumption.

    Calls liq_mapper.classify_sweep_reaction to determine whether the most
    recent interaction with a liquidity zone was a REVERSAL, CONTINUATION,
    or NONE, and votes accordingly.
    """
    try:
        kind, direction, conf = liq_mapper.classify_sweep_reaction(m5_df, pip_size)
    except Exception:
        return "NEUTRAL", 0.0

    if kind == "NONE" or direction == "NEUTRAL":
        return "NEUTRAL", 0.0

    return direction, max(0.0, min(conf, 1.0))


def vote_from_momentum(
    m5_df,
    h1_df,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> tuple[str, float]:
    """
    Direction-independent momentum vote from RSI + MACD.
    RSI overbought (>70) → SHORT bias; oversold (<30) → LONG bias.
    MACD above signal → LONG bias; below → SHORT bias.
    Both agreeing → higher confidence.
    """
    from brain.momentum_divergence import calculate_rsi, calculate_macd

    rsi_dir = "NEUTRAL"
    macd_dir = "NEUTRAL"

    try:
        rsi = calculate_rsi(m5_df["close"], rsi_period)
        if rsi is not None:
            if rsi > 70:
                rsi_dir = "SHORT"
            elif rsi < 30:
                rsi_dir = "LONG"
    except Exception:
        pass

    try:
        result = calculate_macd(m5_df["close"], macd_fast, macd_slow, macd_signal)
        if result is not None:
            ml, sl = result
            if ml > sl:
                macd_dir = "LONG"
            elif ml < sl:
                macd_dir = "SHORT"
    except Exception:
        pass

    if rsi_dir == "NEUTRAL" and macd_dir == "NEUTRAL":
        return "NEUTRAL", 0.0

    if rsi_dir == macd_dir and rsi_dir != "NEUTRAL":
        return rsi_dir, 0.8

    non_neutral = rsi_dir if rsi_dir != "NEUTRAL" else macd_dir
    if rsi_dir != "NEUTRAL" and macd_dir != "NEUTRAL" and rsi_dir != macd_dir:
        return "NEUTRAL", 0.0

    return non_neutral, 0.4


def vote_from_vwap(
    m5_df,
    session_open_minutes: int,
    current_price: float,
    min_session_minutes: int = 30,
) -> tuple[str, float]:
    """
    Direction-independent VWAP vote.
    Price above VWAP → LONG bias; below → SHORT bias.
    """
    from brain.session_vwap import compute_session_vwap

    if session_open_minutes < min_session_minutes:
        return "NEUTRAL", 0.0

    try:
        vwap = compute_session_vwap(m5_df, session_open_minutes)
        if vwap is None or vwap <= 0:
            return "NEUTRAL", 0.0
    except Exception:
        return "NEUTRAL", 0.0

    pip_size = abs(current_price - vwap)
    if pip_size < 1e-8:
        return "NEUTRAL", 0.0

    pct_away = abs(current_price - vwap) / vwap
    conf = min(pct_away * 100, 1.0)

    if current_price > vwap:
        return "LONG", conf
    else:
        return "SHORT", conf
