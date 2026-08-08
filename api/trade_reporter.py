"""APEX TRADER — Trade reporting bridge (engine → API).

A deliberately tiny, dependency-light module that a running per-user trading
instance calls when a trade closes, so the API can serve trade history without
the engine and API sharing in-process state.

Activation is purely environment-driven:

* ``APEX_TRADE_REPORT_DB`` — path to the API's SQLite database
* ``APEX_USER_ID``         — the owning user id

If either is unset (i.e. a standalone single-user run that was NOT spawned by
the API), :func:`report_trade_close` is a no-op — so this has ZERO behavioural
impact outside the multi-tenant deployment. Every operation is best-effort and
swallows its own errors: a reporting failure must never disturb the trading
engine's close path.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_ENV_DB = "APEX_TRADE_REPORT_DB"
_ENV_USER = "APEX_USER_ID"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def report_trade_close(
    *,
    symbol: str,
    direction: str,
    pnl_dollars: float,
    pnl_pips: float = 0.0,
    ticket: str = "",
    entry_price: float = 0.0,
    exit_price: float = 0.0,
    exit_reason: str = "",
    opened_at: Optional[str] = None,
    closed_at: Optional[str] = None,
) -> bool:
    """Insert a completed trade into the API's ``trade_history`` table.

    Returns True if a row was written, False if reporting is disabled or failed.
    Never raises.
    """
    db_path = os.getenv(_ENV_DB, "").strip()
    user_id_raw = os.getenv(_ENV_USER, "").strip()
    if not db_path or not user_id_raw:
        return False  # standalone mode — reporting disabled

    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        return False

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=8000")
            conn.execute(
                """
                INSERT INTO trade_history
                    (user_id, ticket, symbol, direction, entry_price, exit_price,
                     pnl, pnl_pips, exit_reason, opened_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    str(ticket),
                    str(symbol),
                    str(direction),
                    float(entry_price or 0.0),
                    float(exit_price or 0.0),
                    float(pnl_dollars or 0.0),
                    float(pnl_pips or 0.0),
                    str(exit_reason or ""),
                    opened_at,
                    closed_at or _utcnow(),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — reporting must never break the close path
        return False
