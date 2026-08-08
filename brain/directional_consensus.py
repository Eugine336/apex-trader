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

# Vote-Evidence production (types + per-module extractors) now lives in the
# sanctioned Evidence module ``brain.vote_evidence`` (directional-lean votes are
# Constitution III.4 Evidence, not III.2 decisions). Re-exported here so every
# existing ``from brain.directional_consensus import Vote / vote_from_x`` keeps
# resolving. This module retains only the consensus *decision* helpers, which
# are runtime-severed from trading by the single-path cutover (the Brain is the
# sole decider) and feed analysis/candidate-display only.
from brain.vote_evidence import (  # noqa: F401  (re-exported for back-compat)
    DirectionDecision,
    Vote,
    VoteResult,
    _warn_extractor_failure,
    vote_from_correlation,
    vote_from_currency_strength,
    vote_from_fvg,
    vote_from_inducement,
    vote_from_liquidity,
    vote_from_momentum,
    vote_from_order_blocks,
    vote_from_structure,
    vote_from_volatility,
    vote_from_volume,
    vote_from_vwap,
    vote_from_wyckoff,
)


def decide(
    votes: list[Vote],
    min_net_score: float,
    min_agreement: float,
    high_authority_modules: list[str],
    high_authority_oppose_confidence: float,
    min_contributors: int = 1,
    log_suppressed_minorities: bool = True,
    currency_strength_penalty_mode: str = "veto",
) -> DirectionDecision:
    """
    Compute consensus direction from a list of weighted signed votes.

    Returns NEUTRAL (no trade) when:
    - |net| < min_net_score
    - agreement < min_agreement
    - a high-authority module opposes the net direction with high confidence
      AND ``currency_strength_penalty_mode == "veto"`` (legacy binary veto)
    - fewer than min_contributors modules cast a non-NEUTRAL vote

    High-authority opposition is always *recorded* on ``opposed_by`` for
    visibility/penalty, but it only forces NEUTRAL here when the mode is
    ``"veto"``. In ``"penalty"`` mode the opposing module's signed vote already
    weighs against the net (it is a normal vote), and the graded conviction
    penalty is applied downstream in :func:`form_thesis` — so a single
    cross-pair metric no longer nukes an otherwise-unanimous multi-TF thesis.

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
    elif opposed_by and currency_strength_penalty_mode == "veto":
        logger.info(
            "[consensus] NEUTRAL — high-authority opposition (veto mode) from: {}",
            ", ".join(opposed_by),
        )
        direction = "NEUTRAL"
    elif opposed_by:
        # Penalty mode: the opposition is recorded (and penalised downstream in
        # form_thesis) but does NOT force NEUTRAL here. The opposing module's
        # signed vote has already weighed against the net.
        logger.info(
            "[consensus] high-authority opposition (penalty mode) from: {} "
            "— direction held, conviction will be penalised",
            ", ".join(opposed_by),
        )
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


@dataclass
class ConsensusThesis:
    """The Consensus Division's market thesis for one symbol on one cycle.

    Where :class:`DirectionDecision` is the raw weighted-vote outcome, the
    thesis is the *actionable* read the Consensus Division emits every analysis
    cycle: a direction, a single bounded ``conviction`` scalar in ``[0, 1]``, the
    structured panel split (supporting / opposing / abstaining), and a
    ``trigger`` flag that is True only when the thesis is convicted enough to
    *initiate* an entry on its own — no structural zone required. The full
    :class:`DirectionDecision` is retained on ``decision`` so nothing downstream
    is forced to read a collapsed scalar (the structure is never thrown away).

    ``trigger=True`` is the market-driven entry signal: the intelligence layer
    decided, based purely on what the market is doing. Conviction is derived
    from how strongly the panel agrees (``agreement``) and how decisive the net
    weighted vote is (``net_score`` saturated by ``net_scale``) — both already
    incorporate the live :class:`VoteCalibrator` weights applied upstream.
    """

    direction: str                      # "LONG" | "SHORT" | "NEUTRAL"
    conviction: float                   # 0.0 .. 1.0
    decision: DirectionDecision         # full structured panel (never collapsed away)
    supporting: list[Vote] = field(default_factory=list)
    opposing: list[Vote] = field(default_factory=list)
    abstaining: list[Vote] = field(default_factory=list)
    trigger: bool = False
    # The conviction BEFORE symbol-relative normalization (1B). Equals
    # ``conviction`` when no SymbolConvictionStore is applied (cold start / off).
    raw_conviction: float = 0.0

    @property
    def supporting_modules(self) -> list[str]:
        return [v.module for v in self.supporting]

    @property
    def opposing_modules(self) -> list[str]:
        return [v.module for v in self.opposing]

    @property
    def summary(self) -> str:
        sup = ", ".join(self.supporting_modules) if self.supporting else "none"
        opp = ", ".join(self.opposing_modules) if self.opposing else "none"
        return (
            f"thesis {self.direction} conviction={self.conviction:.2f} "
            f"trigger={self.trigger} "
            f"(net={self.decision.net_score:+.2f} agree={self.decision.agreement:.0%}; "
            f"for: {sup}; against: {opp})"
        )


def form_thesis(
    votes: list[Vote],
    *,
    min_net_score: float,
    min_agreement: float,
    high_authority_modules: list[str],
    high_authority_oppose_confidence: float,
    min_contributors: int = 1,
    conviction_threshold: float = 0.62,
    net_scale: float = 0.0,
    log_suppressed_minorities: bool = False,
    symbol: Optional[str] = None,
    conviction_store: Any = None,
    currency_strength_penalty_mode: str = "veto",
    currency_strength_penalty_amount: float = 20.0,
) -> ConsensusThesis:
    """Turn a vote panel into an actionable :class:`ConsensusThesis`.

    Runs the existing weighted :func:`decide` (so all of its NEUTRAL guards —
    net-score floor, agreement floor, high-authority opposition veto, minimum
    contributors — still apply), then derives a single bounded ``conviction``
    scalar and a ``trigger`` flag. ``trigger`` is True only when a directional
    thesis forms AND ``conviction >= conviction_threshold`` — that is the
    market-driven, zone-independent entry signal.

    ``conviction`` blends panel agreement (how unanimous the non-neutral voters
    are) with net decisiveness (``|net_score|`` saturated by ``net_scale``;
    ``net_scale<=0`` derives a sane scale from ``min_net_score``). Both inputs
    are in ``[0, 1]`` so conviction is too. NEUTRAL theses carry conviction 0.0
    and never trigger.

    When ``symbol`` and a ``conviction_store`` (a
    :class:`~adaptive.symbol_conviction.SymbolConvictionStore`) are supplied, the
    raw conviction is recorded for that symbol and re-expressed *relative to the
    symbol's own conviction distribution* before the ``trigger`` comparison — so
    ``conviction_threshold`` means the same thing across instruments. Cold-start
    neutral: until the symbol warms up the store returns the raw value, so the
    trigger is unchanged. Both the live and backtest planes pass the same store,
    so this stays parity-preserving.
    """
    decision = decide(
        votes,
        min_net_score=min_net_score,
        min_agreement=min_agreement,
        high_authority_modules=high_authority_modules,
        high_authority_oppose_confidence=high_authority_oppose_confidence,
        min_contributors=min_contributors,
        log_suppressed_minorities=log_suppressed_minorities,
        currency_strength_penalty_mode=currency_strength_penalty_mode,
    )

    direction = decision.direction
    if direction not in ("LONG", "SHORT"):
        return ConsensusThesis(
            direction="NEUTRAL",
            conviction=0.0,
            raw_conviction=0.0,
            decision=decision,
            supporting=[],
            opposing=[v for v in decision.votes if v.direction not in ("NEUTRAL", direction)],
            abstaining=[v for v in decision.votes if v.direction == "NEUTRAL"],
            trigger=False,
        )

    scale = net_scale if net_scale and net_scale > 0 else max(min_net_score * 2.0, 1e-9)
    net_sat = min(1.0, abs(decision.net_score) / scale)
    raw_conviction = round(min(1.0, 0.5 * decision.agreement + 0.5 * net_sat), 4)

    # Graded currency_strength penalty (verification gap #2). In "penalty" mode
    # decide() let the direction stand despite high-authority opposition; the
    # opposition is now expressed as a confidence-scaled conviction haircut
    # rather than a binary kill. ``penalty_amount`` is on the 0–100 conviction-
    # percent scale, applied as ``amount/100 × opposing_confidence``. A marginal
    # thesis falls below ``conviction_threshold`` (→ no trigger); a strong,
    # unanimous multi-TF thesis survives a single opposing cross-pair metric.
    if currency_strength_penalty_mode == "penalty" and decision.opposed_by:
        try:
            opp_conf = max(
                (
                    float(v.confidence)
                    for v in decision.votes
                    if v.module in decision.opposed_by
                    and v.direction not in ("NEUTRAL", direction)
                ),
                default=0.0,
            )
            penalty = (float(currency_strength_penalty_amount) / 100.0) * opp_conf
            if penalty > 0:
                penalised = round(max(0.0, raw_conviction - penalty), 4)
                logger.info(
                    "[consensus] currency_strength penalty: conviction "
                    "{:.4f} → {:.4f} (−{:.4f}, opp_conf={:.2f}) from {}",
                    raw_conviction, penalised, penalty, opp_conf,
                    ", ".join(decision.opposed_by),
                )
                raw_conviction = penalised
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[consensus] currency_strength penalty failed: {}", exc
            )

    # 1B — symbol-relative conviction. Record the raw value and re-express it
    # against this symbol's own distribution. Guarded: any fault leaves the raw
    # conviction in place (behaviour-neutral).
    conviction = raw_conviction
    if conviction_store is not None and symbol:
        try:
            conviction = round(
                float(conviction_store.record_and_normalize(symbol, raw_conviction)), 4
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[consensus] {} conviction normalization failed: {}", symbol, exc
            )
            conviction = raw_conviction

    supporting = [v for v in decision.votes if v.direction == direction]
    opposing = [
        v for v in decision.votes
        if v.direction not in ("NEUTRAL", direction)
    ]
    abstaining = [v for v in decision.votes if v.direction == "NEUTRAL"]

    trigger = conviction >= conviction_threshold

    return ConsensusThesis(
        direction=direction,
        conviction=conviction,
        raw_conviction=raw_conviction,
        decision=decision,
        supporting=supporting,
        opposing=opposing,
        abstaining=abstaining,
        trigger=trigger,
    )


def decide_opportunities(
    votes: list[Vote],
    *,
    scalp_modules: tuple[str, ...] | list[str] | None = None,
    swing_modules: tuple[str, ...] | list[str] | None = None,
    reward_risk: float = 2.0,
    base_win_rate: float = 0.40,
    confidence_win_rate_gain: float = 0.40,
    min_expected_value: float = 0.0,
    min_cluster_confidence: float = 0.0,
    min_cluster_contributors: int = 1,
    win_rate_provider=None,
    **_legacy,
):
    """Open-ended counterpart to ``decide``.

    Where ``decide`` sums every vote into one scalar direction, this returns a
    ranked list of independent opportunities (one coherent vote cluster per
    DIRECTION — no upfront scalp/swing split), each with its own expected value.
    It reuses the SAME votes and never mutates them, so it can run alongside
    ``decide`` for shadow measurement before it drives any live trade.

    A single ``reward_risk`` is used as the EV ranking proxy; the trade's real
    reward:risk comes from structural targets at the entry layer. Legacy
    ``scalp_reward_risk`` / ``swing_reward_risk`` keyword arguments are accepted
    and ignored for backward compatibility.

    Imported lazily to avoid a circular import (opportunity_ranker imports
    ``Vote`` from this module).
    """
    from brain.opportunity_ranker import (
        DEFAULT_SCALP_MODULES,
        DEFAULT_SWING_MODULES,
        rank_opportunities,
    )

    return rank_opportunities(
        votes,
        scalp_modules=scalp_modules if scalp_modules is not None else DEFAULT_SCALP_MODULES,
        swing_modules=swing_modules if swing_modules is not None else DEFAULT_SWING_MODULES,
        reward_risk=reward_risk,
        base_win_rate=base_win_rate,
        confidence_win_rate_gain=confidence_win_rate_gain,
        min_expected_value=min_expected_value,
        min_cluster_confidence=min_cluster_confidence,
        min_cluster_contributors=min_cluster_contributors,
        win_rate_provider=win_rate_provider,
    )


# ── Per-module vote extractors ───────────────────────────────────────────
# Each returns a ``VoteResult`` — a (direction, confidence) tuple that ALSO
# carries the richer per-module read in ``.evidence`` (#26). They unpack exactly
# like the old 2-tuples (``d, c = vote_from_x(...)``), so every existing caller
# keeps working; the evidence is purely additive context for downstream
# consumers (ranker / orchestrator / dashboard). They are pure — they read the
# analysis objects already computed upstream.

