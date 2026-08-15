"""
APEX TRADER — Multi-Opportunity Foundation Models

Shared dataclasses for the multi-opportunity decision pipeline. The Intelligence
Division discovers opportunities; it does not decide which one wins. These types
let an independent trade idea survive — with full provenance — from consensus,
through entry generation, portfolio selection, and candidate-scoped management,
WITHOUT being net-summed into a single direction that discards every other idea.

This module is the foundation (Session 1 of the multi-opportunity rewire). It is
purely additive: nothing here mutates an existing flow. It wraps a scored
opportunity cluster in a :class:`Candidate` that carries a stable ``candidate_id``
so downstream layers (entry → portfolio → management → learning) can track one
idea end-to-end.

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


def new_candidate_id() -> str:
    """A short, stable, unique id for one trade idea (uuid4 hex, 12 chars)."""
    return uuid.uuid4().hex[:12]


def _opportunity_score(opportunity: Any) -> float:
    """The opportunity's own conviction — its quality, never a vote tally.

    Prefers the constitutional ``quality`` sub-score, then a legacy
    ``confidence``/``conviction`` field. Returns 0.0 when none is present.
    """
    for attr in ("quality", "confidence", "conviction"):
        v = getattr(opportunity, attr, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _opportunity_state(opportunity: Any) -> str:
    """Lifecycle state of the opportunity (enum ``.value`` or str), or ""."""
    state = getattr(opportunity, "state", "")
    return str(getattr(state, "value", state) or "")


def _opportunity_invalidation(opportunity: Any) -> str:
    """What would invalidate the thesis, joined to a single string."""
    inv = getattr(opportunity, "invalidation", None)
    if inv is None:
        inv = getattr(opportunity, "invalidation_conditions", None)
    if isinstance(inv, (list, tuple)):
        return "; ".join(str(x) for x in inv if x)
    return str(inv or "")


def _evidence_sources_of(opportunity: Any) -> List[str]:
    """Observation sources that informed the thesis — evidence, never votes.

    Reads the opportunity's ``evidence_sources`` (a list of module/observation
    names or objects carrying ``.module``/``.source``). It deliberately does NOT
    crack open any ``votes`` collection: provenance is recorded as the evidence a
    thesis rests on, not the ballots that elected a direction.
    """
    raw = getattr(opportunity, "evidence_sources", None)
    if not raw:
        return []
    out: List[str] = []
    for item in raw:
        name = str(
            getattr(item, "module", "")
            or getattr(item, "source", "")
            or item
            or ""
        ).strip()
        if name and name not in out:
            out.append(name)
    return out


def _evidence_timeframes_of(opportunity: Any) -> List[str]:
    """Timeframes the evidence spanned (from evidence sources or an explicit list)."""
    tfs: List[str] = []
    for item in getattr(opportunity, "evidence_sources", None) or []:
        tf = str(getattr(item, "timeframe", "") or "").strip().upper()
        if tf and tf not in tfs:
            tfs.append(tf)
    for tf in getattr(opportunity, "contributing_timeframes", None) or []:
        t = str(tf).strip().upper()
        if t and t not in tfs:
            tfs.append(t)
    return tfs


@dataclass(frozen=True)
class Candidate:
    """A single independent trade idea, identified by the OPPORTUNITY it tracks.

    A Candidate is the survival vehicle for one *discovered opportunity*: it
    carries that opportunity's own ``thesis``, lifecycle ``state``, expected
    value and ``invalidation`` so a single idea can be tracked end-to-end
    (entry → portfolio → management → learning) WITHOUT ever being reduced to a
    cluster of directional votes. Conviction (``score``) is the opportunity's own
    quality estimate — never a vote tally — and provenance is kept as the
    ``evidence_sources`` that informed the thesis, not as the votes that elected a
    direction. Several Candidates can coexist for one symbol — e.g. a LONG swing
    and a SHORT scalp — each living and dying by its own opportunity thesis so the
    Portfolio Division can decide which deserve capital, rather than an upstream
    summation deciding for it.

    Frozen/immutable so it can be safely shared across threads and stored on the
    frozen :class:`~brain.world_model.WorldModel`.
    """

    direction: str                                  # "LONG" or "SHORT"
    timeframe_class: str                            # "SCALP" / "SWING" (opportunity horizon)
    score: float                                    # opportunity conviction / quality (0.0 .. 1.0)
    opportunity_id: str = ""                        # id of the opportunity this candidate tracks
    thesis: str = ""                                # the opportunity's own reasoning
    state: str = ""                                 # opportunity lifecycle state (FORMING/ACTIVE/…)
    invalidation: str = ""                          # what would invalidate the thesis
    evidence_sources: List[str] = field(default_factory=list)   # observations that informed the thesis
    contributing_timeframes: List[str] = field(default_factory=list)
    regime_context: str = ""                        # regime at discovery (trending/ranging/…)
    ev_estimate: float = 0.0                        # expected value in R units
    candidate_id: str = field(default_factory=new_candidate_id)

    @property
    def summary(self) -> str:
        srcs = ", ".join(self.evidence_sources) if self.evidence_sources else "none"
        return (
            f"{self.direction} {self.timeframe_class} "
            f"score={self.score:.2f} EV={self.ev_estimate:+.2f}R "
            f"id={self.candidate_id} (evidence: {srcs})"
        )

    @classmethod
    def from_opportunity(
        cls,
        opportunity: Any,
        *,
        regime_context: str = "",
        candidate_id: str | None = None,
    ) -> "Candidate":
        """Wrap a discovered opportunity-like object as a ``Candidate``.

        Ties candidate identity to the opportunity's own thesis, lifecycle state,
        expected value and invalidation — never to a vote cluster. Reuses the
        opportunity's already-formed reasoning; this never re-scores. ``score``
        maps to the opportunity's quality/conviction and ``ev_estimate`` to its
        expected value in R. Duck-typed on purpose so any opportunity-like object
        (or a test stub) works. Provenance is taken from the opportunity's
        ``evidence_sources`` and recorded as *evidence*, NOT as votes — a
        vote-count is never derived or stored.
        """
        return cls(
            direction=str(getattr(opportunity, "direction", "")),
            timeframe_class=str(
                getattr(opportunity, "timeframe_class", "")
                or getattr(opportunity, "horizon", "")
            ),
            score=_opportunity_score(opportunity),
            opportunity_id=str(getattr(opportunity, "opportunity_id", "") or ""),
            thesis=str(getattr(opportunity, "thesis", "") or ""),
            state=_opportunity_state(opportunity),
            invalidation=_opportunity_invalidation(opportunity),
            evidence_sources=_evidence_sources_of(opportunity),
            contributing_timeframes=_evidence_timeframes_of(opportunity),
            regime_context=regime_context,
            ev_estimate=float(getattr(opportunity, "expected_value", 0.0) or 0.0),
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
    source: str = ""               # "zone" or "consensus"
    sl: float = 0.0                # stop-loss price
    tp: float = 0.0                # take-profit price


@dataclass
class CandidatePosition:
    """Provenance for an open position, linking it back to the candidate that opened it.

    Lets the management plane (Session 4) manage a position against the SAME
    modules and timeframes whose observations opened it, rather than against the
    latest net-summed direction — so a LONG swing and a SHORT scalp on one symbol
    live and die by their own theses.
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
