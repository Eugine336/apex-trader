"""
APEX TRADER — Phase 2 Tests: Correlation IDs + Typed Domain Events
Verifies that:
  1. All events in a scan cycle share the same non-null cycle_id.
  2. DECISION_REJECT carries correlation_id, setup_id, and entry context.
  3. ORDER_SENT + ORDER_FILLED share setup_id linked to cycle_id.
  4. Un-serializable payload does NOT propagate an exception.
"""

import json
import tempfile
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional, Union
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from persistence.event_store import EventStore, new_cycle_id, new_setup_id, get_event_store
from persistence.domain_events import DECISION_REJECT, ORDER_SENT, ORDER_FILLED, TRADE_OPEN


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_store(tmp_path):
    db = str(tmp_path / "test_events.db")
    return EventStore(db_path=db)


def _flush_and_query(store, **kwargs):
    store.flush(timeout=3.0)
    return store.query(**kwargs)


# ── Test 1: Cycle-level correlation ─────────────────────────────────────────

class TestCycleCorrelation:
    def test_all_events_in_cycle_share_cycle_id(self, tmp_path):
        """Simulate a scan cycle: emit multiple events with the same cycle_id.
        All should share a non-null correlation_id."""
        store = _make_store(tmp_path)
        try:
            cid = new_cycle_id()
            assert cid.startswith("cycle-")

            store.emit("LOG", "INFO", correlation_id=cid, payload={"msg": "cycle start"})
            store.emit(DECISION_REJECT, "INFO", symbol="EURUSD", correlation_id=cid,
                       payload={"reason": "test reject"})
            store.emit("LOG", "INFO", correlation_id=cid, payload={"msg": "cycle end"})

            events = _flush_and_query(store, correlation_id=cid)
            assert len(events) == 3
            for ev in events:
                assert ev["correlation_id"] == cid
                assert ev["correlation_id"] is not None
        finally:
            store.close()


# ── Test 2: DECISION_REJECT with setup context ──────────────────────────────

class TestDecisionRejectEvent:
    def test_reject_carries_ids_and_entry_context(self, tmp_path):
        """DECISION_REJECT should have non-null correlation_id, setup_id (parent_id),
        and entry_price + stop_loss in payload when available."""
        store = _make_store(tmp_path)
        try:
            cid = new_cycle_id()
            sid = new_setup_id()
            assert sid.startswith("setup-")

            store.emit(
                event_type=DECISION_REJECT,
                severity="INFO",
                symbol="XAUUSD",
                correlation_id=cid,
                parent_id=sid,
                source_module="platforms.main_loop",
                payload={
                    "direction": "LONG",
                    "score": 92,
                    "reason": "No micro-confirmation on M1",
                    "entry_price": 2345.50,
                    "stop_loss": 2340.00,
                },
            )

            events = _flush_and_query(store, event_type=DECISION_REJECT)
            assert len(events) == 1
            ev = events[0]
            assert ev["correlation_id"] == cid
            assert ev["parent_id"] == sid
            assert ev["symbol"] == "XAUUSD"
            assert ev["event_type"] == DECISION_REJECT

            payload = json.loads(ev["payload_json"])
            assert payload["direction"] == "LONG"
            assert payload["score"] == 92
            assert payload["reason"] == "No micro-confirmation on M1"
            assert payload["entry_price"] == 2345.50
            assert payload["stop_loss"] == 2340.00
        finally:
            store.close()

    def test_reject_without_entry_context(self, tmp_path):
        """Rejections before zone computation should work without entry/SL."""
        store = _make_store(tmp_path)
        try:
            cid = new_cycle_id()
            store.emit(
                event_type=DECISION_REJECT,
                severity="INFO",
                symbol="GBPUSD",
                correlation_id=cid,
                payload={"direction": "SHORT", "score": 75, "reason": "Correlation block"},
            )

            events = _flush_and_query(store, event_type=DECISION_REJECT, symbol="GBPUSD")
            assert len(events) == 1
            payload = json.loads(events[0]["payload_json"])
            assert "entry_price" not in payload
            assert payload["reason"] == "Correlation block"
        finally:
            store.close()


# ── Test 3: ORDER_SENT + ORDER_FILLED linkage ───────────────────────────────

class TestOrderEventLinkage:
    def test_order_sent_and_filled_share_setup_id(self, tmp_path):
        """ORDER_SENT and ORDER_FILLED should share setup_id (parent_id)
        and be linked to the same cycle (correlation_id)."""
        store = _make_store(tmp_path)
        try:
            cid = new_cycle_id()
            sid = new_setup_id()

            store.emit(
                event_type=ORDER_SENT,
                severity="INFO",
                symbol="EURUSD",
                correlation_id=cid,
                parent_id=sid,
                source_module="platforms.main_loop",
                payload={
                    "direction": "LONG",
                    "lots": 0.05,
                    "entry_price": 1.08500,
                    "stop_loss": 1.08200,
                    "tp1": 1.09000,
                    "score": 88,
                },
            )

            store.emit(
                event_type=ORDER_FILLED,
                severity="INFO",
                symbol="EURUSD",
                correlation_id=cid,
                parent_id=sid,
                source_module="platforms.main_loop",
                payload={
                    "direction": "LONG",
                    "order_id": "12345678",
                    "fill_price": 1.08505,
                    "requested_price": 1.08500,
                    "slippage_pips": 0.5,
                    "lots": 0.05,
                    "stop_loss": 1.08200,
                    "tp1": 1.09000,
                    "tp2": 1.09500,
                    "score": 88,
                },
            )

            events = _flush_and_query(store, correlation_id=cid)
            assert len(events) == 2

            sent = [e for e in events if e["event_type"] == ORDER_SENT]
            filled = [e for e in events if e["event_type"] == ORDER_FILLED]
            assert len(sent) == 1
            assert len(filled) == 1

            assert sent[0]["parent_id"] == sid
            assert filled[0]["parent_id"] == sid
            assert sent[0]["correlation_id"] == cid
            assert filled[0]["correlation_id"] == cid

            fill_payload = json.loads(filled[0]["payload_json"])
            assert fill_payload["order_id"] == "12345678"
            assert fill_payload["fill_price"] == 1.08505
        finally:
            store.close()


# ── Test 4: Safety — un-serializable payload ─────────────────────────────────

class TestSafetyEmit:
    def test_unserializable_payload_does_not_raise(self, tmp_path):
        """If payload contains un-serializable objects, emit() should not
        propagate an exception to the caller."""
        store = _make_store(tmp_path)
        try:
            class BadObj:
                pass

            eid = store.emit(
                event_type="TEST",
                severity="INFO",
                payload={"bad": BadObj()},
            )
            assert isinstance(eid, str)
        finally:
            store.close()

    def test_emit_with_none_store_attributes(self, tmp_path):
        """Emit with all-None optional fields should succeed."""
        store = _make_store(tmp_path)
        try:
            eid = store.emit(
                event_type=DECISION_REJECT,
                severity="INFO",
            )
            events = _flush_and_query(store, event_type=DECISION_REJECT)
            assert len(events) >= 1
            assert events[0]["correlation_id"] is None
            assert events[0]["parent_id"] is None
            assert events[0]["symbol"] is None
        finally:
            store.close()


# ── Test 5: Domain event constants ───────────────────────────────────────────

class TestDomainEventConstants:
    def test_constants_are_strings(self):
        assert isinstance(DECISION_REJECT, str)
        assert isinstance(ORDER_SENT, str)
        assert isinstance(ORDER_FILLED, str)
        assert isinstance(TRADE_OPEN, str)

    def test_constants_are_unique(self):
        vals = {DECISION_REJECT, ORDER_SENT, ORDER_FILLED, TRADE_OPEN}
        assert len(vals) == 4


# ── Test 6: Full chain — cycle → setup → reject + order events ──────────────

class TestFullEventChain:
    def test_end_to_end_chain_linkage(self, tmp_path):
        """Simulate a full cycle: two setups, one rejected, one filled.
        Verify the entire chain can be queried by cycle_id and each
        setup's events are isolated by parent_id."""
        store = _make_store(tmp_path)
        try:
            cid = new_cycle_id()

            sid1 = new_setup_id()
            store.emit(DECISION_REJECT, "INFO", symbol="GBPUSD",
                       correlation_id=cid, parent_id=sid1,
                       payload={"reason": "M1 reject", "score": 85})

            sid2 = new_setup_id()
            store.emit(ORDER_SENT, "INFO", symbol="EURUSD",
                       correlation_id=cid, parent_id=sid2,
                       payload={"lots": 0.1})
            store.emit(ORDER_FILLED, "INFO", symbol="EURUSD",
                       correlation_id=cid, parent_id=sid2,
                       payload={"order_id": "99", "fill_price": 1.085})
            store.emit(TRADE_OPEN, "INFO", symbol="EURUSD",
                       correlation_id=cid, parent_id=sid2,
                       payload={"order_id": "99"})

            all_events = _flush_and_query(store, correlation_id=cid)
            assert len(all_events) == 4

            setup1_events = [e for e in all_events if e["parent_id"] == sid1]
            setup2_events = [e for e in all_events if e["parent_id"] == sid2]
            assert len(setup1_events) == 1
            assert setup1_events[0]["event_type"] == DECISION_REJECT
            assert len(setup2_events) == 3
            types = {e["event_type"] for e in setup2_events}
            assert types == {ORDER_SENT, ORDER_FILLED, TRADE_OPEN}
        finally:
            store.close()
