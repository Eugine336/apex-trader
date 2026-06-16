"""
APEX TRADER — Decision Trace

Every component in the entry pipeline (ranker, correlation, margin, max-trades,
entry engine, decision engine, governor, planner) does its job in isolation and
historically passed only a boolean / enum forward.  When a trade was rejected
nobody could see *why the whole chain failed*; when one was placed nobody could
reconstruct the reasoning.

This module threads a single ``DecisionTrace`` object through the pipeline.  Each
stage stamps a ``StageVerdict`` with a **mandatory, meaningful justification** and
the raw ``evidence`` behind it.  Downstream stages can read every upstream verdict
(awareness) and, when they spot a contradiction, record a ``Challenge`` against
the stage they disagree with.

The trace is purely additive: it never changes a decision, it only records the
ones already made.  A completed trace is serialised and emitted as a single
``DECISION_TRACE`` event into the append-only EventStore so the dashboard can
render the pipeline funnel, rejection breakdown, challenge feed and per-pair
decision history.

Design rules:
  * ``justification`` is enforced non-empty and meaningful — an empty or trivially
    short justification raises rather than silently passing.
  * Tracing must never break trading: the *recorder* swallows persistence errors
    (logged), but the *models* validate strictly so programming mistakes surface
    in tests.
  * Silent gaps are loud: finalising a trace that never recorded the ranker stage
    logs an ERROR — a stage that forgot to stamp is a visible problem.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from persistence.domain_events import DECISION_TRACE

# ── Stage name constants ──────────────────────────────────────────────────
# The ordered pipeline stages an opportunity flows through.  Kept as constants
# so the main loop, the dashboard and the tests never drift on stringly-typed
# names.
STAGE_RANKER = "ranker"
STAGE_CORRELATION = "correlation"
STAGE_MARGIN = "margin"
STAGE_MAX_TRADES = "max_trades"
STAGE_ENTRY_ENGINE = "entry_engine"
STAGE_DECISION_ENGINE = "decision_engine"
STAGE_GOVERNOR = "governor"
STAGE_PLANNER = "planner"
# Catch-all bucket for the secondary risk-stack gates (spread, regime, risk
# engine, EV, ML, validator, sidedness …) that reject after the core stages.
STAGE_RISK_STACK = "risk_stack"

# Ordered for funnel rendering (best-effort pipeline order).
PIPELINE_STAGES: tuple[str, ...] = (
    STAGE_RANKER,
    STAGE_CORRELATION,
    STAGE_MARGIN,
    STAGE_MAX_TRADES,
    STAGE_ENTRY_ENGINE,
    STAGE_DECISION_ENGINE,
    STAGE_GOVERNOR,
    STAGE_PLANNER,
)

# Terminal outcome labels.
OUTCOME_TRADE_PLACED = "TRADE_PLACED"
OUTCOME_ABORTED = "ABORTED"
_REJECTED_PREFIX = "REJECTED@"

# A justification must be a real explanation, not "" or "ok".
_MIN_JUSTIFICATION_LEN = 8


@dataclass
class StageVerdict:
    """One pipeline stage's recorded decision, with mandatory justification.

    ``evidence`` holds the raw numbers backing the human-readable
    ``justification`` (e.g. ``{"ev_r": 2.1, "confidence": 0.85}``) so the
    dashboard and any feedback loop can reason about the decision quantitatively.
    """

    stage: str
    owner: str
    verdict: str
    justification: str
    evidence: dict = field(default_factory=dict)
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.stage or not str(self.stage).strip():
            raise ValueError("StageVerdict.stage must be a non-empty name")
        if not self.owner or not str(self.owner).strip():
            raise ValueError("StageVerdict.owner must be a non-empty name")
        if not self.verdict or not str(self.verdict).strip():
            raise ValueError("StageVerdict.verdict must be a non-empty value")
        justification = (self.justification or "").strip()
        if len(justification) < _MIN_JUSTIFICATION_LEN:
            raise ValueError(
                f"StageVerdict.justification must be a meaningful explanation "
                f"(>= {_MIN_JUSTIFICATION_LEN} chars) — stage '{self.stage}' gave "
                f"{self.justification!r}"
            )
        if not isinstance(self.evidence, dict):
            raise ValueError("StageVerdict.evidence must be a dict")
        try:
            self.confidence = float(self.confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"StageVerdict.confidence must be a float, got {self.confidence!r}"
            ) from exc
        self.confidence = max(0.0, min(1.0, self.confidence))

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "owner": self.owner,
            "verdict": self.verdict,
            "justification": self.justification,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 4),
            "timestamp": self.timestamp,
        }


@dataclass
class Challenge:
    """A downstream stage formally disagreeing with an upstream stage's verdict.

    Awareness without the ability to push back is just logging.  A challenge
    records that ``challenger`` saw a contradiction in ``target_stage``'s
    decision — the owner of the challenged stage must have a justification that
    holds up to it.
    """

    challenger: str
    target_stage: str
    reason: str
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.challenger or not str(self.challenger).strip():
            raise ValueError("Challenge.challenger must be non-empty")
        if not self.target_stage or not str(self.target_stage).strip():
            raise ValueError("Challenge.target_stage must be non-empty")
        if not self.reason or len(str(self.reason).strip()) < _MIN_JUSTIFICATION_LEN:
            raise ValueError(
                f"Challenge.reason must be a meaningful explanation "
                f"(>= {_MIN_JUSTIFICATION_LEN} chars), got {self.reason!r}"
            )

    def to_dict(self) -> dict:
        return {
            "challenger": self.challenger,
            "target_stage": self.target_stage,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


@dataclass
class DecisionTrace:
    """The full awareness record of one scan→entry pipeline pass for a pair."""

    pair: str
    trace_id: str
    stages: list[StageVerdict] = field(default_factory=list)
    challenges: list[Challenge] = field(default_factory=list)
    final_outcome: str = ""
    final_reason: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    cycle_id: str = ""
    setup_id: str = ""
    # The stage that recorded a blocking verdict (set by the recorder).  Used to
    # attribute the rejection precisely at finalisation.  Not serialised.
    reject_stage: Optional[str] = field(default=None, repr=False)

    # ── Mutation ──────────────────────────────────────────────────────────
    def add_verdict(self, verdict: StageVerdict) -> None:
        if not isinstance(verdict, StageVerdict):
            raise TypeError("add_verdict expects a StageVerdict")
        self.stages.append(verdict)

    def add_challenge(self, challenge: Challenge) -> None:
        if not isinstance(challenge, Challenge):
            raise TypeError("add_challenge expects a Challenge")
        self.challenges.append(challenge)

    # ── Introspection ──────────────────────────────────────────────────────
    def stage_names(self) -> list[str]:
        return [s.stage for s in self.stages]

    def has_stage(self, stage: str) -> bool:
        return any(s.stage == stage for s in self.stages)

    def is_complete(self) -> bool:
        return self.completed_at is not None

    def rejected_at(self) -> Optional[str]:
        """Return the stage name where the pipeline rejected, or ``None``."""
        if self.final_outcome.startswith(_REJECTED_PREFIX):
            return self.final_outcome[len(_REJECTED_PREFIX):]
        return None

    def full_summary(self) -> str:
        """Human-readable, multi-line reconstruction of the whole chain."""
        lines = [f"DecisionTrace {self.pair} [{self.trace_id}] → {self.final_outcome or 'OPEN'}"]
        for s in self.stages:
            ev = ", ".join(f"{k}={v}" for k, v in s.evidence.items())
            lines.append(
                f"  • {s.stage}({s.owner}): {s.verdict} "
                f"(conf={s.confidence:.2f}) — {s.justification}"
                + (f" [{ev}]" if ev else "")
            )
        for c in self.challenges:
            lines.append(f"  ⚑ {c.challenger} challenges {c.target_stage}: {c.reason}")
        if self.final_reason:
            lines.append(f"  ⇒ {self.final_reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "trace_id": self.trace_id,
            "cycle_id": self.cycle_id,
            "setup_id": self.setup_id,
            "stages": [s.to_dict() for s in self.stages],
            "challenges": [c.to_dict() for c in self.challenges],
            "final_outcome": self.final_outcome,
            "final_reason": self.final_reason,
            "rejected_at": self.rejected_at(),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class DecisionTraceRecorder:
    """Owns the lifecycle of the *current* trace and persists completed ones.

    A single recorder is held by the trading loop.  Each scan→entry attempt
    calls :meth:`begin`, the pipeline stages call :meth:`stamp` / :meth:`challenge`,
    and exactly one terminal call (:meth:`finalize_success`,
    :meth:`finalize_rejection`, :meth:`finalize_abandoned`) closes and persists it.

    Every public method is a no-op when tracing is disabled or no trace is open,
    so the call sites stay clean and a tracing failure never blocks a trade.
    """

    def __init__(self, config: Any = None, event_store: Any = None) -> None:
        self.config = config
        self._enabled = bool(getattr(config, "enabled", True)) if config is not None else True
        self._store = event_store
        self._current: Optional[DecisionTrace] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current(self) -> Optional[DecisionTrace]:
        return self._current

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def begin(self, pair: str, cycle_id: str = "", setup_id: str = "") -> Optional[DecisionTrace]:
        if not self._enabled:
            return None
        # A still-open trace here means a prior pass returned without finalising
        # — surface it loudly and abandon it rather than silently leaking.
        if self._current is not None and not self._current.is_complete():
            logger.error(
                "[decision_trace] previous trace for {} ({}) never finalised — "
                "abandoning to start {}",
                self._current.pair, self._current.trace_id, pair,
            )
            self._finalize_internal(OUTCOME_ABORTED)
        self._current = DecisionTrace(
            pair=pair,
            trace_id=f"trace-{uuid.uuid4().hex[:12]}",
            cycle_id=cycle_id or "",
            setup_id=setup_id or "",
        )
        return self._current

    def stamp(
        self,
        stage: str,
        owner: str,
        verdict: str,
        justification: str,
        *,
        evidence: Optional[dict] = None,
        confidence: float = 1.0,
        blocking: bool = False,
    ) -> None:
        """Record one stage's verdict on the current trace.

        ``blocking=True`` marks this stage as the one that rejected the setup so
        finalisation can attribute the rejection precisely.
        """
        if not self._enabled or self._current is None:
            return
        try:
            verdict_obj = StageVerdict(
                stage=stage,
                owner=owner,
                verdict=verdict,
                justification=justification,
                evidence=dict(evidence) if evidence else {},
                confidence=confidence,
            )
        except Exception as exc:
            # A malformed verdict (e.g. empty justification) is a programming
            # error — log loudly but never break the live loop.
            logger.error("[decision_trace] {} stage '{}' verdict rejected: {}", self._current.pair, stage, exc)
            return
        self._current.add_verdict(verdict_obj)
        if blocking:
            self._current.reject_stage = stage

    def challenge(self, challenger: str, target_stage: str, reason: str) -> None:
        if not self._enabled or self._current is None:
            return
        try:
            self._current.add_challenge(
                Challenge(challenger=challenger, target_stage=target_stage, reason=reason)
            )
        except Exception as exc:
            logger.error("[decision_trace] {} challenge rejected: {}", self._current.pair, exc)

    def finalize_success(self) -> None:
        if not self._enabled or self._current is None:
            return
        self._finalize_internal(OUTCOME_TRADE_PLACED)

    def finalize_rejection(self, reason: str = "", stage: Optional[str] = None) -> None:
        if not self._enabled or self._current is None:
            return
        rejecting = stage or self._current.reject_stage or STAGE_RISK_STACK
        self._finalize_internal(f"{_REJECTED_PREFIX}{rejecting}", reason=reason)

    def finalize_abandoned(self, reason: str = "") -> None:
        """Terminal for infra returns (circuit open, in-flight dup) that did not
        go through the normal rejection path."""
        if not self._enabled or self._current is None:
            return
        self._finalize_internal(OUTCOME_ABORTED, reason=reason)

    # ── Internal ────────────────────────────────────────────────────────────
    def _finalize_internal(self, outcome: str, reason: str = "") -> None:
        trace = self._current
        if trace is None:
            return
        trace.final_outcome = outcome
        trace.final_reason = reason or ""
        trace.completed_at = time.time()
        self._check_completeness(trace)
        self._persist(trace)
        self._current = None

    def _check_completeness(self, trace: DecisionTrace) -> None:
        # A trace that reached a terminal state without the ranker ever stamping
        # means a stage was skipped silently — that is exactly the kind of gap
        # this system exists to make loud.
        if not trace.has_stage(STAGE_RANKER):
            logger.error(
                "[decision_trace] {} finalised ({}) with NO '{}' verdict — "
                "pipeline awareness gap (a stage forgot to stamp)",
                trace.pair, trace.final_outcome, STAGE_RANKER,
            )

    def _persist(self, trace: DecisionTrace) -> None:
        try:
            store = self._store
            if store is None:
                from persistence.event_store import get_event_store

                store = get_event_store()
            store.emit(
                event_type=DECISION_TRACE,
                severity="INFO",
                symbol=trace.pair,
                correlation_id=trace.cycle_id or None,
                parent_id=trace.setup_id or None,
                source_module="brain.decision_trace",
                payload=trace.to_dict(),
            )
        except Exception as exc:
            logger.debug("[decision_trace] persist failed for {}: {}", trace.pair, exc)
