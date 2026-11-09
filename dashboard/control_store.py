"""Durable, local audit store for authenticated dashboard controls."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_paths import data_dir


class DashboardControlStore:
    """Persist operator control state and append-only action history in SQLite."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = Path(db_path) if db_path is not None else data_dir() / "dashboard_controls.db"
        self._lock = threading.RLock()

    @contextmanager
    def _connect(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS operator_controls (
                    control_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operator_control_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    previous_json TEXT,
                    requested_json TEXT,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operator_control_audit_id
                    ON operator_control_audit (id DESC);
                """
            )
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, control_key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM operator_controls WHERE control_key = ?",
                (control_key,),
            ).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, control_key: str, value: Any, *, action: str, actor: str = "dashboard_api") -> None:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value_json FROM operator_controls WHERE control_key = ?",
                (control_key,),
            ).fetchone()
            previous = row[0] if row else None
            conn.execute(
                """
                INSERT INTO operator_controls (control_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (control_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (control_key, encoded, now),
            )
            conn.execute(
                """
                INSERT INTO operator_control_audit
                    (actor, action, previous_json, requested_json, outcome, created_at)
                VALUES (?, ?, ?, ?, 'accepted', ?)
                """,
                (actor, action, previous, encoded, now),
            )

    def record(self, action: str, *, outcome: str, actor: str = "dashboard_api") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operator_control_audit
                    (actor, action, outcome, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (actor, action, outcome, now),
            )

    def apply_to(self, event_driven_system: Any) -> dict[str, Any]:
        """Restore durable entry pause and manual freeze before live operation."""
        paused = self.get("entries_paused", False) is True
        mode = self.get("manual_risk_mode")
        context = getattr(event_driven_system, "_ctx", None)
        guard = getattr(context, "drawdown_guard", None) if context is not None else None
        if mode == "FROZEN" and guard is not None:
            from brain.drawdown_guard import DrawdownMode
            guard.mode = DrawdownMode.FROZEN
        # A persisted freeze with no guard cannot be safely honored through
        # the account-risk authority, so fail closed at the entry boundary.
        event_driven_system._paused = paused or (mode == "FROZEN" and guard is None)
        return {"entries_paused": paused, "manual_risk_mode": mode}

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, actor, action, previous_json, requested_json, outcome, created_at
                FROM operator_control_audit ORDER BY id DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [
            {
                "id": row[0], "actor": row[1], "action": row[2],
                "previous": json.loads(row[3]) if row[3] else None,
                "requested": json.loads(row[4]) if row[4] else None,
                "outcome": row[5], "created_at": row[6],
            }
            for row in rows
        ]


__all__ = ["DashboardControlStore"]
