"""
APEX TRADER — Opportunity Ranker

The intelligence layer that stops collapsing rich, multi-module evidence into a
single net direction.  Instead of summing every vote into one ``LONG``/``SHORT``
/``NEUTRAL`` answer (see :func:`brain.directional_consensus.decide`), this module
lets *coherent clusters* of module votes form naturally, scores each cluster as
an independent trade opportunity with its own expected value (EV), and ranks
them so the executor can ask "which one deserves capital?".

Two opportunities can legitimately coexist on the same instrument at the same
time — e.g. a fast SHORT scalp off an M5 liquidity sweep and a slow LONG swing
aligned with H4 structure.  The old summation destroyed exactly this
information; the ranker preserves it.

Pure functions — no side effects, no broker/network access, no torch/pandas
dependency in the math itself.  Mirrors the style of
``brain/directional_consensus.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from brain.directional_consensus import Vote

# Module → timeframe class.  Fast modules read the lower timeframes (M1/M5) and
# express scalp-style opportunities; slow modules read the higher timeframes
# (H1/H4/D1) and express swing-style opportunities.  A cluster built only from
# fast modules is a SCALP; only from slow modules a SWING; a blend is MIXED.
FAST_MODULES: frozenset[str] = frozenset(
    {"momentum", "vwap", "order_block", "fvg", "liquidity", "volume"}
)
SLOW_MODULES: frozenset[str] = frozenset(
    {"structure", "currency_strength", "wyckoff"}
)

SCALP = "SCALP"
SWING = "SWING"
MIXED = "MIXED"


def timeframe_class(modules: list[str]) -> str:
    """Classify a cluster's horizon from the modules that formed it."""
    has_fast = any(m in FAST_MODULES for m in modules)
    has_slow = any(m in SLOW_MODULES for m in modules)
    if has_fast and has_slow:
        return MIXED
    if has_slow:
        return SWING
    return SCALP


@dataclass(frozen=True)
class Opportunity:
    """A single, coherent trade idea distilled from a cluster of agreeing votes.

    Unlike ``DirectionDecision`` (one answer per instrument), many of these can
    be produced per scan — one per coherent (direction, timeframe) cluster.
    """

    direction: str                 # "LONG" or "SHORT" (never "NEUTRAL")
    horizon: str                   # SCALP | SWING | MIXED
    modules: list[str]             # contributing module names
    net_score: float               # Σ |signed| over the cluster (raw conviction)
    confidence: float              # 0..1 — weighted mean module confidence
    coherence: float               # 0..1 — how concentrated the conviction is
    risk_reward: float             # assumed R:R target for this horizon
    win_prob: float                # 0..1 — calibrated from confidence
    expected_value: float          # EV in R units (can be negative)
    score: float                   # composite ranking score
    votes: list[Vote] = field(default_factory=list)

    @property
    def summary(self) -> str:
        mods = ", ".join(self.modules) if self.modules else "none"
        return (
            f"{self.horizon} {self.direction} "
            f"(EV={self.expected_value:+.2f}R score={self.score:.2f} "
            f"net={self.net_score:.2f} conf={self.confidence:.0%} "
            f"rr={self.risk_reward:.1f}; modules: {mods})"
        )


def cluster_votes(votes: list[Vote]) -> list[list[Vote]]:
    """Group non-neutral votes into coherent clusters.

    A cluster = votes that share both a *direction* and a *timeframe class*.
    Fast modules cluster with fast modules, slow with slow — so an M5 short and
    an H4 long never contaminate one another.  Returns one list of votes per
    non-empty cluster.
    """
    buckets: dict[tuple[str, str], list[Vote]] = {}
    for v in votes:
        if v.direction not in ("LONG", "SHORT"):
            continue
        if v.confidence <= 0.0 or v.weight <= 0.0:
            continue
        horizon = SCALP if v.module in FAST_MODULES else SWING
        buckets.setdefault((v.direction, horizon), []).append(v)
    return [members for members in buckets.values() if members]


def _win_prob_from_confidence(
    confidence: float,
    floor: float,
    scale: float,
) -> float:
    """Map a 0..1 cluster confidence to a calibrated win probability.

    Deliberately conservative: a zero-confidence cluster sits at ``floor`` and
    confidence lifts it by ``scale``.  Clamped to (0, 1).
    """
    p = floor + max(0.0, min(1.0, confidence)) * scale
    return max(1e-6, min(1.0 - 1e-6, p))


def score_opportunity(
    cluster: list[Vote],
    *,
    scalp_target_rr: float,
    swing_target_rr: float,
    mixed_target_rr: float,
    win_prob_floor: float,
    win_prob_scale: float,
    ev_weight: float,
    net_weight: float,
) -> Opportunity:
    """Score one coherent cluster as an independent opportunity.

    Composite score blends expected value (quality of the edge) with net score
    (how much conviction backs it).  EV is in R units:
    ``EV = win_prob * rr - (1 - win_prob)``.
    """
    if not cluster:
        raise ValueError("score_opportunity requires a non-empty cluster")

    direction = cluster[0].direction
    if any(v.direction != direction for v in cluster):
        raise ValueError(
            "score_opportunity received a mixed-direction cluster: "
            f"{[ (v.module, v.direction) for v in cluster ]}"
        )

    modules = [v.module for v in cluster]
    horizon = timeframe_class(modules)

    net_score = sum(abs(v.signed) for v in cluster)
    total_weight = sum(v.weight for v in cluster)
    if total_weight <= 0.0:
        raise ValueError(
            f"score_opportunity cluster has non-positive total weight: {modules}"
        )

    # Weighted-mean confidence — heavier modules pull the cluster confidence.
    confidence = sum(v.confidence * v.weight for v in cluster) / total_weight
    confidence = max(0.0, min(1.0, confidence))

    # Coherence: conviction concentration.  A few strong agreeing modules is
    # more coherent than many lukewarm ones.  Max signed contribution over the
    # cluster's total signed conviction, lifted by member count.
    max_signed = max(abs(v.signed) for v in cluster)
    coherence = (max_signed / net_score) if net_score > 0 else 0.0
    coherence = max(0.0, min(1.0, coherence))

    rr = {SCALP: scalp_target_rr, SWING: swing_target_rr, MIXED: mixed_target_rr}[
        horizon
    ]

    win_prob = _win_prob_from_confidence(confidence, win_prob_floor, win_prob_scale)
    expected_value = win_prob * rr - (1.0 - win_prob)

    score = ev_weight * expected_value + net_weight * net_score

    return Opportunity(
        direction=direction,
        horizon=horizon,
        modules=modules,
        net_score=net_score,
        confidence=confidence,
        coherence=coherence,
        risk_reward=rr,
        win_prob=win_prob,
        expected_value=expected_value,
        score=score,
        votes=list(cluster),
    )


def rank_opportunities(
    votes: list[Vote],
    *,
    min_cluster_net: float = 0.5,
    min_cluster_confidence: float = 0.3,
    require_positive_ev: bool = True,
    scalp_target_rr: float = 2.0,
    swing_target_rr: float = 3.0,
    mixed_target_rr: float = 2.5,
    win_prob_floor: float = 0.30,
    win_prob_scale: float = 0.40,
    ev_weight: float = 1.0,
    net_weight: float = 0.25,
) -> list[Opportunity]:
    """Turn raw module votes into a ranked list of independent opportunities.

    1. Cluster votes by (direction, timeframe class).
    2. Score each cluster (EV, confidence, coherence, R:R, composite).
    3. Drop clusters below the conviction/confidence floors (and, when
       ``require_positive_ev``, below EV>0) — but *never silently*: every drop
       is logged.
    4. Return survivors sorted by composite score, highest first.

    Edge cases handled explicitly:
    - empty / all-neutral votes → ``[]``
    - all clusters below floors → ``[]`` (the executor treats this as "no trade")
    - opposing clusters valid at different horizons (SHORT scalp + LONG swing)
      → both returned and ranked independently; nothing is collapsed.
    """
    if not votes:
        logger.info("[ranker] no votes supplied → no opportunities")
        return []

    clusters = cluster_votes(votes)
    if not clusters:
        logger.info(
            "[ranker] {} vote(s) but no LONG/SHORT cluster formed → no opportunities",
            len(votes),
        )
        return []

    ranked: list[Opportunity] = []
    for cluster in clusters:
        opp = score_opportunity(
            cluster,
            scalp_target_rr=scalp_target_rr,
            swing_target_rr=swing_target_rr,
            mixed_target_rr=mixed_target_rr,
            win_prob_floor=win_prob_floor,
            win_prob_scale=win_prob_scale,
            ev_weight=ev_weight,
            net_weight=net_weight,
        )

        if opp.net_score < min_cluster_net:
            logger.info(
                "[ranker] dropped {} {} — net {:.2f} < min_cluster_net {:.2f}",
                opp.horizon, opp.direction, opp.net_score, min_cluster_net,
            )
            continue
        if opp.confidence < min_cluster_confidence:
            logger.info(
                "[ranker] dropped {} {} — conf {:.0%} < min {:.0%}",
                opp.horizon, opp.direction, opp.confidence, min_cluster_confidence,
            )
            continue
        if require_positive_ev and opp.expected_value <= 0.0:
            logger.info(
                "[ranker] dropped {} {} — EV {:+.2f}R not positive",
                opp.horizon, opp.direction, opp.expected_value,
            )
            continue

        ranked.append(opp)

    ranked.sort(key=lambda o: o.score, reverse=True)

    if ranked:
        logger.info(
            "[ranker] {} opportunity(ies) ranked: {}",
            len(ranked),
            " | ".join(o.summary for o in ranked),
        )
    else:
        logger.info(
            "[ranker] {} cluster(s) formed but none cleared the floors → no trade",
            len(clusters),
        )
    return ranked
