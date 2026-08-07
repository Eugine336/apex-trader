"""APEX TRADER — Governance Division data models.

Small, JSON-friendly carriers used by :class:`governance.division.GovernanceDivision`:

* :class:`PromotionStage` — the unified module-authority lifecycle every module
  (real or virtual) must climb before it can influence capital at full weight:
  ``SHADOW → VALIDATION → LIMITED → FULL``.  No stage is skipped and no module
  reaches ``FULL`` without Governance sign-off at each step.
* :class:`AuthorizationRecord` — one audited Governance decision on a Learning
  recommendation (the ⑦→⑧ boundary).
* :class:`PromotionRecord` — one audited lifecycle transition decision.
* :class:`ToxicPairRecord` — a toxic module-pair finding Governance has received
  from the InteractionAnalyzer and now tracks.

These are deliberately explicit inputs/outputs (rather than reaching into global
state) so the Governance Division stays pure and unit-testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class PromotionStage(str, Enum):
    """Unified module-authority lifecycle (str-valued for trivial JSON / SQL).

    A module climbs the ladder one rung at a time; each promotion is an explicit
    Governance authorisation.  ``weight_factor`` is the fraction of a module's
    full calibrated weight it is allowed to exert while in that stage — so a
    module cannot influence a live decision until it reaches at least
    ``LIMITED``, and never at full strength until ``FULL``.
    """

    SHADOW = "SHADOW"          # runs + graded, ZERO live weight
    VALIDATION = "VALIDATION"  # compared against established modules, ZERO live weight
    LIMITED = "LIMITED"        # participates at reduced weight
    FULL = "FULL"              # full authority

    @property
    def order(self) -> int:
        return _STAGE_ORDER[self]

    @property
    def weight_factor(self) -> float:
        """Fraction of full weight permitted in this stage."""
        return _STAGE_WEIGHT[self]

    @property
    def influences_live(self) -> bool:
        """True when a module in this stage may move a live decision."""
        return self in (PromotionStage.LIMITED, PromotionStage.FULL)

    def next_stage(self) -> Optional["PromotionStage"]:
        """The stage immediately above this one (None at the top)."""
        return _STAGE_NEXT.get(self)

    @staticmethod
    def coerce(value: Any) -> "PromotionStage":
        if isinstance(value, PromotionStage):
            return value
        try:
            return PromotionStage(str(value).upper())
        except (ValueError, AttributeError):
            return PromotionStage.SHADOW


_STAGE_ORDER: Dict[PromotionStage, int] = {
    PromotionStage.SHADOW: 0,
    PromotionStage.VALIDATION: 1,
    PromotionStage.LIMITED: 2,
    PromotionStage.FULL: 3,
}
_STAGE_WEIGHT: Dict[PromotionStage, float] = {
    PromotionStage.SHADOW: 0.0,
    PromotionStage.VALIDATION: 0.0,
    PromotionStage.LIMITED: 0.5,
    PromotionStage.FULL: 1.0,
}
_STAGE_NEXT: Dict[PromotionStage, PromotionStage] = {
    PromotionStage.SHADOW: PromotionStage.VALIDATION,
    PromotionStage.VALIDATION: PromotionStage.LIMITED,
    PromotionStage.LIMITED: PromotionStage.FULL,
}


@dataclass
class AuthorizationRecord:
    """One audited Governance decision on a Learning recommendation."""

    source: str
    recommendation_type: str
    verdict: str                       # GovernanceVerdict value
    reason: str = ""
    confidence: float = 0.0
    payload: Dict[str, Any] = field(default_factory=dict)
    decided_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "recommendation_type": self.recommendation_type,
            "verdict": self.verdict,
            "reason": self.reason,
            "confidence": round(float(self.confidence), 4),
            "payload": dict(self.payload),
            "decided_at": self.decided_at,
        }


@dataclass
class PromotionRecord:
    """One audited module-lifecycle transition decision."""

    module: str
    from_stage: str
    to_stage: str
    verdict: str                       # GovernanceVerdict value
    reason: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    decided_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "verdict": self.verdict,
            "reason": self.reason,
            "metrics": dict(self.metrics),
            "decided_at": self.decided_at,
        }


@dataclass
class ToxicPairRecord:
    """A toxic module-pair finding Governance tracks (from InteractionAnalyzer)."""

    module_a: str
    module_b: str
    interaction_effect: float = 0.0
    occurrences: int = 1
    last_seen: float = field(default_factory=time.time)
    enforced: bool = False             # whether a containment was issued
    enforced_module: str = ""          # which module (if any) was contained

    def key(self) -> tuple[str, str]:
        return _pair_key(self.module_a, self.module_b)

    def to_dict(self) -> dict:
        return {
            "module_a": self.module_a,
            "module_b": self.module_b,
            "interaction_effect": round(float(self.interaction_effect), 4),
            "occurrences": int(self.occurrences),
            "last_seen": self.last_seen,
            "enforced": bool(self.enforced),
            "enforced_module": self.enforced_module,
        }


def _pair_key(module_a: str, module_b: str) -> tuple[str, str]:
    """Order-free key for a module pair so (A,B) and (B,A) collapse to one."""
    a, b = str(module_a or ""), str(module_b or "")
    return (a, b) if a <= b else (b, a)


__all__ = [
    "PromotionStage",
    "AuthorizationRecord",
    "PromotionRecord",
    "ToxicPairRecord",
]
