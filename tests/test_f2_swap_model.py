"""
Tests for F2 Phase 1 — Swap / Rollover Financing Model (Observability Only).

Covers:
  1. Missing rate file → loader returns {} → estimate_swap returns (None, "unavailable").
  2. Non-zero rate → correct single-night, multi-night, direction sign, triple weekday.
  3. Explicit 0.0 rate → (0.0, "modeled") — the zero-vs-unknown proof.
  4. Same-day open/close crossing zero rollover boundaries → (0.0, "modeled").
  5. Night counting edge cases (exact boundary, before/after, multi-day).
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from brain.swap_model import _count_rollover_nights, estimate_swap, load_swap_rates

# ── Loader tests ─────────────────────────────────────────────────────────


def test_missing_rate_file_returns_empty():
    result = load_swap_rates("/nonexistent/path/rates.json")
    assert result == {}


def test_invalid_json_returns_empty():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("{bad json")
        path = f.name
    try:
        result = load_swap_rates(path)
        assert result == {}
    finally:
        os.unlink(path)


def test_non_object_root_returns_empty():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump([1, 2, 3], f)
        path = f.name
    try:
        result = load_swap_rates(path)
        assert result == {}
    finally:
        os.unlink(path)


def test_valid_rate_file_loads():
    data = {"EURUSD": {"long_per_lot_per_night": -6.5, "short_per_lot_per_night": 1.2}}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name
    try:
        result = load_swap_rates(path)
        assert result == data
    finally:
        os.unlink(path)


# ── estimate_swap: unavailable ───────────────────────────────────────────


def test_symbol_not_in_rates_returns_unavailable():
    rates = {"GBPUSD": {"long_per_lot_per_night": -3.0, "short_per_lot_per_night": 0.5}}
    value, status = estimate_swap(
        "EURUSD",
        "BUY",
        1.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rates=rates,
    )
    assert value is None
    assert status == "unavailable"


def test_empty_rates_returns_unavailable():
    value, status = estimate_swap(
        "EURUSD",
        "BUY",
        1.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rates={},
    )
    assert value is None
    assert status == "unavailable"


# ── estimate_swap: zero-vs-unknown proof ─────────────────────────────────


def test_explicit_zero_rate_returns_modeled_zero():
    """A symbol with explicit 0.0 rate returns (0.0, 'modeled'), NOT (None, 'unavailable')."""
    rates = {"EURUSD": {"long_per_lot_per_night": 0.0, "short_per_lot_per_night": 0.0}}
    value, status = estimate_swap(
        "EURUSD",
        "BUY",
        1.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rates=rates,
    )
    assert value == 0.0
    assert status == "modeled"


def test_same_day_no_rollover_returns_modeled_zero():
    """Open and close on the same day before rollover → 0 nights → (0.0, 'modeled')."""
    rates = {"EURUSD": {"long_per_lot_per_night": -6.5, "short_per_lot_per_night": 1.2}}
    value, status = estimate_swap(
        "EURUSD",
        "BUY",
        1.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 2, 18, 0, tzinfo=timezone.utc),
        rates=rates,
        rollover_hour_utc=21,
    )
    assert value == 0.0
    assert status == "modeled"


# ── estimate_swap: single night ──────────────────────────────────────────


def test_single_night_long():
    rates = {"EURUSD": {"long_per_lot_per_night": -6.5, "short_per_lot_per_night": 1.2}}
    value, status = estimate_swap(
        "EURUSD",
        "BUY",
        1.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rates=rates,
        rollover_hour_utc=21,
    )
    assert status == "modeled"
    assert value == -6.5


def test_single_night_short():
    rates = {"EURUSD": {"long_per_lot_per_night": -6.5, "short_per_lot_per_night": 1.2}}
    value, status = estimate_swap(
        "EURUSD",
        "SELL",
        1.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rates=rates,
        rollover_hour_utc=21,
    )
    assert status == "modeled"
    assert value == 1.2


# ── estimate_swap: multi-night ───────────────────────────────────────────


def test_multi_night():
    rates = {"GBPUSD": {"long_per_lot_per_night": -4.0, "short_per_lot_per_night": 0.8}}
    value, status = estimate_swap(
        "GBPUSD",
        "BUY",
        2.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),  # Monday
        datetime(2025, 6, 5, 10, 0, tzinfo=timezone.utc),  # Thursday
        rates=rates,
        rollover_hour_utc=21,
    )
    # Mon 21:00, Tue 21:00, Wed 21:00(triple) = 1+1+3 = 5 nights
    assert status == "modeled"
    assert value == round(-4.0 * 2.0 * 5, 4)


# ── estimate_swap: triple weekday ────────────────────────────────────────


def test_triple_wednesday():
    """Wednesday rollover counts as 3 nights."""
    rates = {"EURUSD": {"long_per_lot_per_night": -10.0, "short_per_lot_per_night": 2.0}}
    # Open Tuesday, close Thursday — crosses Tue 21:00 (1) + Wed 21:00 (3) = 4 nights
    value, status = estimate_swap(
        "EURUSD",
        "BUY",
        1.0,
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),  # Tuesday
        datetime(2025, 6, 5, 10, 0, tzinfo=timezone.utc),  # Thursday
        rates=rates,
        rollover_hour_utc=21,
        triple_weekday=2,
    )
    assert status == "modeled"
    assert value == round(-10.0 * 1.0 * 4, 4)


def test_custom_triple_weekday():
    """Custom triple weekday (Friday=4) counts as 3 on that day."""
    rates = {"EURUSD": {"long_per_lot_per_night": -10.0, "short_per_lot_per_night": 2.0}}
    # Open Thu, close Sat — crosses Thu 21:00 (1) + Fri 21:00 (3) = 4 nights
    value, status = estimate_swap(
        "EURUSD",
        "BUY",
        1.0,
        datetime(2025, 6, 5, 10, 0, tzinfo=timezone.utc),  # Thursday
        datetime(2025, 6, 7, 10, 0, tzinfo=timezone.utc),  # Saturday
        rates=rates,
        rollover_hour_utc=21,
        triple_weekday=4,  # Friday
    )
    assert status == "modeled"
    assert value == round(-10.0 * 1.0 * 4, 4)


# ── estimate_swap: lot scaling ───────────────────────────────────────────


def test_lot_scaling():
    rates = {"XAUUSD": {"long_per_lot_per_night": -20.0, "short_per_lot_per_night": 5.0}}
    value, status = estimate_swap(
        "XAUUSD",
        "SELL",
        0.5,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rates=rates,
        rollover_hour_utc=21,
    )
    assert status == "modeled"
    assert value == round(5.0 * 0.5 * 1, 4)


# ── estimate_swap: case insensitivity ────────────────────────────────────


def test_symbol_case_insensitive():
    rates = {"EURUSD": {"long_per_lot_per_night": -6.5, "short_per_lot_per_night": 1.2}}
    value, status = estimate_swap(
        "eurusd",
        "BUY",
        1.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rates=rates,
        rollover_hour_utc=21,
    )
    assert status == "modeled"
    assert value == -6.5


# ── estimate_swap: direction aliases ─────────────────────────────────────


def test_direction_long_alias():
    rates = {"EURUSD": {"long_per_lot_per_night": -6.5, "short_per_lot_per_night": 1.2}}
    value, status = estimate_swap(
        "EURUSD",
        "LONG",
        1.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rates=rates,
        rollover_hour_utc=21,
    )
    assert status == "modeled"
    assert value == -6.5


def test_direction_short_alias():
    rates = {"EURUSD": {"long_per_lot_per_night": -6.5, "short_per_lot_per_night": 1.2}}
    value, status = estimate_swap(
        "EURUSD",
        "SHORT",
        1.0,
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rates=rates,
        rollover_hour_utc=21,
    )
    assert status == "modeled"
    assert value == 1.2


# ── _count_rollover_nights edge cases ────────────────────────────────────


def test_exact_boundary_open_time_excluded():
    """Open exactly at rollover boundary — that boundary is NOT crossed."""
    nights = _count_rollover_nights(
        datetime(2025, 6, 2, 21, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        rollover_hour_utc=21,
        triple_weekday=2,
    )
    assert nights == 0


def test_exact_boundary_close_time_included():
    """Close exactly at rollover boundary — that boundary IS crossed."""
    nights = _count_rollover_nights(
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 2, 21, 0, tzinfo=timezone.utc),
        rollover_hour_utc=21,
        triple_weekday=2,
    )
    assert nights == 1


def test_close_before_open_returns_zero():
    nights = _count_rollover_nights(
        datetime(2025, 6, 3, 10, 0, tzinfo=timezone.utc),
        datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc),
        rollover_hour_utc=21,
        triple_weekday=2,
    )
    assert nights == 0


def test_naive_datetimes_treated_as_utc():
    """Naive datetimes are assumed UTC."""
    nights = _count_rollover_nights(
        datetime(2025, 6, 2, 10, 0),
        datetime(2025, 6, 3, 10, 0),
        rollover_hour_utc=21,
        triple_weekday=2,
    )
    assert nights == 1
