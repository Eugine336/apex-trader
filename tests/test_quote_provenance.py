"""Source-specific quote provenance contract tests."""

from __future__ import annotations

from cognition.quote_provenance import verify_quote_source_provenance


def test_mt5_quote_requires_consensus_clock_and_original_broker_timestamp():
    raw = {
        "source_adapter": "mt5",
        "source_timezone": "broker_server",
        "time": 1_800_000_000,
        "time_msc": 1_800_000_000_123,
        "source_utc_offset_seconds": 0.0,
        "timestamp_utc_epoch": 1_800_000_000.123,
        "source_utc_offset_uncertainty_seconds": 0.25,
        "source_clock_sources": ["ntp:time.cloudflare.com", "ntp:time.google.com"],
    }
    assert verify_quote_source_provenance(
        raw, symbol="XAUUSD", quote_kind="bid_ask",
        timestamp_epoch=1_800_000_000.123,
    )


def test_mt5_quote_rejects_insufficient_or_uncertain_clock_evidence():
    raw = {
        "source_adapter": "mt5",
        "source_timezone": "broker_server",
        "time": 1_800_000_000,
        "source_utc_offset_seconds": 0.0,
        "timestamp_utc_epoch": 1_800_000_000.0,
        "source_utc_offset_uncertainty_seconds": 3.0,
        "source_clock_sources": ["ntp:time.cloudflare.com"],
    }
    assert not verify_quote_source_provenance(
        raw, symbol="XAUUSD", quote_kind="bid_ask",
        timestamp_epoch=1_800_000_000.0,
    )


def test_mt5_quote_rejects_offset_that_does_not_reconcile_raw_broker_time():
    raw = {
        "source_adapter": "mt5",
        "source_timezone": "broker_server",
        "time": 1_800_000_000,
        "time_msc": 1_800_000_000_000,
        "source_utc_offset_seconds": 3600.0,
        "timestamp_utc_epoch": 1_800_000_000.0,
        "source_utc_offset_uncertainty_seconds": 0.1,
        "source_clock_sources": ["ntp:time.cloudflare.com", "ntp:time.google.com"],
    }
    assert not verify_quote_source_provenance(
        raw, symbol="XAUUSD", quote_kind="bid_ask",
        timestamp_epoch=1_800_000_000.0,
    )


def test_deriv_quote_requires_original_provider_timestamp_and_receipt():
    raw = {
        "source_adapter": "deriv",
        "provider_timestamp_epoch": 1_800_000_000,
        "provider_timestamp_field": "history.times",
        "provider_timestamp_semantics": "unix_utc_seconds",
        "received_at_epoch": 1_800_000_001,
        "timestamp_utc_epoch": 1_800_000_000,
    }
    assert verify_quote_source_provenance(
        raw, symbol="R_75", quote_kind="mid_only",
        timestamp_epoch=1_800_000_000,
    )


def test_deriv_quote_rejects_missing_provider_semantics():
    raw = {
        "source_adapter": "deriv",
        "provider_timestamp_epoch": 1_800_000_000,
        "received_at_epoch": 1_800_000_001,
    }
    assert not verify_quote_source_provenance(
        raw, symbol="R_75", quote_kind="mid_only",
        timestamp_epoch=1_800_000_000,
    )


def test_quote_provenance_rejects_unknown_adapter_and_mismatched_symbol():
    raw = {
        "source_adapter": "other",
        "provider_timestamp_epoch": 1_800_000_000,
        "provider_timestamp_field": "history.times",
        "provider_timestamp_semantics": "unix_utc_seconds",
        "received_at_epoch": 1_800_000_001,
    }
    assert not verify_quote_source_provenance(
        raw, symbol="R_75", quote_kind="mid_only",
        timestamp_epoch=1_800_000_000,
    )
