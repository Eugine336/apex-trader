"""
APEX TRADER — Phase 6: Historical Backfill

Idempotent migration of historical data from trade_journal.db,
apex_positions.db, and apex_shadow.db into the append-only event store.

Every backfilled event carries origin="backfill" + backfill_run_id +
source_db + source_row_id so it is always distinguishable from live events.

Usage:
    python -m persistence.backfill              # execute backfill
    python -m persistence.backfill --dry-run    # report counts without writing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from loguru import logger

from persistence.domain_events import (
    DECISION_REJECT,
    TRADE_CLOSE,
    TRADE_OPEN,
    SHADOW_RESOLVED,
)

_DATA_DIR = Path(__file__).parent.parent / "data"

_JOURNAL_DB = _DATA_DIR / "trade_journal.db"
_POSITION_DB = _DATA_DIR / "apex_positions.db"
_SHADOW_DB = _DATA_DIR / "apex_shadow.db"
_EVENTS_DB = _DATA_DIR / "apex_events.db"

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

BACKFILL_DERIVED_TYPE = "TRADE_CLOSE_DERIVED"


def _deterministic_id(source_db: str, source_row_id: str, event_type: str) -> str:
    raw = f"backfill:{source_db}:{source_row_id}:{event_type}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _ts_to_ms(ts_str: str) -> int:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return int(datetime.now(timezone.utc).timestamp() * 1000)


def _open_readonly(path: Path) -> Optional[sqlite3.Connection]:
    if not path.exists():
        logger.info("[backfill] Source not found, skipping: {}", path)
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as exc:
        logger.warning("[backfill] Cannot open {}: {}", path, exc)
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


class BackfillRunner:
    def __init__(
        self,
        events_db: Path = _EVENTS_DB,
        journal_db: Path = _JOURNAL_DB,
        position_db: Path = _POSITION_DB,
        shadow_db: Path = _SHADOW_DB,
        dry_run: bool = False,
    ):
        self.events_db = events_db
        self.journal_db = journal_db
        self.position_db = position_db
        self.shadow_db = shadow_db
        self.dry_run = dry_run
        self.run_id = uuid4().hex[:8]
        self.stats: Dict[str, int] = {
            "trades_observed": 0,
            "trades_derived": 0,
            "trades_derived_skipped": 0,
            "decisions": 0,
            "positions": 0,
            "shadow": 0,
            "skipped_existing": 0,
            "errors": 0,
        }
        self._conn: Optional[sqlite3.Connection] = None

    def _connect_events(self) -> None:
        self.events_db.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.events_db), timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_CREATE_EVENTS)
        self._conn.commit()

    def _event_exists(self, event_id: str) -> bool:
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
        )
        return cur.fetchone() is not None

    def _insert_event(
        self,
        event_id: str,
        ts_utc_ms: int,
        event_type: str,
        severity: str,
        symbol: Optional[str],
        source_module: str,
        payload: Dict[str, Any],
        correlation_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> bool:
        if self._event_exists(event_id):
            self.stats["skipped_existing"] += 1
            return False
        if self.dry_run:
            return True
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                correlation_id,
                parent_id,
                ts_utc_ms,
                event_type,
                severity,
                symbol,
                source_module,
                json.dumps(payload),
            ),
        )
        return True

    def _backfill_marker(
        self, source_db: str, source_row_id: str
    ) -> Dict[str, str]:
        return {
            "origin": "backfill",
            "backfill_run_id": self.run_id,
            "source_db": source_db,
            "source_row_id": str(source_row_id),
        }

    def backfill_trades(self) -> int:
        conn = _open_readonly(self.journal_db)
        if conn is None:
            return 0
        if not _table_exists(conn, "trades"):
            logger.info("[backfill] trades table not found in trade_journal.db")
            conn.close()
            return 0
        try:
            rows = conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
        except Exception as exc:
            logger.warning("[backfill] Failed to read trades: {}", exc)
            conn.close()
            return 0

        count = 0
        for row in rows:
            row_id = str(row["id"])
            eid = _deterministic_id("trade_journal.trades", row_id, TRADE_CLOSE)
            payload = {
                **self._backfill_marker("trade_journal.trades", row_id),
                "pair": row["pair"],
                "direction": row["direction"],
                "entry_price": row["entry"],
                "close_price": row["exit"],
                "pnl": row["pnl"],
                "score": row["score"],
                "outcome": row["outcome"],
                "regime": row["regime"],
                "session": row["session"],
                "entry_type": row["entry_type"],
                "exit_reason": "UNKNOWN",
                "exit_reason_source": "backfill_unrecoverable",
                "raw_broker_reason": row["outcome"],
            }
            try:
                pnl_dollars = row["pnl_dollars"]
                if pnl_dollars is not None:
                    payload["pnl_dollars"] = pnl_dollars
            except (IndexError, KeyError):
                logger.debug("[backfill] pnl_dollars column not present for trade {}", row_id)

            try:
                if self._insert_event(
                    event_id=eid,
                    ts_utc_ms=_ts_to_ms(row["timestamp"]),
                    event_type=TRADE_CLOSE,
                    severity="INFO",
                    symbol=row["pair"],
                    source_module="backfill",
                    payload=payload,
                ):
                    count += 1
                    self.stats["trades_observed"] += 1
            except Exception as exc:
                logger.warning("[backfill] trade row {} failed: {}", row_id, exc)
                self.stats["errors"] += 1

        conn.close()
        return count

    def backfill_trades_derived(self) -> int:
        pos_conn = _open_readonly(self.position_db)
        journal_conn = _open_readonly(self.journal_db)
        if journal_conn is None:
            return 0
        if not _table_exists(journal_conn, "trades"):
            journal_conn.close()
            return 0

        positions: Dict[str, sqlite3.Row] = {}
        if pos_conn and _table_exists(pos_conn, "managed_positions"):
            try:
                for p in pos_conn.execute("SELECT * FROM managed_positions").fetchall():
                    key = f"{p['symbol']}_{p['direction']}"
                    positions[key] = p
            except Exception as exc:
                logger.debug("[backfill] Cannot read positions for derived: {}", exc)
            pos_conn.close()

        try:
            trades = journal_conn.execute(
                "SELECT * FROM trades ORDER BY id ASC"
            ).fetchall()
        except Exception as exc:
            logger.warning("[backfill] Failed to read trades for derived: {}", exc)
            journal_conn.close()
            return 0

        count = 0
        for row in trades:
            row_id = str(row["id"])
            pair = row["pair"]
            direction = row["direction"]
            exit_price = row["exit"]
            entry_price = row["entry"]

            pos = positions.get(f"{pair}_{direction}")
            if pos is None:
                self.stats["trades_derived_skipped"] += 1
                continue

            sl = pos["sl"]
            tp1 = pos["tp1"]
            tp2 = pos["tp2"]
            pip_size = 0.0001
            try:
                from config import get_pip_size
                pip_size = get_pip_size(pair)
            except Exception:
                pip_size = 0.0001 if "JPY" not in pair.upper() else 0.01

            tolerance = pip_size * 5

            derived_reason = "UNKNOWN_DERIVED"
            if sl and abs(exit_price - sl) <= tolerance:
                derived_reason = "STOP_LOSS_HIT"
            elif tp1 and abs(exit_price - tp1) <= tolerance:
                derived_reason = "TAKE_PROFIT_1_HIT"
            elif tp2 and abs(exit_price - tp2) <= tolerance:
                derived_reason = "TAKE_PROFIT_2_HIT"

            eid = _deterministic_id(
                "trade_journal.trades", row_id, BACKFILL_DERIVED_TYPE
            )
            payload = {
                **self._backfill_marker("trade_journal.trades", row_id),
                "pair": pair,
                "direction": direction,
                "entry_price": entry_price,
                "close_price": exit_price,
                "exit_reason": derived_reason,
                "exit_reason_source": "backfill_derived_price_comparison",
                "derived_sl": sl,
                "derived_tp1": tp1,
                "derived_tp2": tp2,
                "tolerance_pips": 5,
                "outcome": row["outcome"],
            }

            try:
                if self._insert_event(
                    event_id=eid,
                    ts_utc_ms=_ts_to_ms(row["timestamp"]),
                    event_type=BACKFILL_DERIVED_TYPE,
                    severity="INFO",
                    symbol=pair,
                    source_module="backfill",
                    payload=payload,
                ):
                    count += 1
                    self.stats["trades_derived"] += 1
            except Exception as exc:
                logger.warning("[backfill] derived trade {} failed: {}", row_id, exc)
                self.stats["errors"] += 1

        journal_conn.close()
        return count

    def backfill_decisions(self) -> int:
        conn = _open_readonly(self.journal_db)
        if conn is None:
            return 0
        if not _table_exists(conn, "decisions"):
            logger.info("[backfill] decisions table not found")
            conn.close()
            return 0
        try:
            rows = conn.execute("SELECT * FROM decisions ORDER BY id ASC").fetchall()
        except Exception as exc:
            logger.warning("[backfill] Failed to read decisions: {}", exc)
            conn.close()
            return 0

        count = 0
        for row in rows:
            row_id = str(row["id"])
            eid = _deterministic_id(
                "trade_journal.decisions", row_id, DECISION_REJECT
            )
            payload = {
                **self._backfill_marker("trade_journal.decisions", row_id),
                "pair": row["pair"],
                "direction": row["direction"],
                "score": row["score"],
                "reason_rejected": row["reason_rejected"],
            }

            try:
                if self._insert_event(
                    event_id=eid,
                    ts_utc_ms=_ts_to_ms(row["timestamp"]),
                    event_type=DECISION_REJECT,
                    severity="INFO",
                    symbol=row["pair"],
                    source_module="backfill",
                    payload=payload,
                ):
                    count += 1
                    self.stats["decisions"] += 1
            except Exception as exc:
                logger.warning("[backfill] decision {} failed: {}", row_id, exc)
                self.stats["errors"] += 1

        conn.close()
        return count

    def backfill_positions(self) -> int:
        conn = _open_readonly(self.position_db)
        if conn is None:
            return 0
        if not _table_exists(conn, "managed_positions"):
            logger.info("[backfill] managed_positions table not found")
            conn.close()
            return 0
        try:
            rows = conn.execute(
                "SELECT * FROM managed_positions ORDER BY open_time ASC"
            ).fetchall()
        except Exception as exc:
            logger.warning("[backfill] Failed to read positions: {}", exc)
            conn.close()
            return 0

        count = 0
        for row in rows:
            oid = row["order_id"]
            eid = _deterministic_id("apex_positions.managed_positions", oid, TRADE_OPEN)
            payload = {
                **self._backfill_marker("apex_positions.managed_positions", oid),
                "order_id": oid,
                "platform": row["platform"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "lots": row["lots"],
                "entry_price": row["entry_price"],
                "sl": row["sl"],
                "tp1": row["tp1"],
                "tp2": row["tp2"],
                "score": row["score"],
                "regime": row["regime"],
                "session": row["session"],
                "entry_type": row["entry_type"],
            }

            try:
                if self._insert_event(
                    event_id=eid,
                    ts_utc_ms=_ts_to_ms(row["open_time"]),
                    event_type=TRADE_OPEN,
                    severity="INFO",
                    symbol=row["symbol"],
                    source_module="backfill",
                    payload=payload,
                ):
                    count += 1
                    self.stats["positions"] += 1
            except Exception as exc:
                logger.warning("[backfill] position {} failed: {}", oid, exc)
                self.stats["errors"] += 1

        conn.close()
        return count

    def backfill_shadow(self) -> int:
        conn = _open_readonly(self.shadow_db)
        if conn is None:
            return 0
        if not _table_exists(conn, "shadow_contracts"):
            logger.info("[backfill] shadow_contracts table not found")
            conn.close()
            return 0
        try:
            rows = conn.execute(
                "SELECT * FROM shadow_contracts WHERE status != 'PENDING' "
                "ORDER BY ts_utc_ms ASC"
            ).fetchall()
        except Exception as exc:
            logger.warning("[backfill] Failed to read shadow_contracts: {}", exc)
            conn.close()
            return 0

        count = 0
        for row in rows:
            cid = row["contract_id"]
            eid = _deterministic_id(
                "apex_shadow.shadow_contracts", cid, SHADOW_RESOLVED
            )
            payload = {
                **self._backfill_marker("apex_shadow.shadow_contracts", cid),
                "contract_id": cid,
                "symbol": row["symbol"],
                "direction": row["direction"],
                "entry_price": row["entry_price"],
                "stop_loss": row["stop_loss"],
                "tp1": row["tp1"],
                "tp2": row["tp2"],
                "rejecting_gate": row["rejecting_gate"],
                "outcome": row["outcome"],
                "r_multiple": row["r_multiple"],
                "exit_reason": row["exit_reason"],
                "exit_price": row["exit_price"],
                "resolution_granularity": row["resolution_granularity"],
                "bars_replayed": row["bars_replayed"],
            }

            try:
                if self._insert_event(
                    event_id=eid,
                    ts_utc_ms=row["ts_utc_ms"],
                    event_type=SHADOW_RESOLVED,
                    severity="INFO",
                    symbol=row["symbol"],
                    source_module="backfill",
                    payload=payload,
                    correlation_id=row["correlation_id"],
                ):
                    count += 1
                    self.stats["shadow"] += 1
            except Exception as exc:
                logger.warning("[backfill] shadow {} failed: {}", cid, exc)
                self.stats["errors"] += 1

        conn.close()
        return count

    def run(self) -> Dict[str, int]:
        mode = "DRY RUN" if self.dry_run else "LIVE"
        logger.info("[backfill] Starting {} (run_id={})", mode, self.run_id)

        if not self.dry_run:
            self._connect_events()
        else:
            self.events_db.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.events_db), timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_CREATE_EVENTS)
            self._conn.commit()

        try:
            t = self.backfill_trades()
            logger.info("[backfill] trades (observed): {}", t)

            d = self.backfill_trades_derived()
            logger.info("[backfill] trades (derived): {} (skipped={})",
                        d, self.stats["trades_derived_skipped"])

            dec = self.backfill_decisions()
            logger.info("[backfill] decisions: {}", dec)

            p = self.backfill_positions()
            logger.info("[backfill] positions: {}", p)

            s = self.backfill_shadow()
            logger.info("[backfill] shadow: {}", s)

            if not self.dry_run:
                assert self._conn is not None
                self._conn.commit()

            logger.info("[backfill] Complete. stats={}", self.stats)
        finally:
            if self._conn:
                self._conn.close()
                self._conn = None

        return dict(self.stats)


def main() -> None:
    parser = argparse.ArgumentParser(description="APEX historical backfill")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be written without writing",
    )
    args = parser.parse_args()

    runner = BackfillRunner(dry_run=args.dry_run)
    stats = runner.run()

    print(json.dumps(stats, indent=2), file=sys.stdout)


if __name__ == "__main__":
    main()
