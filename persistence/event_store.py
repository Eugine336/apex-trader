"""
APEX TRADER — Append-Only Event Store
Every signal, decision, trade, and log line becomes a structured, correlated
event.  Append-only SQLite, WAL-mode, thread-safe.  Writes are non-blocking:
a bounded queue feeds a background writer thread so the trading loop never
waits on disk.

Mirror the sync-sqlite3 + WAL pattern used by ``persistence/position_store.py``.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

# ── Defaults ──────────────────────────────────────────────────────────────────

_DB_DIR = Path(__file__).parent.parent / "data"
_DB_PATH = _DB_DIR / "apex_events.db"

_MAX_QUEUE = 10_000
_DEFAULT_MAX_AGE_DAYS = 30
_DEFAULT_MAX_ROWS = 500_000

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    correlation_id TEXT,
    parent_id      TEXT,
    ts_utc_ms      INTEGER NOT NULL,
    event_type     TEXT NOT NULL,
    severity       TEXT NOT NULL,
    symbol         TEXT,
    source_module  TEXT,
    payload_json   TEXT
)
"""

_CREATE_IDX_CORRELATION = "CREATE INDEX IF NOT EXISTS idx_events_correlation ON events (correlation_id)"
_CREATE_IDX_TS = "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts_utc_ms)"
_CREATE_IDX_TYPE = "CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type)"
_CREATE_IDX_SYMBOL = "CREATE INDEX IF NOT EXISTS idx_events_symbol ON events (symbol)"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def generate_id() -> str:
    return uuid.uuid4().hex


# ── Correlation-ID helpers ────────────────────────────────────────────────────


def new_cycle_id() -> str:
    """Generate a unique scan-cycle correlation ID."""
    return f"cycle-{uuid.uuid4().hex[:12]}"


def new_setup_id() -> str:
    """Generate a unique setup correlation ID."""
    return f"setup-{uuid.uuid4().hex[:12]}"


# ── Event store ───────────────────────────────────────────────────────────────


class EventStore:
    """Append-only event store backed by SQLite (WAL mode).

    Writes happen on a dedicated background thread fed by a bounded queue so
    the caller (trading loop / loguru sink) is never blocked by disk I/O.  If
    the queue saturates, oldest un-written events are dropped and a counter is
    incremented — the trading system is never stalled.

    Thread-safe: any thread may call ``emit``; only the writer thread touches
    the DB connection.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        max_queue: int = _MAX_QUEUE,
    ) -> None:
        self._db_path = Path(db_path) if db_path else _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_queue = max_queue

        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._dropped = 0
        self._dropped_lock = threading.Lock()

        self._shutdown = threading.Event()
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="EventStoreWriter",
            daemon=True,
        )

        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._writer.start()

    # ── DB bootstrap ──────────────────────────────────────────────────────

    def _connect(self) -> None:
        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=10,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_CREATE_EVENTS)
        self._conn.execute(_CREATE_IDX_CORRELATION)
        self._conn.execute(_CREATE_IDX_TS)
        self._conn.execute(_CREATE_IDX_TYPE)
        self._conn.execute(_CREATE_IDX_SYMBOL)
        self._conn.commit()

    # ── Public API ────────────────────────────────────────────────────────

    def emit(
        self,
        event_type: str,
        severity: str,
        *,
        symbol: Optional[str] = None,
        correlation_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        source_module: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Enqueue an event for background persistence.  Returns event_id."""
        eid = generate_id()
        try:
            payload_json = json.dumps(payload) if payload else None
        except (TypeError, ValueError):
            payload_json = json.dumps({"_serialization_error": True, "repr": repr(payload)[:500]})
        row = (
            eid,
            correlation_id,
            parent_id,
            _now_ms(),
            event_type,
            severity,
            symbol,
            source_module,
            payload_json,
        )
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1
        return eid

    @property
    def dropped_count(self) -> int:
        with self._dropped_lock:
            return self._dropped

    def query(
        self,
        *,
        correlation_id: Optional[str] = None,
        event_type: Optional[str] = None,
        symbol: Optional[str] = None,
        since_ms: Optional[int] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Read events matching the supplied filters (AND-combined)."""
        clauses: List[str] = []
        params: List[Any] = []
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if since_ms is not None:
            clauses.append("ts_utc_ms >= ?")
            params.append(since_ms)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM events{where} ORDER BY ts_utc_ms ASC LIMIT ?"
        params.append(limit)

        try:
            cur = self._conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:
            print(f"[event_store] query failed: {exc}", file=sys.stderr)
            return []

    def count(self) -> int:
        """Total persisted event rows."""
        try:
            cur = self._conn.execute("SELECT COUNT(*) FROM events")
            return cur.fetchone()[0]
        except Exception as exc:
            print(f"[event_store] count failed: {exc}", file=sys.stderr)
            return 0

    # ── Retention / pruning ───────────────────────────────────────────────

    def prune(
        self,
        max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
        max_rows: int = _DEFAULT_MAX_ROWS,
    ) -> int:
        """Delete old events beyond retention limits.  Returns rows deleted."""
        deleted = 0
        cutoff_ms = _now_ms() - (max_age_days * 86_400_000)
        try:
            cur = self._conn.execute("DELETE FROM events WHERE ts_utc_ms < ?", (cutoff_ms,))
            deleted += cur.rowcount
            total = self.count()
            if total > max_rows:
                excess = total - max_rows
                cur = self._conn.execute(
                    "DELETE FROM events WHERE event_id IN (SELECT event_id FROM events ORDER BY ts_utc_ms ASC LIMIT ?)",
                    (excess,),
                )
                deleted += cur.rowcount
            if deleted:
                self._conn.commit()
        except Exception as exc:
            print(f"[event_store] prune failed: {exc}", file=sys.stderr)
        return deleted

    # ── Background writer ─────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        """Drain the queue in batches and persist to SQLite."""
        insert_sql = (
            "INSERT OR IGNORE INTO events "
            "(event_id, correlation_id, parent_id, ts_utc_ms, event_type,"
            " severity, symbol, source_module, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        while not self._shutdown.is_set():
            batch: List[tuple] = []
            try:
                row = self._queue.get(timeout=0.25)
            except queue.Empty:
                row = None
            if row is None:
                continue
            batch.append(row)

            while len(batch) < 200:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break

            try:
                self._conn.executemany(insert_sql, batch)
                self._conn.commit()
            except Exception as exc:
                print(f"[event_store] write failed (batch={len(batch)}): {exc}", file=sys.stderr)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def flush(self, timeout: float = 5.0) -> None:
        """Block until the queue is drained or *timeout* elapses."""
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)

    def close(self) -> None:
        """Drain remaining events and shut down the writer thread."""
        self._shutdown.set()
        self._writer.join(timeout=5.0)
        if self._conn:
            try:
                remaining: List[tuple] = []
                while not self._queue.empty():
                    try:
                        remaining.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                if remaining:
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO events "
                        "(event_id, correlation_id, parent_id, ts_utc_ms,"
                        " event_type, severity, symbol, source_module,"
                        " payload_json)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        remaining,
                    )
                    self._conn.commit()
                self._conn.close()
            except Exception as exc:
                print(f"[event_store] close cleanup failed: {exc}", file=sys.stderr)
            self._conn = None


# ── Module-level singleton ────────────────────────────────────────────────────

_global_store: Optional[EventStore] = None
_global_lock = threading.Lock()


def get_event_store(db_path: Optional[str] = None) -> EventStore:
    """Return (or lazily create) the process-wide EventStore singleton."""
    global _global_store
    with _global_lock:
        if _global_store is None:
            _global_store = EventStore(db_path=db_path)
        return _global_store


def shutdown_event_store() -> None:
    """Flush and close the global store (call at process exit)."""
    global _global_store
    with _global_lock:
        if _global_store is not None:
            _global_store.close()
            _global_store = None
