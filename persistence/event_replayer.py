"""Replayable event-log consumer.

Reads the :class:`EventStore`'s monotonic ``seq`` log in strict order and feeds
each event to a handler, tracking a **durable checkpoint** (the last processed
``seq``) so a consumer resumes exactly where it left off after a restart.  This
turns the append-only event log (sequence numbers added in the event store)
into a foundation for derived projections, crash recovery, and audit
reconstruction.

Delivery is **strictly ordered** (by ``seq``) and **at-least-once**: the
checkpoint is advanced past an event only after its handler returns, and a
handler exception stops the batch at the failing event so it is retried on the
next call.  The checkpoint lives in its own small SQLite file, so the replayer
never contends with the event store's writer.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from loguru import logger

_DEFAULT_CHECKPOINT_DB = Path("data") / "replay_checkpoints.db"

_CREATE_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS replay_checkpoints (
    consumer   TEXT PRIMARY KEY,
    last_seq   INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL
)
"""

EventHandler = Callable[[Dict[str, Any]], None]


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class EventReplayer:
    """Ordered, resumable consumer of the event store's ``seq`` log.

    Parameters
    ----------
    store:
        An ``EventStore`` (anything exposing ``replay(after_seq=, limit=)``).
    consumer:
        Stable name identifying this consumer's checkpoint.  Multiple
        independent consumers can replay the same log at their own pace by
        using distinct names.
    checkpoint_db:
        Path to the SQLite file holding checkpoints.  Defaults to
        ``data/replay_checkpoints.db`` — separate from the events DB so the
        replayer's writes never contend with the event-store writer.
    """

    def __init__(
        self,
        store: Any,
        *,
        consumer: str,
        checkpoint_db: Optional[str] = None,
    ) -> None:
        if not consumer:
            raise ValueError("EventReplayer requires a non-empty consumer name")
        self._store = store
        self._consumer = consumer
        self._db_path = Path(checkpoint_db) if checkpoint_db else _DEFAULT_CHECKPOINT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=10,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_CHECKPOINTS)
        self._conn.commit()
        self._checkpoint = self._load_checkpoint()

    # ── Checkpoint persistence ────────────────────────────────────────────

    def _load_checkpoint(self) -> int:
        row = self._conn.execute(
            "SELECT last_seq FROM replay_checkpoints WHERE consumer = ?",
            (self._consumer,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _persist_checkpoint(self, seq: int) -> None:
        self._conn.execute(
            "INSERT INTO replay_checkpoints (consumer, last_seq, updated_ms) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(consumer) DO UPDATE SET "
            "last_seq = excluded.last_seq, updated_ms = excluded.updated_ms",
            (self._consumer, seq, _now_ms()),
        )
        self._conn.commit()
        self._checkpoint = seq

    @property
    def checkpoint(self) -> int:
        """The last ``seq`` successfully processed by this consumer."""
        with self._lock:
            return self._checkpoint

    # ── Replay ────────────────────────────────────────────────────────────

    def process_once(
        self,
        handler: EventHandler,
        *,
        batch_size: int = 1000,
    ) -> int:
        """Process one batch of events after the checkpoint, in ``seq`` order.

        Returns the number of events processed.  On a handler exception the
        batch stops at (without advancing past) the failing event, so it is
        retried on the next call.  The checkpoint advances only across events
        whose handler returned, and is persisted once per batch — a crash
        mid-batch simply reprocesses the unconfirmed tail (at-least-once).
        """
        with self._lock:
            events = self._store.replay(
                after_seq=self._checkpoint,
                limit=batch_size,
            )
            last_ok = self._checkpoint
            processed = 0
            for ev in events:
                seq = ev.get("seq")
                if seq is None:
                    # Pre-sequence rows are excluded by replay(); guard anyway.
                    continue
                try:
                    handler(ev)
                except Exception as exc:
                    logger.warning(
                        "[replay] handler failed at seq={} consumer={}: {}",
                        seq,
                        self._consumer,
                        exc,
                    )
                    break
                last_ok = int(seq)
                processed += 1
            if last_ok != self._checkpoint:
                self._persist_checkpoint(last_ok)
            return processed

    def drain(
        self,
        handler: EventHandler,
        *,
        batch_size: int = 1000,
        max_batches: int = 10_000,
    ) -> int:
        """Process batches until caught up (a short batch) or *max_batches*.

        Returns the total number of events processed.  Stops early if a batch
        makes no progress (e.g. a handler error blocking the head).
        """
        total = 0
        for _ in range(max_batches):
            n = self.process_once(handler, batch_size=batch_size)
            total += n
            if n < batch_size:
                break
        return total

    def reset(self, seq: int = 0) -> None:
        """Rewind (or fast-forward) this consumer's checkpoint to *seq*."""
        with self._lock:
            self._persist_checkpoint(int(seq))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
