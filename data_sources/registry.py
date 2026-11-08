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

    def fetch_with_status(
        self,
        kind: ObservationKind,
        *,
        canonical_instrument: str | None = None,
        start_epoch: float | None = None,
        end_epoch: float | None = None,
        providers: Sequence[str] | None = None,
    ) -> tuple[tuple[DataObservation, ...], dict[str, str]]:
        """Fetch independently; one provider failure cannot erase healthy providers."""
        if start_epoch is not None and end_epoch is not None and start_epoch > end_epoch:
            raise ValueError("start_epoch cannot exceed end_epoch")
        if not isinstance(kind, ObservationKind):
            raise TypeError("kind must be an ObservationKind")
        with self._lock:
            if providers is None:
                selected = tuple(self._adapters.values())
            else:
                requested = tuple(dict.fromkeys(str(name or "").strip().lower() for name in providers))
                unknown = sorted(name for name in requested if name not in self._adapters)
                if unknown:
                    raise KeyError("unregistered data provider(s): " + ", ".join(unknown))
                selected = tuple(self._adapters[name] for name in requested)
        output: list[DataObservation] = []
        errors: dict[str, str] = {}
        for adapter in selected:
            provider = str(adapter.provider_id).strip().lower()
            provider_rows: list[DataObservation] = []
            try:
                observations = adapter.fetch(
                    kind,
                    canonical_instrument=canonical_instrument,
                    start_epoch=start_epoch,
                    end_epoch=end_epoch,
                )
                for observation in observations or ():
                    if not isinstance(observation, DataObservation):
                        raise TypeError("provider returned a non-DataObservation")
                    if observation.provenance.source.provider.strip().lower() != provider:
                        raise ValueError("observation provider does not match registered adapter")
                    if observation.provenance.kind is not kind:
                        raise ValueError("observation kind does not match requested kind")
                    stamp = observation.provenance.source_timestamp_epoch
                    if start_epoch is not None and stamp is not None and stamp < start_epoch:
                        continue
                    if end_epoch is not None and stamp is not None and stamp > end_epoch:
                        continue
                    if (
                        canonical_instrument is not None
                        and observation.provenance.source.canonical_instrument.upper()
                        != canonical_instrument.upper()
                    ):
                        continue
                    provider_rows.append(observation)
            except Exception as exc:  # noqa: BLE001 — isolate provider faults and expose them to readers
                errors[provider] = type(exc).__name__
                continue
            output.extend(provider_rows)
        return tuple(output), errors

    def fetch(
        self,
        kind: ObservationKind,
        *,
        canonical_instrument: str | None = None,
        start_epoch: float | None = None,
        end_epoch: float | None = None,
        providers: Sequence[str] | None = None,
    ) -> tuple[DataObservation, ...]:
        """Strict compatibility API: fail visibly if any selected provider fails."""
        observations, errors = self.fetch_with_status(
            kind, canonical_instrument=canonical_instrument,
            start_epoch=start_epoch, end_epoch=end_epoch, providers=providers,
        )
        if errors:
            detail = ", ".join(f"{provider}:{error}" for provider, error in sorted(errors.items()))
            raise RuntimeError("data provider fetch failure(s): " + detail)
        return observations
