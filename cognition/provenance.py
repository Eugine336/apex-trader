"""Canonical provenance helpers shared across cognition, execution and learning.

This module derives provenance only from the single identity source. It does not
open persistence stores, inspect runtime credentials, or infer event relationships.
Callers supply the concrete IDs available at each stage of the lifecycle.
"""

from __future__ import annotations

from typing import Any

from cognition.identity import IDENTITY_VERSION


def current_provenance(
    *,
    event_id: Any = "",
    decision_id: Any = "",
    opportunity_id: Any = "",
    campaign_id: Any = "",
    parent_event_id: Any = "",
) -> dict[str, str]:
    """Return the canonical version and explicit correlation identifiers.

    Empty identifiers stay empty rather than being fabricated. A missing link is
    therefore observable and can be repaired at the event producer, not hidden
    behind a synthetic correlation value.
    """
    return {
        "identity_version": IDENTITY_VERSION,
        "event_id": str(event_id or ""),
        "decision_id": str(decision_id or ""),
        "opportunity_id": str(opportunity_id or ""),
        "campaign_id": str(campaign_id or ""),
        "parent_event_id": str(parent_event_id or ""),
    }


def attach_provenance(
    record: dict[str, Any],
    *,
    event_id: Any = "",
    decision_id: Any = "",
    opportunity_id: Any = "",
    campaign_id: Any = "",
    parent_event_id: Any = "",
) -> dict[str, Any]:
    """Return a copy of a record with canonical provenance metadata attached.

    Explicit IDs already present on the record win over omitted call arguments;
    the identity version is always set to the canonical current version for new
    writes. This function never mutates the caller's dictionary.
    """
    result = dict(record)
    ids = current_provenance(
        event_id=event_id or result.get("event_id"),
        decision_id=decision_id or result.get("decision_id"),
        opportunity_id=opportunity_id or result.get("opportunity_id"),
        campaign_id=campaign_id or result.get("campaign_id"),
        parent_event_id=parent_event_id or result.get("parent_event_id"),
    )
    result.update(ids)
    return result


__all__ = ["current_provenance", "attach_provenance"]
