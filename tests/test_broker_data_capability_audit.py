from __future__ import annotations

import pandas as pd

from scripts.audit_broker_data_capabilities import (
    _finite,
    _frame_quality,
    _history_summary,
    _acceptance_summary,
)


def _frame(times: list[int], *, symbol: str = "XAUUSD") -> pd.DataFrame:
    frame = pd.DataFrame({
        "time": pd.to_datetime(times, unit="s", utc=True),
        "open": [1.0] * len(times),
        "high": [2.0] * len(times),
        "low": [0.5] * len(times),
        "close": [1.5] * len(times),
    })
    frame.attrs.update({
        "source_timestamp_verified": True,
        "source_timestamp_origin": "broker_ohlcv",
        "source_symbol": symbol,
        "volume_basis": "tick_volume_alias_with_real_volume_preserved",
        "bar_fields": ["tick_volume", "real_volume", "spread"],
    })
    return frame


def test_frame_quality_accepts_attested_ordered_completed_bars():
    result = _frame_quality(_frame([60, 120, 180]), "XAUUSD", "M1")

    assert result["status"] == "AVAILABLE"
    assert result["rows"] == 3
    assert result["timestamps_parseable"] is True
    assert result["timestamps_strictly_increasing"] is True
    assert result["duplicate_timestamp_count"] == 0
    assert result["interval_gap_count"] == 0
    assert result["source_timestamp_verified"] is True
    assert result["source_symbol_matches"] is True


def test_frame_quality_flags_duplicates_and_interval_gaps():
    result = _frame_quality(_frame([60, 120, 120, 300]), "XAUUSD", "M1")

    assert result["timestamps_strictly_increasing"] is False
    assert result["duplicate_timestamp_count"] == 1
    assert result["interval_gap_count"] == 1


def test_frame_quality_does_not_attest_wrong_or_missing_symbol():
    wrong = _frame([60, 120], symbol="EURUSD")
    missing = _frame([60, 120])
    missing.attrs.pop("source_symbol")

    assert _frame_quality(wrong, "XAUUSD", "M1")["source_symbol_matches"] is False
    assert _frame_quality(missing, "XAUUSD", "M1")["source_symbol_matches"] is False


def test_history_summary_counts_records_without_exposing_them():
    result = _history_summary([
        {"time": 120, "ticket": "sensitive"},
        {"time": 60, "ticket": "sensitive"},
    ], ("time",))

    assert result == {
        "status": "AVAILABLE",
        "record_count": 2,
        "earliest_source_epoch": 60.0,
        "latest_source_epoch": 120.0,
        "timestamped_record_count": 2,
    }


def test_finite_rejects_boolean_and_non_finite_values():
    assert _finite(True) is None
    assert _finite(float("nan")) is None
    assert _finite("12.5") == 12.5



def test_broker_acceptance_requires_verified_account_books_and_every_requested_symbol_timeframe():
    terminal = {"value": {"source_verified": True, "connected": True}, "error": None}
    account = {"value": {"source_verified": True}, "error": None}
    positions = {"value": [], "error": None}
    orders = {"value": [], "error": None}
    deals = {"value": [], "error": None}
    symbols = {
        "XAUUSD": {
            "status": "PARTIAL", "broker_symbol": "GOLD",
            "instrument_specification": {"specification_verified": True},
            "timeframes": {
                "M5": {"status": "AVAILABLE", "source_timestamp_verified": True,
                       "source_symbol_matches": True, "timestamps_parseable": True,
                       "timestamps_strictly_increasing": True},
                "H1": {"status": "AVAILABLE", "source_timestamp_verified": True,
                       "source_symbol_matches": True, "timestamps_parseable": True,
                       "timestamps_strictly_increasing": True},
            },
        },
    }
    accepted = _acceptance_summary(
        terminal=terminal, account=account, positions=positions, orders=orders,
        deals=deals, symbols=symbols, configured_symbols=["XAUUSD"],
        timeframes=["M5", "H1"],
    )
    assert accepted["status"] == "complete"
    assert accepted["execution_mutations_performed"] == 0

    symbols["XAUUSD"]["timeframes"]["H1"]["source_symbol_matches"] = False
    rejected = _acceptance_summary(
        terminal=terminal, account=account, positions=positions, orders=orders,
        deals=deals, symbols=symbols, configured_symbols=["XAUUSD"],
        timeframes=["M5", "H1"],
    )
    assert rejected["status"] == "partial"
    assert "completed_ohlcv_complete" in rejected["missing_acceptance_checks"]
