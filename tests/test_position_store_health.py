"""Tests for PositionStore health tracking, STORE_UNAVAILABLE sentinel,
and get_in_flight_checked() — M3 Phase B (Option C).

Self-contained: creates temporary SQLite DBs, requires no external services.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from persistence.position_store import (
    STORE_UNAVAILABLE,
    PositionStore,
    _StoreUnavailableSentinel,
)


@pytest.fixture
def tmp_store(tmp_path):
    """Create a PositionStore backed by a temp file."""
    db_path = str(tmp_path / "test.db")
    return PositionStore(db_path=db_path)


@pytest.fixture
def broken_store(tmp_path):
    """Create a PositionStore, then break its connection so all ops fail."""
    db_path = str(tmp_path / "broken.db")
    store = PositionStore(db_path=db_path)
    store._conn.close()
    store._conn = None
    return store


# ── STORE_UNAVAILABLE sentinel ──────────────────────────────────────────

class TestStoreUnavailableSentinel:
    def test_singleton(self):
        a = _StoreUnavailableSentinel()
        b = _StoreUnavailableSentinel()
        assert a is b
        assert a is STORE_UNAVAILABLE

    def test_repr(self):
        assert repr(STORE_UNAVAILABLE) == "STORE_UNAVAILABLE"

    def test_falsy(self):
        assert not STORE_UNAVAILABLE
        assert bool(STORE_UNAVAILABLE) is False

    def test_is_not_none(self):
        assert STORE_UNAVAILABLE is not None

    def test_identity_check(self):
        assert STORE_UNAVAILABLE is STORE_UNAVAILABLE


# ── Health tracking on count() ──────────────────────────────────────────

class TestCountHealth:
    def test_healthy_after_successful_count(self, tmp_store):
        result = tmp_store.count()
        assert result == 0
        assert tmp_store.is_healthy() is True
        assert tmp_store.degraded_reason() == ""

    def test_degraded_after_failed_count(self, broken_store):
        result = broken_store.count()
        assert result == 0
        assert broken_store.is_healthy() is False
        assert broken_store._consecutive_failures == 1
        reason = broken_store.degraded_reason()
        assert "consecutive_failures=1" in reason
        assert "count:" in reason

    def test_consecutive_failures_increment(self, broken_store):
        broken_store.count()
        broken_store.count()
        broken_store.count()
        assert broken_store._consecutive_failures == 3
        assert broken_store.is_healthy() is False

    def test_health_resets_after_success(self, tmp_path):
        db_path = str(tmp_path / "reset.db")
        store = PositionStore(db_path=db_path)
        store._conn.close()
        store._conn = None
        store.count()
        assert store.is_healthy() is False
        assert store._consecutive_failures == 1

        store._conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        store._conn.execute("PRAGMA journal_mode=WAL")
        result = store.count()
        assert result == 0
        assert store.is_healthy() is True
        assert store._consecutive_failures == 0
        assert store.degraded_reason() == ""
        store.close()


# ── get_in_flight_checked() ─────────────────────────────────────────────

class TestGetInFlightChecked:
    def test_returns_none_on_genuinely_absent(self, tmp_store):
        result = tmp_store.get_in_flight_checked("nonexistent-key")
        assert result is None
        assert tmp_store.is_healthy() is True

    def test_returns_dict_on_present(self, tmp_store):
        tmp_store.record_in_flight("key-1", "EURUSD", "BUY", 0.1)
        result = tmp_store.get_in_flight_checked("key-1")
        assert isinstance(result, dict)
        assert result["idempotency_key"] == "key-1"
        assert result["symbol"] == "EURUSD"
        assert tmp_store.is_healthy() is True

    def test_returns_store_unavailable_on_db_error(self, broken_store):
        result = broken_store.get_in_flight_checked("any-key")
        assert result is STORE_UNAVAILABLE
        assert broken_store.is_healthy() is False

    def test_store_unavailable_is_not_none(self, broken_store):
        result = broken_store.get_in_flight_checked("any-key")
        assert result is not None
        assert result is STORE_UNAVAILABLE

    def test_store_unavailable_is_falsy_but_distinct(self, broken_store):
        result = broken_store.get_in_flight_checked("any-key")
        assert not result
        assert result is not None
        assert result is not False
        assert result is STORE_UNAVAILABLE


# ── get_in_flight() backward compat ─────────────────────────────────────

class TestGetInFlightBackwardCompat:
    def test_returns_none_on_absent(self, tmp_store):
        assert tmp_store.get_in_flight("nope") is None

    def test_returns_none_on_error(self, broken_store):
        assert broken_store.get_in_flight("any") is None

    def test_returns_dict_on_present(self, tmp_store):
        tmp_store.record_in_flight("k", "XAUUSD", "SELL", 0.05)
        rec = tmp_store.get_in_flight("k")
        assert isinstance(rec, dict)
        assert rec["symbol"] == "XAUUSD"


# ── Write-path health tracking ──────────────────────────────────────────

class TestWritePathHealth:
    def test_save_failure_marks_degraded(self, broken_store):
        from unittest.mock import MagicMock
        pos = MagicMock()
        pos.order_id = "ord-1"
        pos.open_time = datetime.now(timezone.utc)
        broken_store.save_position(pos)
        assert broken_store.is_healthy() is False

    def test_remove_failure_marks_degraded(self, broken_store):
        broken_store.remove_position("ord-1")
        assert broken_store.is_healthy() is False

    def test_record_in_flight_failure(self, broken_store):
        broken_store.record_in_flight("k", "EURUSD", "BUY", 0.1)
        assert broken_store.is_healthy() is False

    def test_resolve_in_flight_failure(self, broken_store):
        broken_store.resolve_in_flight("k", "ord-1")
        assert broken_store.is_healthy() is False

    def test_cancel_in_flight_failure(self, broken_store):
        broken_store.cancel_in_flight("k")
        assert broken_store.is_healthy() is False

    def test_cleanup_stale_failure(self, broken_store):
        broken_store.cleanup_stale_in_flight()
        assert broken_store.is_healthy() is False


# ── Startup check degraded-store path ───────────────────────────────────

class TestStartupCheckDegradedStore:
    def test_degraded_store_warns_on_zero_count(self, tmp_path):
        from platforms.startup_check import StartupCheck
        db_path = str(tmp_path / "degraded.db")

        with patch("platforms.startup_check.PositionStore") as MockStore:
            instance = MockStore.return_value
            instance.count.return_value = 0
            instance.is_healthy.return_value = False
            instance.degraded_reason.return_value = (
                "consecutive_failures=1, last_error=count: disk I/O error"
            )
            instance.close.return_value = None

            check = StartupCheck()
            result = check._check_database()
            assert result.passed is True
            assert "DEGRADED" in result.message
            assert "may be inaccurate" in result.message

    def test_healthy_store_normal_message(self, tmp_path):
        from platforms.startup_check import StartupCheck
        db_path = str(tmp_path / "healthy.db")

        with patch("platforms.startup_check.PositionStore") as MockStore:
            instance = MockStore.return_value
            instance.count.return_value = 3
            instance.is_healthy.return_value = True
            instance.close.return_value = None

            check = StartupCheck()
            result = check._check_database()
            assert result.passed is True
            assert "3 persisted positions" in result.message
            assert "DEGRADED" not in result.message
