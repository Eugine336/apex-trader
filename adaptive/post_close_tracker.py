"""
APEX TRADER — Post-Close Price Tracker (Learning Layer)

Win/loss alone is a contaminated signal: it blends *entry quality* (was the
read right?) with *management quality* (did we hold/stop it well?). A trade can
lose despite a perfect entry — stopped out one tick before price ran 3R our way
— or win despite leaving most of the move on the table. The learning layer
(including the Session 1 ``win_rate_provider``) was sizing on that mixed number.

This module separates the two. After a trade closes it schedules forward price
checks at fixed offsets from ENTRY (T+5m, T+15m, T+30m, T+1h). At each check it
measures Maximum Favorable / Adverse Excursion (MFE / MAE) over the window, in
both price and R units, and from those derives:

  * ``signal_quality``      — did price actually move our way? (correct / marginal / wrong)
  * ``management_quality``  — bad_signal | bad_management | conservative_management | good_management
  * ``optimal_sl_r``        — the SL distance (in R) the trade actually needed to survive (= MAE)
  * ``optimal_tp_r``        — the TP distance (in R) the move could have captured (= MFE)

It is **data collection only** — nothing here changes a live decision. Future
sessions consume the accessors (signal accuracy, management breakdown, optimal
SL stats) to upgrade sizing and stop placement.

Storage is a dedicated ``post_close_checks`` table alongside the existing trade
journal DB, plus a small ``post_close_pending`` table so in-flight checks
survive a restart. Synchronous stdlib ``sqlite3`` (short-lived connections) so
it can run inside the scan loop without dragging in the async journal stack.

Leaf module — pandas + loguru + stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

# Per-user writeable state — the post-close tracker DB resolves under the owning
# user's data tree (APEX_DATA_DIR) rather than a bare cwd-relative ``data/`` path.
from runtime_paths import data_dir as _data_dir

_DEFAULT_INTERVALS = [5, 15, 30, 60]
_MAX_RETRIES = 3
# Bounds for how many M1 bars to request when reconstructing the excursion
# window — enough to cover the longest check horizon with headroom, capped so a
# stale-entry swing trade can never ask for an unbounded history.
_MIN_BARS = 60
_MAX_BARS = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Best-effort UTC datetime from a datetime or ISO string."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_long(direction: str) -> bool:
    return str(direction).upper() in ("BUY", "LONG")


@dataclass
class CheckResult:
    """Metrics for a single completed check interval."""

    interval_minutes: int
    check_timestamp: str
    price_at_check: Optional[float]
    mfe: Optional[float]
    mae: Optional[float]
    mfe_r: Optional[float]
    mae_r: Optional[float]
    r_at_check: Optional[float]
    incomplete: bool = False


@dataclass
class PendingCheck:
    """A trade awaiting its forward price checks."""

    trade_id: str
    pair: str
    direction: str
    entry_price: float
    exit_price: float
    exit_cause: str
    sl_price: float
    tp_price: float
    entry_timestamp: datetime
    exit_timestamp: datetime
    entry_score: int = 0
    entry_confluences: list = field(default_factory=list)
    intervals: list[int] = field(default_factory=lambda: list(_DEFAULT_INTERVALS))
    done: list[int] = field(default_factory=list)
    retries: dict[int, int] = field(default_factory=dict)
    results: dict[int, CheckResult] = field(default_factory=dict)

    @property
    def sl_distance(self) -> float:
        """Price distance that equals 1R (entry → stop). 0 when unknown."""
        try:
            return abs(float(self.entry_price) - float(self.sl_price))
        except (TypeError, ValueError):
            return 0.0

    @property
    def remaining(self) -> list[int]:
        return [iv for iv in self.intervals if iv not in self.done]

    @property
    def finished(self) -> bool:
        return not self.remaining

    # ── (de)serialisation for the pending table ──────────────────────────

    def to_json(self) -> str:
        return json.dumps(
            {
                "trade_id": self.trade_id,
                "pair": self.pair,
                "direction": self.direction,
                "entry_price": self.entry_price,
                "exit_price": self.exit_price,
                "exit_cause": self.exit_cause,
                "sl_price": self.sl_price,
                "tp_price": self.tp_price,
                "entry_timestamp": self.entry_timestamp.isoformat(),
                "exit_timestamp": self.exit_timestamp.isoformat(),
                "entry_score": self.entry_score,
                "entry_confluences": list(self.entry_confluences),
                "intervals": list(self.intervals),
                "done": list(self.done),
                "retries": {str(k): v for k, v in self.retries.items()},
            },
            default=str,
        )

    @classmethod
    def from_json(cls, blob: str) -> "PendingCheck":
        d = json.loads(blob)
        return cls(
            trade_id=str(d["trade_id"]),
            pair=str(d["pair"]),
            direction=str(d["direction"]),
            entry_price=float(d["entry_price"]),
            exit_price=float(d["exit_price"]),
            exit_cause=str(d.get("exit_cause", "")),
            sl_price=float(d.get("sl_price", 0.0) or 0.0),
            tp_price=float(d.get("tp_price", 0.0) or 0.0),
            entry_timestamp=_parse_ts(d["entry_timestamp"]) or _utcnow(),
            exit_timestamp=_parse_ts(d["exit_timestamp"]) or _utcnow(),
            entry_score=int(d.get("entry_score", 0) or 0),
            entry_confluences=list(d.get("entry_confluences", []) or []),
            intervals=list(d.get("intervals", _DEFAULT_INTERVALS)),
            done=list(d.get("done", [])),
            retries={int(k): int(v) for k, v in (d.get("retries", {}) or {}).items()},
        )


# ── Pure metric helpers (unit-tested directly) ───────────────────────────────


def compute_excursions(
    bars,
    entry_price: float,
    direction: str,
    entry_ts: datetime,
    check_ts: datetime,
    sl_distance: float,
) -> Optional[dict]:
    """Compute MFE/MAE (price + R) over [entry_ts, check_ts] from M1 OHLCV.

    ``bars`` is a DataFrame with ``time``/``high``/``low`` columns (the shape
    every connector's ``get_ohlcv`` returns). Returns ``None`` when no bar
    falls inside the window (price data not yet available / too sparse).
    """
    if bars is None or len(bars) == 0:
        return None
    if "time" not in bars.columns or "high" not in bars.columns or "low" not in bars.columns:
        return None

    times = bars["time"]
    # Normalise to tz-aware UTC for comparison.
    try:
        mask = (times >= entry_ts) & (times <= check_ts)
    except TypeError:
        # Naive index — coerce.
        import pandas as pd

        times = pd.to_datetime(times, utc=True)
        mask = (times >= entry_ts) & (times <= check_ts)
    window = bars[mask.values] if hasattr(mask, "values") else bars[mask]
    if window is None or len(window) == 0:
        return None

    hi = float(window["high"].max())
    lo = float(window["low"].min())
    long = _is_long(direction)
    if long:
        mfe = max(0.0, hi - entry_price)
        mae = max(0.0, entry_price - lo)
    else:
        mfe = max(0.0, entry_price - lo)
        mae = max(0.0, hi - entry_price)

    r = sl_distance if sl_distance and sl_distance > 1e-12 else None
    return {
        "mfe": mfe,
        "mae": mae,
        "mfe_r": (mfe / r) if r else None,
        "mae_r": (mae / r) if r else None,
        "bars_used": int(len(window)),
    }


def classify_signal_quality(best_mfe_r: Optional[float]) -> str:
    """correct (MFE ≥ 1R) | marginal (≥ 0.5R) | wrong (< 0.5R) | unknown."""
    if best_mfe_r is None:
        return "unknown"
    if best_mfe_r >= 1.0:
        return "correct"
    if best_mfe_r >= 0.5:
        return "marginal"
    return "wrong"


def classify_management_quality(
    actual_r: Optional[float],
    best_mfe_r: Optional[float],
    signal_correct: bool,
) -> str:
    """Attribute the outcome to the signal vs the management.

    * loss + signal_correct  → bad_management (price ran our way, we lost)
    * loss + signal wrong     → bad_signal
    * win  + MFE ≫ captured   → conservative_management (money left on table)
    * win  + MFE ≈ captured   → good_management
    """
    if actual_r is None or best_mfe_r is None:
        return "unknown"
    is_win = actual_r > 0
    if not is_win:
        return "bad_management" if signal_correct else "bad_signal"
    # Win: did we leave a lot on the table?
    if best_mfe_r >= actual_r * 1.5 and (best_mfe_r - actual_r) >= 0.5:
        return "conservative_management"
    return "good_management"


class PostCloseTracker:
    """Schedules + records forward price checks after every trade close."""

    _SCHEMA_CHECKS = """
        CREATE TABLE IF NOT EXISTS post_close_checks (
            trade_id TEXT, pair TEXT, direction TEXT,
            entry_price REAL, exit_price REAL, exit_cause TEXT,
            sl_price REAL, tp_price REAL, sl_distance_r REAL,
            entry_timestamp TEXT, exit_timestamp TEXT,
            check_interval_minutes INTEGER, check_timestamp TEXT,
            price_at_check REAL, mfe REAL, mae REAL,
            mfe_r REAL, mae_r REAL, r_at_check REAL,
            signal_quality TEXT, management_quality TEXT,
            optimal_sl_r REAL, optimal_tp_r REAL
        )
    """
    _SCHEMA_PENDING = """
        CREATE TABLE IF NOT EXISTS post_close_pending (
            trade_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """

    def __init__(
        self,
        config=None,
        db_path: str | None = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._enabled = bool(getattr(config, "enabled", True)) if config is not None else True
        intervals = getattr(config, "check_intervals_minutes", None) if config is not None else None
        self.intervals = [int(x) for x in intervals] if intervals else list(_DEFAULT_INTERVALS)
        self.max_retries = int(getattr(config, "max_retries", _MAX_RETRIES)) if config is not None else _MAX_RETRIES
        self._clock = clock or _utcnow
        self.db_path = Path(db_path) if db_path else (_data_dir() / "trade_journal.db")
        self._pending: dict[str, PendingCheck] = {}
        self._init_db()
        self._load_pending()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ── DB plumbing ──────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(self._SCHEMA_CHECKS)
                conn.execute(self._SCHEMA_PENDING)
                conn.commit()
        except Exception as exc:
            logger.warning("[post_close] DB init failed: {}", exc)

    def _load_pending(self) -> None:
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT trade_id, state FROM post_close_pending").fetchall()
        except Exception as exc:
            logger.debug("[post_close] pending reload skipped: {}", exc)
            return
        for row in rows:
            try:
                pc = PendingCheck.from_json(row["state"])
                self._pending[pc.trade_id] = pc
            except Exception as exc:
                logger.debug("[post_close] could not reload pending {}: {}", row["trade_id"], exc)
        if self._pending:
            logger.info("[post_close] reloaded {} pending check(s) from disk", len(self._pending))

    def _persist_pending(self, pc: PendingCheck) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO post_close_pending (trade_id, state, updated_at) VALUES (?, ?, ?)",
                    (pc.trade_id, pc.to_json(), self._clock().isoformat()),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("[post_close] persist pending failed for {}: {}", pc.trade_id, exc)

    def _delete_pending(self, trade_id: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM post_close_pending WHERE trade_id = ?", (trade_id,))
                conn.commit()
        except Exception as exc:
            logger.debug("[post_close] delete pending failed for {}: {}", trade_id, exc)

    # ── Recording a close ────────────────────────────────────────────────

    def record_close(
        self,
        *,
        trade_id: str,
        pair: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        exit_cause: str = "",
        sl_price: float = 0.0,
        tp_price: float = 0.0,
        entry_timestamp: Any = None,
        exit_timestamp: Any = None,
        entry_score: int = 0,
        entry_confluences: Optional[list] = None,
    ) -> None:
        """Schedule forward checks for a just-closed trade."""
        if not self._enabled:
            return
        if not trade_id:
            logger.debug("[post_close] skip close with empty trade_id ({})", pair)
            return
        entry_ts = _parse_ts(entry_timestamp) or _utcnow()
        exit_ts = _parse_ts(exit_timestamp) or _utcnow()
        try:
            pc = PendingCheck(
                trade_id=str(trade_id),
                pair=str(pair),
                direction=str(direction),
                entry_price=float(entry_price),
                exit_price=float(exit_price),
                exit_cause=str(exit_cause or ""),
                sl_price=float(sl_price or 0.0),
                tp_price=float(tp_price or 0.0),
                entry_timestamp=entry_ts,
                exit_timestamp=exit_ts,
                entry_score=int(entry_score or 0),
                entry_confluences=list(entry_confluences or []),
                intervals=list(self.intervals),
            )
        except (TypeError, ValueError) as exc:
            logger.debug("[post_close] could not record close for {}: {}", pair, exc)
            return
        self._pending[pc.trade_id] = pc
        self._persist_pending(pc)
        logger.debug(
            "[post_close] scheduled {} check(s) for {} {} (trade {})",
            len(pc.intervals), pc.direction, pc.pair, pc.trade_id,
        )

    # ── Per-cycle processing ─────────────────────────────────────────────

    def process_pending_checks(self, data_source) -> None:
        """Run any checks now due. ``data_source`` must expose ``get_price`` and
        ``fetch_market_data`` (the live ``platform_manager`` does). Never raises
        — a single failing pair must not stall the scan loop."""
        if not self._enabled or not self._pending:
            return
        now = self._clock()
        for trade_id in list(self._pending.keys()):
            pc = self._pending.get(trade_id)
            if pc is None:
                continue
            try:
                self._process_one(pc, data_source, now)
            except Exception as exc:
                logger.debug("[post_close] check failed for {} ({}): {}", pc.pair, trade_id, exc)

    def _process_one(self, pc: PendingCheck, data_source, now: datetime) -> None:
        changed = False
        for interval in pc.remaining:
            check_time = pc.entry_timestamp + timedelta(minutes=interval)
            if now < check_time:
                continue  # not due yet
            result = self._run_check(pc, interval, check_time, data_source)
            if result is None:
                # transient data failure — count a retry, give up after the cap
                pc.retries[interval] = pc.retries.get(interval, 0) + 1
                if pc.retries[interval] >= self.max_retries:
                    incomplete = CheckResult(
                        interval_minutes=interval,
                        check_timestamp=check_time.isoformat(),
                        price_at_check=None,
                        mfe=None, mae=None, mfe_r=None, mae_r=None, r_at_check=None,
                        incomplete=True,
                    )
                    pc.results[interval] = incomplete
                    pc.done.append(interval)
                    changed = True
                    logger.debug(
                        "[post_close] {} T+{}m incomplete after {} retries",
                        pc.pair, interval, pc.retries[interval],
                    )
                else:
                    changed = True  # persist the retry counter
                continue
            pc.results[interval] = result
            pc.done.append(interval)
            changed = True

        if pc.finished:
            self._finalize(pc)
            self._pending.pop(pc.trade_id, None)
            self._delete_pending(pc.trade_id)
        elif changed:
            self._persist_pending(pc)

    def _run_check(
        self, pc: PendingCheck, interval: int, check_time: datetime, data_source
    ) -> Optional[CheckResult]:
        """Single price check. Returns ``None`` on a (retryable) data failure."""
        price = self._fetch_price(data_source, pc.pair)
        bars = self._fetch_m1(data_source, pc.pair, pc.entry_timestamp, check_time)
        exc = compute_excursions(
            bars, pc.entry_price, pc.direction, pc.entry_timestamp, check_time, pc.sl_distance
        )
        if price is None and exc is None:
            return None  # nothing usable — retry next cycle
        r = pc.sl_distance if pc.sl_distance > 1e-12 else None
        r_at_check = None
        if price is not None and r:
            signed = (price - pc.entry_price) if _is_long(pc.direction) else (pc.entry_price - price)
            r_at_check = signed / r
        return CheckResult(
            interval_minutes=interval,
            check_timestamp=check_time.isoformat(),
            price_at_check=price,
            mfe=(exc or {}).get("mfe"),
            mae=(exc or {}).get("mae"),
            mfe_r=(exc or {}).get("mfe_r"),
            mae_r=(exc or {}).get("mae_r"),
            r_at_check=r_at_check,
        )

    def _fetch_price(self, data_source, pair: str) -> Optional[float]:
        try:
            tick = data_source.get_price(pair)
        except Exception as exc:
            logger.debug("[post_close] price fetch failed for {}: {}", pair, exc)
            return None
        if tick is None:
            return None
        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)
        if bid is None or ask is None:
            return None
        try:
            return (float(bid) + float(ask)) / 2.0
        except (TypeError, ValueError):
            return None

    def _fetch_m1(self, data_source, pair: str, entry_ts: datetime, check_ts: datetime):
        minutes = max(1, int((check_ts - entry_ts).total_seconds() // 60) + 5)
        count = max(_MIN_BARS, min(_MAX_BARS, minutes))
        try:
            frames = data_source.fetch_market_data(pair, ["M1"], count)
        except Exception as exc:
            logger.debug("[post_close] M1 fetch failed for {}: {}", pair, exc)
            return None
        if not frames:
            return None
        return frames.get("M1")

    # ── Finalisation (derived classifications) ───────────────────────────

    def _finalize(self, pc: PendingCheck) -> None:
        """Compute derived classifications and write all rows for the trade."""
        usable = [r for r in pc.results.values() if not r.incomplete]
        best_mfe_r = None
        best_mae_r = None
        for r in usable:
            if r.mfe_r is not None:
                best_mfe_r = r.mfe_r if best_mfe_r is None else max(best_mfe_r, r.mfe_r)
            if r.mae_r is not None:
                best_mae_r = r.mae_r if best_mae_r is None else max(best_mae_r, r.mae_r)

        r = pc.sl_distance if pc.sl_distance > 1e-12 else None
        actual_r = None
        if r:
            signed = (
                (pc.exit_price - pc.entry_price)
                if _is_long(pc.direction)
                else (pc.entry_price - pc.exit_price)
            )
            actual_r = signed / r

        signal_correct = best_mfe_r is not None and best_mfe_r >= 1.0
        if usable:
            signal_quality = classify_signal_quality(best_mfe_r)
            management_quality = classify_management_quality(actual_r, best_mfe_r, signal_correct)
        else:
            signal_quality = "incomplete"
            management_quality = "incomplete"
        optimal_sl_r = best_mae_r  # SL needed to survive = worst adverse excursion
        optimal_tp_r = best_mfe_r  # TP that could have captured the move

        # The final (largest completed interval) row carries the derived fields.
        final_interval = max(pc.results.keys()) if pc.results else None
        rows = []
        for interval, res in sorted(pc.results.items()):
            is_final = interval == final_interval
            rows.append(
                (
                    pc.trade_id, pc.pair, pc.direction,
                    pc.entry_price, pc.exit_price, pc.exit_cause,
                    pc.sl_price, pc.tp_price, pc.sl_distance,
                    pc.entry_timestamp.isoformat(), pc.exit_timestamp.isoformat(),
                    res.interval_minutes, res.check_timestamp,
                    res.price_at_check, res.mfe, res.mae,
                    res.mfe_r, res.mae_r, res.r_at_check,
                    signal_quality if is_final else None,
                    management_quality if is_final else None,
                    optimal_sl_r if is_final else None,
                    optimal_tp_r if is_final else None,
                )
            )
        if not rows:
            return
        try:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO post_close_checks (
                        trade_id, pair, direction,
                        entry_price, exit_price, exit_cause,
                        sl_price, tp_price, sl_distance_r,
                        entry_timestamp, exit_timestamp,
                        check_interval_minutes, check_timestamp,
                        price_at_check, mfe, mae,
                        mfe_r, mae_r, r_at_check,
                        signal_quality, management_quality,
                        optimal_sl_r, optimal_tp_r
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
        except Exception as exc:
            logger.warning("[post_close] finalize write failed for {}: {}", pc.trade_id, exc)
            return
        logger.info(
            "[post_close] {} {} → signal={} management={} (MFE={} MAE={} actual={}R)",
            pc.direction, pc.pair, signal_quality, management_quality,
            _fmt(best_mfe_r), _fmt(best_mae_r), _fmt(actual_r),
        )

    # ── Accessors for downstream learners (read-only) ────────────────────

    def get_signal_accuracy(self, pair: Optional[str] = None) -> float:
        """Fraction of finalised trades whose signal was correct (MFE ≥ 1R)."""
        rows = self._final_rows(pair)
        graded = [r for r in rows if r["signal_quality"] in ("correct", "marginal", "wrong")]
        if not graded:
            return 0.0
        correct = sum(1 for r in graded if r["signal_quality"] == "correct")
        return round(correct / len(graded), 4)

    def get_management_score(self, pair: Optional[str] = None) -> dict:
        """Breakdown of management_quality categories for finalised trades."""
        rows = self._final_rows(pair)
        counts: dict[str, int] = {}
        for r in rows:
            mq = r["management_quality"]
            if not mq or mq == "incomplete":
                continue
            counts[mq] = counts.get(mq, 0) + 1
        total = sum(counts.values())
        return {
            "total": total,
            "counts": counts,
            "fractions": {k: round(v / total, 4) for k, v in counts.items()} if total else {},
        }

    def get_optimal_sl_stats(self, pair: Optional[str] = None) -> dict:
        """Median / p75 / p90 of the optimal SL (in R) over finalised trades.

        These are the adverse excursions trades actually survived — the data a
        future session uses to widen/narrow stops per pair.
        """
        rows = self._final_rows(pair)
        vals = sorted(
            float(r["optimal_sl_r"]) for r in rows if r["optimal_sl_r"] is not None
        )
        if not vals:
            return {"sample_size": 0, "median": None, "p75": None, "p90": None, "max": None}
        return {
            "sample_size": len(vals),
            "median": round(_percentile(vals, 50), 4),
            "p75": round(_percentile(vals, 75), 4),
            "p90": round(_percentile(vals, 90), 4),
            "max": round(vals[-1], 4),
        }

    def _final_rows(self, pair: Optional[str]) -> list[dict]:
        """The derived-field-bearing row per finalised trade (one per trade)."""
        try:
            with self._connect() as conn:
                if pair:
                    cur = conn.execute(
                        "SELECT * FROM post_close_checks WHERE pair = ? AND signal_quality IS NOT NULL",
                        (pair,),
                    )
                else:
                    cur = conn.execute(
                        "SELECT * FROM post_close_checks WHERE signal_quality IS NOT NULL"
                    )
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.debug("[post_close] read failed: {}", exc)
            return []


def _fmt(x: Optional[float]) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) else "n/a"


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolation percentile over a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac
