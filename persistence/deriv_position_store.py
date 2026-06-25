"""
APEX TRADER — Deriv Position Persistence Store
SQLite-backed storage for Deriv multiplier-contract positions so a crash never
leaves an open Deriv contract unmanaged.  Mirrors persistence.position_store:
WAL mode, a threading.Lock, and INSERT OR REPLACE upserts.

Deriv positions live in DerivConnector._positions as a dict keyed by
contract_id, carrying the metadata the manager needs (SL, TP, stake,
multiplier, idempotency key).  Unlike MT5 — whose positions are reconstructed
from the broker plus PositionStore — Deriv's broker portfolio does NOT echo
back our internal SL/TP/idem-key, so without this store that context is lost
on restart.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


from runtime_paths import data_dir as _data_dir  # noqa: E402

_DB_DIR = _data_dir()
_DB_PATH = _DB_DIR / "apex_deriv_positions.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS deriv_positions (
    contract_id     TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    lots            REAL NOT NULL DEFAULT 0.0,
    sl              REAL NOT NULL DEFAULT 0.0,
    tp              REAL NOT NULL DEFAULT 0.0,
    open_price      REAL NOT NULL DEFAULT 0.0,
    stake           REAL NOT NULL DEFAULT 0.0,
    multiplier      INTEGER NOT NULL DEFAULT 100,
    idem_key        TEXT NOT NULL DEFAULT '',
    last_update     TEXT NOT NULL
)
"""

# Columns persisted from the in-memory _positions dict entries.
_FIELDS = (
    "symbol", "direction", "lots", "sl", "tp",
    "open_price", "stake", "multiplier", "idem_key",
)


class DerivPositionStore:
    """Thread-safe SQLite store for open Deriv contracts."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = Path(db_path) if db_path else _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        # Health flag — flipped to False on any read/write error so the bootstrap
        # can pause new entries (losing this store leaves multiplier contracts
        # unmanaged). Failures are still swallowed at the call site; this only
        # exposes the degraded state for monitoring.
        self._healthy: bool = True
        self._last_error: str = ""
        self._connect()

    def is_healthy(self) -> bool:
        """True while every store operation has succeeded; False after any error."""
        return self._healthy

    def degraded_reason(self) -> str:
        return self._last_error if not self._healthy else ""

    def _mark_unhealthy(self, reason: str) -> None:
        self._healthy = False
        self._last_error = reason

    def _connect(self) -> None:
        self._conn = sqlite3.connect(
            str(self._db_path), timeout=10, check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()
        logger.debug("DerivPositionStore opened — {}", self._db_path)

    def save_position(self, contract_id: str, data: dict) -> None:
        """Persist (or replace) a single Deriv contract from its _positions entry."""
        if not contract_id:
            return
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO deriv_positions
                    (contract_id, symbol, direction, lots, sl, tp,
                     open_price, stake, multiplier, idem_key, last_update)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(contract_id),
                        str(data.get("symbol", "")),
                        str(data.get("direction", "")),
                        float(data.get("lots", 0.0) or 0.0),
                        float(data.get("sl", 0.0) or 0.0),
                        float(data.get("tp", 0.0) or 0.0),
                        float(data.get("open_price", 0.0) or 0.0),
                        float(data.get("stake", 0.0) or 0.0),
                        int(data.get("multiplier", 100) or 100),
                        str(data.get("idem_key", "")),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("DerivPositionStore save failed for {}: {}", contract_id, exc)
                self._mark_unhealthy(f"save {contract_id}: {exc}")

    def update_position(self, contract_id: str, **fields) -> None:
        """Update specific fields (e.g. sl/tp) on a persisted contract."""
        if not contract_id or not fields:
            return
        updates = {k: v for k, v in fields.items() if k in _FIELDS}
        if not updates:
            return
        updates["last_update"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [str(contract_id)]
        with self._lock:
            try:
                self._conn.execute(
                    f"UPDATE deriv_positions SET {set_clause} WHERE contract_id = ?",
                    values,
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("DerivPositionStore update failed for {}: {}", contract_id, exc)
                self._mark_unhealthy(f"update {contract_id}: {exc}")

    def remove_position(self, contract_id: str) -> None:
        """Remove a closed contract from persistence."""
        if not contract_id:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM deriv_positions WHERE contract_id = ?",
                    (str(contract_id),),
                )
                self._conn.commit()
            except Exception as exc:
                logger.error("DerivPositionStore remove failed for {}: {}", contract_id, exc)
                self._mark_unhealthy(f"remove {contract_id}: {exc}")

    def load_all_positions(self) -> dict[str, dict]:
        """Load all persisted contracts as a {contract_id: data} mapping.

        The returned per-contract dicts match the in-memory ``_positions``
        layout so they can be assigned straight back onto the connector.
        """
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "SELECT contract_id, symbol, direction, lots, sl, tp, "
                    "open_price, stake, multiplier, idem_key FROM deriv_positions"
                )
                rows = cursor.fetchall()
            except Exception as exc:
                logger.error("DerivPositionStore load failed: {}", exc)
                self._mark_unhealthy(f"load_all: {exc}")
                # Do NOT return {} — an empty mapping would make the system boot
                # believing there are zero open Deriv contracts while real ones
                # are live at the broker (they would then run UNMANAGED). Raise
                # so startup surfaces the failure instead of silently dropping
                # all contract metadata (mirrors PositionStore.load_all_positions).
                raise
        out: dict[str, dict] = {}
        for row in rows:
            cid = str(row[0])
            out[cid] = {
                "symbol": row[1],
                "direction": row[2],
                "lots": row[3],
                "sl": row[4],
                "tp": row[5],
                "open_price": row[6],
                "stake": row[7],
                "multiplier": row[8],
                "idem_key": row[9],
            }
        return out

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
