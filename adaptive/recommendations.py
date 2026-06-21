"""
APEX TRADER — Learning Recommendation Contract & Gateway (Department ⑦ → ⑧)

The Learning Division *measures outcomes and recommends* changes; it must never
mutate live behaviour directly.  Department ⑧ (Governance) is the only layer
allowed to authorise a recommendation before it changes anything.

This module defines that boundary:

* :class:`LearningRecommendation` — the uniform, audit-friendly object every
  learner emits instead of reaching into the live path.
* :class:`RecommendationGateway` — the single chokepoint a recommendation passes
  through.  Governance (Phase 7) installs an authoriser and the gateway now
  requires it by default.  Until an authoriser is injected the gateway
  **auto-approves** every recommendation, so wiring a learner through it stays a
  pure contract change with **zero behaviour change**; once Governance injects
  its (permissive-but-bounded) authoriser the system still behaves identically —
  it only gains an explicit authorisation gate that rejects pathological values.

Design properties:

* **Behaviour-neutral by default.**  No authoriser wired (or
  ``governance_required=False``) ⇒ every ``submit`` returns ``APPROVED`` ⇒
  identical behaviour to applying the mutation directly.
* **Fail-open is never silent.**  If an injected authoriser raises, the gateway
  records the fault and falls back to the configured default decision (approve
  while governance is not yet required, reject once it is) — and logs it.
* **Audit trail.**  Every decision is appended to a bounded ring buffer for the
  dashboard / Governance to inspect (``recent`` / ``stats``).
* **Thread-safe.**  A single lock guards the ledger; ``submit`` is cheap.

Leaf module — standard library + loguru only.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

from loguru import logger


# ── Recommendation taxonomy ──────────────────────────────────────────────


class RecommendationType:
    """The kinds of change a learner may recommend.

    String constants (not an enum) so payloads stay JSON-serialisable and new
    types can be added without a migration.
    """

    WEIGHT_UPDATE = "WEIGHT_UPDATE"          # VoteCalibrator: module weight multipliers
    AVOID_PATTERN = "AVOID_PATTERN"          # Optimizer: block a losing pair/regime/session pattern
    SIZE_ADJUST = "SIZE_ADJUST"              # Optimizer / CapitalAllocator: position-size multiplier
    PROFILE_CHANGE = "PROFILE_CHANGE"        # ExecutionProfiles: SL/TP execution profile
    MODULE_SUPPRESS = "MODULE_SUPPRESS"      # Governor: shadow / disable a module
    TOXIC_PAIR_BLOCK = "TOXIC_PAIR_BLOCK"    # InteractionAnalyzer: toxic module-pair finding
    PARAM_PROMOTE = "PARAM_PROMOTE"          # ParameterEvolver: promote a shadow-validated threshold

    ALL = (
        WEIGHT_UPDATE,
        AVOID_PATTERN,
        SIZE_ADJUST,
        PROFILE_CHANGE,
        MODULE_SUPPRESS,
        TOXIC_PAIR_BLOCK,
        PARAM_PROMOTE,
    )


class RecommendationStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class LearningRecommendation:
    """A single recommendation emitted by a Learning component.

    The learner computes the change exactly as before, but instead of applying
    it, it wraps it here and submits it to the :class:`RecommendationGateway`.
    """

    source: str                                  # which learner produced this
    recommendation_type: str                     # one of RecommendationType.*
    payload: Dict[str, Any] = field(default_factory=dict)   # the actual change data
    confidence: float = 0.0                      # learner's confidence in it
    evidence: Dict[str, Any] = field(default_factory=dict)  # data supporting it
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "recommendation_type": self.recommendation_type,
            "payload": dict(self.payload),
            "confidence": float(self.confidence),
            "evidence": dict(self.evidence),
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class RecommendationDecision:
    """The gateway's verdict on one recommendation, retained for audit."""

    recommendation: LearningRecommendation
    status: str = RecommendationStatus.PENDING
    reason: str = ""
    decided_at: float = field(default_factory=time.time)

    @property
    def approved(self) -> bool:
        return self.status == RecommendationStatus.APPROVED

    def to_dict(self) -> dict:
        return {
            "recommendation": self.recommendation.to_dict(),
            "status": self.status,
            "reason": self.reason,
            "decided_at": self.decided_at,
        }


# Authoriser signature: given a recommendation, return (approved, reason).
Authorizer = Callable[[LearningRecommendation], "bool | tuple[bool, str]"]


class RecommendationGateway:
    """The single authorisation boundary between Learning (⑦) and the live path.

    Learners ``submit`` recommendations; consumers apply a change only when the
    returned decision is :attr:`RecommendationDecision.approved`.

    Until Governance installs an authoriser (Phase 7) and flips
    ``governance_required`` on, every submission is auto-approved — so routing a
    learner through the gateway is behaviour-neutral.
    """

    def __init__(
        self,
        *,
        governance_required: bool = True,
        authorizer: Optional[Authorizer] = None,
        history_limit: int = 500,
    ) -> None:
        self._governance_required = bool(governance_required)
        self._authorizer = authorizer
        self._history: Deque[RecommendationDecision] = deque(
            maxlen=max(1, int(history_limit))
        )
        self._counts: Dict[str, int] = {
            RecommendationStatus.APPROVED: 0,
            RecommendationStatus.REJECTED: 0,
        }
        self._lock = threading.Lock()

    # ── Configuration (Phase 7 wires these) ──────────────────────────────

    @property
    def governance_required(self) -> bool:
        return self._governance_required

    def set_governance_required(self, required: bool) -> None:
        """Phase 7: switch on real authorisation. When ``True`` and an
        authoriser is wired, recommendations must be explicitly approved."""
        self._governance_required = bool(required)

    def set_authorizer(self, authorizer: Optional[Authorizer]) -> None:
        """Inject (or clear) the Governance authoriser."""
        self._authorizer = authorizer

    @property
    def has_authorizer(self) -> bool:
        return self._authorizer is not None

    # ── Hot path ──────────────────────────────────────────────────────────

    def submit(self, recommendation: LearningRecommendation) -> RecommendationDecision:
        """Submit a recommendation and get back an authorisation decision.

        Auto-approves while governance is not required (or no authoriser is
        wired). Once Governance is active, delegates to the authoriser; any
        authoriser fault falls back to the safe default (approve until
        governance is required, reject after) and is logged — never silent.
        """
        approved, reason = self._authorize(recommendation)
        status = (
            RecommendationStatus.APPROVED if approved
            else RecommendationStatus.REJECTED
        )
        decision = RecommendationDecision(
            recommendation=recommendation, status=status, reason=reason,
        )
        with self._lock:
            self._history.append(decision)
            self._counts[status] = self._counts.get(status, 0) + 1
        return decision

    def is_approved(self, recommendation: LearningRecommendation) -> bool:
        """Convenience: submit and return only the approve/reject boolean."""
        return self.submit(recommendation).approved

    def _authorize(self, rec: LearningRecommendation) -> tuple[bool, str]:
        # No authoriser, or governance not yet required → auto-approve.
        if self._authorizer is None or not self._governance_required:
            return True, "auto-approved (governance not required)"
        try:
            result = self._authorizer(rec)
        except Exception as exc:  # noqa: BLE001 — a faulty authoriser must not crash Learning
            logger.warning(
                "[recommendations] authoriser raised on {} from {}: {} — "
                "falling back to reject (governance required)",
                rec.recommendation_type, rec.source, exc,
            )
            return False, f"authoriser error: {exc}"
        if isinstance(result, tuple):
            ok = bool(result[0])
            why = str(result[1]) if len(result) > 1 else ""
            return ok, why
        return bool(result), ""

    # ── Audit surface ─────────────────────────────────────────────────────

    def recent(self, limit: Optional[int] = None) -> List[RecommendationDecision]:
        """Most-recent decisions (newest last), optionally capped to ``limit``."""
        with self._lock:
            items = list(self._history)
        if limit is not None and limit >= 0:
            return items[-limit:]
        return items

    def stats(self) -> dict:
        """Approve/reject counts + a by-type breakdown for the dashboard."""
        with self._lock:
            by_type: Dict[str, Dict[str, int]] = {}
            for d in self._history:
                t = d.recommendation.recommendation_type
                bucket = by_type.setdefault(
                    t, {RecommendationStatus.APPROVED: 0, RecommendationStatus.REJECTED: 0}
                )
                bucket[d.status] = bucket.get(d.status, 0) + 1
            return {
                "governance_required": self._governance_required,
                "has_authorizer": self._authorizer is not None,
                "approved": self._counts.get(RecommendationStatus.APPROVED, 0),
                "rejected": self._counts.get(RecommendationStatus.REJECTED, 0),
                "history_size": len(self._history),
                "by_type": by_type,
            }


__all__ = [
    "RecommendationType",
    "RecommendationStatus",
    "LearningRecommendation",
    "RecommendationDecision",
    "RecommendationGateway",
]
