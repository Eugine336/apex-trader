"""APEX TRADER — Entry Plane (Phase 7).

Legacy-retirement (Part XXV, docs/LEGACY_RETIREMENT_PLAN.md): this package no
longer eagerly imports the legacy entry-DECISION modules (orchestrator, gate,
order staging, tick/flip detectors). Only the shared, non-directional data
models are re-exported here, so live evidence code that needs a type
(``EntryConfig`` / ``EntryZone``) does not transitively load any decision
pipeline. The legacy zone-touch entry-decision modules have been retired; import
any remaining shared model explicitly from its submodule (e.g. ``entry.models``).
"""

from entry.models import (
    EntryConfig,
    EntryZone,
    GateResult,
    PendingEntry,
)

__all__ = [
    "EntryConfig",
    "EntryZone",
    "GateResult",
    "PendingEntry",
]
