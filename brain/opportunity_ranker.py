"""
APEX TRADER — Opportunity Ranker

The directional consensus (`decide`) collapses every module's evidence into a
single scalar: LONG, SHORT, or NEUTRAL.  That throws away real information — the
fast modules might see a high-confidence SHORT scalp while the slow modules see
a moderate LONG swing.  Summing those to ``net`` destroys both ideas.

This module keeps the evidence intact.  It groups the same votes ``decide`` uses
into *coherent clusters* (by direction × timeframe class), scores each cluster as
an independent opportunity with its own expected value (EV) expressed in R units,
and returns a ranked list — best idea first.

It is purely additive: it reuses the exact ``Vote`` objects the scanner already
builds and never mutates them.  ``decide`` is untouched.

Pure functions — no side effects, no broker/network access, no pandas/torch
dependency in the math itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from brain.directional_consensus import Vote

# ── Timeframe classification ──────────────────────────────────────────────
# Fast modules read micro/intrasession structure (scalp horizon); slow modules
# read higher-timeframe / positional structure (swing horizon).  These are the
# defaults — callers (via OpportunityRankerConfig) can override them.
SCALP = "SCALP"
SWING = "SWING"

DEFAULT_SCALP_MODULES: tuple[str, ...] = ("momentum", "volume", "vwap", "liquidity")
DEFAULT_SWING_MODULES: tuple[str, ...] = (
    "structure",
    "currency_strength",
    "wyckoff",
    "order_block",
    "fvg",
)


@dataclass(frozen=True)
class Opportunity:
    """A single, self-scored trade idea distilled from a coherent vote cluster.

    Unlike a scalar consensus direction, several Opportunities can coexist for
    the same instrument (e.g. a SHORT scalp and a LONG swing) — each carries its
    own expected value so the executor can pick which one deserves capital.
    """

    direction: str               # "LONG" or "SHORT"
    timeframe_class: str         # "SCALP" or "SWING"
    expected_value: float        # EV in R units (reward/risk-adjusted)
    confidence: float            # 0.0 .. 1.0 — weighted-mean cluster confidence
    coherence: float             # 0.0 .. 1.0 — cluster mass / (cluster + opposing same-tf mass)
    net_score: float             # signed weighted mass of the cluster (LONG +, SHORT −)
    reward_risk: float           # reward:risk proxy used to derive EV
    win_prob: float              # 0.0 .. 1.0 — modelled win probability
    contributors: list[str] = field(default_factory=list)
    votes: list[Vote] = field(default_factory=list)

    @property
    def summary(self) -> str:
        mods = ", ".join(self.contributors) if self.contributors else "none"
        return (
            f"{self.direction} {self.timeframe_class} "
            f"EV={self.expected_value:+.2f}R "
            f"(p_win={self.win_prob:.0%} rr={self.reward_risk:.1f} "
            f"conf={self.confidence:.2f} coherence={self.coherence:.0%}; "
            f"from: {mods})"
        )


def classify_timeframe(
    module: str,
    scalp_modules: tuple[str, ...] | list[str],
    swing_modules: tuple[str, ...] | list[str],
) -> str:
    """Map a module name to its timeframe class.

    Anything not explicitly listed as scalp defaults to SWING (the more
    conservative, slower horizon) so an unknown module never silently inflates
    a fast-scalp opportunity.
    """
    if module in scalp_modules:
        return SCALP
    if module in swing_modules:
        return SWING
    return SWING


def cluster_votes(
    votes: list[Vote],
    scalp_modules: tuple[str, ...] | list[str] = DEFAULT_SCALP_MODULES,
    swing_modules: tuple[str, ...] | list[str] = DEFAULT_SWING_MODULES,
) -> dict[tuple[str, str], list[Vote]]:
    """Group non-neutral votes into clusters keyed by (direction, timeframe_class).

    Coherent clusters form naturally: the fast modules agreeing on a direction
    become one cluster, the slow modules agreeing on a (possibly different)
    direction become another.  NEUTRAL/abstaining votes are dropped.
    """
    clusters: dict[tuple[str, str], list[Vote]] = {}
    for v in votes:
        if v.direction not in ("LONG", "SHORT"):
            continue
        if v.confidence <= 0.0 or v.weight <= 0.0:
            continue
        tf = classify_timeframe(v.module, scalp_modules, swing_modules)
        clusters.setdefault((v.direction, tf), []).append(v)
    return clusters


def score_opportunity(
    direction: str,
    timeframe_class: str,
    cluster: list[Vote],
    all_votes: list[Vote],
    *,
    reward_risk: float,
    base_win_rate: float,
    confidence_win_rate_gain: float,
    scalp_modules: tuple[str, ...] | list[str] = DEFAULT_SCALP_MODULES,
    swing_modules: tuple[str, ...] | list[str] = DEFAULT_SWING_MODULES,
) -> Opportunity:
    """Score one cluster as an independent opportunity with EV in R units.

    EV = p_win × reward_risk − (1 − p_win) × 1.0

    ``p_win`` is derived from the cluster's weighted-mean confidence scaled by
    its *coherence* — how dominant the cluster is versus opposing votes on the
    SAME timeframe horizon.  A fast SHORT scalp is judged against opposing fast
    votes, not against slow swing votes that simply see a different trade.
    """
    cluster_mass = sum(abs(v.signed) for v in cluster)
    weight_sum = sum(v.weight for v in cluster)
    confidence = (
        sum(v.weight * v.confidence for v in cluster) / weight_sum
        if weight_sum > 0
        else 0.0
    )

    # Opposing mass on the SAME timeframe horizon (a real disagreement about
    # this idea), not cross-horizon votes that describe a different trade.
    opposing_mass = 0.0
    for v in all_votes:
        if v.direction not in ("LONG", "SHORT") or v.direction == direction:
            continue
        if classify_timeframe(v.module, scalp_modules, swing_modules) != timeframe_class:
            continue
        opposing_mass += abs(v.signed)

    denom = cluster_mass + opposing_mass
    coherence = cluster_mass / denom if denom > 0 else 1.0

    win_prob = base_win_rate + confidence_win_rate_gain * confidence * coherence
    win_prob = max(0.0, min(1.0, win_prob))

    expected_value = win_prob * reward_risk - (1.0 - win_prob) * 1.0

    net_score = sum(v.signed for v in cluster)
    contributors = [v.module for v in cluster]

    return Opportunity(
        direction=direction,
        timeframe_class=timeframe_class,
        expected_value=expected_value,
        confidence=confidence,
        coherence=coherence,
        net_score=net_score,
        reward_risk=reward_risk,
        win_prob=win_prob,
        contributors=contributors,
        votes=list(cluster),
    )


def rank_opportunities(
    votes: list[Vote],
    *,
    scalp_modules: tuple[str, ...] | list[str] = DEFAULT_SCALP_MODULES,
    swing_modules: tuple[str, ...] | list[str] = DEFAULT_SWING_MODULES,
    scalp_reward_risk: float = 1.5,
    swing_reward_risk: float = 2.5,
    base_win_rate: float = 0.40,
    confidence_win_rate_gain: float = 0.40,
    min_expected_value: float = 0.0,
    min_cluster_confidence: float = 0.0,
    min_cluster_contributors: int = 1,
) -> list[Opportunity]:
    """Cluster, score, filter and rank every coherent opportunity in the panel.

    Returns a list ordered best-first by expected value.  Opportunities below
    the EV / confidence / contributor floors are dropped — when nothing clears
    the floors the list is empty (the "no trade" answer, but graded on quality
    rather than forced by a summation to NEUTRAL).
    """
    clusters = cluster_votes(votes, scalp_modules, swing_modules)
    opportunities: list[Opportunity] = []

    for (direction, tf), cluster in clusters.items():
        if len(cluster) < min_cluster_contributors:
            continue
        reward_risk = scalp_reward_risk if tf == SCALP else swing_reward_risk
        opp = score_opportunity(
            direction,
            tf,
            cluster,
            votes,
            reward_risk=reward_risk,
            base_win_rate=base_win_rate,
            confidence_win_rate_gain=confidence_win_rate_gain,
            scalp_modules=scalp_modules,
            swing_modules=swing_modules,
        )
        if opp.confidence < min_cluster_confidence:
            continue
        if opp.expected_value < min_expected_value:
            continue
        opportunities.append(opp)

    # Best first: highest EV, then highest confidence as a stable tie-break.
    opportunities.sort(key=lambda o: (o.expected_value, o.confidence), reverse=True)

    if opportunities:
        logger.debug(
            "[ranker] {} opportunity(ies): {}",
            len(opportunities),
            " | ".join(o.summary for o in opportunities),
        )
    return opportunities
