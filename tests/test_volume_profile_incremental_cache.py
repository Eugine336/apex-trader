"""Incremental weekly volume-profile cache tests."""

from __future__ import annotations

from cognition.concepts import clear_concept_reading_cache, volume_profile


def _bars():
    first_week = 1_704_067_200.0  # 2024-01-01 UTC
    second_week = 1_704_672_000.0  # 2024-01-08 UTC
    return [
        {"time": first_week, "timestamp_epoch": first_week, "source_timestamp_verified": True, "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "tick_volume": 100},
        {"time": first_week + 3600, "timestamp_epoch": first_week + 3600, "source_timestamp_verified": True, "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2, "tick_volume": 110},
        {"time": second_week, "timestamp_epoch": second_week, "source_timestamp_verified": True, "open": 1.2, "high": 1.4, "low": 1.1, "close": 1.3, "tick_volume": 120},
        {"time": second_week + 3600, "timestamp_epoch": second_week + 3600, "source_timestamp_verified": True, "open": 1.3, "high": 1.5, "low": 1.2, "close": 1.4, "tick_volume": 130},
    ]


def _profile(_bars):
    return {
        "poc_concentration": 0.5,
        "vah": 1.4,
        "val": 1.0,
        "poc": 1.2,
        "hvn": (),
        "lvn": (),
        "price_assignment": "OHLCV_PROXY",
        "volume_basis": "tick_volume_proxy",
    }


def test_weekly_profiles_are_reused_for_unchanged_history(monkeypatch):
    calls = []
    monkeypatch.setattr(
        volume_profile,
        "profile_from_bars",
        lambda bars: calls.append(tuple(row["time"] for row in bars)) or _profile(bars),
    )
    volume_profile.clear_volume_profile_cache()
    reader = volume_profile.VolumeProfileConcept()
    bars = _bars()

    first = reader.read("EURUSD", {"H1": bars})[0]
    assert len(calls) == 2
    assert first.facts["weekly_profile_cache_misses"] == 2
    assert first.facts["weekly_profile_cache_hits"] == 1
    first.facts["current_week_profile"]["poc"] = 99.0

    second = reader.read("EURUSD", {"H1": bars})[0]
    assert second.facts["current_week_profile"]["poc"] == 1.2
    assert len(calls) == 2
    assert second.facts["weekly_profile_cache_misses"] == 0
    assert second.facts["weekly_profile_cache_hits"] == 3

    volume_profile.clear_volume_profile_cache()


def test_appending_a_bar_recomputes_only_the_active_week(monkeypatch):
    calls = []
    monkeypatch.setattr(
        volume_profile,
        "profile_from_bars",
        lambda bars: calls.append(tuple(row["time"] for row in bars)) or _profile(bars),
    )
    volume_profile.clear_volume_profile_cache()
    reader = volume_profile.VolumeProfileConcept()
    bars = _bars()
    reader.read("EURUSD", {"H1": bars})
    calls.clear()

    appended = bars + [{
        "time": bars[-1]["time"] + 3600,
        "timestamp_epoch": bars[-1]["timestamp_epoch"] + 3600,
        "source_timestamp_verified": True,
        "open": 1.4, "high": 1.6, "low": 1.3, "close": 1.5,
        "tick_volume": 140,
    }]
    reading = reader.read("EURUSD", {"H1": appended})[0]

    assert len(calls) == 1
    assert reading.facts["weekly_profile_cache_misses"] == 1
    assert reading.facts["weekly_profile_cache_hits"] == 2
    volume_profile.clear_volume_profile_cache()


def test_historical_correction_invalidates_only_its_week(monkeypatch):
    calls = []
    monkeypatch.setattr(
        volume_profile,
        "profile_from_bars",
        lambda bars: calls.append(tuple(row["time"] for row in bars)) or _profile(bars),
    )
    volume_profile.clear_volume_profile_cache()
    reader = volume_profile.VolumeProfileConcept()
    bars = _bars()
    reader.read("EURUSD", {"H1": bars})
    calls.clear()

    corrected = [dict(row) for row in bars]
    corrected[0]["high"] = 1.25
    reading = reader.read("EURUSD", {"H1": corrected})[0]

    assert len(calls) == 1
    assert reading.facts["weekly_profile_cache_misses"] == 1
    assert reading.facts["weekly_profile_cache_hits"] == 2
    volume_profile.clear_volume_profile_cache()


def test_shared_concept_cache_clear_also_clears_weekly_profile_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(
        volume_profile,
        "profile_from_bars",
        lambda bars: calls.append(tuple(row["time"] for row in bars)) or _profile(bars),
    )
    volume_profile.clear_volume_profile_cache()
    reader = volume_profile.VolumeProfileConcept()
    bars = _bars()
    reader.read("EURUSD", {"H1": bars})
    assert len(calls) == 2

    clear_concept_reading_cache()
    reader.read("EURUSD", {"H1": bars})
    assert len(calls) == 4
    volume_profile.clear_volume_profile_cache()
