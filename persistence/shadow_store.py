"""
APEX TRADER — Shadow Contract Store (Phase 4)

Persists hypothetical trade contracts for rejected/skipped setups and their
resolution outcomes. Uses sync-sqlite3 + WAL pattern matching event_store.py.

A shadow contract captures the exact trade the system *would* have opened
(entry, SL, TP1/TP2/TP3, direction, pip_size) so the resolver can replay
TradeManager.update() bar-by-bar and determine the counterfactual outcome.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

# ── Defaults ─────────────────────────────────────────────────────────────────

_DB_DIR = Path(__file__).parent.parent / "data"
_DB_PATH = _DB_DIR / "apex_shadow.db"

_CREATE_CONTRACTS = """
CREATE TABLE IF NOT EXISTS shadow_contracts (
    contract_id     TEXT PRIMARY KEY,
    correlation_id  TEXT,
    setup_id        TEXT,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    tp1             REAL NOT NULL,
    tp2             REAL NOT NULL,
    tp3             REAL,
    pip_size        REAL NOT NULL,
    position_size   REAL NOT NULL DEFAULT 0.01,
    entry_timeframe TEXT NOT NULL DEFAULT 'M5',
    rejecting_gate  TEXT NOT NULL,
    score           INTEGER NOT NULL DEFAULT 0,
    ts_utc_ms       INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    outcome         TEXT,
    r_multiple      REAL,
    exit_reason     TEXT,
    exit_price      REAL,
    resolution_ts   INTEGER,
    resolution_granularity TEXT,
    bars_replayed   INTEGER,
    resolver_meta   TEXT,
    source          TEXT NOT NULL DEFAULT 'planner'
)
"""

# Migration for DBs created before the `source` column existed. SQLite raises
# if the column is already present, so it is run best-effort.
_MIGRATE_SOURCE = (
    "ALTER TABLE shadow_contracts ADD COLUMN source TEXT NOT NULL DEFAULT 'planner'"
)

_CREATE_IDX_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_contracts (status)"
)
_CREATE_IDX_SYMBOL = (
    "CREATE INDEX IF NOT EXISTS idx_shadow_symbol ON shadow_contracts (symbol)"
)
_CREATE_IDX_TS = (
    "CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_contracts (ts_utc_ms)"
)
_CREATE_IDX_CORR = (
    "CREATE INDEX IF NOT EXISTS idx_shadow_corr ON shadow_contracts (correlation_id)"
)


@dataclass
class ShadowContract:
    contract_id: str
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    pip_size: float
    rejecting_gate: str
    ts_utc_ms: int
    correlation_id: Optional[str] = None
    setup_id: Optional[str] = None
    tp3: Optional[float] = None
    position_size: float = 0.01
    entry_timeframe: str = "M5"
    score: int = 0
    status: str = "PENDING"
    outcome: Optional[str] = None
    r_multiple: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    resolution_ts: Optional[int] = None
    resolution_granularity: Optional[str] = None
    bars_replayed: Optional[int] = None
    resolver_meta: Optional[str] = None
    source: str = "planner"


@dataclass
class ShadowResolution:
    outcome: str
    r_multiple: float
    exit_reason: str
    exit_price: float
    resolution_ts: int
    resolution_granularity: str
    bars_replayed: int
    resolver_meta: Optional[Dict[str, Any]] = None


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class ShadowStore:
    """SQLite store for shadow contracts — WAL-mode, thread-safe."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        # Serialises all access to the shared sqlite connection. The resolver
        # thread and the dashboard reader touch this store concurrently; without
        # a lock their cursors race on the single connection.
        self._lock = threading.RLock()
        self._connect()

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path),
                timeout=10,
                check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_CONTRACTS)
            self._migrate_source_column()
            self._conn.execute(_CREATE_IDX_STATUS)
            self._conn.execute(_CREATE_IDX_SYMBOL)
            self._conn.execute(_CREATE_IDX_TS)
            self._conn.execute(_CREATE_IDX_CORR)
            self._conn.commit()
        except Exception:
            print(
                f"[ShadowStore] DB connect/init failed: {self._db_path}",
                file=sys.stderr,
            )

    def _migrate_source_column(self) -> None:
        """Add the `source` column to pre-existing databases (best-effort)."""
        if self._conn is None:
            return
        try:
            self._conn.execute(_MIGRATE_SOURCE)
        except Exception:
            # Column already exists — nothing to do.
            pass

    def insert_contract(self, contract: ShadowContract) -> Optional[str]:
        if self._conn is None:
            return None
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO shadow_contracts
                       (contract_id, correlation_id, setup_id, symbol, direction,
                        entry_price, stop_loss, tp1, tp2, tp3, pip_size,
                        position_size, entry_timeframe, rejecting_gate, score,
                        ts_utc_ms, status, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        contract.contract_id,
                        contract.correlation_id,
                        contract.setup_id,
                        contract.symbol,
                        contract.direction,
                        contract.entry_price,
                        contract.stop_loss,
                        contract.tp1,
                        contract.tp2,
                        contract.tp3,
                        contract.pip_size,
                        contract.position_size,
                        contract.entry_timeframe,
                        contract.rejecting_gate,
                        contract.score,
                        contract.ts_utc_ms,
                        contract.status,
                        contract.source,
                    ),
                )
                self._conn.commit()
                return contract.contract_id
            except Exception as exc:
                logger.debug("[ShadowStore] insert_contract failed: {}", exc)
                return None

    def resolve_contract(
        self, contract_id: str, resolution: ShadowResolution,
    ) -> bool:
        if self._conn is None:
            return False
        with self._lock:
            try:
                meta_json = (
                    json.dumps(resolution.resolver_meta)
                    if resolution.resolver_meta
                    else None
                )
                self._conn.execute(
                    """UPDATE shadow_contracts
                       SET status=?, outcome=?, r_multiple=?, exit_reason=?,
                           exit_price=?, resolution_ts=?,
                           resolution_granularity=?, bars_replayed=?,
                           resolver_meta=?
                       WHERE contract_id=?""",
                    (
                        "RESOLVED",
                        resolution.outcome,
                        resolution.r_multiple,
                        resolution.exit_reason,
                        resolution.exit_price,
                        resolution.resolution_ts,
                        resolution.resolution_granularity,
                        resolution.bars_replayed,
                        meta_json,
                        contract_id,
                    ),
                )
                self._conn.commit()
                return True
            except Exception as exc:
                logger.debug("[ShadowStore] resolve_contract failed: {}", exc)
                return False

    def mark_expired(self, contract_id: str, bars_replayed: int) -> bool:
        if self._conn is None:
            return False
        with self._lock:
            try:
                self._conn.execute(
                    """UPDATE shadow_contracts
                       SET status='EXPIRED', outcome='EXPIRED',
                           resolution_ts=?, bars_replayed=?
                       WHERE contract_id=?""",
                    (_now_ms(), bars_replayed, contract_id),
                )
                self._conn.commit()
                return True
            except Exception as exc:
                logger.debug("[ShadowStore] mark_expired failed: {}", exc)
                return False

    def discard_stale_pending(self, older_than_ms: int) -> int:
        """Discard PENDING contracts older than the cutoff.

        Un-executed hypothetical setups that are too old to be worth resolving
        are marked DISCARDED so the backlog doesn't replay forever or grow
        unbounded. Returns the number discarded.
        """
        if self._conn is None:
            return 0
        with self._lock:
            try:
                cur = self._conn.execute(
                    """UPDATE shadow_contracts
                       SET status='DISCARDED', outcome='DISCARDED', resolution_ts=?
                       WHERE status='PENDING' AND ts_utc_ms < ?""",
                    (_now_ms(), older_than_ms),
                )
                self._conn.commit()
                return cur.rowcount or 0
            except Exception as exc:
                logger.debug("[ShadowStore] discard_stale_pending failed: {}", exc)
                return 0

    def get_pending(self, limit: int = 100) -> List[ShadowContract]:
        if self._conn is None:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT * FROM shadow_contracts WHERE status='PENDING' "
                    "ORDER BY ts_utc_ms ASC LIMIT ?",
                    (limit,),
                )
                cols = [d[0] for d in cur.description]
                return [ShadowContract(**dict(zip(cols, row))) for row in cur.fetchall()]
            except Exception as exc:
                logger.debug("[ShadowStore] get_pending failed: {}", exc)
                return []

    def get_resolved(
        self, symbol: Optional[str] = None, limit: int = 200,
    ) -> List[ShadowContract]:
        if self._conn is None:
            return []
        with self._lock:
            try:
                if symbol:
                    cur = self._conn.execute(
                        "SELECT * FROM shadow_contracts WHERE status='RESOLVED' "
                        "AND symbol=? ORDER BY ts_utc_ms DESC LIMIT ?",
                        (symbol, limit),
                    )
                else:
                    cur = self._conn.execute(
                        "SELECT * FROM shadow_contracts WHERE status='RESOLVED' "
                        "ORDER BY ts_utc_ms DESC LIMIT ?",
                        (limit,),
                    )
                cols = [d[0] for d in cur.description]
                return [ShadowContract(**dict(zip(cols, row))) for row in cur.fetchall()]
            except Exception as exc:
                logger.debug("[ShadowStore] get_resolved failed: {}", exc)
                return []

    def count_by_status(self) -> Dict[str, int]:
        if self._conn is None:
            return {}
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT status, COUNT(*) FROM shadow_contracts GROUP BY status"
                )
                return dict(cur.fetchall())
            except Exception as exc:
                logger.debug("[ShadowStore] count_by_status failed: {}", exc)
                return {}

    def get_outcomes_by_gate(self) -> List[Dict[str, Any]]:
        """Aggregate resolved+expired contracts grouped by rejecting_gate."""
        if self._conn is None:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    """SELECT rejecting_gate, outcome,
                              COUNT(*) as cnt,
                              AVG(r_multiple) as avg_r,
                              AVG(bars_replayed) as avg_bars
                       FROM shadow_contracts
                       WHERE status IN ('RESOLVED', 'EXPIRED')
                       GROUP BY rejecting_gate, outcome
                       ORDER BY rejecting_gate, outcome"""
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except Exception as exc:
                logger.debug("[ShadowStore] get_outcomes_by_gate failed: {}", exc)
                return []

    def get_gate_edge(self) -> List[Dict[str, Any]]:
        """Per-gate counterfactual edge of the setups each gate REJECTED.

        For every rejecting gate, reports how many rejected setups resolved,
        how many WOULD have won vs lost, and the EV (count-weighted mean R) of
        having taken them. Positive ``ev_r`` ⇒ the gate is rejecting net-
        profitable setups (suspect — it may be too strict); negative ``ev_r`` ⇒
        it is correctly filtering losers (earning its keep). EXPIRED setups are
        counted in ``total`` but excluded from ``ev_r``/``win_rate`` (they never
        resolved to a realized R). This is the decision-relevant view for tuning
        gates — rejection counts alone are misleading.
        """
        if self._conn is None:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    """SELECT rejecting_gate, outcome,
                              COUNT(*) AS cnt,
                              SUM(r_multiple) AS sum_r,
                              SUM(CASE WHEN r_multiple IS NOT NULL THEN 1 ELSE 0 END) AS n_r
                       FROM shadow_contracts
                       WHERE status IN ('RESOLVED', 'EXPIRED')
                       GROUP BY rejecting_gate, outcome"""
                )
                rows = cur.fetchall()
            except Exception as exc:
                logger.debug("[ShadowStore] get_gate_edge failed: {}", exc)
                return []

        _key = {
            "WIN": "wins", "LOSS": "losses", "BE": "be",
            "PARTIAL": "partial", "EXPIRED": "expired",
        }
        agg: Dict[str, Dict[str, Any]] = {}
        for gate, outcome, cnt, sum_r, n_r in rows:
            g = agg.setdefault(gate, {
                "gate": gate, "total": 0,
                "wins": 0, "losses": 0, "be": 0, "partial": 0, "expired": 0,
                "_sum_r": 0.0, "_n_r": 0,
            })
            cnt = int(cnt or 0)
            g["total"] += cnt
            field_name = _key.get(outcome)
            if field_name:
                g[field_name] += cnt
            if sum_r is not None:
                g["_sum_r"] += float(sum_r)
                g["_n_r"] += int(n_r or 0)

        out: List[Dict[str, Any]] = []
        for g in agg.values():
            n_r = g.pop("_n_r")
            sum_r = g.pop("_sum_r")
            decisive = g["wins"] + g["losses"]
            g["decisive"] = decisive
            g["ev_r"] = round(sum_r / n_r, 3) if n_r else 0.0
            g["win_rate"] = round(g["wins"] / decisive * 100, 1) if decisive else 0.0
            out.append(g)
        out.sort(key=lambda x: x["total"], reverse=True)
        return out

    def get_outcomes_by_symbol(self) -> List[Dict[str, Any]]:
        """Aggregate resolved+expired contracts grouped by symbol.

        Lets an operator see which pairs' REJECTED setups systematically win or
        lose — a gate/threshold signal. NOTE: this is deliberately NOT fed into
        the pair/regime/session learners: rejected-setup outcomes measure the
        quality of setups the bot DECLINED, not the pairs it trades, so mixing
        them into those learners would wrongly penalise good pairs. The sound
        automated consumer of rejected-setup outcomes is the gate auto-tuner.
        """
        if self._conn is None:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    """SELECT symbol, outcome,
                              COUNT(*) as cnt,
                              AVG(r_multiple) as avg_r
                       FROM shadow_contracts
                       WHERE status IN ('RESOLVED', 'EXPIRED')
                       GROUP BY symbol, outcome
                       ORDER BY symbol, outcome"""
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except Exception as exc:
                logger.debug("[ShadowStore] get_outcomes_by_symbol failed: {}", exc)
                return []

    def get_all_contracts(
        self,
        status: Optional[str] = None,
        rejecting_gate: Optional[str] = None,
        limit: int = 200,
    ) -> List[ShadowContract]:
        """Get contracts with optional filters, newest-first."""
        if self._conn is None:
            return []
        with self._lock:
            try:
                clauses: List[str] = []
                params: list = []
                if status:
                    clauses.append("status = ?")
                    params.append(status)
                if rejecting_gate:
                    clauses.append("rejecting_gate = ?")
                    params.append(rejecting_gate)
                where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
                cur = self._conn.execute(
                    f"SELECT * FROM shadow_contracts{where} ORDER BY ts_utc_ms DESC LIMIT ?",
                    (*params, limit),
                )
                cols = [d[0] for d in cur.description]
                return [ShadowContract(**dict(zip(cols, row))) for row in cur.fetchall()]
            except Exception as exc:
                logger.debug("[ShadowStore] get_all_contracts failed: {}", exc)
                return []

    def close(self) -> None:
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    logger.debug("[ShadowStore] conn.close() failed during cleanup")
                self._conn = None


def new_contract_id() -> str:
    return f"sc_{uuid.uuid4().hex[:12]}"
