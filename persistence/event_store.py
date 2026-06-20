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
    payload_json   TEXT,
    seq            INTEGER
)
"""

_CREATE_IDX_CORRELATION = "CREATE INDEX IF NOT EXISTS idx_events_correlation ON events (correlation_id)"
_CREATE_IDX_TS = "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts_utc_ms)"
_CREATE_IDX_TYPE = "CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type)"
_CREATE_IDX_SYMBOL = "CREATE INDEX IF NOT EXISTS idx_events_symbol ON events (symbol)"
_CREATE_IDX_SEQ = "CREATE INDEX IF NOT EXISTS idx_events_seq ON events (seq)"


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
        # Monotonic, per-process event sequence.  Assigned at emit time under
        # its own lock and seeded from the persisted MAX(seq) on connect, so it
        # continues across restarts and gives a strict total order for replay
        # even when events share a millisecond timestamp.  Gaps indicate events
        # dropped under queue saturation.
        self._seq = 0
        self._seq_lock = threading.Lock()
        # Serialises every access to the single shared sqlite connection across
        # the writer thread, the dashboard reader thread, and the main-thread
        # pruner — without this, concurrent cursors race ("database is locked" /
        # "recursive use of cursors") and reads silently fail.
        self._db_lock = threading.Lock()

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
        # Migrate DBs created before the seq column existed.
        existing_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(events)").fetchall()
        }
        if "seq" not in existing_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN seq INTEGER")
        self._conn.execute(_CREATE_IDX_CORRELATION)
        self._conn.execute(_CREATE_IDX_TS)
        self._conn.execute(_CREATE_IDX_TYPE)
        self._conn.execute(_CREATE_IDX_SYMBOL)
        self._conn.execute(_CREATE_IDX_SEQ)
        self._conn.commit()
        # Continue the sequence from the persisted maximum (0 on a fresh or
        # pre-migration DB) so replay ordering is monotonic across restarts.
        row = self._conn.execute("SELECT MAX(seq) FROM events").fetchone()
        self._seq = int(row[0]) if row and row[0] is not None else 0

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
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
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
            seq,
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
        sql = f"SELECT * FROM events{where} ORDER BY ts_utc_ms ASC, seq ASC LIMIT ?"
        params.append(limit)

        try:
            with self._db_lock:
                cur = self._conn.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:
            print(f"[event_store] query failed: {exc}", file=sys.stderr)
            return []

    _SEVERITY_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}

    def query_events(
        self,
        *,
        severity_min: str = "INFO",
        event_types: Optional[List[str]] = None,
        symbol: Optional[str] = None,
        correlation_id: Optional[str] = None,
        since_ms: Optional[int] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Dashboard-oriented read: severity-filtered, multi-type, paginated, newest-first."""
        min_rank = self._SEVERITY_RANK.get(severity_min.upper(), 1)
        allowed = [s for s, r in self._SEVERITY_RANK.items() if r >= min_rank]

        clauses: List[str] = []
        params: List[Any] = []

        placeholders = ", ".join("?" for _ in allowed)
        clauses.append(f"severity IN ({placeholders})")
        params.extend(allowed)

        if event_types:
            tp = ", ".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({tp})")
            params.extend(event_types)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if since_ms is not None:
            clauses.append("ts_utc_ms >= ?")
            params.append(since_ms)

        where = " WHERE " + " AND ".join(clauses)
        sql = (
            f"SELECT * FROM events{where}"
            f" ORDER BY ts_utc_ms DESC, seq DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        try:
            with self._db_lock:
                cur = self._conn.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:
            print(f"[event_store] query_events failed: {exc}", file=sys.stderr)
            return []

    def replay(
        self,
        *,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Return events with ``seq > after_seq`` in strict ascending order.

        ``seq`` is a per-process monotonic counter (continued across restarts
        from the persisted maximum), so this yields a deterministic total
        order suitable for replay — unlike timestamp ordering, it never ties.
        Page through by passing the last returned row's ``seq`` as the next
        ``after_seq``.  Rows written before the seq column was introduced have
        ``seq IS NULL`` and are intentionally excluded.
        """
        try:
            with self._db_lock:
                cur = self._conn.execute(
                    "SELECT * FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                    (after_seq, limit),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:
            print(f"[event_store] replay failed: {exc}", file=sys.stderr)
            return []

    def get_trade_close_map(self) -> Dict[str, Dict[str, Any]]:
        """Return {order_id: payload} for all TRADE_CLOSE events (exit attribution)."""
        try:
            with self._db_lock:
                cur = self._conn.execute(
                    "SELECT payload_json FROM events WHERE event_type = 'TRADE_CLOSE'"
                )
                rows = cur.fetchall()
            result: Dict[str, Dict[str, Any]] = {}
            for (pj,) in rows:
                if pj:
                    payload = json.loads(pj)
                    oid = payload.get("order_id")
                    if oid:
                        result[str(oid)] = payload
            return result
        except Exception as exc:
            print(f"[event_store] get_trade_close_map failed: {exc}", file=sys.stderr)
            return {}

    def get_reconciliation(self, limit: int = 200) -> List[Dict[str, Any]]:
        """TRADE_CLOSE events where derived reason != broker reason, or reason missing."""
        try:
            rows = self.query_events(
                severity_min="DEBUG",
                event_types=["TRADE_CLOSE"],
                limit=limit,
            )
            anomalies: List[Dict[str, Any]] = []
            for row in rows:
                pj = row.get("payload_json")
                if not pj:
                    anomalies.append(row)
                    continue
                payload = json.loads(pj) if isinstance(pj, str) else pj
                derived = payload.get("exit_reason", "")
                raw = payload.get("raw_broker_reason", "")
                source = payload.get("exit_reason_source", "")
                if not derived or source == "unknown" or (raw and derived != raw):
                    row["_payload"] = payload
                    anomalies.append(row)
            return anomalies
        except Exception as exc:
            print(f"[event_store] get_reconciliation failed: {exc}", file=sys.stderr)
            return []

    def count(self) -> int:
        """Total persisted event rows."""
        try:
            with self._db_lock:
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
            with self._db_lock:
                cur = self._conn.execute("DELETE FROM events WHERE ts_utc_ms < ?", (cutoff_ms,))
                deleted += cur.rowcount
                total = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
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

    def vacuum(self) -> None:
        """Reclaim disk space freed by pruning. A plain DELETE leaves pages in
        the file for reuse but does NOT shrink it on disk; VACUUM rebuilds the
        database to reclaim that space. Heavy — run off the hot path (daily
        maintenance), never in the trading loop."""
        try:
            with self._db_lock:
                self._conn.commit()
                self._conn.execute("VACUUM")
            logger.info("[event_store] VACUUM complete — disk space reclaimed")
        except Exception as exc:
            logger.warning("[event_store] VACUUM failed: {}", exc)

    # ── Background writer ─────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        """Drain the queue in batches and persist to SQLite."""
        insert_sql = (
            "INSERT OR IGNORE INTO events "
            "(event_id, correlation_id, parent_id, ts_utc_ms, event_type,"
            " severity, symbol, source_module, payload_json, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
                with self._db_lock:
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
                # Acquire the DB lock before touching the connection — if the
                # writer thread failed to join within the timeout it may still
                # be mid-write, and an unsynchronised access here would race it.
                with self._db_lock:
                    if remaining:
                        self._conn.executemany(
                            "INSERT OR IGNORE INTO events "
                            "(event_id, correlation_id, parent_id, ts_utc_ms,"
                            " event_type, severity, symbol, source_module,"
                            " payload_json, seq)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
