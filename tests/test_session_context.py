"""
Tests for the global session context (brain/session_context.py).

Coverage:
  * get_session — each of the four TradingSessions (ASIAN / LONDON / NY /
    LONDON_NY_OVERLAP), the London↔NY overlap detection, and the boundary
    edge cases at every window transition (inclusive-start / exclusive-end).
  * Timezone handling — naive datetimes assumed UTC, tz-aware datetimes
    converted to UTC, and the wrap-around Asian window.
  * Custom SessionWindows — re-tuned windows re-shape the classification.
  * get_session_multiplier / get_session_zone_weight — profile override vs
    EntryConfig fallback vs module default, unknown-session neutral fallback.
  * InstrumentProfile / EntryConfig default tables and the vwap_zone_enabled
    prep flag.

Deterministic: hand-built UTC datetimes; no network, disk, or wall-clock reads.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from brain.instrument_profile import get_profile
from brain.session_context import (
    SessionContext,
    SessionWindows,
    TradingSession,
    _DEFAULT_SESSION_SIZE_MULTIPLIERS,
    _DEFAULT_SESSION_ZONE_WEIGHTS,
)
from entry.models import EntryConfig


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utc(hour, minute=0):
    """A fixed UTC datetime at the given hour/minute (date is irrelevant)."""
    return datetime(2026, 5, 25, hour, minute, tzinfo=timezone.utc)


def _ctx(**kw):
    return SessionContext(**kw)


# ── get_session — the four sessions ──────────────────────────────────────────

def test_classifies_asian_after_midnight():
    assert _ctx().get_session(_utc(3)) is TradingSession.ASIAN


def test_classifies_asian_before_midnight():
    # 22:00 UTC is inside the wrap-around Asian window (21:00 → 07:00).
    assert _ctx().get_session(_utc(22)) is TradingSession.ASIAN


def test_classifies_london():
    assert _ctx().get_session(_utc(9)) is TradingSession.LONDON


def test_classifies_ny_after_overlap():
    # 18:00 UTC — NY is live but the London/NY overlap has ended.
    assert _ctx().get_session(_utc(18)) is TradingSession.NY


def test_classifies_overlap():
    # 13:00 UTC sits inside 12:00–16:00, the London/NY overlap.
    assert _ctx().get_session(_utc(13)) is TradingSession.LONDON_NY_OVERLAP


def test_overlap_takes_priority_over_ny():
    # The overlap window sits INSIDE the NY window; the more-liquid overlap
    # must win rather than being reported as plain NY.
    ctx = _ctx()
    assert ctx.get_session(_utc(12, 30)) is TradingSession.LONDON_NY_OVERLAP
    assert ctx.get_session(_utc(15, 59)) is TradingSession.LONDON_NY_OVERLAP


# ── Boundary edge cases (inclusive start, exclusive end) ─────────────────────

def test_boundary_asian_to_london_at_0700():
    ctx = _ctx()
    assert ctx.get_session(_utc(6, 59)) is TradingSession.ASIAN
    assert ctx.get_session(_utc(7, 0)) is TradingSession.LONDON


def test_boundary_london_to_overlap_at_1200():
    ctx = _ctx()
    assert ctx.get_session(_utc(11, 59)) is TradingSession.LONDON
    assert ctx.get_session(_utc(12, 0)) is TradingSession.LONDON_NY_OVERLAP


def test_boundary_overlap_to_ny_at_1600():
    ctx = _ctx()
    assert ctx.get_session(_utc(15, 59)) is TradingSession.LONDON_NY_OVERLAP
    assert ctx.get_session(_utc(16, 0)) is TradingSession.NY


def test_boundary_ny_to_asian_at_2100():
    ctx = _ctx()
    assert ctx.get_session(_utc(20, 59)) is TradingSession.NY
    assert ctx.get_session(_utc(21, 0)) is TradingSession.ASIAN


def test_midnight_is_asian():
    assert _ctx().get_session(_utc(0, 0)) is TradingSession.ASIAN


# ── Timezone handling ────────────────────────────────────────────────────────

def test_naive_datetime_assumed_utc():
    # A naive 09:00 is treated as 09:00 UTC → LONDON.
    naive = datetime(2026, 5, 25, 9, 0)
    assert _ctx().get_session(naive) is TradingSession.LONDON


def test_tzaware_datetime_converted_to_utc():
    # 14:00 in UTC+2 == 12:00 UTC → the overlap.
    aware = datetime(2026, 5, 25, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert _ctx().get_session(aware) is TradingSession.LONDON_NY_OVERLAP


def test_get_session_defaults_to_now():
    # No argument → uses current UTC time and still returns a valid session.
    assert isinstance(_ctx().get_session(), TradingSession)


# ── Custom windows re-shape the classification ───────────────────────────────

def test_custom_windows_reshape_sessions():
    # Shift London to start at 06:00; 06:30 becomes LONDON instead of ASIAN.
    windows = SessionWindows(asian_end=6, london_start=6)
    ctx = _ctx(windows=windows)
    assert ctx.get_session(_utc(6, 30)) is TradingSession.LONDON
    # The stock windows still classify 06:30 as ASIAN — proving the config,
    # not a hardcoded literal, drove the change.
    assert _ctx().get_session(_utc(6, 30)) is TradingSession.ASIAN


# ── get_session_multiplier: profile vs fallback ──────────────────────────────

def test_multiplier_prefers_profile_over_entry_config():
    ctx = _ctx(
        entry_config=EntryConfig(session_size_multipliers={"LONDON": 1.2}),
        profile_lookup=lambda _s: SimpleNamespace(
            session_size_multipliers={"LONDON": 2.5},
        ),
    )
    assert ctx.get_session_multiplier("X", TradingSession.LONDON) == 2.5


def test_multiplier_falls_back_to_entry_config():
    # Profile lacks the table → EntryConfig default is used.
    ctx = _ctx(
        entry_config=EntryConfig(session_size_multipliers={"LONDON": 1.9}),
        profile_lookup=lambda _s: SimpleNamespace(),
    )
    assert ctx.get_session_multiplier("X", TradingSession.LONDON) == 1.9


def test_multiplier_falls_back_to_module_default():
    # Neither profile nor entry_config supplies the table → module default.
    ctx = _ctx(entry_config=SimpleNamespace(), profile_lookup=lambda _s: None)
    assert ctx.get_session_multiplier("X", TradingSession.ASIAN) == pytest.approx(
        _DEFAULT_SESSION_SIZE_MULTIPLIERS["ASIAN"]
    )


def test_multiplier_accepts_string_session():
    ctx = _ctx(profile_lookup=lambda _s: None, entry_config=SimpleNamespace())
    assert ctx.get_session_multiplier("X", "LONDON") == pytest.approx(
        _DEFAULT_SESSION_SIZE_MULTIPLIERS["LONDON"]
    )


def test_multiplier_unknown_session_is_neutral():
    ctx = _ctx()
    assert ctx.get_session_multiplier("EURUSD", "NOT_A_SESSION") == 1.0


def test_multiplier_reads_real_profile_defaults():
    # The real InstrumentProfile path returns the profile's default table.
    ctx = _ctx()
    assert ctx.get_session_multiplier("EURUSD", TradingSession.ASIAN) == pytest.approx(0.5)
    assert ctx.get_session_multiplier("EURUSD", TradingSession.LONDON_NY_OVERLAP) == pytest.approx(1.3)


# ── get_session_zone_weight: profile vs fallback ─────────────────────────────

def test_zone_weight_prefers_profile():
    ctx = _ctx(
        profile_lookup=lambda _s: SimpleNamespace(
            session_zone_weights={"NY": 1.4},
        ),
    )
    assert ctx.get_session_zone_weight("X", TradingSession.NY) == 1.4


def test_zone_weight_module_default():
    ctx = _ctx(entry_config=SimpleNamespace(), profile_lookup=lambda _s: None)
    assert ctx.get_session_zone_weight("X", TradingSession.LONDON) == pytest.approx(
        _DEFAULT_SESSION_ZONE_WEIGHTS["LONDON"]
    )


def test_zone_weight_unknown_session_is_neutral():
    assert _ctx().get_session_zone_weight("EURUSD", "NOT_A_SESSION") == 1.0


# ── describe() observability snapshot ────────────────────────────────────────

def test_describe_returns_session_and_tuning():
    snap = _ctx().describe("EURUSD", _utc(13))
    assert snap["session"] == "LONDON_NY_OVERLAP"
    assert snap["size_multiplier"] == pytest.approx(1.3)
    assert snap["zone_weight"] == pytest.approx(
        _DEFAULT_SESSION_ZONE_WEIGHTS["LONDON_NY_OVERLAP"]
    )


# ── Resilience: a broken profile lookup never raises ─────────────────────────

def test_broken_profile_lookup_is_neutral():
    def _boom(_s):
        raise RuntimeError("profile lookup exploded")

    ctx = _ctx(entry_config=SimpleNamespace(), profile_lookup=_boom)
    # Falls through to the module default rather than propagating.
    assert ctx.get_session_multiplier("X", TradingSession.LONDON) == pytest.approx(
        _DEFAULT_SESSION_SIZE_MULTIPLIERS["LONDON"]
    )


# ── Profile / config default tables & the VWAP prep flag ─────────────────────

def test_instrument_profile_has_session_defaults():
    prof = get_profile("EURUSD")
    assert prof.session_size_multipliers == _DEFAULT_SESSION_SIZE_MULTIPLIERS
    assert prof.session_zone_weights == _DEFAULT_SESSION_ZONE_WEIGHTS


def test_entry_config_has_session_defaults():
    cfg = EntryConfig()
    assert cfg.session_size_multipliers == _DEFAULT_SESSION_SIZE_MULTIPLIERS
    assert cfg.session_zone_weights == _DEFAULT_SESSION_ZONE_WEIGHTS


def test_vwap_zone_enabled_defaults_off():
    assert EntryConfig().vwap_zone_enabled is False
    assert get_profile("EURUSD").vwap_zone_enabled is False


def test_instrument_profiles_get_independent_default_dicts():
    # frozen-dataclass default_factory must give each profile its own dict copy,
    # never a shared mutable default.
    a = get_profile("EURUSD")
    b = get_profile("XAUUSD")
    assert a.session_size_multipliers == b.session_size_multipliers
    assert a.session_size_multipliers is not b.session_size_multipliers
