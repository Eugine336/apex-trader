"""Cross-timeframe summaries require point-in-time source timestamp evidence."""
from datetime import datetime, timezone
import time

import pandas as pd

from cognition.contracts import EvidenceDomain
from cognition.market_lenses import analyze_market_frames


def _frame(timeframe_seconds, *, naive=False, count=12):
    now = time.time()
    base = int(now // timeframe_seconds) * timeframe_seconds
    start = base - (count + 2) * timeframe_seconds
    rows = []
    for i in range(count):
        stamp = datetime.fromtimestamp(
            start + i * timeframe_seconds, tz=timezone.utc
        )
        if naive:
            stamp = stamp.replace(tzinfo=None)
        close = 100.0 + i * 0.5
        rows.append({
            "time": stamp,
            "open": close - 0.2,
            "high": close + 0.3,
            "low": close - 0.4,
            "close": close,
            "volume": 10 + i,
        })
    # The last row represents a forming bar, matching the production call.
    forming_stamp = datetime.fromtimestamp(
        start + count * timeframe_seconds, tz=timezone.utc
    )
    close = 100.0 + count * 0.5
    rows.append({
        "time": forming_stamp.replace(tzinfo=None) if naive else forming_stamp,
        "open": close - 0.2,
        "high": close + 0.3,
        "low": close - 0.4,
        "close": close,
        "volume": 10 + count,
    })
    frame = pd.DataFrame(rows)
    frame.attrs.update({
        "source_timestamp_verified": True,
        "source_symbol": "XAUUSD",
        "source_timestamp_unit": "unix_seconds",
        "source_timestamp_origin": "broker_ohlcv",
        "source_broker": "test-broker",
        "source_timezone": "UTC",
        "forming_bar_included": True,
    })
    return frame


def _alignment(frames):
    evidence = analyze_market_frames(
        "XAUUSD",
        frames,
        source_includes_forming_bar=True,
        concept_context={"multi_symbol_snapshot_cutoff_epoch": time.time() + 1.0},
    )
    return next(
        item for item in evidence
        if item.domain == EvidenceDomain.MULTI_TIMEFRAME
    )


def test_cross_timeframe_alignment_records_common_cutoff_and_boundaries():
    evidence = _alignment({
        "M1": _frame(60),
        "M5": _frame(300),
        "H1": _frame(3600),
    })
    measurements = evidence.measurements
    assert measurements["synchronized_timestamps"] is True
    assert measurements["source_verified"] is True
    assert measurements["synchronization_method"] == (
        "common_snapshot_cutoff_and_intraday_epoch_boundaries"
    )
    assert len(measurements["timeframe_observations"]) == 3
    assert measurements["daily_boundary_included"] is False
    assert measurements["causation_established"] is False


def test_cross_timeframe_alignment_does_not_claim_sync_for_naive_timestamps():
    evidence = _alignment({
        "M1": _frame(60, naive=True),
        "M5": _frame(300, naive=True),
    })
    measurements = evidence.measurements
    assert measurements["synchronized_timestamps"] is False
    assert measurements["source_verified"] is False
    assert measurements["synchronization_limitation"]


def test_timestamp_parser_rejects_naive_datetime_values():
    from cognition.market_lenses import _timestamp_epoch

    assert _timestamp_epoch(datetime(2026, 1, 1, 12, 0, 0)) is None
    assert _timestamp_epoch(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)) is not None



def test_parseable_candle_timestamp_is_not_verified_without_broker_attestation():
    unverified = _frame(60)
    unverified.attrs.clear()
    evidence = analyze_market_frames(
        "XAUUSD", {"M1": unverified}, source_includes_forming_bar=True,
    )
    structure = next(
        item for item in evidence
        if item.domain == EvidenceDomain.STRUCTURE
    )
    assert structure.measurements["source_timestamp_known"] is False
    assert structure.source_timestamp_verified is False
    assert structure.source_timestamp_epoch is None

    wrong_symbol = _frame(60)
    wrong_symbol.attrs["source_symbol"] = "EURUSD"
    mismatched = analyze_market_frames(
        "XAUUSD", {"M1": wrong_symbol}, source_includes_forming_bar=True,
    )
    mismatched_structure = next(
        item for item in mismatched
        if item.domain == EvidenceDomain.STRUCTURE
    )
    assert mismatched_structure.source_timestamp_verified is False
    assert mismatched_structure.source_timestamp_epoch is None



def test_broker_attestation_does_not_verify_unordered_candle_rows():
    frame = _frame(60).iloc[::-1].copy()
    evidence = analyze_market_frames(
        "XAUUSD", {"M1": frame}, source_includes_forming_bar=True,
    )
    structure = next(
        item for item in evidence
        if item.domain == EvidenceDomain.STRUCTURE
    )
    assert structure.measurements["source_timestamp_known"] is False
    assert structure.source_timestamp_verified is False
    assert structure.source_timestamp_epoch is None

def test_cross_timeframe_alignment_fails_closed_without_verified_common_cutoff():
    evidence = analyze_market_frames(
        "XAUUSD",
        {"M1": _frame(60), "M5": _frame(300)},
        source_includes_forming_bar=True,
    )
    alignment = next(
        item for item in evidence
        if item.domain == EvidenceDomain.MULTI_TIMEFRAME
    )
    assert alignment.measurements["synchronized_timestamps"] is False
    assert alignment.measurements["source_verified"] is False
    assert alignment.measurements["snapshot_cutoff_epoch"] is None

def test_market_lens_evidence_separates_ingestion_time_from_source_time():
    frame = _frame(60)
    before = time.time()
    unanchored = analyze_market_frames(
        "XAUUSD", {"M1": frame}, source_includes_forming_bar=True,
    )
    structure_unanchored = next(
        item for item in unanchored
        if item.domain == EvidenceDomain.STRUCTURE
    )
    assert before <= structure_unanchored.timestamp_epoch <= time.time() + 1.0
    assert structure_unanchored.measurements["evidence_timestamp_basis"] == (
        "host_ingestion_time_unverified"
    )
    assert structure_unanchored.source_timestamp_verified is True

    trusted_epoch = 1_800_000_000.0
    verified = analyze_market_frames(
        "XAUUSD",
        {"M1": frame},
        source_includes_forming_bar=True,
        concept_context={"analysis_epoch": trusted_epoch},
    )
    structure_verified = next(
        item for item in verified
        if item.domain == EvidenceDomain.STRUCTURE
    )
    assert structure_verified.timestamp_epoch == trusted_epoch
    assert structure_verified.timestamp_epoch != structure_verified.source_timestamp_epoch
    assert structure_verified.measurements["evidence_timestamp_basis"] == (
        "verified_utc_analysis_cutoff"
    )

def test_per_symbol_cutoff_supports_timeframe_alignment_when_shared_venue_cut_is_unknown():
    cutoff = time.time() + 1.0
    evidence = analyze_market_frames(
        "XAUUSD",
        {"M1": _frame(60), "M5": _frame(300)},
        source_includes_forming_bar=True,
        concept_context={
            "multi_symbol_snapshot_cutoff_epoch": None,
            "analysis_epoch": cutoff,
        },
    )
    alignment = next(
        item for item in evidence
        if item.domain == EvidenceDomain.MULTI_TIMEFRAME
    )
    assert alignment.measurements["synchronized_timestamps"] is True
    assert alignment.measurements["snapshot_cutoff_epoch"] == cutoff
