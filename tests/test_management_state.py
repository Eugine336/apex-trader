"""Tests for execution.management_state module."""

import threading
from datetime import datetime, timezone

from execution.management_state import ManagementState, ManagementStateStore


class TestManagementState:
    def test_defaults(self):
        ms = ManagementState(ticket="T1")
        assert ms.ticket == "T1"
        assert ms.tp1_hit is False
        assert ms.at_breakeven is False
        assert ms.trailing is False
        assert ms.partial_closed is False
        assert ms.highest_price_since_entry == 0.0
        assert ms.candles_since_entry == 0
        assert ms.status == "OPEN"

    def test_custom_init(self):
        ms = ManagementState(ticket="T2", original_stop_loss=1.05, tp1_hit=True)
        assert ms.original_stop_loss == 1.05
        assert ms.tp1_hit is True


class TestManagementStateStore:
    def test_get_or_create_new(self):
        store = ManagementStateStore()
        ms = store.get_or_create("T1", original_stop_loss=1.10)
        assert ms.ticket == "T1"
        assert ms.original_stop_loss == 1.10

    def test_get_or_create_existing(self):
        store = ManagementStateStore()
        ms1 = store.get_or_create("T1", original_stop_loss=1.10)
        ms1.tp1_hit = True
        ms2 = store.get_or_create("T1", original_stop_loss=9.99)
        assert ms2.tp1_hit is True
        assert ms2.original_stop_loss == 1.10  # first value kept

    def test_get_missing(self):
        store = ManagementStateStore()
        assert store.get("nonexistent") is None

    def test_update(self):
        store = ManagementStateStore()
        store.get_or_create("T1")
        store.update("T1", at_breakeven=True, trailing=True)
        ms = store.get("T1")
        assert ms.at_breakeven is True
        assert ms.trailing is True

    def test_update_missing_ticket_no_error(self):
        store = ManagementStateStore()
        store.update("missing", at_breakeven=True)

    def test_remove(self):
        store = ManagementStateStore()
        store.get_or_create("T1")
        store.remove("T1")
        assert store.get("T1") is None
        assert len(store) == 0

    def test_cleanup(self):
        store = ManagementStateStore()
        store.get_or_create("T1")
        store.get_or_create("T2")
        store.get_or_create("T3")
        removed = store.cleanup({"T1", "T3"})
        assert removed == 1
        assert store.get("T2") is None
        assert len(store) == 2

    def test_all_tickets(self):
        store = ManagementStateStore()
        store.get_or_create("A")
        store.get_or_create("B")
        assert store.all_tickets() == {"A", "B"}

    def test_thread_safety(self):
        store = ManagementStateStore()
        errors = []

        def writer(prefix, count):
            try:
                for i in range(count):
                    t = f"{prefix}-{i}"
                    store.get_or_create(t)
                    store.update(t, at_breakeven=True)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(f"w{n}", 50)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store) == 200
