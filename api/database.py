"""APEX TRADER — API persistence layer (SQLite).

A thin, dependency-free data-access layer over a single SQLite database that
holds all control-plane state:

* ``users``              — accounts (email + bcrypt password hash)
* ``broker_credentials`` — per-user encrypted MT5/Deriv credentials
* ``trading_instances``  — per-user APEX process lifecycle records
* ``trade_history``      — completed trades reported by running instances
* ``user_config``        — per-user trading preferences (JSON)
* ``password_resets``    — outstanding password-reset tokens

The database is created automatically on first use. SQLite is opened in WAL
mode with a busy timeout so the API server and the per-user trading
subprocesses (which write trade history via the reporting hook) can access the
file concurrently without ``database is locked`` errors.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from loguru import logger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_credentials (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL,
    broker_type           TEXT NOT NULL,
    label                 TEXT NOT NULL DEFAULT '',
    encrypted_credentials TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (user_id, broker_type),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trading_instances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'STOPPED',
    pid         INTEGER,
    config_json TEXT NOT NULL DEFAULT '{}',
    restarts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    started_at  TEXT,
    stopped_at  TEXT,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trade_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    ticket      TEXT NOT NULL DEFAULT '',
    symbol      TEXT NOT NULL,
    direction   TEXT NOT NULL,
    entry_price REAL NOT NULL DEFAULT 0.0,
    exit_price  REAL NOT NULL DEFAULT 0.0,
    pnl         REAL NOT NULL DEFAULT 0.0,
    pnl_pips    REAL NOT NULL DEFAULT 0.0,
    exit_reason TEXT NOT NULL DEFAULT '',
    opened_at   TEXT,
    closed_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_config (
    user_id     INTEGER PRIMARY KEY,
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS password_resets (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trade_history_user ON trade_history (user_id, closed_at);
CREATE INDEX IF NOT EXISTS idx_broker_creds_user ON broker_credentials (user_id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thread-safe SQLite access layer for the API control plane."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    # ── connection plumbing ──────────────────────────────────────────────
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._path), timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
        logger.debug("[api-db] schema ready at {}", self._path)

    # ── users ────────────────────────────────────────────────────────────
    def create_user(
        self, email: str, password_hash: str, is_admin: bool = False
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, is_admin, created_at) "
                "VALUES (?, ?, ?, ?)",
                (email.lower(), password_hash, int(is_admin), _utcnow()),
            )
            return int(cur.lastrowid)

    def get_user_by_email(self, email: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email.lower(),)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET is_active = ? WHERE id = ?",
                (int(active), user_id),
            )

    def count_users(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    # ── broker credentials ───────────────────────────────────────────────
    def upsert_broker_credentials(
        self,
        user_id: int,
        broker_type: str,
        encrypted_credentials: str,
        label: str = "",
    ) -> None:
        now = _utcnow()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO broker_credentials
                    (user_id, broker_type, label, encrypted_credentials, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, broker_type) DO UPDATE SET
                    label = excluded.label,
                    encrypted_credentials = excluded.encrypted_credentials,
                    updated_at = excluded.updated_at
                """,
                (user_id, broker_type, label, encrypted_credentials, now, now),
            )

    def list_broker_credentials(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM broker_credentials WHERE user_id = ? ORDER BY broker_type",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_broker_credentials(
        self, user_id: int, broker_type: str
    ) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM broker_credentials WHERE user_id = ? AND broker_type = ?",
                (user_id, broker_type),
            ).fetchone()
            return dict(row) if row else None

    def delete_broker_credentials(self, user_id: int, broker_type: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM broker_credentials WHERE user_id = ? AND broker_type = ?",
                (user_id, broker_type),
            )
            return cur.rowcount > 0

    # ── trading instances ────────────────────────────────────────────────
    def upsert_instance(
        self,
        user_id: int,
        status: str,
        pid: Optional[int] = None,
        config_json: str = "{}",
    ) -> None:
        now = _utcnow()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trading_instances
                    (user_id, status, pid, config_json, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    status = excluded.status,
                    pid = excluded.pid,
                    config_json = excluded.config_json,
                    started_at = CASE WHEN excluded.status = 'RUNNING'
                                      THEN excluded.started_at
                                      ELSE trading_instances.started_at END,
                    updated_at = excluded.updated_at
                """,
                (user_id, status, pid, config_json, now, now),
            )

    def update_instance_status(
        self,
        user_id: int,
        status: str,
        pid: Optional[int] = None,
        last_error: Optional[str] = None,
        increment_restarts: bool = False,
    ) -> None:
        now = _utcnow()
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        if pid is not None or status == "STOPPED":
            sets.append("pid = ?")
            params.append(pid)
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)
        if increment_restarts:
            sets.append("restarts = restarts + 1")
        if status == "STOPPED":
            sets.append("stopped_at = ?")
            params.append(now)
        params.append(user_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE trading_instances SET {', '.join(sets)} WHERE user_id = ?",
                params,
            )

    def get_instance(self, user_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trading_instances WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_instances(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trading_instances ORDER BY user_id"
            ).fetchall()
            return [dict(r) for r in rows]

    # ── trade history ────────────────────────────────────────────────────
    def insert_trade(self, user_id: int, trade: dict[str, Any]) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trade_history
                    (user_id, ticket, symbol, direction, entry_price, exit_price,
                     pnl, pnl_pips, exit_reason, opened_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    str(trade.get("ticket", "")),
                    str(trade.get("symbol", "")),
                    str(trade.get("direction", "")),
                    float(trade.get("entry_price", 0.0) or 0.0),
                    float(trade.get("exit_price", 0.0) or 0.0),
                    float(trade.get("pnl", 0.0) or 0.0),
                    float(trade.get("pnl_pips", 0.0) or 0.0),
                    str(trade.get("exit_reason", "")),
                    trade.get("opened_at"),
                    trade.get("closed_at") or _utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def list_trades(
        self,
        user_id: int,
        limit: int = 100,
        offset: int = 0,
        symbol: Optional[str] = None,
        direction: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if since:
            clauses.append("closed_at >= ?")
            params.append(since)
        if until:
            clauses.append("closed_at <= ?")
            params.append(until)
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM trade_history WHERE {' AND '.join(clauses)} "
                "ORDER BY closed_at DESC, id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def trade_stats(self, user_id: int) -> dict[str, Any]:
        """Aggregate trade statistics for the dashboard summary."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*)                                   AS total,
                    COALESCE(SUM(pnl), 0.0)                     AS total_pnl,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                    COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) AS losses,
                    COALESCE(MAX(pnl), 0.0)                     AS best,
                    COALESCE(MIN(pnl), 0.0)                     AS worst
                FROM trade_history WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            data = dict(row) if row else {}
            total = int(data.get("total", 0) or 0)
            wins = int(data.get("wins", 0) or 0)
            data["win_rate"] = round(wins / total, 4) if total else 0.0
            return data

    def equity_curve(self, user_id: int, limit: int = 500) -> list[dict[str, Any]]:
        """Cumulative realized P&L over time (oldest → newest)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT closed_at, pnl FROM trade_history WHERE user_id = ? "
                "ORDER BY closed_at ASC, id ASC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        cumulative = 0.0
        curve: list[dict[str, Any]] = []
        for r in rows:
            cumulative += float(r["pnl"] or 0.0)
            curve.append({"closed_at": r["closed_at"], "equity": round(cumulative, 2)})
        return curve

    # ── user config ──────────────────────────────────────────────────────
    def set_user_config(self, user_id: int, config_json: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_config (user_id, config_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, config_json, _utcnow()),
            )

    def get_user_config(self, user_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT config_json FROM user_config WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row["config_json"] if row else None

    # ── password resets ──────────────────────────────────────────────────
    def create_password_reset(
        self, token: str, user_id: int, expires_at: str
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO password_resets (token, user_id, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (token, user_id, expires_at, _utcnow()),
            )

    def get_password_reset(self, token: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM password_resets WHERE token = ?", (token,)
            ).fetchone()
            return dict(row) if row else None

    def mark_password_reset_used(self, token: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE password_resets SET used = 1 WHERE token = ?", (token,)
            )
