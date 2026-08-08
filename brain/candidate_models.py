"""
APEX TRADER — Multi-Opportunity Foundation Models

Shared dataclasses for the multi-opportunity decision pipeline. The Intelligence
Division discovers opportunities; it does not decide which one wins. These types
let an independent trade idea survive — with full provenance — from consensus,
through entry generation, portfolio selection, and candidate-scoped management,
WITHOUT being net-summed into a single direction that discards every other idea.

This module is the foundation (Session 1 of the multi-opportunity rewire). It is
purely additive: nothing here mutates an existing flow. It reuses the ``Vote``
objects the scanner already builds and the ``Opportunity`` clusters the
:mod:`brain.opportunity_ranker` already scores — wrapping a scored opportunity in
a :class:`Candidate` that carries a stable ``candidate_id`` so downstream layers
(entry → portfolio → management → learning) can track one idea end-to-end.

Naming note — these are deliberately distinct from existing types so there is no
collision: ``decision.actions.EntryDecision`` is the decision engine's
enter/skip recommendation (unrelated to this dispatch envelope), and
``ManagedPosition`` is used elsewhere only as a duck-typed broker-level concept.
The foundation types here are: :class:`Candidate`, :class:`CandidateEntryDecision`,
:class:`CandidatePosition`, and :class:`Allocation`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, List

from brain.vote_evidence import Vote


def new_candidate_id() -> str:
    """A short, stable, unique id for one trade idea (uuid4 hex, 12 chars)."""
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class Candidate:
    """A single independent trade idea that survives as its own object.

    Built from one coherent ``Opportunity`` cluster (direction × timeframe
    class). Several Candidates can coexist for the same symbol — e.g. a LONG
    swing and a SHORT scalp — each carrying its own conviction (``score``) and
    expected value so the Portfolio Division can decide which deserve capital
    instead of an upstream summation deciding for it.

    Frozen/immutable so it can be safely shared across threads and stored on the
    frozen :class:`~brain.world_model.WorldModel`.
    """

    direction: str                                  # "LONG" or "SHORT"
    timeframe_class: str                            # "SCALP" / "SWING" (ranker horizon)
    score: float                                    # composite conviction (0.0 .. 1.0)
    contributing_votes: List[Vote] = field(default_factory=list)
    regime_context: str = ""                        # regime at discovery (trending/ranging/…)
    ev_estimate: float = 0.0                        # expected value in R units
    vote_count: int = 0                             # number of contributing votes
    candidate_id: str = field(default_factory=new_candidate_id)

    @property
    def contributing_modules(self) -> List[str]:
        """Module names that voted for this idea (e.g. ["structure", "wyckoff"])."""
        return [v.module for v in self.contributing_votes]

    @property
    def contributing_timeframes(self) -> List[str]:
        """Distinct real timeframes behind this idea (order-preserving, e.g. ["H4", "D1"])."""
        tfs: List[str] = []
        for v in self.contributing_votes:
            tf = (getattr(v, "timeframe", "") or "").strip().upper()
            if tf and tf not in tfs:
                tfs.append(tf)
        return tfs

    @property
    def summary(self) -> str:
        mods = ", ".join(self.contributing_modules) if self.contributing_votes else "none"
        return (
            f"{self.direction} {self.timeframe_class} "
            f"score={self.score:.2f} EV={self.ev_estimate:+.2f}R "
            f"id={self.candidate_id} (from: {mods})"
        )

    @classmethod
    def from_opportunity(
        cls,
        opportunity: Any,
        *,
        regime_context: str = "",
        candidate_id: str | None = None,
    ) -> "Candidate":
        """Wrap a scored ``Opportunity`` (from the ranker) as a ``Candidate``.

        Reuses the opportunity's already-computed clustering, confidence and EV —
        this never re-scores. ``score`` maps to the opportunity's weighted-mean
        cluster confidence; ``ev_estimate`` to its expected value in R. Duck-typed
        on purpose so any opportunity-like object (or a test stub) works.
        """
        votes = list(getattr(opportunity, "votes", []) or [])
        return cls(
            direction=str(getattr(opportunity, "direction", "")),
            timeframe_class=str(getattr(opportunity, "timeframe_class", "")),
            score=float(getattr(opportunity, "confidence", 0.0) or 0.0),
            contributing_votes=votes,
            regime_context=regime_context,
            ev_estimate=float(getattr(opportunity, "expected_value", 0.0) or 0.0),
            vote_count=len(votes),
            candidate_id=candidate_id or new_candidate_id(),
        )


@dataclass
class CandidateEntryDecision:
    """An entry decision dispatched for a single :class:`Candidate`.

    The envelope that carries one idea from the entry layer (zone or consensus
    path) to portfolio selection and execution, keeping the ``candidate`` (and
    therefore its provenance) attached so management can later be scoped to the
    very modules that opened the position.
    """

    symbol: str
    candidate: Candidate
    thesis: Any = None              # the thesis object form_thesis() returns (Session 2)
    source: str = ""               # "zone" or "consensus"
    sl: float = 0.0                # stop-loss price
    tp: float = 0.0                # take-profit price


@dataclass
class CandidatePosition:
    """Provenance for an open position, linking it back to the candidate that opened it.

    Lets the management plane (Session 4) manage a position against the SAME
    modules and timeframes that voted it open, rather than against the latest
    net-summed direction — so a LONG swing and a SHORT scalp on one symbol live
    and die by their own theses.
    """

    symbol: str
    direction: str
    candidate_id: str = ""
    timeframe_class: str = ""
    contributing_modules: List[str] = field(default_factory=list)
    contributing_timeframes: List[str] = field(default_factory=list)
    entry_regime: str = ""


@dataclass
class Allocation:
    """Portfolio Division's capital verdict for one candidate (Session 3).

    Replaces a binary allow/deny with a budget answer: how much risk this idea
    may receive given existing exposure, correlation, and concentration limits.
    """

    approved: bool
    max_risk_pct: float = 0.0       # share of remaining risk budget granted
    reason: str = ""               # why approved / rejected
    conflicts: List[str] = field(default_factory=list)  # positions that limited this
