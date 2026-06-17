"""
APEX TRADER — Capital Allocation Engine (L5.5a)

Most systems try to pick the single *best* execution style and pour everything
into it. Markets rarely have one best style, and chasing the current winner
makes the system thrash: scalp wins 12 → gets more capital → loses 15 → switch
to swing → swing loses → switch again. That is a performance-chasing machine,
not a portfolio.

This engine instead treats execution style as a *capital allocation* problem.
Every trade carries a **strategy fingerprint** — its execution signature, e.g.
``MARKET|SCALP`` vs ``PENDING|SWING`` — derived from metadata the pipeline
already produces (entry mode × horizon). The allocator scores each fingerprint's
expectancy across three trade horizons:

    style_score = 0.2·E[last 50] + 0.3·E[last 500] + 0.5·E[last 5000]

The long horizon dominates so long-term evidence sets the baseline and short-term
evidence only nudges — structurally conservative, no week-to-week reinvention.
Scores are Bayesian-shrunk toward the book average (thin fingerprints can't grab
extreme allocations) and mapped to a portfolio split that sums to 1.0.

The split is then turned into a *sizing multiplier* per fingerprint — its
allocation normalised against the strongest fingerprint in the book and clamped
to ``[min_allocation, 1.0]``. Purely de-risking: the best style sizes at full,
weaker styles size down proportionally, nothing is ever amplified above base.
With insufficient history every fingerprint resolves to a 1.0 multiplier, so the
system behaves *identically* to the pre-allocator pipeline until evidence exists.

Anti-thrashing: allocations recompute only every ``rebalance_interval_trades``
closed trades, and no fingerprint's allocation may move more than
``max_allocation_shift`` per rebalance.

It only ever changes how a trade is SIZED — never direction, entry, or whether
a trade is taken. Storage mirrors the proven ``signal_ledger`` pattern: sync
sqlite3 in WAL mode, exception-safe, survives restarts.

Leaf module — standard library + loguru only (plus the leaf ``adaptive.tunable``
guard mixin), so anything may depend on it without an import cycle.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from adaptive.tunable import TuningGuardMixin

# ── Defaults ─────────────────────────────────────────────────────────────────

_DB_DIR = Path(__file__).parent.parent / "data"
_DB_PATH = _DB_DIR / "capital_allocation.db"

# Sentinel fingerprint when no execution-style metadata is available at all.
_UNKNOWN_FINGERPRINT = "unknown"

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS allocation_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    r           REAL NOT NULL DEFAULT 0.0,
    ts          REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_ALLOCATIONS = """
CREATE TABLE IF NOT EXISTS allocations (
    fingerprint TEXT PRIMARY KEY,
    allocation  REAL NOT NULL DEFAULT 0.0,
    updated_at  REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_REBALANCE_LOG = """
CREATE TABLE IF NOT EXISTS rebalance_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL DEFAULT 0.0,
    fingerprints  INTEGER NOT NULL DEFAULT 0,
    max_shift     REAL NOT NULL DEFAULT 0.0,
    floored       INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS allocation_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""

_CREATE_IDX_FP = (
    "CREATE INDEX IF NOT EXISTS idx_alloc_fp_ts ON allocation_trades (fingerprint, ts)"
)


@dataclass
class FingerprintStats:
    """Per-fingerprint scoring breakdown (read-only view for the dashboard)."""

    fingerprint: str
    trades: int = 0
    exp_short: float = 0.0
    exp_medium: float = 0.0
    exp_long: float = 0.0
    blended_score: float = 0.0
    shrunk_score: float = 0.0
    allocation: float = 0.0
    sizing_multiplier: float = 1.0


def compute_fingerprint(
    entry_mode: Optional[str] = None,
    horizon: Optional[str] = None,
    *,
    extra: Optional[str] = None,
) -> str:
    """Derive a stable strategy fingerprint from a trade's execution metadata.

    The fingerprint is the trade's *execution style* — deliberately low
    cardinality so groups accrue enough trades to score. ``entry_mode`` is the
    order style (MARKET / PENDING / LIMIT) and ``horizon`` is the ranker's
    timeframe class (SCALP / SWING / MIXED). Empty / missing parts collapse to
    ``any`` so an under-specified trade still maps to a deterministic group.
    ``extra`` is an optional third axis (e.g. a confirmation family) for future
    refinement; it is omitted from the key when empty.
    """

    def _norm(v: Optional[str]) -> str:
        s = str(v or "").strip().upper()
        return s if s else "ANY"

    parts = [_norm(entry_mode), _norm(horizon)]
    extra_norm = str(extra or "").strip().upper()
    if extra_norm:
        parts.append(extra_norm)
    fp = "|".join(parts)
    return fp if fp.replace("|", "").replace("ANY", "") else _UNKNOWN_FINGERPRINT


def _mean(values: List[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


class CapitalAllocator(TuningGuardMixin):
    """SQLite-backed multi-strategy capital allocator with 3-horizon scoring."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        enabled: bool = True,
        short_horizon_trades: int = 50,
        medium_horizon_trades: int = 500,
        long_horizon_trades: int = 5000,
        short_weight: float = 0.2,
        medium_weight: float = 0.3,
        long_weight: float = 0.5,
        rebalance_interval_trades: int = 25,
        max_allocation_shift: float = 0.10,
        min_allocation: float = 0.05,
        min_trades_for_scoring: int = 50,
        bayesian_prior_trades: int = 100,
        allocation_temperature: float = 0.5,
    ) -> None:
        self.enabled = bool(enabled)
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        # Tunable parameters (mutable; the TunerAgent adapter snapshots these).
        self.short_horizon_trades = int(short_horizon_trades)
        self.medium_horizon_trades = int(medium_horizon_trades)
        self.long_horizon_trades = int(long_horizon_trades)
        self.short_weight = float(short_weight)
        self.medium_weight = float(medium_weight)
        self.long_weight = float(long_weight)
        self.rebalance_interval_trades = max(1, int(rebalance_interval_trades))
        self.max_allocation_shift = float(max_allocation_shift)
        self.min_allocation = float(min_allocation)
        self.min_trades_for_scoring = max(1, int(min_trades_for_scoring))
        self.bayesian_prior_trades = max(1, int(bayesian_prior_trades))
        self.allocation_temperature = max(1e-6, float(allocation_temperature))

        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        # In-memory cache of the current allocation split (sums to 1.0).
        self._allocations: Dict[str, float] = {}
        self._trades_since_rebalance = 0
        self._last_rebalance_ts: float = 0.0
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CapitalAllocator] could not create db dir: {}", exc)
        self._connect()
        self._load_state()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_TRADES)
            self._conn.execute(_CREATE_ALLOCATIONS)
            self._conn.execute(_CREATE_REBALANCE_LOG)
            self._conn.execute(_CREATE_META)
            self._conn.execute(_CREATE_IDX_FP)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CapitalAllocator] DB connect/init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[CapitalAllocator] conn.close() failed during cleanup")
                self._conn = None

    def _load_state(self) -> None:
        """Restore the cached allocation split + rebalance counter from disk."""
        if self._conn is None:
            return
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT fingerprint, allocation FROM allocations"
                )
                self._allocations = {
                    str(fp): float(a) for fp, a in cur.fetchall()
                }
                row = self._conn.execute(
                    "SELECT value FROM allocation_meta WHERE key='trades_since_rebalance'"
                ).fetchone()
                self._trades_since_rebalance = int(float(row[0])) if row and row[0] else 0
                row = self._conn.execute(
                    "SELECT value FROM allocation_meta WHERE key='last_rebalance_ts'"
                ).fetchone()
                self._last_rebalance_ts = float(row[0]) if row and row[0] else 0.0
            except Exception as exc:  # noqa: BLE001
                logger.debug("[CapitalAllocator] _load_state failed: {}", exc)

    def _set_meta(self, key: str, value) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO allocation_meta (key, value) VALUES (?, ?)",
                (str(key), str(value)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CapitalAllocator] _set_meta failed: {}", exc)

    # ── Data ingestion (always allowed — not a tuning op) ─────────────────────

    def record_outcome(
        self, fingerprint: str, r_multiple: float, *, timestamp: Optional[float] = None,
    ) -> None:
        """Record one closed trade's realised R against its strategy fingerprint.

        Pure data ingestion — never blocked by the TunerAgent (recording is not
        tuning). Increments the rebalance counter; the actual recompute happens
        in :meth:`rebalance` on its own cadence. Prunes per-fingerprint history
        to the long-horizon window so the table stays bounded.
        """
        if self._conn is None or not fingerprint:
            return
        fp = str(fingerprint)
        r = float(r_multiple)
        if not math.isfinite(r):
            return
        ts = float(timestamp) if timestamp is not None else time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO allocation_trades (fingerprint, r, ts) VALUES (?,?,?)",
                    (fp, r, ts),
                )
                # Bound the per-fingerprint history to the long horizon.
                self._conn.execute(
                    """DELETE FROM allocation_trades
                       WHERE fingerprint=? AND id NOT IN (
                           SELECT id FROM allocation_trades
                           WHERE fingerprint=? ORDER BY ts DESC, id DESC LIMIT ?
                       )""",
                    (fp, fp, self.long_horizon_trades),
                )
                self._trades_since_rebalance += 1
                self._set_meta("trades_since_rebalance", self._trades_since_rebalance)
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[CapitalAllocator] record_outcome failed: {}", exc)

    def due_for_rebalance(self) -> bool:
        """Whether enough trades have closed since the last rebalance."""
        return self._trades_since_rebalance >= self.rebalance_interval_trades

    def maybe_rebalance(self) -> bool:
        """Rebalance iff due. Returns True when a rebalance actually ran.

        Convenience for the legacy (no TunerAgent) path — when the agent owns
        scheduling it drives :meth:`rebalance` directly via the adapter instead.
        """
        if self.due_for_rebalance():
            res = self.rebalance()
            return bool(res is not None)
        return False

    # ── Reading raw history ───────────────────────────────────────────────────

    def _recent_r(self, fingerprint: str, limit: int) -> List[float]:
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(
                "SELECT r FROM allocation_trades WHERE fingerprint=? ORDER BY ts DESC, id DESC LIMIT ?",
                (str(fingerprint), int(max(1, limit))),
            )
            return [float(row[0]) for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CapitalAllocator] _recent_r failed: {}", exc)
            return []

    def _all_fingerprints(self) -> List[str]:
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(
                "SELECT DISTINCT fingerprint FROM allocation_trades"
            )
            return [str(row[0]) for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CapitalAllocator] _all_fingerprints failed: {}", exc)
            return []

    def total_trades(self) -> int:
        if self._conn is None:
            return 0
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM allocation_trades"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CapitalAllocator] total_trades failed: {}", exc)
            return 0

    # ── Scoring ────────────────────────────────────────────────────────────────

    def _blended_score(self, fingerprint: str) -> tuple[float, float, float, float, int]:
        """Three-horizon weighted expectancy for a fingerprint.

        Returns ``(exp_short, exp_medium, exp_long, blended, n)``. Horizon weights
        are renormalised across whichever windows actually have samples so a young
        fingerprint with only short-window data is still scored sensibly.
        """
        recent_long = self._recent_r(fingerprint, self.long_horizon_trades)
        n = len(recent_long)
        if n == 0:
            return 0.0, 0.0, 0.0, 0.0, 0
        exp_short = _mean(recent_long[: self.short_horizon_trades])
        exp_medium = _mean(recent_long[: self.medium_horizon_trades])
        exp_long = _mean(recent_long)  # already capped at long_horizon_trades

        windows = [
            (exp_short, self.short_weight, min(n, self.short_horizon_trades)),
            (exp_medium, self.medium_weight, min(n, self.medium_horizon_trades)),
            (exp_long, self.long_weight, n),
        ]
        present = [(e, w) for (e, w, cnt) in windows if cnt > 0 and w > 0.0]
        wsum = sum(w for _, w in present)
        if wsum <= 0.0:
            blended = exp_long
        else:
            blended = sum(e * w for e, w in present) / wsum
        return exp_short, exp_medium, exp_long, blended, n

    def _compute_target_allocations(self) -> tuple[Dict[str, float], Dict[str, FingerprintStats]]:
        """Compute the fresh target split (sums to 1.0) + per-fingerprint stats.

        Blended expectancy is Bayesian-shrunk toward the book average, then mapped
        to weights via a temperature-controlled softmax. The ``min_allocation``
        floor is applied and the result renormalised.
        """
        fps = self._all_fingerprints()
        stats: Dict[str, FingerprintStats] = {}
        if not fps:
            return {}, stats

        blended: Dict[str, float] = {}
        counts: Dict[str, int] = {}
        for fp in fps:
            e_s, e_m, e_l, b, n = self._blended_score(fp)
            blended[fp] = b
            counts[fp] = n
            stats[fp] = FingerprintStats(
                fingerprint=fp, trades=n, exp_short=e_s, exp_medium=e_m,
                exp_long=e_l, blended_score=b,
            )

        # Book-average prior for shrinkage (trade-weighted across fingerprints).
        total_n = sum(counts.values())
        if total_n > 0:
            global_avg = sum(blended[fp] * counts[fp] for fp in fps) / total_n
        else:
            global_avg = 0.0

        shrunk: Dict[str, float] = {}
        for fp in fps:
            n = counts[fp]
            prior = self.bayesian_prior_trades
            shrunk[fp] = (n * blended[fp] + prior * global_avg) / (n + prior)
            stats[fp].shrunk_score = shrunk[fp]

        # Softmax over shrunk scores (numerically stable).
        temp = self.allocation_temperature
        max_s = max(shrunk.values())
        exps = {fp: math.exp((shrunk[fp] - max_s) / temp) for fp in fps}
        denom = sum(exps.values()) or 1.0
        alloc = {fp: exps[fp] / denom for fp in fps}

        alloc = self._apply_floor(alloc)
        for fp in fps:
            stats[fp].allocation = alloc.get(fp, 0.0)
        return alloc, stats

    def _apply_floor(self, alloc: Dict[str, float]) -> Dict[str, float]:
        """Raise any allocation below ``min_allocation`` to the floor, renormalise.

        Guards against an infeasible floor (n × floor > 1): falls back to an equal
        split in that degenerate case.
        """
        if not alloc:
            return {}
        floor = max(0.0, self.min_allocation)
        n = len(alloc)
        if floor * n >= 1.0:
            return {fp: 1.0 / n for fp in alloc}
        floored = {fp: max(a, floor) for fp, a in alloc.items()}
        total = sum(floored.values()) or 1.0
        # Renormalise the above-floor mass so the whole thing still sums to 1.0
        # without dropping anyone back below the floor.
        excess = total - 1.0
        if excess <= 1e-12:
            return floored
        head_room = {fp: max(0.0, floored[fp] - floor) for fp in floored}
        room_total = sum(head_room.values())
        if room_total <= 1e-12:
            return {fp: 1.0 / n for fp in floored}
        return {
            fp: floored[fp] - excess * (head_room[fp] / room_total)
            for fp in floored
        }

    def _rate_limit(self, target: Dict[str, float]) -> tuple[Dict[str, float], float]:
        """Clamp each fingerprint's move from the previous split, renormalise.

        Returns ``(new_allocations, max_observed_shift)``. New fingerprints are
        seeded at the previous-book minimum so they enter gradually.
        """
        prev = dict(self._allocations)
        if not prev:
            return target, 0.0
        max_shift = max(0.0, self.max_allocation_shift)
        limited: Dict[str, float] = {}
        observed = 0.0
        for fp, tgt in target.items():
            base = prev.get(fp, 0.0)
            delta = tgt - base
            if delta > max_shift:
                delta = max_shift
            elif delta < -max_shift:
                delta = -max_shift
            observed = max(observed, abs(delta))
            limited[fp] = max(0.0, base + delta)
        total = sum(limited.values()) or 1.0
        return {fp: v / total for fp, v in limited.items()}, observed

    # ── Rebalance (the tuning op — guarded) ────────────────────────────────────

    def rebalance(self, *, force: bool = False) -> Optional[Dict[str, float]]:
        """Recompute the capital split from stored outcomes.

        This is the allocator's *tuning* step, so it defers to a sole-authority
        TunerAgent: a direct call while the agent owns tuning is a no-op (returns
        the current cached split) unless the agent is authorising it. ``force``
        only bypasses the trades-since-rebalance cadence, never the agent guard.
        """
        if self._conn is None or not self.enabled:
            return None
        if self._tuning_blocked("rebalance"):
            return dict(self._allocations)
        with self._lock:
            target, _stats = self._compute_target_allocations()
            if not target:
                # Reset the counter so we don't spin every close on an empty book.
                self._trades_since_rebalance = 0
                self._set_meta("trades_since_rebalance", 0)
                self._conn.commit()
                return dict(self._allocations)
            limited, observed = self._rate_limit(target)
            floored_count = sum(
                1 for v in limited.values() if v <= self.min_allocation + 1e-9
            )
            now = time.time()
            try:
                self._conn.execute("DELETE FROM allocations")
                for fp, a in limited.items():
                    self._conn.execute(
                        "INSERT OR REPLACE INTO allocations (fingerprint, allocation, updated_at) VALUES (?,?,?)",
                        (fp, float(a), now),
                    )
                self._conn.execute(
                    "INSERT INTO rebalance_log (ts, fingerprints, max_shift, floored) VALUES (?,?,?,?)",
                    (now, len(limited), round(observed, 6), int(floored_count)),
                )
                self._trades_since_rebalance = 0
                self._last_rebalance_ts = now
                self._set_meta("trades_since_rebalance", 0)
                self._set_meta("last_rebalance_ts", now)
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[CapitalAllocator] rebalance persist failed: {}", exc)
                return dict(self._allocations)
            self._allocations = limited
            logger.info(
                "[CapitalAllocator] rebalanced {} fingerprint(s) "
                "(max shift {:.3f}, {} at floor)",
                len(limited), observed, floored_count,
            )
            return dict(self._allocations)

    # ── Consumption (always allowed — read path) ───────────────────────────────

    def get_allocation(self, fingerprint: str) -> float:
        """Raw portfolio allocation for a fingerprint (0..1). 0 if unknown."""
        return float(self._allocations.get(str(fingerprint), 0.0))

    def get_sizing_multiplier(self, fingerprint: str) -> float:
        """De-risking sizing multiplier in ``[min_allocation, 1.0]`` for a trade.

        The strongest fingerprint in the book sizes at 1.0; weaker styles scale
        down proportionally. Returns ``1.0`` (a true no-op, identical to the
        pre-allocator behaviour) when disabled, when the book has too little
        history, or for a brand-new fingerprint not yet in the split — a new
        style is never penalised before it has been measured.
        """
        if not self.enabled:
            return 1.0
        if self.total_trades() < self.min_trades_for_scoring:
            return 1.0
        alloc = self._allocations
        if not alloc:
            return 1.0
        max_alloc = max(alloc.values())
        if max_alloc <= 0.0:
            return 1.0
        a = alloc.get(str(fingerprint))
        if a is None:
            return 1.0  # cold fingerprint — don't penalise an unmeasured style
        mult = a / max_alloc
        return round(max(self.min_allocation, min(1.0, mult)), 6)

    # ── State (dashboard / tunable) ────────────────────────────────────────────

    def get_current_params(self) -> dict:
        """The tunable scalar knobs (snapshotted by the TunerAgent adapter)."""
        return {
            "short_weight": round(self.short_weight, 6),
            "medium_weight": round(self.medium_weight, 6),
            "long_weight": round(self.long_weight, 6),
            "min_allocation": round(self.min_allocation, 6),
            "max_allocation_shift": round(self.max_allocation_shift, 6),
            "rebalance_interval_trades": int(self.rebalance_interval_trades),
            "allocation_temperature": round(self.allocation_temperature, 6),
        }

    def apply_params(self, params: dict) -> None:
        """Apply a params dict (used by the tunable for tune / rollback)."""
        if not params:
            return
        if "short_weight" in params:
            self.short_weight = float(params["short_weight"])
        if "medium_weight" in params:
            self.medium_weight = float(params["medium_weight"])
        if "long_weight" in params:
            self.long_weight = float(params["long_weight"])
        if "min_allocation" in params:
            self.min_allocation = float(params["min_allocation"])
        if "max_allocation_shift" in params:
            self.max_allocation_shift = float(params["max_allocation_shift"])
        if "rebalance_interval_trades" in params:
            self.rebalance_interval_trades = max(1, int(params["rebalance_interval_trades"]))
        if "allocation_temperature" in params:
            self.allocation_temperature = max(1e-6, float(params["allocation_temperature"]))

    def get_state(self) -> dict:
        """Read-only snapshot for the dashboard."""
        total = self.total_trades()
        active = total >= self.min_trades_for_scoring
        _alloc, stats = self._compute_target_allocations() if self._conn is not None else ({}, {})
        fingerprints = []
        for fp, st in stats.items():
            fingerprints.append({
                "fingerprint": fp,
                "trades": st.trades,
                "exp_short": round(st.exp_short, 4),
                "exp_medium": round(st.exp_medium, 4),
                "exp_long": round(st.exp_long, 4),
                "blended_score": round(st.blended_score, 4),
                "shrunk_score": round(st.shrunk_score, 4),
                "allocation": round(self._allocations.get(fp, st.allocation), 4),
                "sizing_multiplier": self.get_sizing_multiplier(fp),
            })
        fingerprints.sort(key=lambda r: r["allocation"], reverse=True)
        return {
            "enabled": self.enabled,
            "source": "live",
            "active": active,
            "total_trades": total,
            "min_trades_for_scoring": self.min_trades_for_scoring,
            "trades_since_rebalance": self._trades_since_rebalance,
            "rebalance_interval_trades": self.rebalance_interval_trades,
            "last_rebalance_ts": self._last_rebalance_ts or None,
            "horizon_weights": {
                "short": round(self.short_weight, 4),
                "medium": round(self.medium_weight, 4),
                "long": round(self.long_weight, 4),
            },
            "min_allocation": round(self.min_allocation, 4),
            "max_allocation_shift": round(self.max_allocation_shift, 4),
            "fingerprint_count": len(fingerprints),
            "fingerprints": fingerprints,
            "rebalance_history": self._recent_rebalances(),
        }

    def _recent_rebalances(self, limit: int = 20) -> List[dict]:
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(
                "SELECT ts, fingerprints, max_shift, floored FROM rebalance_log "
                "ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            )
            return [
                {
                    "ts": float(ts),
                    "fingerprints": int(fpc),
                    "max_shift": round(float(ms), 4),
                    "floored": int(fl),
                }
                for ts, fpc, ms, fl in cur.fetchall()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CapitalAllocator] _recent_rebalances failed: {}", exc)
            return []


__all__ = ["CapitalAllocator", "FingerprintStats", "compute_fingerprint"]
