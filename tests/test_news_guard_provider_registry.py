"""Provider-neutral economic calendar integration for the shared NewsGuard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brain.session_engine import NewsGuard
from data_sources import (
    CapabilityState,
    Completeness,
    DataObservation,
    DataSourceRegistry,
    ObservationKind,
    ObservationProvenance,
    SourceIdentity,
)


class _CalendarAdapter:
    provider_id = "fixture_calendar"

    def __init__(self, rows=(), *, fail=False):
        self.rows = tuple(rows)
        self.fail = fail

    def capabilities(self):
        return (CapabilityState(
            provider=self.provider_id,
            capability=ObservationKind.ECONOMIC_EVENT,
            completeness=Completeness.COMPLETE,
            sample_count=len(self.rows),
        ),)

    def fetch(self, kind, **_kwargs):
        if self.fail:
            raise RuntimeError("offline fixture failure")
        assert kind is ObservationKind.ECONOMIC_EVENT
        return self.rows


def _event(now, *, completeness=Completeness.COMPLETE, revision=None):
    epoch = now.timestamp()
    provenance = ObservationProvenance(
        source=SourceIdentity(
            provider="fixture_calendar",
            venue="global_calendar",
            canonical_instrument="EURUSD",
            native_instrument="EURUSD",
        ),
        kind=ObservationKind.ECONOMIC_EVENT,
        source_timestamp_epoch=epoch - 30,
        received_timestamp_epoch=epoch - 2,
        source_timestamp_verified=True,
        completeness=completeness,
        source_record_id="event-cpi-001",
        publication_timestamp_epoch=epoch - 60,
        revision_timestamp_epoch=revision,
    )
    return DataObservation(provenance, {
        "event_id": "event-cpi-001",
        "title": "Consumer price index",
        "currency": "USD",
        "impact": "HIGH",
        "event_time_epoch": epoch + 8 * 60,
    })


def _guard(rows=(), *, fail=False):
    registry = DataSourceRegistry()
    registry.register(_CalendarAdapter(rows, fail=fail))
    return NewsGuard(data_source_registry=registry)


def test_news_guard_uses_verified_registry_events_and_preserves_provenance():
    now = datetime.now(timezone.utc)
    guard = _guard([_event(now)])

    status = guard.check(["EURUSD"], utc_now=now)

    assert status.is_clear is False
    event = status.events_nearby[0]
    assert event.title == "Consumer price index"
    assert event.provider == "fixture_calendar"
    assert event.venue == "global_calendar"
    assert event.source_record_id == "event-cpi-001"
    assert event.publication_timestamp_epoch == now.timestamp() - 60


def test_configured_calendar_failure_fails_closed_without_legacy_fallback():
    now = datetime.now(timezone.utc)
    guard = _guard(fail=True)

    status = guard.check(["EURUSD"], utc_now=now)

    assert status.is_clear is False
    assert "UNAVAILABLE" in status.warning_message


def test_capability_discovery_failure_does_not_fall_back_to_an_unrelated_calendar():
    class BrokenRegistry:
        def capabilities(self):
            raise RuntimeError("capability registry unavailable")

    guard = NewsGuard(data_source_registry=BrokenRegistry())
    assert guard._fetch_events(datetime.now(timezone.utc)) is None


def test_incomplete_or_future_revised_calendar_evidence_fails_closed():
    now = datetime.now(timezone.utc)
    partial = _guard([_event(now, completeness=Completeness.PARTIAL)])
    future_revision = _guard([_event(now, revision=now.timestamp() + 1)])

    assert partial._fetch_events(now) is None
    assert future_revision._fetch_events(now) is None


def test_calendar_cache_is_not_reused_for_an_earlier_cutoff():
    now = datetime.now(timezone.utc)
    guard = _guard([_event(now)])

    assert guard._fetch_events(now) is not None
    assert guard._fetch_events(now - timedelta(hours=1)) is None
