"""Canonical provider-neutral observation contracts for Apex data sources.

Broker connectors and external market-data providers normalize into these
contracts before evidence reaches the Brain. This module has no vendor SDK,
credential, network, or execution dependency.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence


class ObservationKind(str, Enum):
    OHLCV = "ohlcv"
    QUOTE = "quote"
    TRADE_PRINT = "trade_print"
    DEPTH = "depth"
    INSTRUMENT_SPEC = "instrument_spec"
    SESSION = "session"
    ECONOMIC_EVENT = "economic_event"
    MACRO_SERIES = "macro_series"
    DERIVATIVES = "derivatives"
    FUNDING = "funding"
    OPEN_INTEREST = "open_interest"
    LIQUIDATION = "liquidation"
    ACCOUNT = "account"
    POSITION = "position"
    ORDER = "order"
    DEAL = "deal"
    EXECUTION_COST = "execution_cost"
    PORTFOLIO_SNAPSHOT = "portfolio_snapshot"


class Completeness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True)
class SourceIdentity:
    provider: str
    venue: str
    canonical_instrument: str
    native_instrument: str

    def __post_init__(self) -> None:
        for name in ("provider", "venue", "canonical_instrument", "native_instrument"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"{name} must be non-empty")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class ObservationProvenance:
    source: SourceIdentity
    kind: ObservationKind
    source_timestamp_epoch: float | None
    received_timestamp_epoch: float
    source_timestamp_verified: bool
    completeness: Completeness = Completeness.UNKNOWN
    source_record_id: str | None = None
    publication_timestamp_epoch: float | None = None
    revision_timestamp_epoch: float | None = None
    units: str | None = None
    volume_basis: str | None = None
    aggressor_side_basis: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_timestamp_verified, bool):
            raise TypeError("source_timestamp_verified must be a bool")
        if not isinstance(self.kind, ObservationKind):
            raise TypeError("kind must be an ObservationKind")
        if not isinstance(self.completeness, Completeness):
            raise TypeError("completeness must be a Completeness value")
        received = _epoch(self.received_timestamp_epoch, "received_timestamp_epoch")
        object.__setattr__(self, "received_timestamp_epoch", received)
        source_epoch = _optional_epoch(self.source_timestamp_epoch, "source_timestamp_epoch")
        publication = _optional_epoch(self.publication_timestamp_epoch, "publication_timestamp_epoch")
        revision = _optional_epoch(self.revision_timestamp_epoch, "revision_timestamp_epoch")
        object.__setattr__(self, "source_timestamp_epoch", source_epoch)
        object.__setattr__(self, "publication_timestamp_epoch", publication)
        object.__setattr__(self, "revision_timestamp_epoch", revision)
        if self.source_timestamp_verified and source_epoch is None:
            raise ValueError("verified source timestamps require a finite positive source epoch")
        if revision is not None and publication is not None and revision < publication:
            raise ValueError("revision timestamp cannot precede publication timestamp")
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


@dataclass(frozen=True)
class DataObservation:
    provenance: ObservationProvenance
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, ObservationProvenance):
            raise TypeError("provenance must be ObservationProvenance")
        if not isinstance(self.values, Mapping):
            raise TypeError("values must be a mapping")
        object.__setattr__(self, "values", dict(self.values))


@dataclass(frozen=True)
class CapabilityState:
    provider: str
    capability: ObservationKind
    completeness: Completeness
    sample_count: int = 0
    first_source_timestamp_epoch: float | None = None
    last_source_timestamp_epoch: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not str(self.provider or "").strip():
            raise ValueError("provider must be non-empty")
        if not isinstance(self.capability, ObservationKind):
            raise TypeError("capability must be an ObservationKind")
        if not isinstance(self.completeness, Completeness):
            raise TypeError("completeness must be a Completeness value")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 0:
            raise ValueError("sample_count must be a non-negative integer")
        first = _optional_epoch(self.first_source_timestamp_epoch, "first_source_timestamp_epoch")
        last = _optional_epoch(self.last_source_timestamp_epoch, "last_source_timestamp_epoch")
        if first is not None and last is not None and first > last:
            raise ValueError("first source timestamp cannot exceed last source timestamp")
        object.__setattr__(self, "first_source_timestamp_epoch", first)
        object.__setattr__(self, "last_source_timestamp_epoch", last)


class DataSourceAdapter(Protocol):
    """Read-only source contract. Execution remains in platform connectors."""

    provider_id: str

    def capabilities(self) -> Sequence[CapabilityState]:
        """Describe currently available observations without inventing entitlements."""

    def fetch(
        self,
        kind: ObservationKind,
        *,
        canonical_instrument: str | None = None,
        start_epoch: float | None = None,
        end_epoch: float | None = None,
    ) -> Sequence[DataObservation]:
        """Return normalized source observations; no execution side effects."""


def _epoch(value: Any, name: str) -> float:
    parsed = _optional_epoch(value, name)
    if parsed is None:
        raise ValueError(f"{name} must be a finite positive epoch")
    return parsed


def _optional_epoch(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive epoch")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite positive epoch") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a finite positive epoch")
    return parsed
