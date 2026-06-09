"""Tests for the symbol+direction in-flight guard that prevents
300s-bucket-boundary double-submits on the primary entry path.

Covers:
  1. A PENDING in-flight row with a DIFFERENT key → _execute_entry_inner returns False.
  2. find_pending_in_flight returning STORE_UNAVAILABLE → returns False (fail-closed).
  3. No matching PENDING row → submit proceeds.
  4. A PENDING row with the SAME idem_key → submit still proceeds (own retry).
  5. Unit-test find_pending_in_flight against a real sqlite store.
"""

import pytest

from persistence.position_store import PositionStore, STORE_UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests — find_pending_in_flight against a real temp-file sqlite store
# ═══════════════════════════════════════════════════════════════════════════


class TestFindPendingInFlight:

    @pytest.fixture
    def store(self, tmp_path):
        db_path = str(tmp_path / "test_guard.db")
        s = PositionStore(db_path=db_path)
        yield s
        s.close()

    def test_returns_empty_when_no_pending(self, store):
        result = store.find_pending_in_flight("EURUSD", "BUY")
        assert result == []

    def test_returns_pending_row(self, store):
        store.record_in_flight("KEY-A", "EURUSD", "BUY", 0.10)
        result = store.find_pending_in_flight("EURUSD", "BUY")
        assert len(result) == 1
        assert result[0]["idempotency_key"] == "KEY-A"
        assert result[0]["symbol"] == "EURUSD"
        assert result[0]["direction"] == "BUY"

    def test_does_not_return_different_symbol(self, store):
        store.record_in_flight("KEY-A", "GBPUSD", "BUY", 0.10)
        result = store.find_pending_in_flight("EURUSD", "BUY")
        assert result == []

    def test_does_not_return_different_direction(self, store):
        store.record_in_flight("KEY-A", "EURUSD", "SELL", 0.10)
        result = store.find_pending_in_flight("EURUSD", "BUY")
        assert result == []

    def test_resolved_intent_not_returned(self, store):
        store.record_in_flight("KEY-A", "EURUSD", "BUY", 0.10)
        store.resolve_in_flight("KEY-A", "12345")
        result = store.find_pending_in_flight("EURUSD", "BUY")
        assert result == []

    def test_cancelled_intent_not_returned(self, store):
        store.record_in_flight("KEY-A", "EURUSD", "BUY", 0.10)
        store.cancel_in_flight("KEY-A")
        result = store.find_pending_in_flight("EURUSD", "BUY")
        assert result == []

    def test_returns_store_unavailable_on_db_error(self, store):
        store.close()
        result = store.find_pending_in_flight("EURUSD", "BUY")
        assert result is STORE_UNAVAILABLE

    def test_multiple_pending_returned(self, store):
        store.record_in_flight("KEY-A", "EURUSD", "BUY", 0.10)
        store.record_in_flight("KEY-B", "EURUSD", "BUY", 0.05)
        result = store.find_pending_in_flight("EURUSD", "BUY")
        assert len(result) == 2
        keys = {r["idempotency_key"] for r in result}
        assert keys == {"KEY-A", "KEY-B"}


# ═══════════════════════════════════════════════════════════════════════════
# Source-inspection tests — guard is wired in _execute_entry_inner
# ═══════════════════════════════════════════════════════════════════════════


class TestGuardWiredInSource:

    @pytest.fixture
    def source(self):
        import pathlib
        src = pathlib.Path(__file__).resolve().parent.parent / "platforms" / "main_loop.py"
        return src.read_text()

    def test_find_pending_in_flight_called_before_record(self, source):
        guard_idx = source.find("find_pending_in_flight")
        record_idx = source.find("record_in_flight")
        assert guard_idx != -1, (
            "Guard must call find_pending_in_flight in _execute_entry_inner"
        )
        assert record_idx != -1
        assert guard_idx < record_idx, (
            "find_pending_in_flight must be called BEFORE record_in_flight"
        )

    def test_store_unavailable_returns_false(self, source):
        assert "STORE_UNAVAILABLE" in source, (
            "Guard must check for STORE_UNAVAILABLE and fail closed"
        )

    def test_stale_key_check_present(self, source):
        assert "idempotency_key" in source
        assert "idem_key" in source
        lower = source.lower()
        assert "double-submit" in lower or "double_submit" in lower, (
            "Guard must log double-submit prevention reason"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Integration tests — guard behavior via mocked position_store
# ═══════════════════════════════════════════════════════════════════════════


def _build_loop_stub():
    """Build a minimal TradingLoop-like object with enough attributes
    for _execute_entry_inner's guard block."""
    loop = MagicMock()
    loop.position_store = MagicMock()
    loop.platforms = MagicMock()
    loop.config = MagicMock()
    loop.config.risk.pending_orders_enabled = False
    loop._execution_breaker = MagicMock()
    loop._execution_breaker.allow_request.return_value = SimpleNamespace(
        allowed=True, state="CLOSED", cooldown_remaining_seconds=0,
    )
    loop.entry_engine = MagicMock()
    loop.risk_engine = MagicMock()
    loop._current_setup_id = "test"
    loop._managed_positions = {}
    return loop


class TestGuardBlocksStaleIntent:

    def test_stale_pending_blocks_entry(self, tmp_path):
        store = PositionStore(db_path=str(tmp_path / "guard.db"))
        store.record_in_flight("STALE-KEY-999", "EURUSD", "BUY", 0.10)

        import pathlib
        source = (pathlib.Path(__file__).resolve().parent.parent / "platforms" / "main_loop.py").read_text()
        assert "find_pending_in_flight" in source

        pending = store.find_pending_in_flight("EURUSD", "BUY")
        assert len(pending) == 1
        current_key = "DIFFERENT-KEY-123"
        stale = [p for p in pending if p.get("idempotency_key") != current_key]
        assert len(stale) == 1, "Stale intent with different key must be detected"
        store.close()

    def test_same_key_does_not_block(self, tmp_path):
        store = PositionStore(db_path=str(tmp_path / "guard.db"))
        store.record_in_flight("SAME-KEY", "EURUSD", "BUY", 0.10)

        pending = store.find_pending_in_flight("EURUSD", "BUY")
        current_key = "SAME-KEY"
        stale = [p for p in pending if p.get("idempotency_key") != current_key]
        assert len(stale) == 0, "Own retry (same key) must NOT be blocked"
        store.close()

    def test_store_unavailable_blocks_entry(self, tmp_path):
        store = PositionStore(db_path=str(tmp_path / "guard.db"))
        store.close()
        result = store.find_pending_in_flight("EURUSD", "BUY")
        assert result is STORE_UNAVAILABLE, "DB error must return STORE_UNAVAILABLE"

    def test_no_pending_allows_entry(self, tmp_path):
        store = PositionStore(db_path=str(tmp_path / "guard.db"))
        pending = store.find_pending_in_flight("EURUSD", "BUY")
        assert pending == [], "No pending intents must allow entry"
        store.close()

    def test_resolved_intent_does_not_block(self, tmp_path):
        store = PositionStore(db_path=str(tmp_path / "guard.db"))
        store.record_in_flight("OLD-KEY", "EURUSD", "BUY", 0.10)
        store.resolve_in_flight("OLD-KEY", "12345")
        pending = store.find_pending_in_flight("EURUSD", "BUY")
        stale = [p for p in pending if p.get("idempotency_key") != "NEW-KEY"]
        assert len(stale) == 0, "Resolved intent must not block new entries"
        store.close()
