"""APEX TRADER — Action Planner (Constitution Part IX, Article 11).

The Action Planner sits between the AI Cognitive Brain and Composio. The Brain
authors *objectives* in a purely semantic vocabulary (Article 2 — it never names
a provider or an API); the planner:

    interpret objective → select capability → choose provider → construct plan →
    submit to Governance → execute through Composio → observe → return to Memory

It holds **no market intelligence** and originates **no** objectives of its own
(Article 6): it only transforms an already-authored :class:`ObjectiveRequest`
into a concrete :class:`~action.orchestrator.ActionObjective` whose ``capability``
is the exact Composio action for the chosen provider, then hands it to the
:class:`~action.orchestrator.ActionOrchestrator` (which applies deterministic
governance, executes, verifies and records). Fail-safe throughout; default-off.

The provider the planner chose is recorded in the objective's human-readable
``objective`` text and in ``params['_provider']`` / ``params['_capability']`` so
the audit trail shows *which* provider realised the objective — the Brain never
had to know (Article 11).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from action.capabilities import Capability, CapabilityRegistry, default_registry
from action.orchestrator import ActionObjective, ActionRecord

logger = logging.getLogger("apex.action.planner")


@dataclass
class ObjectiveRequest:
    """A Brain-authored objective in semantic terms (no provider, no API).

    ``intent`` names a registered capability (e.g. ``operator.notify``);
    ``params`` are the capability inputs; ``preferred_provider`` is an optional
    hint the planner honours only when that provider is a valid, available
    candidate.
    """

    intent: str
    objective: str = ""
    params: dict = field(default_factory=dict)
    confidence: float = 0.5
    priority: int = 3
    source: str = "ai_brain"
    evidence_ref: str = ""
    preferred_provider: str = ""

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "objective": self.objective,
            "params": dict(self.params),
            "confidence": round(float(self.confidence), 4),
            "priority": int(self.priority),
            "source": self.source,
            "evidence_ref": self.evidence_ref,
            "preferred_provider": self.preferred_provider,
        }


class ActionPlanner:
    """Transforms semantic objectives into governed, provider-bound actions.

    Default-OFF: when ``enabled`` is False, :meth:`submit` is an inert no-op that
    returns ``None`` (no objective ever reaches the orchestrator). The planner
    never reasons about markets and never invents objectives.
    """

    def __init__(
        self,
        orchestrator: Any,
        registry: Optional[CapabilityRegistry] = None,
        *,
        enabled: bool = False,
        available_providers: Optional[list] = None,
        provider_preferences: Optional[dict] = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._registry = registry or default_registry()
        self.enabled = bool(enabled)
        # None ⇒ availability unconstrained (any candidate may be chosen).
        self._available = (
            {str(p).strip().lower() for p in available_providers if str(p).strip()}
            if available_providers else None
        )
        self._preferences = {str(k): str(v) for k, v in (provider_preferences or {}).items()}
        self._lock = threading.Lock()
        self._planned = 0
        self._rejected = 0
        self._submitted = 0

    def _reject(self, reason: str, request: Any) -> None:
        with self._lock:
            self._rejected += 1
        logger.debug("[planner] rejected %s: %s",
                     getattr(request, "intent", "?"), reason)

    def build_objective(self, request: Any) -> Optional[ActionObjective]:
        """Resolve capability + provider and construct the ActionObjective.

        Returns ``None`` (and records a rejection) for an unknown capability,
        missing required params, or when no provider is available. Never raises.
        """
        try:
            intent = str(getattr(request, "intent", "") or "")
            cap: Optional[Capability] = self._registry.get(intent)
            if cap is None:
                self._reject("unknown capability", request)
                return None
            params = dict(getattr(request, "params", {}) or {})
            missing = cap.missing_params(params)
            if missing:
                self._reject(f"missing params {missing}", request)
                return None
            preferred = (
                str(getattr(request, "preferred_provider", "") or "")
                or self._preferences.get(cap.name, "")
            )
            binding = self._registry.resolve_provider(
                cap, preferred=preferred, available=self._available,
            )
            if binding is None:
                self._reject("no available provider", request)
                return None
            # Record provenance so the audit shows which provider realised it —
            # the Brain never had to know (Article 11). Kept under underscore keys
            # so they are clearly planner metadata, not capability inputs.
            audit_params = dict(params)
            audit_params["_capability"] = cap.name
            audit_params["_provider"] = binding.provider
            objective_text = str(getattr(request, "objective", "") or cap.description)
            with self._lock:
                self._planned += 1
            return ActionObjective(
                capability=binding.action,              # the exact Composio action
                objective=f"[{cap.name}→{binding.provider}] {objective_text}",
                params=audit_params,
                expected_outcome=cap.description,
                confidence=float(getattr(request, "confidence", 0.5) or 0.0),
                priority=int(getattr(request, "priority", 3) or 3),
                reversible=cap.reversible,
                risk_tier=cap.risk_tier,
                required_permissions=list(cap.required_permissions),
                source=str(getattr(request, "source", "") or "ai_brain"),
                evidence_ref=str(getattr(request, "evidence_ref", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 — planning must never raise
            logger.debug("[planner] build_objective fault: %s", exc)
            self._reject(f"planner fault: {exc}", request)
            return None

    def submit(self, request: Any) -> Optional[ActionRecord]:
        """Plan an objective and submit it to the orchestrator. Fail-safe.

        Inert (returns ``None``) when the planner is disabled or the orchestrator
        is unwired, or when planning rejects the request.
        """
        if not self.enabled or self._orchestrator is None:
            return None
        try:
            objective = self.build_objective(request)
            if objective is None:
                return None
            record = self._orchestrator.submit(objective)
            with self._lock:
                self._submitted += 1
            return record
        except Exception as exc:  # noqa: BLE001 — the planner must never break a cycle
            logger.debug("[planner] submit fault: %s", exc)
            return None

    def get_status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "capabilities": self._registry.names(),
                "available_providers": (sorted(self._available)
                                        if self._available is not None else "unconstrained"),
                "planned": self._planned,
                "rejected": self._rejected,
                "submitted": self._submitted,
            }


__all__ = ["ObjectiveRequest", "ActionPlanner"]
