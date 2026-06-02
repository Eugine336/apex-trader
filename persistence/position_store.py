"""
APEX TRADER — Position Persistence Store
SQLite-backed position storage so no trade is ever lost to a crash.
Survival precedes growth — every open position is written to disk.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


_DB_DIR = Path(__file__).parent.parent / "data"
_DB_PATH = _DB_DIR / "apex_positions.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS managed_positions (
    order_id        TEXT PRIMARY KEY,
    platform        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    lots            REAL NOT NULL,
    entry_price     REAL NOT NULL,
    sl              REAL NOT NULL,
    tp1             REAL NOT NULL,
    tp2             REAL NOT NULL,
    score           INTEGER NOT NULL DEFAULT 0,
    regime          TEXT NOT NULL DEFAULT '',
    session         TEXT NOT NULL DEFAULT '',
    entry_type      TEXT NOT NULL DEFAULT '',
    open_time       TEXT NOT NULL,
    tp1_hit         INTEGER NOT NULL DEFAULT 0,
    at_breakeven    INTEGER NOT NULL DEFAULT 0,
    trailing        INTEGER NOT NULL DEFAULT 0,
    tm_trade_id     TEXT NOT NULL DEFAULT '',
    stake_usd       REAL NOT NULL DEFAULT 0.0,
    multiplier      INTEGER NOT NULL DEFAULT 100,
    last_update     TEXT NOT NULL
)
"""

_CREATE_IN_FLIGHT = """
CREATE TABLE IF NOT EXISTS in_flight_intents (
    idempotency_key TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    lots            REAL NOT NULL,
    created_at      TEXT NOT NULL,
    order_id        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'PENDING'
)
"""

_MIGRATE_IDEM_KEY = (
    "ALTER TABLE managed_positions ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''"
)


class PositionStore:
    """Thread-safe SQLite store for managed positions."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = Path(db_path) if db_path else _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        self._conn = sqlite3.connect(
            str(self._db_path), timeout=10, check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.execute(_CREATE_IN_FLIGHT)
        self._migrate_schema()
        self._conn.commit()
        logger.debug("PositionStore opened — {}", self._db_path)

    def _migrate_schema(self) -> None:
        """Add columns that may not exist in older databases."""
        cursor = self._conn.execute("PRAGMA table_info(managed_positions)")
        cols = {row[1] for row in cursor.fetchall()}
        if "idempotency_key" not in cols:
            try:
                self._conn.execute(_MIGRATE_IDEM_KEY)
            except sqlite3.OperationalError as exc:
                logger.debug("[position_store] idempotency-key migration skipped (likely already exists): {}", exc)
                pass

    def save_position(self, pos) -> None:
        """Persist a ManagedPosition to disk."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO managed_positions
                    (order_id, platform, symbol, direction, lots, entry_price,
                     sl, tp1, tp2, score, regime, session, entry_type,
                     open_time, tp1_hit, at_breakeven, trailing,
                     tm_trade_id, stake_usd, multiplier, idempotency_key,
                     last_update)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(pos.order_id),
                        pos.platform,
                        pos.symbol,
                        pos.direction,
                        pos.lots,
                        pos.entry_price,
                        pos.sl,
                        pos.tp1,
                        pos.tp2,
                        pos.score,
                        pos.regime,
                        pos.session,
                        pos.entry_type,
                        pos.open_time.isoformat(),
                        int(pos.tp1_hit),
                        int(pos.at_breakeven),
                        int(pos.trailing),
                        pos.tm_trade_id,
                        pos.stake_usd,
                        getattr(pos, "multiplier", 100),
                        getattr(pos, "idempotency_key", ""),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("PositionStore save failed for {}: {}", pos.order_id, exc)

    def update_position(self, order_id: str, **fields) -> None:
        """Update specific fields on a persisted position."""
        if not fields:
            return
        allowed = {
            "sl", "tp1", "tp2", "lots", "tp1_hit", "at_breakeven",
            "trailing", "tm_trade_id", "stake_usd", "multiplier",
            "idempotency_key",
        }
        updates = {}
        for key, val in fields.items():
            if key not in allowed:
                continue
            if key in ("tp1_hit", "at_breakeven", "trailing"):
                updates[key] = int(val)
            else:
                updates[key] = val
        if not updates:
            return
        updates["last_update"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [str(order_id)]
        with self._lock:
            try:
                self._conn.execute(
                    f"UPDATE managed_positions SET {set_clause} WHERE order_id = ?",
                    values,
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("PositionStore update failed for {}: {}", order_id, exc)

    def remove_position(self, order_id: str) -> None:
        """Remove a closed position from persistence."""
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM managed_positions WHERE order_id = ?",
                    (str(order_id),),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("PositionStore remove failed for {}: {}", order_id, exc)

    def load_all_positions(self) -> list[dict]:
        """Load all persisted positions as dicts for reconstruction."""
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "SELECT * FROM managed_positions"
                )
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                return [dict(zip(columns, row)) for row in rows]
            except Exception as exc:
                logger.error("PositionStore load failed: {}", exc)
                return []

    def clear_all(self) -> None:
        """Delete all persisted positions (used by emergency flatten)."""
        with self._lock:
            try:
                self._conn.execute("DELETE FROM managed_positions")
                self._conn.commit()
            except Exception as exc:
                logger.error("PositionStore clear_all failed: {}", exc)

    def count(self) -> int:
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM managed_positions"
                )
                return cursor.fetchone()[0]
            except Exception as exc:
                logger.warning("[position_store] open-position count read failed, returning 0: {}", exc)
                return 0

    def flush(self) -> None:
        """Force WAL checkpoint — call before shutdown."""
        with self._lock:
            try:
                if self._conn:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as exc:
                logger.warning("[position_store] WAL checkpoint failed during flush: {}", exc)
                pass

    # ── In-flight intent tracking (H3 idempotency) ──────────────────────

    def record_in_flight(
        self, idempotency_key: str, symbol: str, direction: str, lots: float,
    ) -> None:
        """Write an intent BEFORE submitting the order to the broker."""
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO in_flight_intents
                       (idempotency_key, symbol, direction, lots, created_at, order_id, status)
                       VALUES (?, ?, ?, ?, ?, '', 'PENDING')""",
                    (idempotency_key, symbol, direction, lots,
                     datetime.now(timezone.utc).isoformat()),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("in_flight record failed for {}: {}", idempotency_key, exc)

    def resolve_in_flight(self, idempotency_key: str, order_id: str) -> None:
        """Mark an in-flight intent as filled after broker confirmation."""
        with self._lock:
            try:
                self._conn.execute(
                    """UPDATE in_flight_intents
                       SET order_id = ?, status = 'FILLED'
                       WHERE idempotency_key = ?""",
                    (order_id, idempotency_key),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("in_flight resolve failed for {}: {}", idempotency_key, exc)

    def cancel_in_flight(self, idempotency_key: str) -> None:
        """Remove an in-flight intent after confirmed failure."""
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM in_flight_intents WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("in_flight cancel failed for {}: {}", idempotency_key, exc)

    def get_in_flight(self, idempotency_key: str) -> Optional[dict]:
        """Return the in-flight record for *idempotency_key*, or None."""
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "SELECT * FROM in_flight_intents WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            except Exception as exc:
                logger.warning("[position_store] in-flight record lookup failed, returning None: {}", exc)
                return None

    def cleanup_stale_in_flight(self, max_age_seconds: int = 600) -> None:
        """Remove PENDING in-flight records older than *max_age_seconds*."""
        with self._lock:
            try:
                cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
                self._conn.execute(
                    """DELETE FROM in_flight_intents
                       WHERE status = 'PENDING'
                       AND created_at < ?""",
                    (datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(),),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("[position_store] stale in-flight cleanup commit failed: {}", exc)
                pass

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception as exc:
                    logger.warning("[position_store] WAL checkpoint failed during close: {}", exc)
                    pass
                self._conn.close()
                self._conn = None
