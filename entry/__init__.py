"""APEX TRADER — Entry Plane (Phase 7).

Legacy-retirement Step 2 (Part XXV, docs/LEGACY_RETIREMENT_PLAN.md): this
package no longer eagerly imports the legacy entry-DECISION modules
(orchestrator, gate, order staging, tick/flip detectors). Only the shared,
non-directional data models are re-exported here, so live evidence code that
needs a type (``EntryConfig`` / ``EntryZone``) does not transitively load the
decision pipeline. Import a decision module explicitly from its submodule, e.g.
``from entry.entry_orchestrator import EntryOrchestrator`` — those modules are
scheduled for deletion as the retirement track proceeds.
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
