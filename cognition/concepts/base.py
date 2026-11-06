"""Concept modules: full readings of trading concepts, not single measurements.

A concept module reads completed bars and returns :class:`ConceptReading` objects.
Each reading carries what an expert takes from the concept in context:

* **applicability** — whether the concept is valid in the present state;
* **implications** — what it says for LONG, SHORT or FLAT, each with the evidence
  strength, whether its trigger is satisfied now, the price/event that proves it
  wrong (invalidation), where it expects price to travel (objective, milestones),
  what is still unconfirmed (requires) and what it means for managing a position;
* **facts** — the raw measurements behind it.

Readings are emitted as :class:`~cognition.contracts.Evidence` so the Brain (the
only decision authority) can combine, challenge or reject them. A concept never
chooses a trade. Strengths are *uncalibrated reasoning strength* measured against
the instrument's own history (percentile ranks), not configured thresholds; the
few constants a module contains are definitions of the concept itself (e.g. the
three-candle structure of CRT), not tuning knobs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, Sequence

from cognition.contracts import Evidence, EvidenceDomain

LONG, SHORT, FLAT = "LONG", "SHORT", "FLAT"

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
MEASUREMENT_VERSION = "concept-reading-v1"


def finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def percentile_rank(value: float, sample: Sequence[float]) -> Optional[float]:
    """Fraction of the instrument's own history at or below ``value`` (0..1).

    Returns ``None`` when there is no history, so callers keep the strength
    unknown instead of inventing one.
    """
    clean = [float(s) for s in sample if finite(s) is not None]
    if not clean:
        return None
    return sum(1 for s in clean if s <= value) / len(clean)


def mean_known(values: Sequence[Optional[float]]) -> Optional[float]:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


@dataclass(frozen=True)
class Implication:
    """What a concept implies for one direction, with the means to test and manage it."""

    direction: str                       # LONG | SHORT | FLAT
    statement: str
    strength: Optional[float]            # 0..1 reasoning strength vs own history; None = unknown
    activated: bool                      # trigger satisfied on completed bars right now
    activation_reason: str
    invalidation_price: Optional[float] = None
    invalidation_reason: str = ""
    objective_price: Optional[float] = None
    # Missing entry instructions mean market-at-reference. Non-market orders
    # require an explicit price or a zone containing the reference price.
    entry_price: Optional[float] = None
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None
    order_type: str = "MARKET"
    milestones: tuple[float, ...] = ()
    requires: tuple[str, ...] = ()       # confirmations still missing
    conflicts_with: tuple[str, ...] = () # what would weaken it
    management: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "statement": self.statement,
            "strength": self.strength,
            "activated": self.activated,
            "activation_reason": self.activation_reason,
            "invalidation_price": self.invalidation_price,
            "invalidation_reason": self.invalidation_reason,
            "objective_price": self.objective_price,
            "entry_price": self.entry_price,
            "entry_zone_low": self.entry_zone_low,
            "entry_zone_high": self.entry_zone_high,
            "order_type": self.order_type,
            "milestones": list(self.milestones),
            "requires": list(self.requires),
            "conflicts_with": list(self.conflicts_with),
            "management": dict(self.management),
        }


@dataclass(frozen=True)
class ConceptReading:
    """One concept read on one timeframe at one completed-bar instant."""

    concept: str
    timeframe: str
    domain: EvidenceDomain
    applicable: bool
    applicability_reason: str
    observation: str
    implications: tuple[Implication, ...]
    facts: dict
    source_bar_epoch: Optional[float]
    reference_close: Optional[float] = None       # latest completed close on the finest timeframe
    reference_timeframe: str = ""
    unknowns: tuple[str, ...] = ()
    data_basis: str = ""
    independence_group: str = ""
    relevance_horizon_seconds: Optional[float] = None
    # Non-candle sources keep their provider timestamp separate from completed bars.
    source_timestamp_epoch: Optional[float] = None
    source_timestamp_verified: bool = False
    # Optional reliability fields for non-directional, source-backed readings.
    evidence_confidence: Optional[float] = None
    evidence_uncertainty: Optional[float] = None


class ConceptModule(Protocol):
    key: str
    family: str
    covers_methods: tuple[str, ...]   # analytical_knowledge keys this module reads in full
    covers_lenses: tuple[str, ...]    # strategy_knowledge lens keys this module reads in full

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        ...


def reference_price(bars_by_tf: dict[str, list[dict]]) -> tuple[Optional[float], str]:
    """Latest completed close on the finest available timeframe."""
    for tf in sorted(bars_by_tf, key=lambda t: TIMEFRAME_SECONDS.get(t, 10**9)):
        bars = bars_by_tf[tf]
        if bars:
            close = finite(bars[-1].get("close"))
            if close is not None and close > 0:
                return close, tf
    return None, ""


def reading_to_evidence(symbol: str, reading: ConceptReading) -> Evidence:
    seconds = TIMEFRAME_SECONDS.get(reading.timeframe, 60)
    explicit_source_epoch = finite(reading.source_timestamp_epoch)
    if reading.source_timestamp_epoch is not None:
        known = bool(
            reading.source_timestamp_verified is True
            and explicit_source_epoch is not None
            and explicit_source_epoch > 0.0
        )
        source_epoch = explicit_source_epoch if known else None
    else:
        bar_epoch = finite(reading.source_bar_epoch)
        known = bool(reading.source_timestamp_verified is True and bar_epoch is not None and bar_epoch > 0.0)
        source_epoch = bar_epoch if known else None
    sampled_at = datetime.now(timezone.utc).timestamp()
    iso = datetime.fromtimestamp(sampled_at, tz=timezone.utc).isoformat()
    strengths = [i.strength for i in reading.implications if i.strength is not None]
    inferred_confidence = max(strengths) if strengths else 0.0
    explicit_confidence = finite(reading.evidence_confidence)
    confidence = (
        min(1.0, max(0.0, explicit_confidence))
        if explicit_confidence is not None else inferred_confidence
    )
    explicit_uncertainty = finite(reading.evidence_uncertainty)
    uncertainty = (
        min(1.0, max(0.0, explicit_uncertainty))
        if explicit_uncertainty is not None else
        (min(1.0, max(0.0, 1.0 - confidence)) if strengths or explicit_confidence is not None else 1.0)
    )
    facts = dict(reading.facts) if isinstance(reading.facts, dict) else {}
    evidence_source_module = str(facts.pop("evidence_source_module", "") or "").strip()
    return Evidence(
        source_module=(
            evidence_source_module
            or f"concept.{reading.concept}.{reading.timeframe.lower()}"
        ),
        domain=reading.domain,
        symbol=str(symbol or ""),
        observation=reading.observation[:1400],
        confidence=confidence,
        uncertainty=uncertainty,
        polarity=0.0,   # a reading is not a vote; the Brain decides what it means
        measurements={
            # Keep source facts available both as a nested audit record and as
            # measurements consumed by method assessment and the Brain's
            # existing cost gate. Reserved adapter fields below remain canonical.
            **facts,
            "source_kind": "concept_reading",
            "concept": reading.concept,
            "timeframe": reading.timeframe,
            "provisional": False,
            "data_basis": reading.data_basis or "completed_ohlc",
            "source_timestamp_known": known,
            "source_timestamp_epoch": source_epoch,
            "source_timestamp_verified": known,
            "source_bar_epoch": reading.source_bar_epoch,
            "applicable": reading.applicable,
            "applicability_reason": reading.applicability_reason,
            "reference_close": reading.reference_close,
            "reference_timeframe": reading.reference_timeframe,
            "implications": [i.to_dict() for i in reading.implications],
            "facts": facts,
            "unknowns": list(reading.unknowns),
        },
        timestamp_epoch=sampled_at,
        timestamp_iso=iso,
        relevance_horizon_seconds=(
            reading.relevance_horizon_seconds
            if reading.relevance_horizon_seconds is not None
            else max(3 * seconds, 180.0)
        ),
        source_timestamp_epoch=source_epoch if known else None,
        source_timestamp_verified=known,
        data_basis=reading.data_basis or "completed_ohlc",
        provisional=False,
        # An interpretation of the same OHLC is not independent of that OHLC.
        independence_group=(
            reading.independence_group
            or (
                f"ohlc:{str(symbol or '').upper()}:{reading.timeframe}"
                if (reading.data_basis or "completed_ohlc") == "completed_ohlc"
                else ""
            )
        ),
        measurement_version=MEASUREMENT_VERSION,
    )
