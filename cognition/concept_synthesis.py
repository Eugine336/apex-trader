"""Contextual synthesis of full concept readings for the native Brain.

Readings are observations, not votes. This layer groups overlapping readings
into fewer market hypotheses while retaining every source, geometry candidate,
requirement and opposing interpretation. Shared OHLC provenance is not counted
as independent confluence and never boosts confidence additively.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"
_DIRECTIONS = {LONG, SHORT, FLAT}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def synthesize_concept_readings(readings: Iterable[dict]) -> list[dict]:
    """Select verified applicable readings and group them by shared context.

    Group identity is timeframe + source completed bar + direction + activation
    state. Opposing active groups on the same timeframe/bar remain separate and
    are explicitly cross-referenced. Confidence is the strongest member's
    uncalibrated strength, reduced by opposing active strength; it is not summed.
    """
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for reading in readings or ():
        if not isinstance(reading, dict) or reading.get("applicable") is not True:
            continue
        if reading.get("source_timestamp_verified") is not True:
            continue
        if reading.get("data_basis") != "completed_ohlc" or reading.get("provisional") is not False:
            continue
        epoch = _finite(reading.get("source_bar_epoch"))
        if epoch is None or epoch <= 0:
            continue
        concept = str(reading.get("concept", "") or "").strip()
        timeframe = str(reading.get("timeframe", "") or "").strip().upper()
        evidence_id = str(reading.get("evidence_id", "") or "").strip()
        if not concept or not timeframe or not evidence_id:
            continue
        for implication in reading.get("implications", []) or []:
            if not isinstance(implication, dict):
                continue
            direction = str(implication.get("direction", "") or "").strip().upper()
            if direction not in _DIRECTIONS:
                continue
            strength = _finite(implication.get("strength"))
            # Missing strength is epistemically unknown, not a measured zero.
            # Keep it absent through synthesis; the Brain may then assign a
            # zero decision score without rewriting the source reading.
            activated = implication.get("activated") is True
            buckets[(timeframe, epoch, direction, activated)].append({
                "concept": concept,
                "timeframe": timeframe,
                "source_bar_epoch": epoch,
                "evidence_id": evidence_id,
                "direction": direction,
                "activated": activated,
                "strength": strength,
                "statement": str(implication.get("statement", "") or "").strip(),
                "invalidation_reason": str(implication.get("invalidation_reason", "") or "").strip(),
                "invalidation_price": _finite(implication.get("invalidation_price")),
                "objective_price": _finite(implication.get("objective_price")),
                "requires": [str(x) for x in implication.get("requires", []) or [] if str(x).strip()],
                "conflicts": [str(x) for x in implication.get("conflicts_with", []) or [] if str(x).strip()],
                "independence_group": str(reading.get("independence_group", "") or "").strip(),
            })

    groups: list[dict] = []
    for (timeframe, epoch, direction, activated), members in sorted(
        buckets.items(), key=lambda item: (item[0][0], item[0][1], item[0][2], item[0][3])
    ):
        concepts = sorted({member["concept"] for member in members})
        evidence_ids = list(dict.fromkeys(member["evidence_id"] for member in members))
        # A concept count is not an independence count. Multiple readers over
        # the same completed OHLC stream are correlated interpretations, not
        # separate confirmations. Unknown provenance is never counted as an
        # independent source.
        independence_groups = sorted({
            member["independence_group"] for member in members
            if member["independence_group"]
        })
        known_strengths = [member["strength"] for member in members if member["strength"] is not None]
        strongest = max(known_strengths) if known_strengths else None
        geometry = []
        for member in members:
            if member["invalidation_price"] is None and member["objective_price"] is None:
                continue
            geometry.append({
                "concept": member["concept"],
                "invalidation_price": member["invalidation_price"],
                "objective_price": member["objective_price"],
                "invalidation_reason": member["invalidation_reason"],
            })
        requirements = list(dict.fromkeys(
            item for member in members for item in member["requires"]
        ))
        conflicts = list(dict.fromkeys(
            item for member in members for item in member["conflicts"]
        ))
        groups.append({
            "timeframe": timeframe,
            "source_bar_epoch": epoch,
            "direction": direction,
            "activated": activated,
            "status": "active" if activated else "watch",
            "concepts": concepts,
            "evidence_ids": evidence_ids,
            "strength": strongest,
            "statements": [member["statement"] for member in members if member["statement"]],
            "geometry_candidates": geometry,
            "geometry_divergent": (
                len({item["invalidation_price"] for item in geometry if item["invalidation_price"] is not None}) > 1
                or len({item["objective_price"] for item in geometry if item["objective_price"] is not None}) > 1
            ),
            "requirements": requirements,
            "conflicts": conflicts,
            "independence_groups": independence_groups,
            "independence_status": (
                "multiple_independent_sources" if len(independence_groups) > 1
                else "single_source" if len(independence_groups) == 1
                else "provenance_unknown"
            ),
            "member_count": len(members),
            "concept_count": len(concepts),
            # Backward-compatible field: confluence now means distinct,
            # explicitly identified independence groups, never concept count.
            "confluence_count": len(independence_groups),
        })

    for group in groups:
        if group["direction"] not in {LONG, SHORT} or not group["activated"]:
            group["opposing_concepts"] = []
            group["opposing_evidence_ids"] = []
            group["opposing_strength"] = 0.0
            group["confidence"] = group["strength"]
            continue
        opposite = SHORT if group["direction"] == LONG else LONG
        opposing = [
            candidate for candidate in groups
            if candidate["timeframe"] == group["timeframe"]
            and candidate["source_bar_epoch"] == group["source_bar_epoch"]
            and candidate["direction"] == opposite
            and candidate["activated"]
        ]
        opposing_strengths = [item["strength"] for item in opposing if item["strength"] is not None]
        opposing_strength_unknown = any(item["strength"] is None for item in opposing)
        if not opposing:
            opposing_strength = 0.0
        elif opposing_strength_unknown or not opposing_strengths:
            # An activated opposing reading with unknown strength is not zero
            # opposition. Preserve uncertainty instead of manufacturing confidence.
            opposing_strength = None
        else:
            opposing_strength = max(opposing_strengths)
        group["opposing_strength_unknown"] = opposing_strength_unknown
        group["opposing_concepts"] = sorted({
            concept for item in opposing for concept in item["concepts"]
        })
        group["opposing_evidence_ids"] = list(dict.fromkeys(
            evidence_id for item in opposing for evidence_id in item["evidence_ids"]
        ))
        group["opposing_strength"] = opposing_strength
        # The same candle stream feeds these readers. Confluence is descriptive,
        # not independent statistical confirmation. Opposing activated readings
        # reduce the strongest member's strength; no additive boost is allowed.
        group["confidence"] = (
            group["strength"] * (1.0 - 0.5 * opposing_strength)
            if group["strength"] is not None and opposing_strength is not None
            else None
        )

    return groups
