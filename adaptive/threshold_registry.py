"""
APEX TRADER — Decision Threshold Hierarchy (authoritative reference)

The opportunistic-trading rewire draws a hard line between values an operator
sets as *guardrails* and values the system *learns* from its own track record.
This module is the single, auditable place that records which decision values
belong to which tier, so a reviewer can answer "is this number hardcoded
intelligence, or a deliberately-fixed safety limit?" without spelunking the
codebase.

It is documentation-as-code: a structured, importable description of the
hierarchy plus a tiny lookup helper. It deliberately holds NO live state and
changes NO behaviour — the authoritative *implementations* live in the modules
named in each entry's ``owner`` field. A test asserts the three tiers stay
disjoint and that the headline values are classified, so the hierarchy can't
silently drift.

The three tiers
---------------

1. GUARDRAILS — operator-fixed risk limits. These are NOT intelligence and must
   never be learned: loosening them risks capital. They live in config and are
   the neutral fallback the learned layers blend back toward.

2. LEARNED — thresholds/weights the system derives from its own graded outcomes
   (SignalLedger, shadow store, PairLearner/EVEstimator). Each has a
   conservative cold-start prior (matching legacy behaviour), a minimum-sample
   gate before it influences a decision, bounded movement, and staleness decay
   back toward the prior when the evidence dries up.

3. STRUCTURE_DERIVED — values the MARKET supplies per setup, read off the live
   WorldModel rather than fixed or learned (opportunistic-trading rewire,
   Sessions 1–2): the trade's targets and stop come from the structure ahead,
   not a horizon label or an R-multiple constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class ThresholdEntry:
    """One classified decision value and where its live implementation lives."""

    key: str  # canonical short name
    tier: str  # "guardrail" | "learned" | "structure_derived"
    owner: str  # module that implements/consumes it
    cold_start: str  # the value used with no history (priors)
    description: str = ""


TIER_GUARDRAIL = "guardrail"
TIER_LEARNED = "learned"
TIER_STRUCTURE = "structure_derived"


# ── GUARDRAILS — operator-fixed, never learned ───────────────────────────────
GUARDRAILS: List[ThresholdEntry] = [
    ThresholdEntry(
        "min_structural_rr",
        TIER_GUARDRAIL,
        "entry.models.EntryConfig",
        "1.0",
        "Skip a setup whose nearest structural target is closer than its stop. "
        "Single source of truth shared by the entry gate, validator and broker "
        "post-adjust check.",
    ),
    ThresholdEntry(
        "max_risk_per_trade",
        TIER_GUARDRAIL,
        "risk.risk_engine / position_sizer",
        "config risk %",
        "Maximum fraction of equity risked on a single trade.",
    ),
    ThresholdEntry(
        "max_positions / concurrency",
        TIER_GUARDRAIL,
        "governor.portfolio_governor",
        "config caps",
        "Per-symbol, per-timeframe-class and global position caps + risk budget.",
    ),
    ThresholdEntry(
        "max_daily_drawdown",
        TIER_GUARDRAIL,
        "risk.drawdown_guard",
        "config %",
        "Daily drawdown that freezes new entries.",
    ),
    ThresholdEntry(
        "min_htf_alignment_floor",
        TIER_GUARDRAIL,
        "entry.models.EntryConfig",
        "-1.0",
        "Absolute permissive cap the learned HTF-alignment floor can never drop "
        "below — a fully-opposed setup is never blindly admitted.",
    ),
    ThresholdEntry(
        "watchlist_score",
        TIER_GUARDRAIL,
        "entry.models.EntryConfig",
        "70",
        "Hard floor the learned entry-score bar can never drop below.",
    ),
]


# ── LEARNED — derived from the system's own graded track record ───────────────
LEARNED: List[ThresholdEntry] = [
    ThresholdEntry(
        "module_vote_weights",
        TIER_LEARNED,
        "adaptive.vote_calibrator.VoteCalibrator",
        "1.0 (neutral, equal panel)",
        "Each brain module's consensus vote weight, scaled by its graded "
        "accuracy (mean-1.0, Bayesian-shrunk, floor/ceiling clamped). Wired into "
        "brain.decision_core.build_consensus. ON by default.",
    ),
    ThresholdEntry(
        "opportunity_win_rate",
        TIER_LEARNED,
        "adaptive.win_rate_provider.AdaptiveWinRateProvider",
        "0.40 prior",
        "Per-pair win probability feeding opportunity EV / sizing — PairLearner "
        "then EVEstimator then cold-start prior, Bayesian-shrunk for thin "
        "samples. Wired into the ranker via win_rate_provider. ON by default.",
    ),
    ThresholdEntry(
        "entry_score_bar",
        TIER_LEARNED,
        "adaptive.gate_tuner.GateTuner('entry_engine')",
        "min_entry_score (85), offset 0",
        "Confluence-score bar, lowered within a bounded envelope when its "
        "rejected setups keep winning (shadow counterfactuals), never below "
        "watchlist_score. Applied in entry.entry_gate.EntryGate._check_score.",
    ),
    ThresholdEntry(
        "htf_alignment_floor",
        TIER_LEARNED,
        "adaptive.gate_tuner.GateTuner('htf_alignment')",
        "min_htf_alignment (-0.5), offset 0",
        "HTF-alignment gate floor, loosened within a bounded envelope when the "
        "counter-HTF setups it rejected keep winning, never below "
        "min_htf_alignment_floor. Applied in "
        "entry.entry_gate.EntryGate._check_alignment.",
    ),
    ThresholdEntry(
        "ev_gate_cutoff",
        TIER_LEARNED,
        "adaptive.gate_tuner.GateTuner('ev_gate')",
        "0.0, offset 0",
        "Expected-value entry cutoff, loosened to slightly-negative EV within a "
        "bounded envelope when its rejected setups keep winning.",
    ),
]


# ── STRUCTURE_DERIVED — the market supplies these per setup ───────────────────
STRUCTURE_DERIVED: List[ThresholdEntry] = [
    ThresholdEntry(
        "tp_targets",
        TIER_STRUCTURE,
        "entry / WorldModel (Session 1)",
        "nearest structure ahead",
        "TP1/TP2 come from the next FVG / order block / liquidity pool ahead of price, not a hardcoded R-multiple.",
    ),
    ThresholdEntry(
        "sl_placement",
        TIER_STRUCTURE,
        "entry / WorldModel (Session 1)",
        "structure invalidation level",
        "Stop sits at the structural level that invalidates the thesis.",
    ),
    ThresholdEntry(
        "stall_exit",
        TIER_STRUCTURE,
        "execution.position_worker (Session 2)",
        "live bias flip / conviction decay",
        "A flat trade is cut on a live WorldModel bias flip or conviction decay, not a per-timeframe stall clock.",
    ),
]


_ALL: List[ThresholdEntry] = GUARDRAILS + LEARNED + STRUCTURE_DERIVED


def all_entries() -> List[ThresholdEntry]:
    """Every classified decision value across all three tiers."""
    return list(_ALL)


def by_tier() -> Dict[str, List[ThresholdEntry]]:
    """The hierarchy grouped by tier."""
    return {
        TIER_GUARDRAIL: list(GUARDRAILS),
        TIER_LEARNED: list(LEARNED),
        TIER_STRUCTURE: list(STRUCTURE_DERIVED),
    }


def tier_of(key: str) -> str:
    """Return the tier for a threshold key, or '' if it is not classified."""
    for e in _ALL:
        if e.key == key:
            return e.tier
    return ""


__all__ = [
    "ThresholdEntry",
    "TIER_GUARDRAIL",
    "TIER_LEARNED",
    "TIER_STRUCTURE",
    "GUARDRAILS",
    "LEARNED",
    "STRUCTURE_DERIVED",
    "all_entries",
    "by_tier",
    "tier_of",
]
