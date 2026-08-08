"""APEX TRADER — Execution Plane (Phases 4–6).

Position management decoupled from broker execution.
Workers evaluate positions and emit Intents; the Intent Aggregator
(Phase 5) resolves conflicts; the Action Executor (Phase 6) serializes
broker calls through a risk gate.
"""

from execution.intent_aggregator import (
    AggregatorConfig,
    IntentAggregator,
    should_skip_sl_update,
)
from execution.intents import Intent, IntentType
from execution.management_state import ManagementState, ManagementStateStore
from execution.position_snapshot import PositionSnapshot
from execution.position_worker import PositionWorker

__all__ = [
    "AggregatorConfig",
    "Intent",
    "IntentAggregator",
    "IntentType",
    "ManagementState",
    "ManagementStateStore",
    "PositionSnapshot",
    "PositionWorker",
    "should_skip_sl_update",
]
