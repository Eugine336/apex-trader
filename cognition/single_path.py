"""APEX TRADER — Single Reasoner path predicate (Constitution I.4 / III.2).

The constitutional endpoint is one decision path: the AI Cognitive Brain is the
sole authority that may open a trade. Legacy market-deciders (directional
consensus, zone/thesis entry triggers) are demoted to Evidence and must never
open a position on their own.

This module holds the tiny, pure predicate that the live entry funnel
(``event_driven_bootstrap._on_entry_decision``) uses to enforce that: when the
single path is active, any entry whose source is NOT the Brain is suppressed.
Keeping it here (pure stdlib, no imports) makes the rule a single source of
truth that is unit-testable offline, independent of the heavy runtime module.

The Brain's own originated entries carry ``source == "ai_brain"`` (set by the
origination sink) and flow on a separate plane (loop → origination sink →
aggregator → RiskGate → broker), so they are never suppressed here.
"""

from __future__ import annotations

# The provenance tag the Brain's origination sink stamps on its intents.
BRAIN_SOURCE = "ai_brain"


def legacy_entry_suppressed(source: object, single_path: bool) -> bool:
    """Return True when a legacy (non-Brain) entry must be refused.

    * ``single_path`` False  → never suppress (legacy path retained).
    * source is the Brain    → never suppress (the one sanctioned authority).
    * otherwise (single path on, legacy source) → suppress.

    Robust to ``None``/odd source values (treated as legacy). Never raises.
    """
    if not single_path:
        return False
    return str(source or "").strip().lower() != BRAIN_SOURCE
