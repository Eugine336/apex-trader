"""APEX TRADER — Execution Plane (Phase 4 + 6).

Position management decoupled from broker execution.
Workers evaluate positions and emit Intents; the Action Executor
(Phase 6) serializes broker calls through a risk gate.

Phase 6 components (ActionExecutor, RiskGate) import from
``execution.action_executor`` and ``execution.risk_gate`` directly
to avoid pulling in heavy platform dependencies at package level.
"""APEX TRADER — Execution Plane (Phases 4–5).

Position management decoupled from broker execution.
Workers evaluate positions and emit Intents; the Intent Aggregator
(Phase 5) resolves conflicts; the Action Executor (Phase 6) serializes
broker calls.
"""

from execution.intent_aggregator import (
    AggregatorConfig,
    IntentAggregator,
    should_skip_sl_update,
)
from execution.intents import Intent, IntentType
from execution.position_snapshot import PositionSnapshot
from execution.position_worker import PositionWorker

__all__ = [
    "AggregatorConfig",
    "Intent",
    "IntentAggregator",
    "IntentType",
    "PositionSnapshot",
    "PositionWorker",
    "should_skip_sl_update",
]
