"""Market-derived payoff geometry and activation evidence for opportunities.

This module contains no strategy preference or fixed reward multiple. It derives
entry/stop/target geometry from fresh completed-candle market-lens evidence and
requires an observable activation condition. Missing geometry or stale inputs
produce no qualification, allowing callers to fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import time
from typing import Any, Optional

from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION


def _finite_positive(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) and number > 0.0 else None


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


@dataclass(frozen=True)
class OpportunityGeometry:
    """Completed-bar market geometry for one directional opportunity."""

    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    risk_distance: float
    reward_distance: float
    reward_r: float
    activated: bool
    activation_reason: str
    invalidation_reason: str
    evidence_ids: tuple[str, ...]
    entry_timeframe: str = ""
    stop_timeframe: str = ""
    target_timeframe: str = ""
    identity_version: str = IDENTITY_VERSION
    identity: str = IDENTITY_STATEMENT
    symbol: str = ""
    evidence_timestamp_epoch: float = 0.0
    # Set when a full concept reading supplied the geometry: the concept, the
    # timeframe of its range, and the first milestones the concept expects before
    # the objective (management consumes them).
    concept: str = ""
    concept_timeframe: str = ""
    milestones: tuple[float, ...] = ()
    supporting_concepts: tuple[str, ...] = ()
    geometry_sources: tuple[str, ...] = ()
    independent_source_count: int = 0
    independence_groups: tuple[str, ...] = ()
    order_type: str = "MARKET"
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_version": self.identity_version,
            "identity": self.identity,
            "symbol": self.symbol,
            "evidence_timestamp_epoch": self.evidence_timestamp_epoch,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "risk_distance": self.risk_distance,
            "reward_distance": self.reward_distance,
            "reward_r": self.reward_r,
            "activated": self.activated,
            "activation_reason": self.activation_reason,
            "invalidation_reason": self.invalidation_reason,
            "evidence_ids": list(self.evidence_ids),
            "entry_timeframe": self.entry_timeframe,
            "stop_timeframe": self.stop_timeframe,
            "target_timeframe": self.target_timeframe,
            "concept": self.concept,
            "concept_timeframe": self.concept_timeframe,
            "milestones": list(self.milestones),
            "supporting_concepts": list(self.supporting_concepts),
            "geometry_sources": list(self.geometry_sources),
            "independent_source_count": self.independent_source_count,
            "independence_groups": list(self.independence_groups),
            "order_type": self.order_type,
            "entry_zone_low": self.entry_zone_low,
            "entry_zone_high": self.entry_zone_high,
        }


def _concept_reading_record(item: Any, measurements: dict, now: Optional[float]) -> Optional[dict]:
    """A fresh, completed-bar concept reading with a known source time, or None."""
    if (
        measurements.get("source_timestamp_known") is not True
        or measurements.get("data_basis") != "completed_ohlc"
        or measurements.get("provisional") is True
        or measurements.get("applicable") is not True
    ):
        return None
    source_time = _finite(measurements.get("source_bar_epoch"))
    decision_time = _finite(now) if now is not None else time.time()
    horizon = _finite(getattr(item, "relevance_horizon_seconds", None))
    if (
        source_time is None or decision_time is None or source_time > decision_time
        or horizon is None or horizon <= 0.0 or decision_time - source_time > horizon
    ):
        return None
    reference = _finite_positive(measurements.get("reference_close"))
    if reference is None:
        return None
    return {
        "item": item,
        "time": source_time,
        "reference": reference,
        "concept": str(measurements.get("concept", "") or ""),
        "timeframe": str(measurements.get("timeframe", "") or "").strip().upper(),
        "reference_timeframe": str(measurements.get("reference_timeframe", "") or "").strip().upper(),
        "independence_group": str(measurements.get("independence_group", "") or "").strip(),
        "implications": [i for i in (measurements.get("implications") or []) if isinstance(i, dict)],
    }


def _geometry_from_concept_readings(
    readings: list[dict], side: str, symbol: str,
) -> Optional[OpportunityGeometry]:
    """Synthesize compatible entry/stop/target levels across concept readings.

    Readings are grouped only when they share timeframe and completed source
    bar. Materially divergent candidates invalidate the group. Within a
    compatible group, the protective stop is the closest valid structural
    invalidation and the target is the nearest valid favorable draw. No reward
    multiple is invented.
    """
    candidates: dict[tuple[str, str, float], list[dict]] = {}
    for reading in readings:
        entry = reading["reference"]
        for implication in reading["implications"]:
            if str(implication.get("direction", "")).upper() != side:
                continue
            if implication.get("activated") is not True:
                continue
            stop = _finite_positive(implication.get("invalidation_price"))
            target = _finite_positive(implication.get("objective_price"))
            order_type = str(implication.get("order_type", "MARKET") or "MARKET").strip().upper()
            if order_type not in {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}:
                continue
            explicit_entry = _finite_positive(implication.get("entry_price"))
            zone_low = _finite_positive(implication.get("entry_zone_low"))
            zone_high = _finite_positive(implication.get("entry_zone_high"))
            if (zone_low is None) != (zone_high is None):
                continue
            if zone_low is not None and zone_low > zone_high:
                continue
            candidate_entry = explicit_entry if explicit_entry is not None else entry
            if order_type != "MARKET" and explicit_entry is None:
                if zone_low is None or not zone_low <= entry <= zone_high:
                    continue
            if zone_low is not None and not zone_low <= candidate_entry <= zone_high:
                continue
            if stop is None or target is None:
                continue
            valid = (stop < candidate_entry < target) if side == "LONG" else (target < candidate_entry < stop)
            if not valid:
                continue
            candidates.setdefault((
                reading["timeframe"], reading["reference_timeframe"], reading["time"],
            ), []).append({
                "reading": reading,
                "implication": implication,
                "entry": candidate_entry,
                "stop": stop,
                "target": target,
                "order_type": order_type,
                "entry_zone_low": zone_low,
                "entry_zone_high": zone_high,
                "strength": _finite(implication.get("strength")),
                "independence_group": reading.get("independence_group", ""),
            })

    best: Optional[dict] = None
    for (timeframe, reference_timeframe, source_time), members in candidates.items():
        entries = [member["entry"] for member in members]
        # Same source instant must agree on both entry and order semantics.
        if (
            any(value != entries[0] for value in entries[1:])
            or len({member["order_type"] for member in members}) != 1
            or len({(member["entry_zone_low"], member["entry_zone_high"]) for member in members}) != 1
        ):
            continue
        entry = entries[0]
        stops = [member["stop"] for member in members]
        targets = [member["target"] for member in members]

        # Select the closest valid invalidation and nearest real draw. Distinct
        # structural levels are not averaged or rejected by arbitrary distance
        # thresholds: each candidate is already validated on the correct side.
        stop = max(stops) if side == "LONG" else min(stops)
        target = min(targets) if side == "LONG" else max(targets)
        risk = entry - stop if side == "LONG" else stop - entry
        reward = target - entry if side == "LONG" else entry - target
        if not (isfinite(risk) and isfinite(reward) and risk > 0.0 and reward > 0.0):
            continue
        unique_members: dict[str, dict] = {}
        for member in members:
            reading = member["reading"]
            concept = str(reading.get("concept", "") or "")
            evidence_id = str(getattr(reading["item"], "evidence_id", "") or "")
            unique_members.setdefault(f"{concept}:{evidence_id}", member)
        selected = list(unique_members.values())
        concepts = tuple(sorted({
            str(member["reading"].get("concept", "") or "")
            for member in selected if member["reading"].get("concept")
        }))
        evidence_ids = tuple(dict.fromkeys(
            str(getattr(member["reading"]["item"], "evidence_id", "") or "")
            for member in selected
            if getattr(member["reading"]["item"], "evidence_id", "")
        ))
        strengths = [
            member["strength"] for member in selected
            if member["strength"] is not None
        ]
        strongest = max(strengths, default=-1.0)
        independence_groups = tuple(sorted({
            member["independence_group"] for member in selected
            if member.get("independence_group")
        }))
        rank = (
            len(independence_groups), len(concepts), strongest,
            source_time, timeframe, reference_timeframe,
        )
        milestones = tuple(sorted({
            float(value)
            for member in selected
            for value in (member["implication"].get("milestones") or [])
            if _finite_positive(value) is not None
            and ((entry < float(value) < target) if side == "LONG" else (target < float(value) < entry))
        }, reverse=side == "SHORT"))
        geometry_sources = tuple(
            f"{member['reading'].get('concept')}: entry={member['entry']:.10g}, "
            f"stop={member['stop']:.10g}, target={member['target']:.10g}"
            for member in selected
        )
        invalidations = tuple(dict.fromkeys(
            str(member["implication"].get("invalidation_reason", "") or "")
            for member in selected if member["implication"].get("invalidation_reason")
        ))
        activation_reasons = tuple(dict.fromkeys(
            str(member["implication"].get("activation_reason", "") or "")
            for member in selected if member["implication"].get("activation_reason")
        ))
        group = {
            "rank": rank, "entry": entry, "stop": stop, "target": target,
            "risk": risk, "reward": reward, "timeframe": timeframe, "reference_timeframe": reference_timeframe,
            "source_time": source_time, "concepts": concepts,
            "evidence_ids": evidence_ids, "milestones": milestones,
            "order_type": members[0]["order_type"],
            "entry_zone_low": members[0]["entry_zone_low"],
            "entry_zone_high": members[0]["entry_zone_high"],
            "geometry_sources": geometry_sources, "invalidations": invalidations,
            "activation_reasons": activation_reasons,
            "independence_groups": independence_groups,
        }
        if best is None or rank > best["rank"]:
            best = group

    if best is None:
        return None
    concepts = best["concepts"]
    return OpportunityGeometry(
        direction=side,
        entry_price=best["entry"],
        stop_price=best["stop"],
        target_price=best["target"],
        risk_distance=best["risk"],
        reward_distance=best["reward"],
        reward_r=best["reward"] / best["risk"],
        activated=True,
        activation_reason=(
            f"{len(concepts)} concept reading(s) from "
            f"{len(best['independence_groups'])} identified independent evidence source(s) "
            f"support the completed-bar setup: "
            + (", ".join(concepts) or "unnamed concept")
            + (("; " + "; ".join(best["activation_reasons"])) if best["activation_reasons"] else "")
        ),
        invalidation_reason=(
            "; ".join(best["invalidations"])
            or "The consolidated concept invalidation geometry is breached"
        ),
        evidence_ids=best["evidence_ids"],
        entry_timeframe=best["reference_timeframe"],
        stop_timeframe=best["timeframe"],
        target_timeframe=best["timeframe"],
        symbol=symbol,
        evidence_timestamp_epoch=best["source_time"],
        concept="+".join(concepts),
        concept_timeframe=best["timeframe"],
        milestones=best["milestones"],
        supporting_concepts=concepts,
        geometry_sources=best["geometry_sources"],
        independent_source_count=len(best["independence_groups"]),
        independence_groups=best["independence_groups"],
        order_type=best["order_type"],
        entry_zone_low=best["entry_zone_low"],
        entry_zone_high=best["entry_zone_high"],
    )


def derive_opportunity_geometry(
    market_state: Any,
    direction: str,
    *,
    now: Optional[float] = None,
) -> Optional[OpportunityGeometry]:
    """Derive geometry only from fresh, timestamp-confirmed market evidence.

    Entry is the latest confirmed structural close. The protective boundary is
    the closest observed structural level on the adverse side; the target is the
    nearest observed structural level in the favorable direction. No 1R/2R
    default or guessed ATR multiple is introduced.

    Activation requires a directional completed-bar breakout, a trend plus
    same-direction completed-bar momentum, a measured range rotation at an edge
    with a fresh response, or a counter-side sweep/reclaim.
    Unavailable, stale, conflicting, or incomplete inputs return None.
    """
    side = str(direction or "").strip().upper()
    if side not in {"LONG", "SHORT"} or market_state is None:
        return None

    try:
        fresh = list(market_state.fresh_evidence(now))
    except Exception:
        return None

    state_symbol = str(getattr(market_state, "symbol", "") or "").strip().upper()
    if not state_symbol:
        return None
    structures: list[dict[str, Any]] = []
    momenta: list[dict[str, Any]] = []
    liquidity: list[dict[str, Any]] = []
    concept_readings: list[dict[str, Any]] = []
    for item in fresh:
        evidence_symbol = str(getattr(item, "symbol", "") or "").strip().upper()
        # Geometry is instrument-specific. Missing symbol identity is not safe
        # to inherit from the container because mixed-source states can occur.
        if not evidence_symbol or evidence_symbol != state_symbol:
            continue
        measurements = getattr(item, "measurements", None)
        if not isinstance(measurements, dict):
            continue
        if measurements.get("source_kind") == "concept_reading":
            reading = _concept_reading_record(item, measurements, now)
            if reading is not None:
                concept_readings.append(reading)
            continue
        # Only completed market-lens facts with known source timestamps can
        # define price levels or satisfy activation.
        if (
            measurements.get("source_kind") != "market_lens"
            or measurements.get("source_timestamp_known") is not True
            or measurements.get("data_basis") != "completed_ohlc"
            or measurements.get("provisional") is True
            or measurements.get("completed") is False
        ):
            continue
        domain_value = getattr(getattr(item, "domain", None), "value", None)
        domain = str(domain_value or getattr(item, "domain", "") or "").lower()
        # Evidence arrival time is not the candle's observation time. Geometry
        # must use the source bar epoch and enforce its own age at the decision
        # cutoff, so late-arriving old bars cannot masquerade as current facts.
        source_time = _finite(measurements.get("source_bar_epoch"))
        decision_time = _finite(now) if now is not None else time.time()
        horizon = _finite(getattr(item, "relevance_horizon_seconds", None))
        if (
            source_time is None or decision_time is None
            or source_time > decision_time
            or horizon is None or horizon <= 0.0
            or decision_time - source_time > horizon
        ):
            continue
        if domain == "structure":
            price = _finite_positive(measurements.get("current_close"))
            high = _finite_positive(measurements.get("prior_range_high"))
            low = _finite_positive(measurements.get("prior_range_low"))
            if price is None or high is None or low is None or not high > low:
                continue
            structures.append({
                "item": item,
                "time": source_time,
                "price": price,
                "high": high,
                "low": low,
                "range_position": _finite(measurements.get("range_position")),
                "breakout_direction": str(measurements.get("breakout_direction", "") or "").lower(),
                "trend_state": str(measurements.get("trend_state", "") or "").lower(),
                "trend_strength": _finite(measurements.get("trend_strength")),
                "range_rotation": bool(measurements.get("range_rotation", False)),
                "upper_wick": _finite(measurements.get("upper_wick")),
                "lower_wick": _finite(measurements.get("lower_wick")),
                "timeframe": str(measurements.get("timeframe", "") or "").strip().upper(),
            })
        elif domain == "momentum":
            change = _finite(measurements.get("close_change"))
            if change is not None:
                momenta.append({
                    "item": item, "time": source_time, "change": change,
                    "timeframe": str(measurements.get("timeframe", "") or "").strip().upper(),
                })
        elif domain == "liquidity":
            liquidity.append({
                "item": item,
                "time": source_time,
                "side": str(measurements.get("sweep_side", "") or "").lower(),
                "close": _finite_positive(measurements.get("current_close")),
                "swept_level": _finite_positive(measurements.get("swept_level")),
                "reclaimed": measurements.get("sweep_reclaimed") is True,
                "low": _finite_positive(measurements.get("prior_range_low")),
                "high": _finite_positive(measurements.get("prior_range_high")),
                "timeframe": str(measurements.get("timeframe", "") or "").strip().upper(),
            })

    concept_geometry = _geometry_from_concept_readings(concept_readings, side, state_symbol)
    if concept_geometry is not None:
        return concept_geometry

    structures = [record for record in structures if record["timeframe"]]
    momenta = [record for record in momenta if record["timeframe"]]
    liquidity = [record for record in liquidity if record["timeframe"]]
    if not structures:
        return None
    structures.sort(key=lambda record: record["time"], reverse=True)
    entry_record = structures[0]
    # Two incompatible readings from the same timeframe at the same source
    # instant cannot be resolved by list order. Preserve the conflict and stand down.
    peers = [
        record for record in structures
        if record["timeframe"] == entry_record["timeframe"]
        and record["time"] == entry_record["time"]
    ]
    if any(
        record["price"] != entry_record["price"]
        or record["high"] != entry_record["high"]
        or record["low"] != entry_record["low"]
        or record["breakout_direction"] != entry_record["breakout_direction"]
        or record["trend_state"] != entry_record["trend_state"]
        for record in peers[1:]
    ):
        return None
    entry = entry_record["price"]

    if side == "LONG":
        stops = [record for record in structures if record["low"] < entry]
        targets = [record for record in structures if record["high"] > entry]
        stop_record = max(stops, key=lambda record: record["low"]) if stops else None
        target_record = min(targets, key=lambda record: record["high"]) if targets else None
    else:
        stops = [record for record in structures if record["high"] > entry]
        targets = [record for record in structures if record["low"] < entry]
        stop_record = min(stops, key=lambda record: record["high"]) if stops else None
        target_record = max(targets, key=lambda record: record["low"]) if targets else None

    if stop_record is None or target_record is None:
        return None
    stop = stop_record["low"] if side == "LONG" else stop_record["high"]
    target = target_record["high"] if side == "LONG" else target_record["low"]
    risk_distance = entry - stop if side == "LONG" else stop - entry
    reward_distance = target - entry if side == "LONG" else entry - target
    if (
        not isfinite(risk_distance) or not isfinite(reward_distance)
        or risk_distance <= 0.0 or reward_distance <= 0.0
    ):
        return None

    direction_word = "up" if side == "LONG" else "down"
    matching_breakout = (
        [entry_record]
        if entry_record["breakout_direction"] == direction_word else []
    )
    matching_trend = (
        [entry_record]
        if entry_record["trend_state"] == ("uptrend" if side == "LONG" else "downtrend")
        and (entry_record["trend_strength"] or 0.0) > 0.0 else []
    )
    matching_momentum = [
        record for record in momenta
        if record["timeframe"] == entry_record["timeframe"]
        and record["time"] <= entry_record["time"]
        and (record["change"] > 0.0 if side == "LONG" else record["change"] < 0.0)
    ]
    matching_sweep_side = "sell" if side == "LONG" else "buy"
    matching_sweeps = [
        item for item in liquidity
        if item["side"] == matching_sweep_side
        and item["reclaimed"]
        and item["swept_level"] is not None
        and item["close"] is not None
        and item["timeframe"] == entry_record["timeframe"]
        and item["time"] <= entry_record["time"]
        and (
            item["close"] > item["swept_level"]
            if side == "LONG" else item["close"] < item["swept_level"]
        )
    ]
    # Range rotation is a distinct market state, not a pattern-name gate. It
    # requires a measured range edge plus a fresh response in the candidate
    # direction; merely being in a range or touching an edge is insufficient.
    matching_range_rotation = [
        record for record in structures
        if record["timeframe"] == entry_record["timeframe"]
        and record["time"] == entry_record["time"]
        and record["trend_state"] == "range"
        and record["range_rotation"]
        and (
            (record["range_position"] is not None and record["range_position"] <= 0.15)
            if side == "LONG" else
            (record["range_position"] is not None and record["range_position"] >= 0.85)
        )
    ]
    range_response = bool(matching_range_rotation and matching_momentum)

    if matching_breakout:
        activation_reason = (
            f"completed-bar structural breakout {direction_word} "
            f"observed on {matching_breakout[0]['timeframe'] or 'a confirmed timeframe'}"
        )
    elif matching_trend and matching_momentum:
        activation_reason = (
            f"confirmed {matching_trend[0]['trend_state']} structure and "
            f"same-direction completed-bar momentum"
        )
    elif range_response:
        activation_reason = (
            f"measured range rotation at the {'lower' if side == 'LONG' else 'upper'} "
            "boundary with a fresh directional response"
        )
    elif matching_sweeps:
        activation_reason = (
            f"counter-side liquidity sweep/reclaim observed for {side}; "
            "retain uncertainty until independent market-domain gates are satisfied"
        )
    else:
        return None

    ids = {
        str(getattr(entry_record["item"], "evidence_id", "") or ""),
        str(getattr(stop_record["item"], "evidence_id", "") or ""),
        str(getattr(target_record["item"], "evidence_id", "") or ""),
    }
    for records in (matching_breakout[:1], matching_trend[:1], matching_momentum[:1]):
        for record in records:
            if "item" in record:
                ids.add(str(getattr(record["item"], "evidence_id", "") or ""))
    for record in matching_sweeps[:1]:
        ids.add(str(getattr(record["item"], "evidence_id", "") or ""))
    for record in matching_range_rotation[:1]:
        ids.add(str(getattr(record["item"], "evidence_id", "") or ""))
    ids.discard("")

    return OpportunityGeometry(
        direction=side,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        risk_distance=risk_distance,
        reward_distance=reward_distance,
        reward_r=reward_distance / risk_distance,
        activated=True,
        activation_reason=activation_reason,
        invalidation_reason=(
            f"{side} thesis invalidated by completed-bar acceptance "
            f"{'below' if side == 'LONG' else 'above'} structural stop {stop:.10g}"
        ),
        evidence_ids=tuple(sorted(ids)),
        entry_timeframe=entry_record["timeframe"],
        stop_timeframe=stop_record["timeframe"],
        target_timeframe=target_record["timeframe"],
        symbol=state_symbol,
        evidence_timestamp_epoch=entry_record["time"],
    )


__all__ = ["OpportunityGeometry", "derive_opportunity_geometry"]
