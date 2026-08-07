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
from typing import Callable, Optional

from loguru import logger

from brain.vote_evidence import Vote

# ── Horizon labels — INFORMATIONAL ONLY ───────────────────────────────────
# Opportunistic-trading rewire: the system no longer decides "this is a scalp"
# or "this is a swing" up front and then applies different rules per box. The
# MARKET decides what the opportunity is; the system reads the evidence and
# takes whatever opportunity is there. These labels are kept purely so
# dashboards / the trade journal can SHOW which horizons the evidence spanned —
# they never gate a trade, size it, scale HTF authority, or pick a winner.
SCALP = "SCALP"
SWING = "SWING"
MIXED = "MIXED"

DEFAULT_SCALP_MODULES: tuple[str, ...] = ("momentum", "volume", "vwap", "liquidity")
DEFAULT_SWING_MODULES: tuple[str, ...] = (
    "structure",
    "currency_strength",
    "wyckoff",
    "order_block",
    "fvg",
)

# When a vote records the *actual* timeframe it was computed on, the horizon is
# read from that real signal rather than inferred from the module name (collapse
# #12 — "H1 momentum forced to SCALP by module name").  Module-name mapping
# stays the fallback for producers that do not yet record a timeframe.
SCALP_TIMEFRAMES: frozenset[str] = frozenset({"M1", "M5", "M15"})
SWING_TIMEFRAMES: frozenset[str] = frozenset({"M30", "H1", "H4", "D1", "W1", "MN", "MN1"})

# Optional callable: given (direction, timeframe_class) return a calibrated win
# probability in [0, 1], or None to fall back to the modelled formula.  Lets a
# downstream feedback loop replace the constant ``base_win_rate`` heuristic with
# observed per-horizon win rates (collapse #27) without touching this module.
WinRateProvider = Callable[[str, str], Optional[float]]



@dataclass(frozen=True)
class Opportunity:
    """A single, self-scored trade idea distilled from a coherent vote cluster.

    Unlike a scalar consensus direction, several Opportunities can coexist for
    the same instrument (e.g. a SHORT scalp and a LONG swing) — each carries its
    own expected value so the executor can pick which one deserves capital.
    """

    direction: str               # "LONG" or "SHORT"
    timeframe_class: str         # INFORMATIONAL horizon label (SCALP/SWING/MIXED)
    expected_value: float        # EV in R units (reward/risk-adjusted)
    confidence: float            # 0.0 .. 1.0 — weighted-mean cluster confidence
    coherence: float             # 0.0 .. 1.0 — cluster mass / (cluster + opposing same-tf mass)
    net_score: float             # signed weighted mass of the cluster (LONG +, SHORT −)
    reward_risk: float           # reward:risk proxy used to derive EV
    win_prob: float              # 0.0 .. 1.0 — modelled win probability
    contributors: list[str] = field(default_factory=list)
    votes: list[Vote] = field(default_factory=list)
    # ── Richer provenance (collapse #12 / #27) ───────────────────────────
    # Distinct real timeframes of the contributing votes (e.g. ["M5", "M15"]).
    # Empty when no vote recorded a timeframe — the horizon then came from the
    # module-name fallback.  Lets the orchestrator see WHICH timeframes formed
    # the idea instead of only the coarse SCALP/SWING bucket.
    timeframes: list[str] = field(default_factory=list)
    # How ``win_prob`` was derived, so a consumer can audit it rather than
    # trusting an opaque scalar: base, the confidence×coherence gain term, and
    # whether a calibrated provider overrode the modelled formula.
    win_prob_components: dict = field(default_factory=dict)

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
    timeframe: str = "",
) -> str:
    """Map a vote to its timeframe class (SCALP / SWING).

    When the vote records the real ``timeframe`` it was computed on, the horizon
    is read from that signal first (M1/M5/M15 → SCALP; M30/H1/H4/D1/… → SWING),
    so an H1 momentum read is a SWING idea rather than being forced to SCALP by
    its module name (collapse #12).  Falls back to the module-name lists when no
    timeframe is recorded.

    Anything not explicitly classified defaults to SWING (the more conservative,
    slower horizon) so an unknown module never silently inflates a fast-scalp
    opportunity.
    """
    tf = (timeframe or "").strip().upper()
    if tf in SCALP_TIMEFRAMES:
        return SCALP
    if tf in SWING_TIMEFRAMES:
        return SWING
    if module in scalp_modules:
        return SCALP
    if module in swing_modules:
        return SWING
    return SWING


def cluster_votes(
    votes: list[Vote],
    *_legacy_module_lists,
) -> dict[str, list[Vote]]:
    """Group non-neutral votes into clusters keyed by DIRECTION only.

    Opportunistic-trading rewire: the system no longer pre-sorts evidence into
    a SCALP/SWING horizon bucket before deciding. The market decides what the
    opportunity is — every vote agreeing on a direction forms one coherent
    opportunity; the opposing votes form the other. NEUTRAL/abstaining votes are
    dropped.

    Legacy ``scalp_modules`` / ``swing_modules`` positional arguments are
    accepted and ignored for backward compatibility with older call sites.
    """
    clusters: dict[str, list[Vote]] = {}
    for v in votes:
        if v.direction not in ("LONG", "SHORT"):
            continue
        if v.confidence <= 0.0 or v.weight <= 0.0:
            continue
        clusters.setdefault(v.direction, []).append(v)
    return clusters


def summarize_horizon(
    cluster: list[Vote],
    scalp_modules: tuple[str, ...] | list[str] = DEFAULT_SCALP_MODULES,
    swing_modules: tuple[str, ...] | list[str] = DEFAULT_SWING_MODULES,
) -> str:
    """Informational-only horizon label for a direction cluster.

    The decision pipeline NEVER branches on this — it exists purely so a
    consumer (dashboard / trade journal) can see which horizons the evidence
    spanned: SCALP when every contributing vote is fast, SWING when every one is
    slow, otherwise MIXED. It does not gate, size, scale HTF, or select a winner.
    """
    classes = {
        classify_timeframe(
            v.module, scalp_modules, swing_modules, getattr(v, "timeframe", ""),
        )
        for v in cluster
    }
    if not classes:
        return MIXED
    if classes == {SCALP}:
        return SCALP
    if classes == {SWING}:
        return SWING
    return MIXED


def score_opportunity(
    direction: str,
    timeframe_class: str,
    cluster: list[Vote],
    all_votes: list[Vote],
    *,
    reward_risk: float,
    base_win_rate: float,
    confidence_win_rate_gain: float,
    win_rate_provider: Optional[WinRateProvider] = None,
) -> Opportunity:
    """Score one direction cluster as an independent opportunity with EV in R.

    EV = p_win × reward_risk − (1 − p_win) × 1.0

    ``p_win`` is derived from the cluster's weighted-mean confidence scaled by
    its *coherence* — how dominant the cluster is versus ALL opposing votes,
    regardless of which timeframe they came from. The system no longer carves
    opposition into a same-horizon bucket: a LONG idea is judged against every
    SHORT vote, because the market does not care which timeframe disagreed.

    ``timeframe_class`` is an informational label only (see ``summarize_horizon``)
    — it is stamped on the result and handed to a calibrated ``win_rate_provider``
    if one is wired, but it never changes the EV math or the ranking.

    ``reward_risk`` here is a single ranking proxy for EV; the trade's ACTUAL
    reward:risk is derived later from structural targets at the entry layer, not
    from a per-horizon constant.
    """
    cluster_mass = sum(abs(v.signed) for v in cluster)
    weight_sum = sum(v.weight for v in cluster)
    confidence = (
        sum(v.weight * v.confidence for v in cluster) / weight_sum
        if weight_sum > 0
        else 0.0
    )

    # Opposing mass = every opposite-direction vote, any timeframe. A real
    # disagreement about THIS idea is any evidence pointing the other way.
    opposing_mass = 0.0
    for v in all_votes:
        if v.direction not in ("LONG", "SHORT") or v.direction == direction:
            continue
        opposing_mass += abs(v.signed)

    denom = cluster_mass + opposing_mass
    coherence = cluster_mass / denom if denom > 0 else 1.0

    gain_term = confidence_win_rate_gain * confidence * coherence
    modelled_win_prob = max(0.0, min(1.0, base_win_rate + gain_term))

    calibrated_win_prob: Optional[float] = None
    if win_rate_provider is not None:
        try:
            provided = win_rate_provider(direction, timeframe_class)
            if provided is not None:
                calibrated_win_prob = max(0.0, min(1.0, float(provided)))
        except Exception as exc:  # never let a bad hook break ranking
            logger.debug("[ranker] win_rate_provider failed: {}", exc)

    win_prob = calibrated_win_prob if calibrated_win_prob is not None else modelled_win_prob

    expected_value = win_prob * reward_risk - (1.0 - win_prob) * 1.0

    net_score = sum(v.signed for v in cluster)
    contributors = [v.module for v in cluster]
    # Distinct real timeframes of the cluster (order-preserving).
    timeframes: list[str] = []
    for v in cluster:
        tfl = (getattr(v, "timeframe", "") or "").strip().upper()
        if tfl and tfl not in timeframes:
            timeframes.append(tfl)

    win_prob_components = {
        "base_win_rate": round(base_win_rate, 4),
        "gain_term": round(gain_term, 4),
        "confidence": round(confidence, 4),
        "coherence": round(coherence, 4),
        "modelled_win_prob": round(modelled_win_prob, 4),
        "calibrated": calibrated_win_prob is not None,
    }

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
        timeframes=timeframes,
        win_prob_components=win_prob_components,
    )



def rank_opportunities(
    votes: list[Vote],
    *,
    reward_risk: float = 2.0,
    base_win_rate: float = 0.40,
    confidence_win_rate_gain: float = 0.40,
    min_expected_value: float = 0.0,
    min_cluster_confidence: float = 0.0,
    min_cluster_contributors: int = 1,
    win_rate_provider: Optional[WinRateProvider] = None,
    scalp_modules: tuple[str, ...] | list[str] = DEFAULT_SCALP_MODULES,
    swing_modules: tuple[str, ...] | list[str] = DEFAULT_SWING_MODULES,
    **_legacy,
) -> list[Opportunity]:
    """Cluster, score, filter and rank every coherent opportunity in the panel.

    Clusters are formed by DIRECTION only — no upfront scalp/swing split. Each
    surviving cluster is scored on a single ``reward_risk`` ranking proxy (the
    trade's real R:R comes from structural targets downstream, not a horizon
    constant). Returns the FULL list ordered best-first by expected value: every
    coherent idea that clears the EV / confidence / contributor floors survives
    so the executor can choose among ALL of them by capacity, not a hardcoded
    top-N. An empty list is the "no trade" answer, graded on quality rather than
    forced by a summation to NEUTRAL.

    ``scalp_modules`` / ``swing_modules`` are used only to derive the
    informational horizon label. Legacy ``scalp_reward_risk`` /
    ``swing_reward_risk`` keyword arguments are accepted and ignored.
    """
    clusters = cluster_votes(votes)
    opportunities: list[Opportunity] = []

    for direction, cluster in clusters.items():
        if len(cluster) < min_cluster_contributors:
            continue
        horizon = summarize_horizon(cluster, scalp_modules, swing_modules)
        opp = score_opportunity(
            direction,
            horizon,
            cluster,
            votes,
            reward_risk=reward_risk,
            base_win_rate=base_win_rate,
            confidence_win_rate_gain=confidence_win_rate_gain,
            win_rate_provider=win_rate_provider,
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
