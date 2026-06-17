"""
APEX TRADER — Universal Signal Ledger

Every brain module emits a directional read every scan cycle — momentum sees a
SHORT, structure sees a LONG, the consensus collapses them into one verdict.
Historically only the signals that became *trades* were ever graded, so the
modules that were repeatedly overruled never learned whether they were right.
That is selection bias baked into the learning layer: the system can only ever
discover that its taken trades win or lose, never that a blocked signal would
have been profitable.

This ledger removes that blind spot.  It records EVERY signal at the moment of
emission — before any gate runs — together with the price at that instant.  A
background grading cycle then samples price after the signal and asks the only
question that matters independent of trade management: *did price actually move
in the predicted direction?*  Signals that became trades are linked to the
order; signals that a gate blocked record which gate blocked them.  Joining the
two halves yields per-emitter accuracy split by traded vs blocked — the data a
later phase needs to re-weight votes by track record and to tell whether a gate
is over-filtering profitable setups.

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

_DB_DIR = Path(__file__).parent.parent / "data"
_DB_PATH = _DB_DIR / "signal_ledger.db"

# Minimum signed move (in %) in the predicted direction for a signal to count as
# directionally correct at the primary grading check.
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
    direction_correct      INTEGER,
    max_favorable_move_pct REAL NOT NULL DEFAULT 0.0,
    max_adverse_move_pct   REAL NOT NULL DEFAULT 0.0,
    graded                 INTEGER NOT NULL DEFAULT 0,
    graded_at              REAL,
    updated_at             REAL,
    trade_outcome          TEXT
)
"""

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
    """A single directional read emitted by one module, before any gate runs."""

    pair: str
    emitter: str
    direction: str                      # "LONG" or "SHORT" (NEUTRAL is dropped)
    strength: float = 0.0               # the signal's own confidence / score
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
    """The graded result of a signal — did price move the predicted way?"""

    signal_id: str
    graded_at: float = 0.0
    price_at_signal: float = 0.0
    price_at_check: float = 0.0
    check_delays: dict = field(default_factory=dict)   # {"5": price, "15": price}
    direction_correct: bool = False
    max_favorable_move_pct: float = 0.0
    max_adverse_move_pct: float = 0.0
    trade_outcome: Optional[dict] = None


def _signed_move_pct(direction: str, price_at_signal: float, current: float) -> float:
    """Signed price move (%) in the *predicted* direction.

    Positive ⇒ price moved the way the signal called; negative ⇒ against it.
    Returns 0.0 when the reference price is unusable.
    """
    if not price_at_signal or price_at_signal <= 0.0:
        return 0.0
    raw = (current - price_at_signal) / price_at_signal * 100.0
    return raw if direction == "LONG" else -raw


class SignalLedger(TuningGuardMixin):
    """SQLite-backed universal signal recorder + grader. WAL-mode, thread-safe."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        grading_delay_minutes: float = 30.0,
        check_intervals: Optional[List[int]] = None,
        min_move_pct: float = _DEFAULT_MIN_MOVE_PCT,
        trade_outcome_provider: Optional[Callable[[str], Optional[dict]]] = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._grading_delay_minutes = float(grading_delay_minutes)
        self._check_intervals = sorted(check_intervals or [5, 15, 30, 60])
        self._min_move_pct = float(min_move_pct)
        # Optional hook: given a trade_id, return realised trade-outcome data to
        # merge into the signal outcome (e.g. a PostCloseTracker lookup). Kept
        # injectable so this module stays a leaf with no learning-layer imports.
        self._trade_outcome_provider = trade_outcome_provider
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SignalLedger] could not create db dir: {}", exc)
        self._connect()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _connect(self) -> None:
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
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SignalLedger] DB connect/init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[SignalLedger] conn.close() failed during cleanup")
                self._conn = None

    def set_trade_outcome_provider(
        self, provider: Optional[Callable[[str], Optional[dict]]],
    ) -> None:
        """Inject (or clear) the callable used to enrich graded signals that
        became trades with realised outcome data."""
        self._trade_outcome_provider = provider

    # ── Writing ────────────────────────────────────────────────────────────

    def record_signal(self, signal: SignalRecord) -> Optional[str]:
        """Record a freshly emitted signal (before any gate). Returns its id.

        NEUTRAL / empty-direction signals are ignored — there is nothing to
        grade. An accompanying ``signal_outcomes`` row is created so grading can
        accumulate price observations against it.
        """
        if self._conn is None or signal is None:
            return None
        if signal.direction not in ("LONG", "SHORT"):
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
        """Accumulate a price observation for one signal and finalise if due.

        Updates the running favorable/adverse excursion and records the price at
        each elapsed check interval. Once the primary grading delay has elapsed
        the signal is finalised: ``direction_correct`` is set from the signed
        move and (if it became a trade and a provider is set) the realised
        trade outcome is merged. Returns True if the signal became graded on
        this call.
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
                       WHERE o.graded = 0"""
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
                if self._apply_observation(sig, out, price):
                    graded += 1
            return {"observed": observed, "graded": graded}

    def _apply_observation(self, sig: dict, out: dict, price: Optional[float]) -> bool:
        """Core grading step (caller holds the lock). Returns True if finalised."""
        if price is None or price <= 0.0:
            return False
        now = time.time()
        price_at_signal = float(out.get("price_at_signal") or sig.get("price_at_signal") or 0.0)
        direction = sig.get("direction", "")
        signed = _signed_move_pct(direction, price_at_signal, float(price))

        max_fav = max(float(out.get("max_favorable_move_pct") or 0.0), max(0.0, signed))
        max_adv = max(float(out.get("max_adverse_move_pct") or 0.0), max(0.0, -signed))

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
        direction_correct = None
        graded_at = None
        trade_outcome_json = None
        if finalise:
            direction_correct = 1 if signed >= self._min_move_pct else 0
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
                       max_adverse_move_pct=?, direction_correct=?, graded=?,
                       graded_at=?, updated_at=?,
                       trade_outcome=COALESCE(?, trade_outcome)
                   WHERE signal_id=?""",
                (
                    round(float(price), 8),
                    json.dumps(delays),
                    round(max_fav, 6),
                    round(max_adv, 6),
                    direction_correct,
                    1 if finalise else 0,
                    graded_at,
                    now,
                    trade_outcome_json,
                    sig["signal_id"],
                ),
            )
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
            "direction_correct": _as_bool_or_none(out.get("direction_correct")),
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
                               o.graded, o.direction_correct, o.price_at_check,
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
            r["direction_correct"] = _as_bool_or_none(r.get("direction_correct"))
            r["context"] = _loads(r.get("context"))
            r["trade_outcome"] = _loads(r.get("trade_outcome")) or None
        return rows

    def get_emitter_accuracy(
        self, emitter: str, pair: Optional[str] = None, lookback_trades: int = 100,
    ) -> dict:
        """Accuracy stats for one emitter over its most recent graded signals.

        Splits accuracy into *all* / *traded* / *blocked* so the selection bias
        is visible: if blocked accuracy is high, a gate is over-filtering.
        """
        rows = self.get_graded_signals(emitter=emitter, pair=pair, lookback=lookback_trades)
        return _accuracy_from_rows(emitter, rows)

    def get_emitter_accuracy_all(self, lookback_trades: int = 100) -> Dict[str, dict]:
        """Per-emitter accuracy across all known emitters."""
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
            e: self.get_emitter_accuracy(e, lookback_trades=lookback_trades)
            for e in emitters
        }


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
        correct = sum(1 for r in items if r.get("direction_correct"))
        return round(correct / len(items), 4)

    correct_all = sum(1 for r in rows if r.get("direction_correct"))
    fav = [float(r.get("max_favorable_move_pct") or 0.0) for r in rows]
    adv = [float(r.get("max_adverse_move_pct") or 0.0) for r in rows]
    return {
        "emitter": emitter,
        "total": total,
        "correct": correct_all,
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
