"""APEX TRADER — Action Orchestrator (the Autonomous Action Layer's gateway).

The constitution's Single Reasoner Principle: there is exactly one cognitive
authority — the AI Cognitive Brain. Every other subsystem may only observe,
provide evidence, validate feasibility, execute authorised decisions, remember,
learn, or act externally. This module is the *execution + governance + memory*
gateway for external actions. It contains **no reasoning of its own**: it never
decides *whether* to act, *what* to do, or *whether an opportunity exists*. It
only takes an :class:`ActionObjective` that the Brain already reasoned into
existence, evaluates it against deterministic governance policy, executes the
authorised ones through an adapter, verifies, and records everything.

The lifecycle every external capability follows is identical, regardless of the
target system (Slack, GitHub, Telegram, cloud, docs, …):

    objective (from the Brain) → policy evaluation → [authorise | require
    approval | reject] → execute → verify → audit → memory

Design principles:

* **Single reasoner.** An objective with no ``source`` is rejected — actions
  must originate from the Brain, never from this layer. The orchestrator holds
  no market opinion and generates no objectives.
* **Do-nothing is valid.** The ``noop`` capability is always accepted and
  recorded as a deliberate SKIP — the absence of action is a first-class choice.
* **Hierarchical autonomy.** Negligible/low risk can auto-authorise; medium risk
  needs high confidence or human approval; high/destructive risk always requires
  approval. Autonomy is earned, never assumed.
* **Fail-safe.** Every public method swallows faults and records a safe terminal
  state — an action-layer fault must never break the trading cycle.
* **Fully observable.** Every objective, decision, policy reason, result and
  verification is captured in a bounded audit log and optionally forwarded to a
  memory sink, so nothing happens without traceability.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Optional

from loguru import logger


class RiskTier(Enum):
    NEGLIGIBLE = "negligible"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DESTRUCTIVE = "destructive"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# Ordered severity so policy can compare tiers (higher = riskier).
_TIER_ORDER = {
    RiskTier.NEGLIGIBLE: 0,
    RiskTier.LOW: 1,
    RiskTier.MEDIUM: 2,
    RiskTier.HIGH: 3,
    RiskTier.DESTRUCTIVE: 4,
}


def tier_from(value: Any, default: RiskTier = RiskTier.LOW) -> RiskTier:
    """Coerce a string / RiskTier into a RiskTier (fail-safe)."""
    if isinstance(value, RiskTier):
        return value
    try:
        return RiskTier(str(value or "").strip().lower())
    except ValueError:
        return default


class Decision(Enum):
    AUTHORIZE = "authorize"
    REQUIRE_APPROVAL = "require_approval"
    REJECT = "reject"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ActionStatus(Enum):
    PROPOSED = "proposed"
    REJECTED = "rejected"
    PENDING_APPROVAL = "pending_approval"
    EXECUTED = "executed"
    FAILED = "failed"
    SKIPPED = "skipped"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


NOOP_CAPABILITY = "noop"


@dataclass
class ActionObjective:
    """A Brain-authored objective — WHAT to achieve, WHY, and its risk profile.

    The orchestrator never fills these in; the Brain does. ``source`` records
    which reasoner produced it (required — see the Single Reasoner Principle),
    and ``evidence_ref`` links to the supporting thesis/evidence for the audit.
    """

    capability: str                              # e.g. "operator.notify", "engineering.create_task", "noop"
    objective: str = ""                          # human-readable WHY
    params: dict = field(default_factory=dict)   # capability inputs
    expected_outcome: str = ""
    confidence: float = 0.0                      # 0..1 — the Brain's confidence this action is correct
    priority: int = 3                            # 1 (highest) .. 5 (lowest)
    reversible: bool = True
    risk_tier: RiskTier = RiskTier.LOW
    required_permissions: list[str] = field(default_factory=list)
    fallback: str = ""
    source: str = ""                             # originating reasoner (e.g. "ai_brain")
    evidence_ref: str = ""                       # traceability into institutional memory
    objective_id: str = ""

    def __post_init__(self) -> None:
        if not self.objective_id:
            self.objective_id = uuid.uuid4().hex[:12]
        self.risk_tier = tier_from(self.risk_tier)
        try:
            self.confidence = min(1.0, max(0.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 0.0

    def to_dict(self) -> dict:
        return {
            "objective_id": self.objective_id,
            "capability": self.capability,
            "objective": self.objective,
            "params": dict(self.params),
            "expected_outcome": self.expected_outcome,
            "confidence": round(self.confidence, 4),
            "priority": int(self.priority),
            "reversible": bool(self.reversible),
            "risk_tier": self.risk_tier.value,
            "required_permissions": list(self.required_permissions),
            "fallback": self.fallback,
            "source": self.source,
            "evidence_ref": self.evidence_ref,
        }


@dataclass
class ActionResult:
    """The outcome an adapter reports for one executed capability."""

    ok: bool
    external_ref: str = ""       # id/url the external system returned
    detail: str = ""
    verified: bool = False       # did Observation confirm the intended effect?
    # Part IX Art 3/4/8 — the payload a READ capability returns (search hits,
    # research passages, an advisor's answer). Empty for pure side-effect
    # actions (notify/ticket); carried so knowledge retrievals can become
    # Evidence for the Brain rather than being discarded on success.
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "external_ref": self.external_ref,
            "detail": self.detail,
            "verified": self.verified,
            "data": dict(self.data or {}),
        }


@dataclass
class ActionRecord:
    """The complete cognitive-loop record for one objective — audit + memory."""

    objective: ActionObjective
    status: ActionStatus
    decision: Decision
    decision_reason: str = ""
    result: Optional[ActionResult] = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "objective": self.objective.to_dict(),
            "status": self.status.value,
            "decision": self.decision.value,
            "decision_reason": self.decision_reason,
            "result": self.result.to_dict() if self.result is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class GovernancePolicy:
    """Deterministic authorisation policy — the hierarchical-autonomy gate.

    Pure and side-effect free: :meth:`evaluate` maps an objective to a
    :class:`Decision`. No reasoning about markets — only permission/risk policy.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        require_source: bool = True,
        min_confidence: float = 0.2,
        auto_max_risk: RiskTier = RiskTier.LOW,
        medium_confidence_threshold: float = 0.7,
    ) -> None:
        self.enabled = bool(enabled)
        self.require_source = bool(require_source)
        self.min_confidence = min(1.0, max(0.0, float(min_confidence)))
        # Safety cap: auto-authorisation can never exceed MEDIUM — HIGH and
        # DESTRUCTIVE always require approval no matter how it is configured.
        capped = tier_from(auto_max_risk)
        if _TIER_ORDER[capped] > _TIER_ORDER[RiskTier.MEDIUM]:
            capped = RiskTier.MEDIUM
        self.auto_max_risk = capped
        self.medium_confidence_threshold = min(1.0, max(0.0, float(medium_confidence_threshold)))

    def evaluate(self, objective: ActionObjective) -> "tuple[Decision, str]":
        if not self.enabled:
            return Decision.REJECT, "action layer disabled"
        if self.require_source and not str(objective.source or "").strip():
            return (
                Decision.REJECT,
                "no originating reasoner — objectives must come from the Brain",
            )
        if objective.confidence < self.min_confidence:
            return (
                Decision.REJECT,
                f"confidence {objective.confidence:.2f} below floor {self.min_confidence:.2f}",
            )
        tier = objective.risk_tier
        order = _TIER_ORDER[tier]
        if order <= _TIER_ORDER[self.auto_max_risk]:
            return Decision.AUTHORIZE, f"{tier.value} risk within auto ceiling"
        if tier == RiskTier.MEDIUM:
            if objective.confidence >= self.medium_confidence_threshold:
                return (
                    Decision.AUTHORIZE,
                    f"medium risk auto-authorised on high confidence "
                    f"{objective.confidence:.2f} ≥ {self.medium_confidence_threshold:.2f}",
                )
            return Decision.REQUIRE_APPROVAL, "medium risk, confidence below auto threshold"
        # HIGH / DESTRUCTIVE — always human/deterministic approval.
        return Decision.REQUIRE_APPROVAL, f"{tier.value} risk requires explicit approval"


# A memory sink receives every finalised record (best-effort).
MemorySink = Callable[[ActionRecord], None]


class ActionOrchestrator:
    """Single gateway for all external actions. Governs, executes, remembers.

    It has NO cognition: objectives arrive already reasoned by the Brain. This
    class only evaluates policy, executes authorised objectives through the
    injected ``adapter``, verifies, and records. Fail-safe throughout.
    """

    def __init__(
        self,
        policy: GovernancePolicy,
        adapter: Any,
        *,
        memory_sink: Optional[MemorySink] = None,
        audit_limit: int = 500,
    ) -> None:
        self._policy = policy
        self._adapter = adapter
        self._memory_sink = memory_sink
        self._audit: Deque[ActionRecord] = deque(maxlen=max(1, int(audit_limit)))
        self._pending: dict[str, ActionObjective] = {}
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    # ── Submit (the cognitive loop for one objective) ─────────────────────

    def submit(self, objective: ActionObjective) -> ActionRecord:
        """Run one objective through the full lifecycle. Never raises."""
        try:
            now = _now_iso()
            # Do-nothing is always a valid, recorded decision.
            if str(objective.capability or "").strip().lower() == NOOP_CAPABILITY:
                return self._finalise(
                    objective, ActionStatus.SKIPPED, Decision.AUTHORIZE,
                    "do-nothing is a valid decision", None, now,
                )
            decision, reason = self._policy.evaluate(objective)
            if decision == Decision.REJECT:
                return self._finalise(
                    objective, ActionStatus.REJECTED, decision, reason, None, now,
                )
            if decision == Decision.REQUIRE_APPROVAL:
                with self._lock:
                    self._pending[objective.objective_id] = objective
                return self._finalise(
                    objective, ActionStatus.PENDING_APPROVAL, decision, reason, None, now,
                )
            # AUTHORIZE → execute.
            return self._execute(objective, decision, reason, now)
        except Exception as exc:  # noqa: BLE001 — the action layer must never break a cycle
            logger.debug("[action] submit({}) ignored a fault: {}",
                         getattr(objective, "capability", "?"), exc)
            return self._finalise(
                objective, ActionStatus.FAILED, Decision.REJECT,
                f"orchestrator fault: {exc}", None, _now_iso(),
            )

    def approve(self, objective_id: str, approver: str = "operator") -> Optional[ActionRecord]:
        """Execute a previously PENDING_APPROVAL objective after approval."""
        try:
            with self._lock:
                objective = self._pending.pop(str(objective_id), None)
            if objective is None:
                return None
            return self._execute(
                objective, Decision.AUTHORIZE,
                f"approved by {approver}", _now_iso(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[action] approve({}) ignored a fault: {}", objective_id, exc)
            return None

    def _execute(
        self, objective: ActionObjective, decision: Decision, reason: str, now: str,
    ) -> ActionRecord:
        try:
            raw = self._adapter.execute(objective.capability, dict(objective.params))
            result = raw if isinstance(raw, ActionResult) else ActionResult(
                ok=bool(getattr(raw, "ok", False)),
                external_ref=str(getattr(raw, "external_ref", "") or ""),
                detail=str(getattr(raw, "detail", "") or ""),
                verified=bool(getattr(raw, "verified", False)),
            )
        except Exception as exc:  # noqa: BLE001 — an adapter fault is a FAILED action, not a crash
            logger.debug("[action] adapter.execute({}) fault: {}", objective.capability, exc)
            result = ActionResult(ok=False, detail=f"adapter fault: {exc}")
        status = ActionStatus.EXECUTED if result.ok else ActionStatus.FAILED
        return self._finalise(objective, status, decision, reason, result, now)

    def _finalise(
        self, objective: ActionObjective, status: ActionStatus, decision: Decision,
        reason: str, result: Optional[ActionResult], now: str,
    ) -> ActionRecord:
        record = ActionRecord(
            objective=objective, status=status, decision=decision,
            decision_reason=reason, result=result,
            created_at=now, updated_at=_now_iso(),
        )
        with self._lock:
            self._audit.append(record)
            self._counts[status.value] = self._counts.get(status.value, 0) + 1
        if self._memory_sink is not None:
            try:
                self._memory_sink(record)
            except Exception as exc:  # noqa: BLE001 — memory must never break the loop
                logger.debug("[action] memory sink fault: {}", exc)
        logger.debug(
            "[action] {} {} — {} ({})",
            status.value.upper(), objective.capability, decision.value, reason,
        )
        return record

    # ── Introspection ─────────────────────────────────────────────────────

    def get_status(self) -> dict:
        try:
            with self._lock:
                recent = [r.to_dict() for r in list(self._audit)[-25:]]
                counts = dict(self._counts)
                pending = len(self._pending)
            return {
                "enabled": self._policy.enabled,
                "auto_max_risk": self._policy.auto_max_risk.value,
                "adapter": getattr(self._adapter, "name", type(self._adapter).__name__),
                "status_counts": counts,
                "pending_approval": pending,
                "recent": recent,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[action] get_status ignored a fault: {}", exc)
            return {"enabled": False, "recent": []}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


__all__ = [
    "RiskTier",
    "Decision",
    "ActionStatus",
    "ActionObjective",
    "ActionResult",
    "ActionRecord",
    "GovernancePolicy",
    "ActionOrchestrator",
    "MemorySink",
    "NOOP_CAPABILITY",
    "tier_from",
]
