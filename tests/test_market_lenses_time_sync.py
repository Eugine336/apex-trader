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
    return pd.DataFrame(rows)


def _alignment(frames):
    evidence = analyze_market_frames(
        "XAUUSD", frames, source_includes_forming_bar=True
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
