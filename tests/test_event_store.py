"""
APEX TRADER — EventStore + Loguru Tee Sink Tests
Covers: emit, ordering, concurrent writes, queue saturation / drop,
prune, loguru sink integration, and correlation-ID propagation.
"""

import logging
import sqlite3
import threading
import time

import pytest
from loguru import logger

from persistence.event_store import EventStore, new_cycle_id, new_setup_id

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_events.db")
    s = EventStore(db_path=db_path)
    yield s
    s.close()


# ── Basic emit + query ────────────────────────────────────────────────────────


class TestEventStoreEmitAndQuery:
    def test_emit_returns_event_id(self, store):
        eid = store.emit("LOG", "INFO", payload={"msg": "hello"})
        assert isinstance(eid, str) and len(eid) == 32

    def test_emitted_event_persists(self, store):
        store.emit("SCAN_CYCLE", "INFO", symbol="EURUSD", payload={"cycle": 1})
        store.flush()
        rows = store.query(event_type="SCAN_CYCLE")
        assert len(rows) == 1
        assert rows[0]["symbol"] == "EURUSD"
        assert rows[0]["severity"] == "INFO"

    def test_ordering_is_chronological(self, store):
        for i in range(5):
            store.emit("LOG", "DEBUG", payload={"seq": i})
        store.flush()
        rows = store.query(event_type="LOG")
        timestamps = [r["ts_utc_ms"] for r in rows]
        assert timestamps == sorted(timestamps)

    def test_query_by_correlation_id(self, store):
        cid = new_cycle_id()
        store.emit("LOG", "INFO", correlation_id=cid, payload={"a": 1})
        store.emit("LOG", "INFO", correlation_id="other", payload={"a": 2})
        store.flush()
        rows = store.query(correlation_id=cid)
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == cid

    def test_query_by_symbol(self, store):
        store.emit("LOG", "INFO", symbol="XAUUSD")
        store.emit("LOG", "INFO", symbol="EURUSD")
        store.flush()
        rows = store.query(symbol="XAUUSD")
        assert len(rows) == 1

    def test_count(self, store):
        assert store.count() == 0
        for _ in range(3):
            store.emit("LOG", "DEBUG")
        store.flush()
        assert store.count() == 3

    def test_parent_id_stored(self, store):
        parent = store.emit("SCAN_CYCLE", "INFO")
        store.emit("SETUP_SCORED", "INFO", parent_id=parent)
        store.flush()
        rows = store.query(event_type="SETUP_SCORED")
        assert rows[0]["parent_id"] == parent


# ── Concurrent writes ─────────────────────────────────────────────────────────


class TestConcurrentWrites:
    def test_multi_thread_emit(self, store):
        n_threads = 8
        n_per_thread = 50

        def writer():
            for _ in range(n_per_thread):
                store.emit("LOG", "DEBUG", payload={"t": threading.current_thread().name})

        threads = [threading.Thread(target=writer) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        store.flush()
        assert store.count() == n_threads * n_per_thread


# ── Queue saturation / drop ───────────────────────────────────────────────────


class TestQueueSaturation:
    def test_drop_on_full_queue(self, tmp_path):
        tiny_store = EventStore(
            db_path=str(tmp_path / "tiny.db"),
            max_queue=5,
        )
        tiny_store._shutdown.set()
        tiny_store._writer.join(timeout=2)

        for _ in range(20):
            tiny_store.emit("LOG", "INFO")

        assert tiny_store.dropped_count > 0
        tiny_store.close()


# ── Prune ─────────────────────────────────────────────────────────────────────


class TestPrune:
    def test_prune_by_max_rows(self, store):
        for _ in range(10):
            store.emit("LOG", "DEBUG")
        store.flush()
        assert store.count() == 10
        deleted = store.prune(max_age_days=9999, max_rows=3)
        assert deleted == 7
        assert store.count() == 3


# ── Correlation-ID helpers ────────────────────────────────────────────────────


class TestCorrelationHelpers:
    def test_cycle_id_format(self):
        cid = new_cycle_id()
        assert cid.startswith("cycle-")
        assert len(cid) == 18

    def test_setup_id_format(self):
        sid = new_setup_id()
        assert sid.startswith("setup-")
        assert len(sid) == 18

    def test_ids_are_unique(self):
        ids = {new_cycle_id() for _ in range(100)}
        assert len(ids) == 100


# ── Loguru tee sink integration ───────────────────────────────────────────────


class TestLoguruSink:
    def test_logger_info_creates_event(self, store, tmp_path):
        from persistence import event_store as _es_mod
        from persistence.event_sink import event_store_sink

        original = _es_mod._global_store
        _es_mod._global_store = store
        sid = logger.add(event_store_sink, level="DEBUG")
        try:
            logger.info("test message from loguru")
            store.flush()
            rows = store.query(event_type="LOG")
            assert any("test message from loguru" in (r.get("payload_json") or "") for r in rows)
        finally:
            logger.remove(sid)
            _es_mod._global_store = original

    def test_warning_severity_mapped(self, store, tmp_path):
        from persistence import event_store as _es_mod
        from persistence.event_sink import event_store_sink

        original = _es_mod._global_store
        _es_mod._global_store = store
        sid = logger.add(event_store_sink, level="DEBUG")
        try:
            logger.warning("a warning")
            store.flush()
            rows = store.query(event_type="LOG")
            warnings = [r for r in rows if r["severity"] == "WARNING"]
            assert len(warnings) >= 1
        finally:
            logger.remove(sid)
            _es_mod._global_store = original

    def test_correlation_id_propagates(self, store, tmp_path):
        from persistence import event_store as _es_mod
        from persistence.event_sink import event_store_sink

        original = _es_mod._global_store
        _es_mod._global_store = store
        sid = logger.add(event_store_sink, level="DEBUG")
        try:
            cid = new_cycle_id()
            with logger.contextualize(correlation_id=cid, symbol="GBPUSD"):
                logger.info("correlated log")
            store.flush()
            rows = store.query(correlation_id=cid)
            assert len(rows) >= 1
            assert rows[0]["symbol"] == "GBPUSD"
        finally:
            logger.remove(sid)
            _es_mod._global_store = original


# ── stdlib intercept ──────────────────────────────────────────────────────────


class TestStdlibIntercept:
    def test_stdlib_logger_reaches_event_store(self, store, tmp_path):
        from persistence import event_store as _es_mod
        from persistence.event_sink import event_store_sink, install_stdlib_intercept

        original = _es_mod._global_store
        _es_mod._global_store = store
        sid = logger.add(event_store_sink, level="DEBUG")
        install_stdlib_intercept(["test.intercept.target"])
        try:
            stdlib_log = logging.getLogger("test.intercept.target")
            stdlib_log.info("from stdlib")
            store.flush()
            rows = store.query(event_type="LOG")
            assert any("from stdlib" in (r.get("payload_json") or "") for r in rows)
        finally:
            logger.remove(sid)
            _es_mod._global_store = original


# ── Writer-loop recursion safety ──────────────────────────────────────────────


class _FailingConnection:
    """Proxy that raises on executemany while forwarding everything else."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.fail = True

    def executemany(self, *args, **kwargs):
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.executemany(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestWriterRecursionSafety:
    def test_persistent_write_failure_does_not_reenqueue(self, tmp_path):
        """A persistent DB-write failure must NOT re-enter the loguru sink
        and produce an unbounded stream of self-referential events."""
        from persistence import event_store as _es_mod
        from persistence.event_sink import event_store_sink

        store = EventStore(db_path=str(tmp_path / "fail.db"))
        original = _es_mod._global_store
        _es_mod._global_store = store
        sid = logger.add(event_store_sink, level="DEBUG")
        try:
            real_conn = store._conn
            proxy = _FailingConnection(real_conn)
            store._conn = proxy

            store.emit("LOG", "INFO", payload={"msg": "trigger"})
            time.sleep(1.0)

            qsize = store._queue.qsize()
            assert qsize <= 1, (
                f"Queue grew to {qsize} — writer failure is re-enqueueing events"
            )

            proxy.fail = False
            store._conn = real_conn
            store.emit("LOG", "INFO", payload={"msg": "canary"})
            store.flush()
            rows = store.query(event_type="LOG")
            failure_events = [
                r for r in rows
                if "write failed" in (r.get("payload_json") or "")
                or "disk I/O" in (r.get("payload_json") or "")
            ]
            assert len(failure_events) == 0, (
                "Writer failure message was persisted as an event — sink recursion still present"
            )
        finally:
            logger.remove(sid)
            _es_mod._global_store = original
            store.close()
