"""APEX TRADER — Persistent position management state for event-driven mode.

Without a TradingLoop/TradeManager, position management state (breakeven,
trailing, partial close status, price extremes) is lost between evaluation
cycles.  This store persists that state in-memory keyed by position ticket,
with optional SQLite persistence for crash recovery.

Thread-safe: all mutations are guarded by an RLock.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


@dataclass
class ManagementState:
    """Per-position management state that persists across eval cycles."""

    ticket: str
    original_stop_loss: float = 0.0
    original_tp2: float = 0.0
    stop_loss: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    remaining_size_lots: float = 0.0
    pnl_pips: float = 0.0
    pnl_dollars: float = 0.0
    pip_size: float = 0.0001
    pip_value_per_lot: float = 10.0
    candles_since_entry: int = 0
    entry_timeframe: str = "M5"
    highest_price_since_entry: float = 0.0
    lowest_price_since_entry: float = float("inf")
    tp1_hit: bool = False
    at_breakeven: bool = False
    trailing: bool = False
    partial_closed: bool = False
    tp3: Optional[float] = None
    tp3_hit: bool = False
    status: str = "OPEN"
    plan_be_trigger_r: Optional[float] = None
    plan_trail_activation_r: Optional[float] = None
    plan_trail_strategy: Optional[str] = None
    plan_partial_ratio: Optional[float] = None
    strategic_structure_integrity: Optional[float] = None
    strategic_tf_alignment: Optional[float] = None
    strategic_assessment_time: Optional[datetime] = None
    last_eval_time: Optional[datetime] = None
    score_history: list = field(default_factory=list)
    # Entry-time setup quality (OQ/EQ), captured on the first management eval
    # so the decision engine can measure quality decay since entry. Mirrors the
    # backtest plane, which captures entry OQ/EQ from the WorldModel at open.
    entry_oq: Optional[float] = None
    entry_eq: Optional[float] = None
    # Transient (never persisted): a worker-path SL move is optimistically
    # written into ``stop_loss`` before the broker confirms it. While the modify
    # is in flight the synthetic stop-hit check must NOT fire against this
    # unconfirmed level — a broker rejection ("Invalid stops") would otherwise
    # leave the rejected SL in place long enough for a phantom stop-hit CLOSE.
    # ``sl_modify_pending_until`` is a monotonic deadline (auto-expires so the
    # guard can never get stuck); ``sl_pending_confirmation`` is the per-cycle
    # boolean derived from it that the snapshot carries to the worker.
    sl_modify_pending_until: float = 0.0
    sl_pending_confirmation: bool = False


from runtime_paths import data_dir as _data_dir  # noqa: E402

_DB_DIR = _data_dir()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS management_state (
    ticket              TEXT PRIMARY KEY,
    original_stop_loss  REAL NOT NULL DEFAULT 0.0,
    original_tp2        REAL NOT NULL DEFAULT 0.0,
    stop_loss           REAL NOT NULL DEFAULT 0.0,
    tp1                 REAL NOT NULL DEFAULT 0.0,
    tp2                 REAL NOT NULL DEFAULT 0.0,
    remaining_size_lots REAL NOT NULL DEFAULT 0.0,
    pip_size            REAL NOT NULL DEFAULT 0.0001,
    highest_price       REAL NOT NULL DEFAULT 0.0,
    lowest_price        REAL NOT NULL DEFAULT 1e18,
    tp1_hit             INTEGER NOT NULL DEFAULT 0,
    at_breakeven        INTEGER NOT NULL DEFAULT 0,
    trailing            INTEGER NOT NULL DEFAULT 0,
    partial_closed      INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'OPEN',
    last_update         TEXT NOT NULL
)
"""

_UPSERT = """
INSERT INTO management_state (
    ticket, original_stop_loss, original_tp2, stop_loss, tp1, tp2,
    remaining_size_lots, pip_size, highest_price, lowest_price,
    tp1_hit, at_breakeven, trailing, partial_closed, status, last_update
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticket) DO UPDATE SET
    stop_loss=excluded.stop_loss,
    tp1=excluded.tp1,
    tp2=excluded.tp2,
    remaining_size_lots=excluded.remaining_size_lots,
    highest_price=excluded.highest_price,
    lowest_price=excluded.lowest_price,
    tp1_hit=excluded.tp1_hit,
    at_breakeven=excluded.at_breakeven,
    trailing=excluded.trailing,
    partial_closed=excluded.partial_closed,
    status=excluded.status,
    last_update=excluded.last_update
"""


class ManagementStateStore:
    """Thread-safe store for position management state with optional SQLite persistence."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._states: dict[str, ManagementState] = {}
        # Signature of the last-persisted SQLite columns per ticket, so
        # ``persist`` can skip a write when nothing material changed this cycle.
        self._last_sig: dict[str, tuple] = {}
        self._lock = threading.RLock()
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        if db_path:
            self._init_db(db_path)

    def _init_db(self, db_path: str) -> None:
        try:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_CREATE_TABLE)
            self._conn.commit()
            self._load_from_db()
            logger.info(
                "[mgmt-state] SQLite persistence enabled — loaded {} states from {}",
                len(self._states), db_path,
            )
        except Exception as exc:
            logger.warning("[mgmt-state] SQLite init failed (in-memory only): {}", exc)
            self._conn = None

    def _load_from_db(self) -> None:
        if self._conn is None:
            return
        try:
            rows = self._conn.execute(
                "SELECT ticket, original_stop_loss, original_tp2, stop_loss, "
                "tp1, tp2, remaining_size_lots, pip_size, highest_price, "
                "lowest_price, tp1_hit, at_breakeven, trailing, partial_closed, "
                "status FROM management_state"
            ).fetchall()
            for row in rows:
                state = ManagementState(
                    ticket=row[0],
                    original_stop_loss=row[1],
                    original_tp2=row[2],
                    stop_loss=row[3],
                    tp1=row[4],
                    tp2=row[5],
                    remaining_size_lots=row[6],
                    pip_size=row[7],
                    highest_price_since_entry=row[8],
                    lowest_price_since_entry=row[9],
                    tp1_hit=bool(row[10]),
                    at_breakeven=bool(row[11]),
                    trailing=bool(row[12]),
                    partial_closed=bool(row[13]),
                    status=row[14],
                )
                self._states[state.ticket] = state
                self._last_sig[state.ticket] = self._sig(state)
        except Exception as exc:
            logger.warning("[mgmt-state] DB load failed: {}", exc)

    def _persist(self, state: ManagementState) -> None:
        if self._conn is None:
            return
        try:
            now_str = datetime.now(timezone.utc).isoformat()
            self._conn.execute(_UPSERT, (
                state.ticket,
                state.original_stop_loss,
                state.original_tp2,
                state.stop_loss,
                state.tp1,
                state.tp2,
                state.remaining_size_lots,
                state.pip_size,
                state.highest_price_since_entry,
                state.lowest_price_since_entry,
                int(state.tp1_hit),
                int(state.at_breakeven),
                int(state.trailing),
                int(state.partial_closed),
                state.status,
                now_str,
            ))
            self._conn.commit()
            self._last_sig[state.ticket] = self._sig(state)
        except Exception as exc:
            logger.debug("[mgmt-state] persist failed for {}: {}", state.ticket, exc)

    @staticmethod
    def _sig(state: ManagementState) -> tuple:
        """Signature of the persisted columns that matter for crash recovery.

        The volatile price extremes are deliberately excluded so a per-cycle
        ``persist`` is a no-op write when only the price drifted; the SL and
        breakeven/partial/tp1 flags (the columns that, if lost, cause wrong
        stops or a double partial close on recovery) are all included.
        """
        return (
            round(state.stop_loss, 8),
            round(state.tp1, 8),
            round(state.tp2, 8),
            round(state.remaining_size_lots, 8),
            bool(state.tp1_hit),
            bool(state.at_breakeven),
            bool(state.trailing),
            bool(state.partial_closed),
            state.status,
        )

    def persist(self, state: ManagementState, *, force: bool = False) -> bool:
        """Persist a state the caller mutated in place (e.g. the position
        worker updating trailing SL / breakeven / partial flags).

        Dirty-gated: writes to SQLite only when a persisted column actually
        changed since the last write, so calling it every evaluation cycle is
        cheap. Returns ``True`` if a write occurred.
        """
        with self._lock:
            if not force and self._sig(state) == self._last_sig.get(state.ticket):
                return False
            self._persist(state)
            return True

    def get(self, ticket: str) -> Optional[ManagementState]:
        with self._lock:
            return self._states.get(ticket)

    def get_or_create(self, ticket: str, **defaults) -> ManagementState:
        with self._lock:
            if ticket not in self._states:
                self._states[ticket] = ManagementState(ticket=ticket, **defaults)
                self._persist(self._states[ticket])
            return self._states[ticket]

    def update(self, ticket: str, **updates) -> None:
        with self._lock:
            state = self._states.get(ticket)
            if state is not None:
                for k, v in updates.items():
                    if hasattr(state, k):
                        setattr(state, k, v)
                self._persist(state)

    def remove(self, ticket: str) -> None:
        with self._lock:
            self._states.pop(ticket, None)
            self._last_sig.pop(ticket, None)
            if self._conn is not None:
                try:
                    self._conn.execute(
                        "DELETE FROM management_state WHERE ticket = ?", (ticket,),
                    )
                    self._conn.commit()
                except Exception as exc:
                    logger.debug("[mgmt-state] delete failed for {}: {}", ticket, exc)

    def cleanup(self, active_tickets: set[str]) -> int:
        """Remove states for positions no longer open. Returns count removed."""
        with self._lock:
            stale = [t for t in self._states if t not in active_tickets]
            for t in stale:
                del self._states[t]
                self._last_sig.pop(t, None)
            if self._conn is not None and stale:
                try:
                    placeholders = ",".join("?" for _ in stale)
                    self._conn.execute(
                        f"DELETE FROM management_state WHERE ticket IN ({placeholders})",
                        stale,
                    )
                    self._conn.commit()
                except Exception as exc:
                    logger.debug("[mgmt-state] cleanup DB failed: {}", exc)
            return len(stale)

    def all_tickets(self) -> set[str]:
        with self._lock:
            return set(self._states.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)

    def close(self) -> None:
        """Close the SQLite connection (call on shutdown)."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
