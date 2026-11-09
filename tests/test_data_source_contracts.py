"""Tests for canonical provider-neutral data-source contracts."""
import pytest

from data_sources import (
    CapabilityState, Completeness, DataObservation, DataSourceRegistry,
    ObservationKind, ObservationProvenance, SourceIdentity,
)


def _observation(provider="demo", kind=ObservationKind.ECONOMIC_EVENT, stamp=1_900_000_000):
    return DataObservation(
        provenance=ObservationProvenance(
            source=SourceIdentity(
                provider=provider, venue="global", canonical_instrument="XAUUSD",
                native_instrument="GOLD",
            ),
            kind=kind,
            source_timestamp_epoch=stamp,
            received_timestamp_epoch=1_800_000_000,
            source_timestamp_verified=True,
            publication_timestamp_epoch=1_800_000_000,
            completeness=Completeness.COMPLETE,
            source_record_id="event-1",
        ),
        values={"event": "scheduled_release"},
    )


class _Adapter:
    provider_id = "demo"

    def capabilities(self):
        return [CapabilityState(
            provider=self.provider_id, capability=ObservationKind.ECONOMIC_EVENT,
            completeness=Completeness.COMPLETE, sample_count=1,
        )]

    def fetch(self, kind, *, canonical_instrument=None, start_epoch=None, end_epoch=None):
        return [_observation(kind=kind)]


def test_provenance_allows_future_event_time_with_past_publication_time():
    observation = _observation()
    assert observation.provenance.source_timestamp_epoch == 1_900_000_000
    assert observation.provenance.publication_timestamp_epoch == 1_800_000_000


def test_verified_source_timestamp_requires_valid_epoch():
    with pytest.raises(ValueError):
        ObservationProvenance(
            source=SourceIdentity("demo", "global", "XAUUSD", "GOLD"),
            kind=ObservationKind.OHLCV,
            source_timestamp_epoch=None,
            received_timestamp_epoch=1_800_000_000,
            source_timestamp_verified=True,
        )


def test_registry_validates_provider_kind_and_instrument_identity():
    registry = DataSourceRegistry()
    adapter = _Adapter()
    registry.register(adapter)
    assert registry.providers() == ("demo",)
    assert registry.capabilities()[0].sample_count == 1
    found = registry.fetch(
        ObservationKind.ECONOMIC_EVENT, canonical_instrument="XAUUSD",
    )
    assert len(found) == 1
    assert found[0].values["event"] == "scheduled_release"
    assert registry.fetch(
        ObservationKind.ECONOMIC_EVENT, canonical_instrument="EURUSD",
    ) == ()


def test_registry_rejects_duplicate_provider_and_invalid_observation_kind():
    registry = DataSourceRegistry()
    registry.register(_Adapter())
    with pytest.raises(ValueError):
        registry.register(_Adapter())

    class _WrongKind(_Adapter):
        def fetch(self, kind, **kwargs):
            return [_observation(kind=ObservationKind.QUOTE)]

    wrong = DataSourceRegistry()
    wrong.register(_WrongKind())
    with pytest.raises(RuntimeError, match="demo:ValueError"):
        wrong.fetch(ObservationKind.ECONOMIC_EVENT)


def test_capability_sample_count_must_be_integer():
    with pytest.raises(ValueError):
        CapabilityState(
            provider="demo", capability=ObservationKind.QUOTE,
            completeness=Completeness.UNKNOWN, sample_count=1.5,
        )


def test_registry_does_not_silently_ignore_unregistered_provider():
    registry = DataSourceRegistry()
    with pytest.raises(KeyError):
        registry.fetch(ObservationKind.QUOTE, providers=["not-installed"])


def test_registry_isolates_provider_failure_and_returns_healthy_observations():
    class _Broken(_Adapter):
        provider_id = "broken"
        def fetch(self, kind, **kwargs):
            raise ConnectionError("provider unavailable")

    class _Healthy(_Adapter):
        provider_id = "healthy"
        def fetch(self, kind, **kwargs):
            return [_observation(provider="healthy", kind=kind)]

    registry = DataSourceRegistry()
    registry.register(_Broken())
    registry.register(_Healthy())
    observations, errors = registry.fetch_with_status(ObservationKind.ECONOMIC_EVENT)
    assert len(observations) == 1
    assert observations[0].provenance.source.provider == "healthy"
    assert errors == {"broken": "ConnectionError"}
    with pytest.raises(RuntimeError):
        registry.fetch(ObservationKind.ECONOMIC_EVENT)
