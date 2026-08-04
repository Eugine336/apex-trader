"""APEX TRADER — Autonomous Action Layer (Composio).

The governed gateway through which the AI Cognitive Brain's decisions become
real actions in the external world. The Brain reasons and authors objectives;
this layer only governs, executes, verifies and remembers — it never reasons.

Public surface:

* :class:`action.orchestrator.ActionOrchestrator` — the single action gateway.
* :class:`action.orchestrator.ActionObjective` — a Brain-authored objective.
* :class:`action.orchestrator.GovernancePolicy` — hierarchical-autonomy policy.
* :func:`action.composio.build_adapter` — Mock or Composio REST adapter.
"""

from action.composio import ComposioAdapter, MockActionAdapter, build_adapter
from action.orchestrator import (
    NOOP_CAPABILITY,
    ActionObjective,
    ActionOrchestrator,
    ActionRecord,
    ActionResult,
    ActionStatus,
    Decision,
    GovernancePolicy,
    RiskTier,
    tier_from,
)

__all__ = [
    "ActionOrchestrator",
    "ActionObjective",
    "ActionResult",
    "ActionRecord",
    "ActionStatus",
    "Decision",
    "GovernancePolicy",
    "RiskTier",
    "tier_from",
    "NOOP_CAPABILITY",
    "MockActionAdapter",
    "ComposioAdapter",
    "build_adapter",
]
