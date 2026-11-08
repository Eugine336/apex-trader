"""Small read-only registry for optional provider adapters."""
from __future__ import annotations

from threading import RLock
from typing import Sequence

from data_sources.contracts import (
    CapabilityState, DataObservation, DataSourceAdapter, ObservationKind,
)


class DataSourceRegistry:
    """Register source adapters without coupling cognition to vendor SDKs."""

    def __init__(self) -> None:
        self._adapters: dict[str, DataSourceAdapter] = {}
        self._lock = RLock()

    def register(self, adapter: DataSourceAdapter) -> None:
        provider = str(getattr(adapter, "provider_id", "") or "").strip().lower()
        if not provider:
            raise ValueError("adapter.provider_id must be non-empty")
        if not callable(getattr(adapter, "capabilities", None)) or not callable(getattr(adapter, "fetch", None)):
            raise TypeError("adapter must implement capabilities() and fetch()")
        with self._lock:
            if provider in self._adapters and self._adapters[provider] is not adapter:
                raise ValueError(f"provider adapter already registered: {provider}")
            self._adapters[provider] = adapter

    def unregister(self, provider_id: str) -> bool:
        with self._lock:
            return self._adapters.pop(str(provider_id or "").strip().lower(), None) is not None

    def providers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._adapters))

    def capabilities(self) -> tuple[CapabilityState, ...]:
        with self._lock:
            adapters = tuple(self._adapters.values())
        output: list[CapabilityState] = []
        for adapter in adapters:
            states = adapter.capabilities()
            for state in states or ():
                if not isinstance(state, CapabilityState):
                    raise TypeError(f"{adapter.provider_id} returned a non-CapabilityState")
                if state.provider.strip().lower() != adapter.provider_id.strip().lower():
                    raise ValueError("capability provider does not match registered adapter")
                output.append(state)
        return tuple(output)

    def fetch(
        self,
        kind: ObservationKind,
        *,
        canonical_instrument: str | None = None,
        start_epoch: float | None = None,
        end_epoch: float | None = None,
        providers: Sequence[str] | None = None,
    ) -> tuple[DataObservation, ...]:
        if start_epoch is not None and end_epoch is not None and start_epoch > end_epoch:
            raise ValueError("start_epoch cannot exceed end_epoch")
        with self._lock:
            selected = (
                tuple(self._adapters[name.strip().lower()] for name in providers if name.strip().lower() in self._adapters)
                if providers is not None
                else tuple(self._adapters.values())
            )
        output: list[DataObservation] = []
        for adapter in selected:
            observations = adapter.fetch(
                kind,
                canonical_instrument=canonical_instrument,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
            )
            for observation in observations or ():
                if not isinstance(observation, DataObservation):
                    raise TypeError(f"{adapter.provider_id} returned a non-DataObservation")
                if observation.provenance.source.provider.strip().lower() != adapter.provider_id.strip().lower():
                    raise ValueError("observation provider does not match registered adapter")
                if observation.provenance.kind is not kind:
                    raise ValueError("observation kind does not match requested kind")
                stamp = observation.provenance.source_timestamp_epoch
                if start_epoch is not None and stamp is not None and stamp < start_epoch:
                    continue
                if end_epoch is not None and stamp is not None and stamp > end_epoch:
                    continue
                if canonical_instrument is not None and observation.provenance.source.canonical_instrument.upper() != canonical_instrument.upper():
                    continue
                output.append(observation)
        return tuple(output)
