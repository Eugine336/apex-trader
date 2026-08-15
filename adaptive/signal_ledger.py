"""
APEX TRADER — Universal Observation Ledger

Every brain module emits *observations* each scan cycle — structure sees higher
highs, liquidity sees a sweep, momentum measures acceleration. Modules are
measurement instruments, not directional voters (Constitution §XXIX): an
observation is graded on its *quality* — whether a material market event
actually followed it — never on whether price happened to move in some predicted
direction. Historically only observations that became *trades* were ever graded,
so a module whose observation was repeatedly overruled never learned whether it
was seeing something real. That is selection bias baked into the learning layer.

This ledger removes that blind spot. It records EVERY observation at the moment
of emission — before any gate runs — together with the price at that instant. A
background grading cycle then samples price after the observation and asks the
only question that matters independent of trade management: *did a material move
follow — in EITHER direction?* An observation that correctly flagged a liquidity
sweep is high-quality when meaningful movement followed, even if price ultimately
travelled against the observation's directional character. Observations that
became trades are linked to the order; observations a gate blocked record which
gate blocked them. Joining the two halves yields per-emitter observation quality
split by traded vs blocked — the data a later phase needs to re-weight modules by
track record and to tell whether a gate is over-filtering high-quality reads.

It is purely observational — nothing here changes a live decision.  Storage
mirrors the proven ``persistence.shadow_store`` pattern: sync sqlite3 in WAL
mode, exception-safe, survives restarts.

Leaf module — standard library + loguru only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from loguru import logger

from adaptive.tunable import TuningGuardMixin

# ── Defaults ─────────────────────────────────────────────────────────────────

from runtime_paths import data_dir as _data_dir  # noqa: E402

_DB_DIR = _data_dir()
_DB_PATH = _DB_DIR / "signal_ledger.db"
_ARCHIVE_DIR = _DB_DIR / "archive"

# Cap the number of ungraded signals processed in a single grading cycle so the
# scan loop never stalls on a large backlog. Remaining rows are picked up on
# subsequent cycles.
_GRADING_BATCH_LIMIT = 500

# Rolling retention: graded signals older than this are archived to a JSONL file
# (preserving the full history off the hot DB) and then deleted, keeping the
# live ledger bounded. Retention runs at most once per _RETENTION_INTERVAL_S.
_RETENTION_DAYS = 30
_RETENTION_INTERVAL_S = 3600.0

# Minimum realized move (in %, in EITHER direction) after an observation for it
# to count as a high-quality observation — i.e. a material market event actually
# followed. Direction-agnostic: a move for OR against the observation's
# directional character both confirm that the module saw something real.
_DEFAULT_MIN_MOVE_PCT = 0.1

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signal_ledger (
    signal_id        TEXT PRIMARY KEY,
    timestamp        REAL NOT NULL,
    pair             TEXT NOT NULL,
    emitter          TEXT NOT NULL,
    direction        TEXT NOT NULL,
    strength         REAL NOT NULL DEFAULT 0.0,
    price_at_signal  REAL NOT NULL DEFAULT 0.0,
    context          TEXT,
    trade_opened     INTEGER NOT NULL DEFAULT 0,
    gate_blocked_by  TEXT,
    trade_id         TEXT
)
"""

_CREATE_OUTCOMES = """
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id              TEXT PRIMARY KEY,
    price_at_signal        REAL NOT NULL DEFAULT 0.0,
    price_at_check         REAL,
    check_delays           TEXT,
    observation_useful     INTEGER,
    max_favorable_move_pct REAL NOT NULL DEFAULT 0.0,
    max_adverse_move_pct   REAL NOT NULL DEFAULT 0.0,
    graded                 INTEGER NOT NULL DEFAULT 0,
    graded_at              REAL,
    updated_at             REAL,
    trade_outcome          TEXT
)
"""

# Columns added / renamed after the outcomes table's first release — applied
# idempotently on connect so an older DB (which graded ``direction_correct``)
# gains the ``observation_useful`` column without losing history. Old graded rows
# keep their legacy column (now unread) and read as ungraded-for-quality until
# retention ages them out.
_MIGRATE_OUTCOME_COLS = (
    ("observation_useful", "INTEGER"),
)

_CREATE_IDX_EMITTER = (
    "CREATE INDEX IF NOT EXISTS idx_signal_emitter ON signal_ledger (emitter)"
)
_CREATE_IDX_PAIR = (
    "CREATE INDEX IF NOT EXISTS idx_signal_pair ON signal_ledger (pair)"
)
_CREATE_IDX_TS = (
    "CREATE INDEX IF NOT EXISTS idx_signal_ts ON signal_ledger (timestamp)"
)
_CREATE_IDX_GRADED = (
    "CREATE INDEX IF NOT EXISTS idx_outcome_graded ON signal_outcomes (graded)"
)


@dataclass
class SignalRecord:
    """A single observation emitted by one module, before any gate runs.

    Modules are measurement instruments (Constitution §XXIX): ``direction`` is
    the observation's directional *character* (e.g. a bullish FVG), recorded for
    provenance only — it never drives grading. Non-directional observations are
    valid and recorded too (``direction`` may be empty / "NEUTRAL"). Grading asks
    only whether a material move followed, in either direction.
    """

    pair: str
    emitter: str
    direction: str = ""                 # observed directional character (provenance only)
    strength: float = 0.0               # the observation's own confidence / magnitude
    price_at_signal: float = 0.0        # instrument price at emission
    context: dict = field(default_factory=dict)
    signal_id: str = ""
    timestamp: float = 0.0
    trade_opened: bool = False
    gate_blocked_by: Optional[str] = None
    trade_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.signal_id:
            self.signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class SignalOutcome:
    """The graded result of an observation — did a material move follow it?"""

    signal_id: str
    graded_at: float = 0.0
    price_at_signal: float = 0.0
    price_at_check: float = 0.0
    check_delays: dict = field(default_factory=dict)   # {"5": price, "15": price}
    observation_useful: bool = False
    max_favorable_move_pct: float = 0.0
    max_adverse_move_pct: float = 0.0
    trade_outcome: Optional[dict] = None


def _signed_move_pct(direction: str, price_at_signal: float, current: float) -> float:
    """Signed price move (%) relative to an observation's directional character.

    Positive ⇒ price moved with the observed character; negative ⇒ against it.
    Retained for provenance/context only — grading uses the direction-agnostic
    :func:`_abs_move_pct`. Returns 0.0 when the reference price is unusable.
    """
    if not price_at_signal or price_at_signal <= 0.0:
        return 0.0
    raw = (current - price_at_signal) / price_at_signal * 100.0
    return raw if direction == "LONG" else -raw


def _abs_move_pct(price_at_signal: float, current: float) -> float:
    """Absolute price move (%) since emission — direction-agnostic.

    This is the magnitude of the market event that followed an observation,
    regardless of which way it went. Returns 0.0 when the reference price is
    unusable.
    """
    if not price_at_signal or price_at_signal <= 0.0:
        return 0.0
    return abs((current - price_at_signal) / price_at_signal * 100.0)


class SignalLedger(TuningGuardMixin):
    """SQLite-backed universal signal recorder + grader. WAL-mode, thread-safe."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        grading_delay_minutes: float = 30.0,
        check_intervals: Optional[List[int]] = None,
        min_move_pct: float = _DEFAULT_MIN_MOVE_PCT,
        accuracy_lookback: int = 100,
        trade_outcome_provider: Optional[Callable[[str], Optional[dict]]] = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._grading_delay_minutes = float(grading_delay_minutes)
        self._check_intervals = sorted(check_intervals or [5, 15, 30, 60])
        self._min_move_pct = float(min_move_pct)
        # Default rolling window (most-recent graded signals) for accuracy
        # aggregation when a caller does not specify its own lookback.
        self._accuracy_lookback = int(accuracy_lookback) if accuracy_lookback and accuracy_lookback > 0 else 100
        # Optional hook: given a trade_id, return realised trade-outcome data to
        # merge into the signal outcome (e.g. a PostCloseTracker lookup). Kept
        # injectable so this module stays a leaf with no learning-layer imports.
        self._trade_outcome_provider = trade_outcome_provider
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._last_retention_ts: float = 0.0
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SignalLedger] could not create db dir: {}", exc)
        self._connect()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        # Connect with one retry on failure, then raise. A silent in-memory
        # fallback would make every persistence op a DEBUG-level no-op and lose
        # the full signal history on restart; raising lets the caller leave the
        # subsystem None visibly instead of running a zombie ledger.
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                self._conn = sqlite3.connect(
                    str(self._db_path), timeout=10, check_same_thread=False,
                )
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute(_CREATE_SIGNALS)
                self._conn.execute(_CREATE_OUTCOMES)
                self._conn.execute(_CREATE_IDX_EMITTER)
                self._conn.execute(_CREATE_IDX_PAIR)
                self._conn.execute(_CREATE_IDX_TS)
                self._conn.execute(_CREATE_IDX_GRADED)
                self._migrate_outcomes()
                self._conn.commit()
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self._conn = None
                logger.error(
                    "[SignalLedger] DB connect/init failed (attempt {}/2) ({}): {}",
                    attempt, self._db_path, exc,
                )
                if attempt == 1:
                    time.sleep(1.0)
        raise RuntimeError(
            f"SignalLedger DB connect failed after retry ({self._db_path}): {last_exc}"
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[SignalLedger] conn.close() failed during cleanup")
                self._conn = None

    def _migrate_outcomes(self) -> None:
        """Add post-release columns to the outcomes table if missing.

        Idempotent and exception-safe: a fresh DB already has the columns (from
        ``_CREATE_OUTCOMES``); an older DB (which graded a now-retired
        ``direction_correct``) gains ``observation_useful`` via ALTER TABLE
        without losing rows. Caller holds the connection; the caller commits.
        """
        if self._conn is None:
            return
        try:
            cur = self._conn.execute("PRAGMA table_info(signal_outcomes)")
            existing = {str(r[1]) for r in cur.fetchall()}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[SignalLedger] table_info failed: {}", exc)
            return
        for col, decl in _MIGRATE_OUTCOME_COLS:
            if col in existing:
                continue
            try:
                self._conn.execute(
                    f"ALTER TABLE signal_outcomes ADD COLUMN {col} {decl}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] add column {} failed: {}", col, exc)

    def set_trade_outcome_provider(
        self, provider: Optional[Callable[[str], Optional[dict]]],
    ) -> None:
        """Inject (or clear) the callable used to enrich graded signals that
        became trades with realised outcome data."""
        self._trade_outcome_provider = provider

    # ── Writing ────────────────────────────────────────────────────────────

    def record_signal(self, signal: SignalRecord) -> Optional[str]:
        """Record a freshly emitted observation (before any gate). Returns its id.

        Observations of any directional character are recorded — modules are
        measurement instruments, so a non-directional / "NEUTRAL" observation is
        just as gradeable (did a material move follow?) as a directional one. A
        record with no pair or emitter carries no observation and is ignored. An
        accompanying ``signal_outcomes`` row is created so grading can accumulate
        price observations against it.
        """
        if self._conn is None or signal is None:
            return None
        if not signal.pair or not signal.emitter:
            return None
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO signal_ledger
                       (signal_id, timestamp, pair, emitter, direction, strength,
                        price_at_signal, context, trade_opened, gate_blocked_by,
                        trade_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        signal.signal_id,
                        float(signal.timestamp),
                        str(signal.pair),
                        str(signal.emitter),
                        str(signal.direction),
                        float(signal.strength or 0.0),
                        float(signal.price_at_signal or 0.0),
                        json.dumps(signal.context or {}, default=str),
                        1 if signal.trade_opened else 0,
                        signal.gate_blocked_by,
                        signal.trade_id,
                    ),
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO signal_outcomes
                       (signal_id, price_at_signal, check_delays, graded, updated_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        signal.signal_id,
                        float(signal.price_at_signal or 0.0),
                        json.dumps({}),
                        0,
                        time.time(),
                    ),
                )
                self._conn.commit()
                return signal.signal_id
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] record_signal failed: {}", exc)
                return None

    def record_signals(self, signals: List[SignalRecord]) -> int:
        """Batch-record many freshly emitted observations in ONE transaction.

        Equivalent to calling ``record_signal`` per item but issues a single
        ``executemany`` + ``commit`` instead of two INSERTs and a commit per
        observation — the per-cycle scan path emits dozens of observations, so
        this removes the per-observation fsync that was stalling the loop.
        Records with no pair/emitter carry no observation and are skipped.
        Returns the number of observations written.
        """
        if self._conn is None or not signals:
            return 0
        valid = [s for s in signals if s is not None and s.pair and s.emitter]
        if not valid:
            return 0
        ledger_rows = [
            (
                s.signal_id,
                float(s.timestamp),
                str(s.pair),
                str(s.emitter),
                str(s.direction),
                float(s.strength or 0.0),
                float(s.price_at_signal or 0.0),
                json.dumps(s.context or {}, default=str),
                1 if s.trade_opened else 0,
                s.gate_blocked_by,
                s.trade_id,
            )
            for s in valid
        ]
        now = time.time()
        outcome_rows = [
            (s.signal_id, float(s.price_at_signal or 0.0), json.dumps({}), 0, now)
            for s in valid
        ]
        with self._lock:
            try:
                self._conn.executemany(
                    """INSERT OR REPLACE INTO signal_ledger
                       (signal_id, timestamp, pair, emitter, direction, strength,
                        price_at_signal, context, trade_opened, gate_blocked_by,
                        trade_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    ledger_rows,
                )
                self._conn.executemany(
                    """INSERT OR IGNORE INTO signal_outcomes
                       (signal_id, price_at_signal, check_delays, graded, updated_at)
                       VALUES (?,?,?,?,?)""",
                    outcome_rows,
                )
                self._conn.commit()
                return len(valid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] record_signals batch failed: {}", exc)
                return 0

    def record_gate_block(self, signal_id: str, gate_name: str) -> bool:
        """Mark a recorded signal as blocked by a named gate."""
        if self._conn is None or not signal_id:
            return False
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE signal_ledger SET gate_blocked_by=? WHERE signal_id=?",
                    (str(gate_name), signal_id),
                )
                self._conn.commit()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] record_gate_block failed: {}", exc)
                return False

    def record_gate_block_for_pair(
        self, pair: str, gate_name: str, *, since: Optional[float] = None,
    ) -> int:
        """Block all of a pair's ungraded, not-yet-traded signals.

        Convenience for the centralised rejection hook: a gate rejects a pair,
        so every signal emitted for that pair this cycle (and not already linked
        to a trade) is attributed to that gate. Returns the number updated.
        """
        if self._conn is None or not pair:
            return 0
        with self._lock:
            try:
                params: list = [str(gate_name), str(pair)]
                clause = "pair=? AND trade_opened=0 AND gate_blocked_by IS NULL"
                if since is not None:
                    clause += " AND timestamp>=?"
                    params.append(float(since))
                cur = self._conn.execute(
                    f"UPDATE signal_ledger SET gate_blocked_by=? WHERE {clause}",
                    params,
                )
                self._conn.commit()
                return cur.rowcount or 0
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] record_gate_block_for_pair failed: {}", exc)
                return 0

    def record_trade_opened(self, signal_id: str, trade_id: str) -> bool:
        """Link a signal to the trade it produced (clears any gate block)."""
        if self._conn is None or not signal_id:
            return False
        with self._lock:
            try:
                self._conn.execute(
                    """UPDATE signal_ledger
                       SET trade_opened=1, trade_id=?, gate_blocked_by=NULL
                       WHERE signal_id=?""",
                    (str(trade_id), signal_id),
                )
                self._conn.commit()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] record_trade_opened failed: {}", exc)
                return False

    def record_trade_opened_for_pair(
        self,
        pair: str,
        trade_id: str,
        direction: Optional[str] = None,
        *,
        since: Optional[float] = None,
    ) -> int:
        """Link a pair's emitted signals to a trade that was just opened.

        Convenience for the centralised trade-open hook. When ``direction`` is
        given only matching-direction signals are linked (the modules that
        actually called the trade). Returns the number updated.
        """
        if self._conn is None or not pair:
            return 0
        with self._lock:
            try:
                params: list = [str(trade_id), str(pair)]
                clause = "pair=?"
                if direction in ("LONG", "SHORT"):
                    clause += " AND direction=?"
                    params.append(direction)
                if since is not None:
                    clause += " AND timestamp>=?"
                    params.append(float(since))
                cur = self._conn.execute(
                    f"""UPDATE signal_ledger
                        SET trade_opened=1, trade_id=?, gate_blocked_by=NULL
                        WHERE {clause}""",
                    params,
                )
                self._conn.commit()
                return cur.rowcount or 0
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] record_trade_opened_for_pair failed: {}", exc)
                return 0

    # ── Grading ────────────────────────────────────────────────────────────

    def grade_signal(self, signal_id: str, current_prices: Dict[str, float]) -> bool:
        """Accumulate a price observation for one record and finalise if due.

        Updates the running up/down excursion and records the price at each
        elapsed check interval. Once the primary grading delay has elapsed the
        record is finalised: ``observation_useful`` is set from the largest
        realized move in EITHER direction and (if it became a trade and a
        provider is set) the realised trade outcome is merged. Returns True if
        the observation became graded on this call.
        """
        if self._conn is None or not signal_id:
            return False
        with self._lock:
            sig = self._fetch_signal(signal_id)
            out = self._fetch_outcome(signal_id)
            if sig is None or out is None or out.get("graded"):
                return False
            price = current_prices.get(sig["pair"]) if current_prices else None
            return self._apply_observation(sig, out, price)

    def run_grading_cycle(self, current_prices: Dict[str, float]) -> dict:
        """Grade every ungraded signal against the supplied current prices.

        Call this once per scan cycle with a {pair: price} map. Returns a small
        summary {"observed": n, "graded": n} for logging/telemetry.
        """
        # Blocked (no grading performed) when the Tuner Agent is sole authority
        # and this is a direct call rather than an agent-driven one.
        if self._tuning_blocked("run_grading_cycle"):
            return {"observed": 0, "graded": 0}
        if self._conn is None:
            return {"observed": 0, "graded": 0}
        with self._lock:
            try:
                cur = self._conn.execute(
                    """SELECT s.signal_id FROM signal_ledger s
                       JOIN signal_outcomes o ON o.signal_id = s.signal_id
                       WHERE o.graded = 0
                       ORDER BY s.timestamp ASC
                       LIMIT ?""",
                    (_GRADING_BATCH_LIMIT,),
                )
                ids = [r[0] for r in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] run_grading_cycle query failed: {}", exc)
                return {"observed": 0, "graded": 0}

            observed = 0
            graded = 0
            for sid in ids:
                sig = self._fetch_signal(sid)
                out = self._fetch_outcome(sid)
                if sig is None or out is None:
                    continue
                price = current_prices.get(sig["pair"]) if current_prices else None
                if price is None:
                    continue
                observed += 1
                # Defer per-row commits — one commit for the whole batch below.
                if self._apply_observation(sig, out, price, commit=False):
                    graded += 1
            if observed:
                try:
                    self._conn.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[SignalLedger] run_grading_cycle commit failed: {}", exc)
        # Retention runs outside the grading transaction (its own lock scope).
        self._maybe_run_retention()
        return {"observed": observed, "graded": graded}

    def _maybe_run_retention(self) -> None:
        """Archive + delete graded signals older than the retention window.

        Throttled to once per _RETENTION_INTERVAL_S. Old graded rows are written
        to data/archive/signal_ledger_<date>.jsonl (full history preserved) and
        then removed from the live DB so it stays bounded. Fully guarded.
        """
        if self._conn is None:
            return
        now = time.time()
        if now - self._last_retention_ts < _RETENTION_INTERVAL_S:
            return
        self._last_retention_ts = now
        cutoff = now - (_RETENTION_DAYS * 86400.0)
        with self._lock:
            try:
                cur = self._conn.execute(
                    """SELECT s.signal_id, s.timestamp, s.pair, s.emitter,
                              s.direction, s.strength, s.price_at_signal,
                              s.context, s.trade_opened, s.gate_blocked_by,
                              s.trade_id, o.graded, o.observation_useful,
                              o.price_at_check, o.max_favorable_move_pct,
                              o.max_adverse_move_pct, o.graded_at, o.trade_outcome
                       FROM signal_ledger s
                       JOIN signal_outcomes o ON o.signal_id = s.signal_id
                       WHERE o.graded = 1 AND s.timestamp < ?
                       ORDER BY s.timestamp ASC""",
                    (cutoff,),
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                if not rows:
                    return
                if self._archive_rows(rows):
                    ids = [(r["signal_id"],) for r in rows]
                    self._conn.executemany(
                        "DELETE FROM signal_outcomes WHERE signal_id=?", ids
                    )
                    self._conn.executemany(
                        "DELETE FROM signal_ledger WHERE signal_id=?", ids
                    )
                    self._conn.commit()
                    logger.info(
                        "[SignalLedger] retention archived+pruned {} graded signals "
                        "older than {}d", len(rows), _RETENTION_DAYS,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] retention failed: {}", exc)

    def _archive_rows(self, rows: List[dict]) -> bool:
        """Append rows to a dated JSONL archive. Returns True on success.

        Deletion only proceeds if archiving succeeded, so retention never loses
        data on a write failure.
        """
        try:
            _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timezone

            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = _ARCHIVE_DIR / f"signal_ledger_{stamp}.jsonl"
            with open(path, "a", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, default=str) + "\n")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SignalLedger] archive write failed, skipping prune: {}", exc)
            return False

    def _apply_observation(
        self, sig: dict, out: dict, price: Optional[float], *, commit: bool = True
    ) -> bool:
        """Core grading step (caller holds the lock). Returns True if finalised.

        When ``commit`` is False the UPDATE is staged on the connection but not
        committed — the batch caller (run_grading_cycle) issues a single commit
        for the whole cycle instead of one fsync per row.
        """
        if price is None or price <= 0.0:
            return False
        now = time.time()
        price_at_signal = float(out.get("price_at_signal") or sig.get("price_at_signal") or 0.0)
        # Direction-agnostic excursions: the largest move up and down since the
        # observation. Grading measures the MAGNITUDE of the market event that
        # followed, never whether it matched the observation's directional
        # character (Constitution §XXIX — observation quality, not correctness).
        raw_pct = _abs_move_pct(price_at_signal, float(price))
        signed_raw = 0.0
        if price_at_signal and price_at_signal > 0.0:
            signed_raw = (float(price) - price_at_signal) / price_at_signal * 100.0
        up_move = max(0.0, signed_raw)
        down_move = max(0.0, -signed_raw)

        max_fav = max(float(out.get("max_favorable_move_pct") or 0.0), up_move)
        max_adv = max(float(out.get("max_adverse_move_pct") or 0.0), down_move)

        try:
            delays = json.loads(out.get("check_delays") or "{}")
        except (TypeError, ValueError):
            delays = {}
        elapsed_min = (now - float(sig.get("timestamp") or now)) / 60.0
        for interval in self._check_intervals:
            key = str(interval)
            if elapsed_min >= interval and key not in delays:
                delays[key] = round(float(price), 8)

        finalise = elapsed_min >= self._grading_delay_minutes
        observation_useful = None
        graded_at = None
        trade_outcome_json = None
        if finalise:
            # High-quality observation ⇔ a material move followed in EITHER
            # direction (largest excursion so far vs the min-move floor).
            realized_move = max(max_fav, max_adv, raw_pct)
            observation_useful = 1 if realized_move >= self._min_move_pct else 0
            graded_at = now
            if sig.get("trade_opened") and sig.get("trade_id") and self._trade_outcome_provider:
                try:
                    to = self._trade_outcome_provider(sig["trade_id"])
                    if to:
                        trade_outcome_json = json.dumps(to, default=str)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[SignalLedger] trade_outcome_provider failed: {}", exc)

        try:
            self._conn.execute(
                """UPDATE signal_outcomes
                   SET price_at_check=?, check_delays=?, max_favorable_move_pct=?,
                       max_adverse_move_pct=?, observation_useful=?, graded=?,
                       graded_at=?, updated_at=?,
                       trade_outcome=COALESCE(?, trade_outcome)
                   WHERE signal_id=?""",
                (
                    round(float(price), 8),
                    json.dumps(delays),
                    round(max_fav, 6),
                    round(max_adv, 6),
                    observation_useful,
                    1 if finalise else 0,
                    graded_at,
                    now,
                    trade_outcome_json,
                    sig["signal_id"],
                ),
            )
            if commit:
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[SignalLedger] _apply_observation update failed: {}", exc)
            return False
        return bool(finalise)

    def attach_trade_outcome(self, trade_id: str, outcome: dict) -> int:
        """Merge realised trade-outcome data onto every signal that drove a trade.

        Lets the close path push the final result (PnL, R, exit cause) back to
        the contributing signals without the ledger needing to import the
        learning layer. Returns the number of outcome rows updated.
        """
        if self._conn is None or not trade_id:
            return 0
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT signal_id FROM signal_ledger WHERE trade_id=?",
                    (str(trade_id),),
                )
                ids = [r[0] for r in cur.fetchall()]
                payload = json.dumps(outcome or {}, default=str)
                for sid in ids:
                    self._conn.execute(
                        "UPDATE signal_outcomes SET trade_outcome=?, updated_at=? WHERE signal_id=?",
                        (payload, time.time(), sid),
                    )
                self._conn.commit()
                return len(ids)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] attach_trade_outcome failed: {}", exc)
                return 0

    # ── Reading / aggregation ─────────────────────────────────────────────

    def _fetch_signal(self, signal_id: str) -> Optional[dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM signal_ledger WHERE signal_id=?", (signal_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            rec = dict(zip(cols, row))
            rec["trade_opened"] = bool(rec.get("trade_opened"))
            return rec
        except Exception as exc:  # noqa: BLE001
            logger.debug("[SignalLedger] _fetch_signal failed: {}", exc)
            return None

    def _fetch_outcome(self, signal_id: str) -> Optional[dict]:
        try:
            cur = self._conn.execute(
                "SELECT * FROM signal_outcomes WHERE signal_id=?", (signal_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            rec = dict(zip(cols, row))
            rec["graded"] = bool(rec.get("graded"))
            return rec
        except Exception as exc:  # noqa: BLE001
            logger.debug("[SignalLedger] _fetch_outcome failed: {}", exc)
            return None

    def get_signal(self, signal_id: str) -> Optional[dict]:
        """Return the full joined signal + outcome record (or None)."""
        if self._conn is None:
            return None
        with self._lock:
            sig = self._fetch_signal(signal_id)
            if sig is None:
                return None
            out = self._fetch_outcome(signal_id) or {}
        sig["context"] = _loads(sig.get("context"))
        merged = dict(sig)
        merged["outcome"] = {
            "graded": bool(out.get("graded")),
            "observation_useful": _as_bool_or_none(out.get("observation_useful")),
            "price_at_check": out.get("price_at_check"),
            "check_delays": _loads(out.get("check_delays")),
            "max_favorable_move_pct": out.get("max_favorable_move_pct"),
            "max_adverse_move_pct": out.get("max_adverse_move_pct"),
            "trade_outcome": _loads(out.get("trade_outcome")) or None,
        }
        return merged

    def get_graded_signals(
        self,
        emitter: Optional[str] = None,
        pair: Optional[str] = None,
        lookback: int = 100,
        *,
        include_ungraded: bool = False,
    ) -> List[dict]:
        """Joined signal+outcome rows, newest-first, with optional filters."""
        if self._conn is None:
            return []
        with self._lock:
            try:
                clauses: List[str] = []
                params: list = []
                if not include_ungraded:
                    clauses.append("o.graded=1")
                if emitter:
                    clauses.append("s.emitter=?")
                    params.append(emitter)
                if pair:
                    clauses.append("s.pair=?")
                    params.append(pair)
                where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
                params.append(int(lookback) if lookback and lookback > 0 else 1000000)
                cur = self._conn.execute(
                    f"""SELECT s.signal_id, s.timestamp, s.pair, s.emitter,
                               s.direction, s.strength, s.context, s.trade_opened,
                               s.gate_blocked_by, s.trade_id,
                               o.graded, o.observation_useful, o.price_at_check,
                               o.max_favorable_move_pct, o.max_adverse_move_pct,
                               o.trade_outcome
                        FROM signal_ledger s
                        JOIN signal_outcomes o ON o.signal_id = s.signal_id
                        {where}
                        ORDER BY s.timestamp DESC
                        LIMIT ?""",
                    params,
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] get_graded_signals failed: {}", exc)
                return []
        for r in rows:
            r["trade_opened"] = bool(r.get("trade_opened"))
            r["graded"] = bool(r.get("graded"))
            r["observation_useful"] = _as_bool_or_none(r.get("observation_useful"))
            r["context"] = _loads(r.get("context"))
            r["trade_outcome"] = _loads(r.get("trade_outcome")) or None
        return rows

    def get_emitter_accuracy(
        self, emitter: str, pair: Optional[str] = None, lookback_trades: Optional[int] = None,
    ) -> dict:
        """Observation-quality stats for one emitter over its recent graded rows.

        ``accuracy`` is the fraction of the emitter's observations that were
        high-quality (a material move followed). Split into *all* / *traded* /
        *blocked* so the selection bias is visible: if blocked quality is high, a
        gate is over-filtering real reads. When ``lookback_trades`` is None the
        configured default window is used.
        """
        lb = int(lookback_trades) if lookback_trades else self._accuracy_lookback
        rows = self.get_graded_signals(emitter=emitter, pair=pair, lookback=lb)
        return _accuracy_from_rows(emitter, rows)

    def get_emitter_accuracy_all(self, lookback_trades: Optional[int] = None) -> Dict[str, dict]:
        """Per-emitter accuracy across all known emitters."""
        lb = int(lookback_trades) if lookback_trades else self._accuracy_lookback
        if self._conn is None:
            return {}
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT DISTINCT emitter FROM signal_ledger"
                )
                emitters = [r[0] for r in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] get_emitter_accuracy_all failed: {}", exc)
                return {}
        return {
            e: self.get_emitter_accuracy(e, lookback_trades=lb)
            for e in emitters
        }

    def count_graded_since(
        self, emitter: str, since_ts: float, pair: Optional[str] = None,
    ) -> int:
        """Count an emitter's GRADED signals emitted at/after ``since_ts``.

        Unlike :meth:`get_graded_signals` (a fixed most-recent-N window), this is
        an absolute time count, so a consumer can measure how many graded signals
        accrued *since* an event (e.g. a module entering SHADOW) without being
        capped by the rolling lookback. Returns 0 when no DB / on error.
        """
        if self._conn is None or not emitter:
            return 0
        with self._lock:
            try:
                clauses = ["o.graded=1", "s.emitter=?", "s.timestamp>=?"]
                params: list = [str(emitter), float(since_ts or 0.0)]
                if pair:
                    clauses.append("s.pair=?")
                    params.append(str(pair))
                where = " AND ".join(clauses)
                cur = self._conn.execute(
                    f"""SELECT COUNT(*)
                        FROM signal_ledger s
                        JOIN signal_outcomes o ON o.signal_id = s.signal_id
                        WHERE {where}""",
                    params,
                )
                row = cur.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
            except Exception as exc:  # noqa: BLE001
                logger.debug("[SignalLedger] count_graded_since failed: {}", exc)
                return 0


# ── Helpers ──────────────────────────────────────────────────────────────────

def _loads(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


def _as_bool_or_none(v) -> Optional[bool]:
    if v is None:
        return None
    return bool(v)


def _accuracy_from_rows(emitter: str, rows: List[dict]) -> dict:
    total = len(rows)
    traded = [r for r in rows if r.get("trade_opened")]
    blocked = [r for r in rows if (not r.get("trade_opened")) and r.get("gate_blocked_by")]

    def _acc(items: List[dict]) -> float:
        if not items:
            return 0.0
        useful = sum(1 for r in items if r.get("observation_useful"))
        return round(useful / len(items), 4)

    useful_all = sum(1 for r in rows if r.get("observation_useful"))
    fav = [float(r.get("max_favorable_move_pct") or 0.0) for r in rows]
    adv = [float(r.get("max_adverse_move_pct") or 0.0) for r in rows]
    return {
        "emitter": emitter,
        "total": total,
        "useful": useful_all,
        "accuracy": _acc(rows),
        "traded": len(traded),
        "blocked": len(blocked),
        "accuracy_traded": _acc(traded),
        "accuracy_blocked": _acc(blocked),
        "avg_max_favorable_pct": round(sum(fav) / len(fav), 4) if fav else 0.0,
        "avg_max_adverse_pct": round(sum(adv) / len(adv), 4) if adv else 0.0,
    }


def new_signal_id() -> str:
    return f"sig_{uuid.uuid4().hex[:12]}"
