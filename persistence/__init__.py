"""
APEX TRADER — Persistence Package
Crash-proof state storage. Every position survives a restart.
Every event is auditable.
"""

from persistence.event_store import EventStore, get_event_store, shutdown_event_store, new_cycle_id, new_setup_id
from persistence.position_store import PositionStore
from persistence.domain_events import (
    DECISION_REJECT, ORDER_SENT, ORDER_FILLED, TRADE_OPEN,
    SHADOW_CONTRACT_CREATED, SHADOW_RESOLVED,
)
from persistence.shadow_store import ShadowStore, ShadowContract, ShadowResolution, new_contract_id

__all__ = [
    "PositionStore",
    "EventStore",
    "get_event_store",
    "shutdown_event_store",
    "new_cycle_id",
    "new_setup_id",
    "DECISION_REJECT",
    "ORDER_SENT",
    "ORDER_FILLED",
    "TRADE_OPEN",
    "SHADOW_CONTRACT_CREATED",
    "SHADOW_RESOLVED",
    "ShadowStore",
    "ShadowContract",
    "ShadowResolution",
    "new_contract_id",
]
