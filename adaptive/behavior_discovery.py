"""
APEX TRADER — Behaviour Discovery (L6)

L1–L5 measure, weight, prune, attribute, and *discover signal combinations*. They
never ask the generative execution question: are there *clusters of execution
behaviour* — combinations of how a trade was set up (entry mode, horizon, the
chosen profile's SL/TP shape, the regime it fired in, the time of day, the
volatility) — that, taken together, carry an edge nobody hard-coded?

"Scalper", "Swing", "Breakout" are human labels. This engine refuses to assume
them. It records each closed trade's *execution feature vector* + realised R and
periodically clusters those vectors (density-based, no pre-set ``k``, no heavy
ML).  Each emergent cluster is a **Behaviour** — a discovered execution style.
Behaviours are scored (win-rate, expectancy, Sharpe-like) with Bayesian
shrinkage toward the book average so a thin lucky cluster cannot run away, then
walked through a SHADOW → ACTIVE → RETIRED lifecycle:

    * **SHADOW**   — newly discovered; observed, never acted on.
    * **ACTIVE**   — sustained top-percentile expectancy; a proven hypothesis.
    * **RETIRED**  — fell to the bottom percentile, or stopped re-appearing.

The lifecycle is purely advisory and rate-limited (a cooldown between
transitions per behaviour) so it never thrashes and never touches the live
consensus / execution hot path — exactly like the L5b/L5c discovery engines, it
surfaces *what the data shows* for the evolution stack (and a human) to adopt.
It is dormant by construction: with fewer than ``min_trades_to_cluster`` recorded
trades nothing clusters, nothing is scored, and every accessor returns a clean
empty shape — behaviour identical to the pre-L6 system.

Storage mirrors the proven ``capital_allocator`` / ``signal_discovery`` pattern:
sync ``sqlite3`` in WAL mode, exception-safe, survives restarts.  Leaf module —
standard library + loguru only — so anything may depend on it without an import
cycle, and ``record_trade`` ingestion is always allowed (it is not a tuning op).
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

# ── Defaults ─────────────────────────────────────────────────────────────────

from runtime_paths import data_dir as _data_dir  # noqa: E402

_DB_DIR = _data_dir()
_DB_PATH = _DB_DIR / "behavior_discovery.db"

# Lifecycle states for a discovered behaviour.
SHADOW = "SHADOW"
ACTIVE = "ACTIVE"
RETIRED = "RETIRED"

# Sentinel for a missing categorical feature (matches other missing values, so
# two under-specified trades are treated as similar on that axis).
_NA = "NA"

# Number of time-of-day buckets (4-hour blocks across 24h).
_TIME_BUCKETS = 6

# Numeric features and their (lo, hi) normalisation ranges → [0, 1]. A missing
# numeric feature normalises to the neutral midpoint 0.5 so it never dominates a
# distance. Mirrors the execution-profile field bounds where they overlap.
_NUMERIC_RANGES: Dict[str, Tuple[float, float]] = {
    "sl_atr_mult": (0.2, 10.0),
    "tp1_rr": (0.5, 20.0),
    "tp2_rr": (0.5, 40.0),
    "consensus_strength": (0.0, 1.0),
    "conviction": (0.0, 1.0),
    "score": (0.0, 100.0),
    "volatility": (0.0, 5.0),
    # time-of-day handled specially (bucketed) but stored as a numeric too.
    "time_of_day": (0.0, float(_TIME_BUCKETS - 1)),
}

# Unordered categorical features — distance is 0 when equal, 1 when different.
_CATEGORICAL_FEATURES: Tuple[str, ...] = (
    "entry_mode",
    "horizon",
    "profile",
    "direction",
    "instrument_class",
    "regime",
)

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS behavior_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    TEXT,
    features    TEXT NOT NULL,
    r           REAL NOT NULL DEFAULT 0.0,
    cluster_id  INTEGER NOT NULL DEFAULT -1,
    ts          REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_BEHAVIORS = """
CREATE TABLE IF NOT EXISTS behaviors (
    behavior_id      TEXT PRIMARY KEY,
    centroid         TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'SHADOW',
    trades           INTEGER NOT NULL DEFAULT 0,
    win_rate         REAL NOT NULL DEFAULT 0.0,
    expectancy       REAL NOT NULL DEFAULT 0.0,
    shrunk_expectancy REAL NOT NULL DEFAULT 0.0,
    sharpe           REAL NOT NULL DEFAULT 0.0,
    percentile       REAL NOT NULL DEFAULT 0.0,
    confirmations    INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL DEFAULT 0.0,
    updated_at       REAL NOT NULL DEFAULT 0.0,
    last_action_trades INTEGER NOT NULL DEFAULT 0,
    last_seen_pass   INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS behavior_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at   REAL NOT NULL,
    trades_analyzed INTEGER NOT NULL DEFAULT 0,
    cluster_count INTEGER NOT NULL DEFAULT 0,
    stability     REAL NOT NULL DEFAULT 0.0,
    payload       TEXT NOT NULL
)
"""

_CREATE_IDX_TS = (
    "CREATE INDEX IF NOT EXISTS idx_behavior_trade_ts ON behavior_trades (ts)"
)

# Bound the recorded-trade table so it never grows without limit.
_TRADE_HISTORY_CAP = 20000


# ── Feature extraction ─────────────────────────────────────────────────────


@dataclass
class FeatureVector:
    """A trade's execution behaviour as a normalised numeric + categorical view."""

    numeric: Dict[str, float] = field(default_factory=dict)      # all in [0, 1]
    categorical: Dict[str, str] = field(default_factory=dict)
    r: float = 0.0
    trade_id: str = ""
    ts: float = 0.0
    raw: Dict[str, object] = field(default_factory=dict)


def _time_bucket(hour: float) -> int:
    """Map an hour-of-day (0..24) to one of ``_TIME_BUCKETS`` blocks."""
    try:
        h = float(hour)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(h):
        return 0
    h = h % 24.0
    width = 24.0 / _TIME_BUCKETS
    return min(_TIME_BUCKETS - 1, int(h // width))


def _normalize(name: str, value) -> Optional[float]:
    """Normalise a numeric feature to [0, 1] within its configured range.

    Returns ``None`` when the value is missing/unparseable so the caller can
    substitute the neutral midpoint (a missing axis must not skew the distance).
    """
    lo, hi = _NUMERIC_RANGES.get(name, (0.0, 1.0))
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


class TradeFeatureExtractor:
    """Turns a raw trade-feature dict into a normalised :class:`FeatureVector`.

    The raw dict is whatever the pipeline can assemble at close (entry mode,
    horizon, profile name + its SL/TP shape, regime, consensus strength,
    conviction, score, time-of-day, volatility, instrument class, direction).
    Every field is optional — missing numerics fall back to the neutral 0.5 and
    missing categoricals to the ``NA`` sentinel, so an under-specified trade is
    still placed deterministically without crashing.
    """

    def extract(
        self, raw: dict, r_multiple: float, *, trade_id: str = "", ts: float = 0.0,
    ) -> FeatureVector:
        raw = dict(raw or {})
        numeric: Dict[str, float] = {}
        for name in _NUMERIC_RANGES:
            if name == "time_of_day":
                continue  # handled below from hour/time_of_day
            n = _normalize(name, raw.get(name))
            numeric[name] = 0.5 if n is None else n

        # Time-of-day: accept an explicit bucket, an hour, or a unix ts.
        hour = raw.get("hour")
        if hour is None and raw.get("time_of_day") is not None:
            hour = raw.get("time_of_day")
        if hour is None:
            tod_source = ts or raw.get("ts") or 0.0
            if tod_source:
                try:
                    hour = time.gmtime(float(tod_source)).tm_hour
                except (TypeError, ValueError, OverflowError, OSError):
                    hour = None
        if hour is None:
            numeric["time_of_day"] = 0.5
        else:
            bucket = _time_bucket(hour)
            denom = float(max(1, _TIME_BUCKETS - 1))
            numeric["time_of_day"] = bucket / denom

        categorical: Dict[str, str] = {}
        for name in _CATEGORICAL_FEATURES:
            val = raw.get(name)
            s = str(val).strip().upper() if val not in (None, "") else _NA
            categorical[name] = s

        try:
            r = float(r_multiple)
            if not math.isfinite(r):
                r = 0.0
        except (TypeError, ValueError):
            r = 0.0

        return FeatureVector(
            numeric=numeric,
            categorical=categorical,
            r=r,
            trade_id=str(trade_id or ""),
            ts=float(ts) if ts else time.time(),
            raw=raw,
        )


def feature_distance(a: FeatureVector, b: FeatureVector) -> float:
    """Normalised behaviour distance in [0, 1].

    Equal-weight blend of the mean absolute numeric difference and the mean
    categorical mismatch rate — keeping numeric and unordered-categorical axes
    on the same [0, 1] footing so neither dominates the clustering.
    """
    nkeys = set(a.numeric) | set(b.numeric)
    if nkeys:
        num_d = sum(
            abs(a.numeric.get(k, 0.5) - b.numeric.get(k, 0.5)) for k in nkeys
        ) / len(nkeys)
    else:
        num_d = 0.0
    ckeys = set(a.categorical) | set(b.categorical)
    if ckeys:
        cat_d = sum(
            0.0 if a.categorical.get(k, _NA) == b.categorical.get(k, _NA) else 1.0
            for k in ckeys
        ) / len(ckeys)
    else:
        cat_d = 0.0
    return 0.5 * num_d + 0.5 * cat_d


# ── Clustering (density-based, dependency-free) ─────────────────────────────


class BehaviorClusterer:
    """A small deterministic DBSCAN-style clusterer over feature vectors.

    No pre-set ``k`` (emergent behaviours), no sklearn/scipy. A point is a core
    point when it has at least ``min_cluster_size`` neighbours within ``eps``;
    clusters grow from core points. Everything else is noise (label ``-1``).
    When more than ``max_clusters`` form, only the largest are kept.
    """

    def __init__(self, *, eps: float, min_cluster_size: int, max_clusters: int) -> None:
        self.eps = float(eps)
        self.min_cluster_size = max(2, int(min_cluster_size))
        self.max_clusters = max(1, int(max_clusters))

    def cluster(self, vectors: List[FeatureVector]) -> List[int]:
        n = len(vectors)
        labels = [-1] * n
        if n < self.min_cluster_size:
            return labels

        # Precompute symmetric neighbour lists (O(n^2), fine for the bounded
        # lookback window this runs on, periodically).
        neighbors: List[List[int]] = [[] for _ in range(n)]
        for i in range(n):
            vi = vectors[i]
            for j in range(i + 1, n):
                if feature_distance(vi, vectors[j]) <= self.eps:
                    neighbors[i].append(j)
                    neighbors[j].append(i)

        visited = [False] * n
        cluster_id = 0
        for i in range(n):
            if visited[i]:
                continue
            visited[i] = True
            if len(neighbors[i]) + 1 < self.min_cluster_size:
                continue  # not (yet) a core point → leave as noise
            labels[i] = cluster_id
            seeds = list(neighbors[i])
            in_seeds = set(seeds)
            k = 0
            while k < len(seeds):
                j = seeds[k]
                k += 1
                if not visited[j]:
                    visited[j] = True
                    if len(neighbors[j]) + 1 >= self.min_cluster_size:
                        for nb in neighbors[j]:
                            if nb not in in_seeds:
                                in_seeds.add(nb)
                                seeds.append(nb)
                if labels[j] == -1:
                    labels[j] = cluster_id
            cluster_id += 1

        return self._cap_clusters(labels, cluster_id)

    def _cap_clusters(self, labels: List[int], cluster_count: int) -> List[int]:
        """Keep only the ``max_clusters`` largest clusters; the rest → noise."""
        if cluster_count <= self.max_clusters:
            return labels
        sizes: Dict[int, int] = {}
        for lab in labels:
            if lab >= 0:
                sizes[lab] = sizes.get(lab, 0) + 1
        keep = sorted(sizes, key=lambda c: (-sizes[c], c))[: self.max_clusters]
        remap = {old: new for new, old in enumerate(keep)}
        return [remap.get(lab, -1) for lab in labels]


# ── Scoring ─────────────────────────────────────────────────────────────────


def _mean(values: List[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def _std(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    var = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(max(0.0, var))


@dataclass
class BehaviorScore:
    """Aggregate quality of one cluster of trades."""

    trades: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    shrunk_expectancy: float = 0.0
    sharpe: float = 0.0


class BehaviorScorer:
    """Scores a cluster's R distribution with Bayesian shrinkage to the book."""

    def __init__(self, *, min_trades: int, prior_trades: int) -> None:
        self.min_trades = max(1, int(min_trades))
        self.prior_trades = max(1, int(prior_trades))

    def score(self, cluster_r: List[float], global_mean: float) -> BehaviorScore:
        n = len(cluster_r)
        if n == 0:
            return BehaviorScore()
        exp = _mean(cluster_r)
        wins = sum(1 for r in cluster_r if r > 0)
        sd = _std(cluster_r)
        # Shrink toward the book average so thin clusters can't grab extremes.
        prior = self.prior_trades
        shrunk = (n * exp + prior * global_mean) / (n + prior)
        sharpe = (exp / sd) if sd > 0 else 0.0
        return BehaviorScore(
            trades=n,
            win_rate=wins / n,
            expectancy=exp,
            shrunk_expectancy=shrunk,
            sharpe=sharpe,
        )


# ── Behaviour records / lifecycle ───────────────────────────────────────────


@dataclass
class BehaviorRecord:
    """A persisted discovered behaviour and its lifecycle state."""

    behavior_id: str
    centroid_numeric: Dict[str, float] = field(default_factory=dict)
    centroid_categorical: Dict[str, str] = field(default_factory=dict)
    state: str = SHADOW
    trades: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    shrunk_expectancy: float = 0.0
    sharpe: float = 0.0
    percentile: float = 0.0
    confirmations: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_action_trades: int = 0
    last_seen_pass: int = 0

    def centroid_json(self) -> str:
        return json.dumps(
            {"numeric": self.centroid_numeric, "categorical": self.centroid_categorical},
            default=str,
        )

    def to_dict(self) -> dict:
        return {
            "behavior_id": self.behavior_id,
            "state": self.state,
            "trades": int(self.trades),
            "win_rate": round(self.win_rate, 4),
            "expectancy": round(self.expectancy, 4),
            "shrunk_expectancy": round(self.shrunk_expectancy, 4),
            "sharpe": round(self.sharpe, 4),
            "percentile": round(self.percentile, 4),
            "confirmations": int(self.confirmations),
            "centroid": {
                "numeric": {k: round(v, 4) for k, v in self.centroid_numeric.items()},
                "categorical": dict(self.centroid_categorical),
            },
            "created_at": self.created_at or None,
            "updated_at": self.updated_at or None,
        }


def _centroid(vectors: List[FeatureVector]) -> Tuple[Dict[str, float], Dict[str, str]]:
    """Per-numeric mean + per-categorical mode for a cluster of vectors."""
    numeric: Dict[str, float] = {}
    keys = set()
    for v in vectors:
        keys |= set(v.numeric)
    for k in keys:
        numeric[k] = _mean([v.numeric.get(k, 0.5) for v in vectors])
    categorical: Dict[str, str] = {}
    ckeys = set()
    for v in vectors:
        ckeys |= set(v.categorical)
    for k in ckeys:
        counts: Dict[str, int] = {}
        for v in vectors:
            val = v.categorical.get(k, _NA)
            counts[val] = counts.get(val, 0) + 1
        # Deterministic mode: highest count, ties broken by value.
        categorical[k] = sorted(counts, key=lambda x: (-counts[x], x))[0]
    return numeric, categorical


def _centroid_distance(
    a_num: Dict[str, float], a_cat: Dict[str, str],
    b_num: Dict[str, float], b_cat: Dict[str, str],
) -> float:
    """Distance between two centroids (same metric as feature_distance)."""
    return feature_distance(
        FeatureVector(numeric=a_num, categorical=a_cat),
        FeatureVector(numeric=b_num, categorical=b_cat),
    )


# ── Engine ───────────────────────────────────────────────────────────────────


class BehaviorDiscoveryEngine:
    """Records execution-feature vectors, clusters them into behaviours, scores
    + walks them through a SHADOW → ACTIVE → RETIRED lifecycle.

    Purely advisory + observational, dormant until ``min_trades_to_cluster``
    closed trades exist. Every public method is exception-safe.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        db_path: Optional[Path | str] = None,
        lookback: int = 1000,
        min_trades_to_cluster: int = 100,
        min_cluster_size: int = 20,
        max_clusters: int = 15,
        recluster_every_n_trades: int = 50,
        cluster_eps: float = 0.25,
        bayesian_prior_trades: int = 50,
        promote_threshold: float = 0.65,
        retire_threshold: float = 0.30,
        cooldown_trades: int = 100,
        centroid_match_eps: float = 0.20,
    ) -> None:
        self.enabled = bool(enabled)
        self._lookback = max(1, int(lookback))
        self._min_trades_to_cluster = max(1, int(min_trades_to_cluster))
        self._min_cluster_size = max(2, int(min_cluster_size))
        self._max_clusters = max(1, int(max_clusters))
        self._interval = max(1, int(recluster_every_n_trades))
        self._cluster_eps = float(cluster_eps)
        self._prior_trades = max(1, int(bayesian_prior_trades))
        self._promote_threshold = min(1.0, max(0.0, float(promote_threshold)))
        self._retire_threshold = min(1.0, max(0.0, float(retire_threshold)))
        self._cooldown_trades = max(0, int(cooldown_trades))
        self._centroid_match_eps = float(centroid_match_eps)

        self._extractor = TradeFeatureExtractor()
        self._clusterer = BehaviorClusterer(
            eps=self._cluster_eps,
            min_cluster_size=self._min_cluster_size,
            max_clusters=self._max_clusters,
        )
        self._scorer = BehaviorScorer(
            min_trades=self._min_cluster_size, prior_trades=self._prior_trades,
        )

        self._lock = threading.RLock()
        self._last_computed_trades = 0
        self._pass_counter = 0
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[behavior-discovery] could not create db dir: {}", exc)
        self._connect()

    # ── Tunable-adapter compatibility surface ──────────────────────────────

    @property
    def lookback(self) -> int:
        return self._lookback

    @property
    def interval(self) -> int:
        return self._interval

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_TRADES)
            self._conn.execute(_CREATE_BEHAVIORS)
            self._conn.execute(_CREATE_HISTORY)
            self._conn.execute(_CREATE_IDX_TS)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[behavior-discovery] DB init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[behavior-discovery] conn close failed")
                self._conn = None

    # ── Ingestion (always allowed — not a tuning op) ────────────────────────

    def record_trade(
        self,
        features: dict,
        r_multiple: float,
        *,
        trade_id: str = "",
        timestamp: Optional[float] = None,
    ) -> None:
        """Record one closed trade's execution feature vector + realised R.

        Pure data ingestion. Prunes the table to the recent history cap. No-op
        when disabled, when the DB is unavailable, or when ``features`` is empty.
        """
        if self._conn is None or not self.enabled or not features:
            return
        try:
            r = float(r_multiple)
            if not math.isfinite(r):
                return
        except (TypeError, ValueError):
            return
        ts = float(timestamp) if timestamp is not None else time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO behavior_trades (trade_id, features, r, cluster_id, ts) "
                    "VALUES (?,?,?,?,?)",
                    (str(trade_id or ""), json.dumps(features, default=str), r, -1, ts),
                )
                self._conn.execute(
                    """DELETE FROM behavior_trades WHERE id NOT IN (
                           SELECT id FROM behavior_trades ORDER BY ts DESC, id DESC LIMIT ?
                       )""",
                    (_TRADE_HISTORY_CAP,),
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[behavior-discovery] record_trade failed: {}", exc)

    def total_trades(self) -> int:
        if self._conn is None:
            return 0
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM behavior_trades").fetchone()
            return int(row[0]) if row and row[0] else 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("[behavior-discovery] total_trades failed: {}", exc)
            return 0

    # ── Reading ─────────────────────────────────────────────────────────────

    def _recent_vectors(self) -> List[FeatureVector]:
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(
                "SELECT id, trade_id, features, r, ts FROM behavior_trades "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (self._lookback,),
            )
            rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[behavior-discovery] _recent_vectors failed: {}", exc)
            return []
        out: List[FeatureVector] = []
        for _id, trade_id, features, r, ts in rows:
            try:
                raw = json.loads(features) if features else {}
            except (TypeError, ValueError):
                raw = {}
            fv = self._extractor.extract(raw, r, trade_id=str(trade_id or ""), ts=float(ts or 0.0))
            fv.raw["_row_id"] = int(_id)
            out.append(fv)
        # Oldest → newest (deterministic clustering order).
        out.reverse()
        return out

    def _load_behaviors(self) -> Dict[str, BehaviorRecord]:
        if self._conn is None:
            return {}
        try:
            cur = self._conn.execute("SELECT * FROM behaviors")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[behavior-discovery] _load_behaviors failed: {}", exc)
            return {}
        out: Dict[str, BehaviorRecord] = {}
        for r in rows:
            try:
                centroid = json.loads(r.get("centroid") or "{}")
            except (TypeError, ValueError):
                centroid = {}
            out[str(r["behavior_id"])] = BehaviorRecord(
                behavior_id=str(r["behavior_id"]),
                centroid_numeric=dict(centroid.get("numeric", {}) or {}),
                centroid_categorical=dict(centroid.get("categorical", {}) or {}),
                state=str(r.get("state") or SHADOW),
                trades=int(r.get("trades") or 0),
                win_rate=float(r.get("win_rate") or 0.0),
                expectancy=float(r.get("expectancy") or 0.0),
                shrunk_expectancy=float(r.get("shrunk_expectancy") or 0.0),
                sharpe=float(r.get("sharpe") or 0.0),
                percentile=float(r.get("percentile") or 0.0),
                confirmations=int(r.get("confirmations") or 0),
                created_at=float(r.get("created_at") or 0.0),
                updated_at=float(r.get("updated_at") or 0.0),
                last_action_trades=int(r.get("last_action_trades") or 0),
                last_seen_pass=int(r.get("last_seen_pass") or 0),
            )
        return out

    def _save_behaviors(self, behaviors: Dict[str, BehaviorRecord]) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute("DELETE FROM behaviors")
            for b in behaviors.values():
                self._conn.execute(
                    """INSERT OR REPLACE INTO behaviors
                       (behavior_id, centroid, state, trades, win_rate, expectancy,
                        shrunk_expectancy, sharpe, percentile, confirmations,
                        created_at, updated_at, last_action_trades, last_seen_pass)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        b.behavior_id, b.centroid_json(), b.state, int(b.trades),
                        float(b.win_rate), float(b.expectancy), float(b.shrunk_expectancy),
                        float(b.sharpe), float(b.percentile), int(b.confirmations),
                        float(b.created_at), float(b.updated_at),
                        int(b.last_action_trades), int(b.last_seen_pass),
                    ),
                )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[behavior-discovery] _save_behaviors failed: {}", exc)

    # ── Recompute (the analysis pass) ───────────────────────────────────────

    def maybe_recompute(self, total_trades: int) -> Optional[dict]:
        """Cluster + score + advance lifecycle when due. Returns the fresh
        payload, or ``None`` when below the floor / not yet due / disabled."""
        if not self.enabled:
            return None
        tt = int(total_trades or 0)
        if tt < self._min_trades_to_cluster:
            return None
        if self._last_computed_trades and (tt - self._last_computed_trades) < self._interval:
            return None
        self._last_computed_trades = tt
        return self.recompute()

    def recompute(self) -> dict:
        """Run one full clustering + scoring + lifecycle pass (uncached read)."""
        with self._lock:
            vectors = self._recent_vectors()
            if len(vectors) < self._min_trades_to_cluster:
                return self._empty_payload(len(vectors))

            self._pass_counter += 1
            labels = self._clusterer.cluster(vectors)

            prev_assignments = self._prev_clustered_row_ids()
            self._persist_assignments(vectors, labels)
            stability = self._stability(vectors, labels, prev_assignments)

            clusters: Dict[int, List[FeatureVector]] = {}
            for fv, lab in zip(vectors, labels):
                if lab >= 0:
                    clusters.setdefault(lab, []).append(fv)

            global_mean = _mean([fv.r for fv in vectors])
            scored = self._score_clusters(clusters, global_mean)
            existing = self._load_behaviors()
            updated = self._reconcile(existing, scored)
            self._apply_lifecycle(updated, self._last_computed_trades)
            self._save_behaviors(updated)

            payload = self._build_payload(updated, len(vectors), len(clusters), stability)
            self._write_history(payload)
            return payload

    def _score_clusters(
        self, clusters: Dict[int, List[FeatureVector]], global_mean: float,
    ) -> List[Tuple[BehaviorScore, Dict[str, float], Dict[str, str]]]:
        out = []
        for _lab, members in clusters.items():
            score = self._scorer.score([m.r for m in members], global_mean)
            cnum, ccat = _centroid(members)
            out.append((score, cnum, ccat))
        return out

    def _reconcile(
        self,
        existing: Dict[str, BehaviorRecord],
        scored: List[Tuple[BehaviorScore, Dict[str, float], Dict[str, str]]],
    ) -> Dict[str, BehaviorRecord]:
        """Match fresh clusters to existing behaviours by centroid (lifecycle
        continuity), updating matches and creating new SHADOW behaviours.

        Existing behaviours not matched this pass are kept (so a temporarily
        sparse style does not vanish) but flagged stale via ``last_seen_pass``.
        """
        now = time.time()
        result: Dict[str, BehaviorRecord] = {bid: b for bid, b in existing.items()}
        claimed: set[str] = set()

        # Order fresh clusters by trade count so the biggest claim matches first.
        scored_sorted = sorted(scored, key=lambda s: s[0].trades, reverse=True)
        for score, cnum, ccat in scored_sorted:
            match_id = self._best_match(result, cnum, ccat, claimed)
            if match_id is not None:
                b = result[match_id]
                claimed.add(match_id)
                b.centroid_numeric = cnum
                b.centroid_categorical = ccat
                b.trades = score.trades
                b.win_rate = score.win_rate
                b.expectancy = score.expectancy
                b.shrunk_expectancy = score.shrunk_expectancy
                b.sharpe = score.sharpe
                b.confirmations += 1
                b.updated_at = now
                b.last_seen_pass = self._pass_counter
            else:
                bid = f"bhv_{uuid.uuid4().hex[:10]}"
                result[bid] = BehaviorRecord(
                    behavior_id=bid,
                    centroid_numeric=cnum,
                    centroid_categorical=ccat,
                    state=SHADOW,
                    trades=score.trades,
                    win_rate=score.win_rate,
                    expectancy=score.expectancy,
                    shrunk_expectancy=score.shrunk_expectancy,
                    sharpe=score.sharpe,
                    confirmations=1,
                    created_at=now,
                    updated_at=now,
                    last_action_trades=self._last_computed_trades,
                    last_seen_pass=self._pass_counter,
                )
                claimed.add(bid)
        return result

    def _best_match(
        self,
        behaviors: Dict[str, BehaviorRecord],
        cnum: Dict[str, float],
        ccat: Dict[str, str],
        claimed: set,
    ) -> Optional[str]:
        best_id: Optional[str] = None
        best_d = self._centroid_match_eps
        for bid, b in behaviors.items():
            if bid in claimed:
                continue
            d = _centroid_distance(b.centroid_numeric, b.centroid_categorical, cnum, ccat)
            if d <= best_d:
                best_d = d
                best_id = bid
        return best_id

    def _apply_lifecycle(self, behaviors: Dict[str, BehaviorRecord], total_trades: int) -> None:
        """Promote / retire behaviours by shrunk-expectancy percentile, rate-
        limited by a per-behaviour cooldown. Advisory only — records the state.

        A behaviour seen this pass with enough trades and a top-percentile
        expectancy promotes SHADOW → ACTIVE; one in the bottom percentile (or
        gone stale — not seen for several passes) retires. Percentile is the
        rank of shrunk expectancy across the currently-seen behaviours.
        """
        seen = [
            b for b in behaviors.values() if b.last_seen_pass == self._pass_counter
        ]
        if seen:
            ordered = sorted(seen, key=lambda b: b.shrunk_expectancy)
            denom = max(1, len(ordered) - 1)
            for rank, b in enumerate(ordered):
                b.percentile = rank / denom if denom else 1.0

        for b in behaviors.values():
            stale_passes = self._pass_counter - b.last_seen_pass
            cooldown_ok = (total_trades - b.last_action_trades) >= self._cooldown_trades

            # Retire stale behaviours that stopped re-appearing.
            if stale_passes >= 3 and b.state != RETIRED:
                b.state = RETIRED
                b.last_action_trades = total_trades
                b.updated_at = time.time()
                continue

            if b.last_seen_pass != self._pass_counter:
                continue  # not measured this pass — leave state unchanged

            if b.trades < self._min_cluster_size:
                continue  # too thin to act on

            if not cooldown_ok:
                continue  # rate-limited

            if b.percentile >= self._promote_threshold and b.state == SHADOW:
                b.state = ACTIVE
                b.last_action_trades = total_trades
                b.updated_at = time.time()
                logger.info(
                    "[behavior-discovery] promoted {} → ACTIVE "
                    "(expectancy {:.3f}R, pct {:.2f}, {} trades)",
                    b.behavior_id, b.shrunk_expectancy, b.percentile, b.trades,
                )
            elif b.percentile <= self._retire_threshold and b.state == ACTIVE:
                b.state = RETIRED
                b.last_action_trades = total_trades
                b.updated_at = time.time()
                logger.info(
                    "[behavior-discovery] retired {} → RETIRED "
                    "(expectancy {:.3f}R, pct {:.2f})",
                    b.behavior_id, b.shrunk_expectancy, b.percentile,
                )

    # ── Stability / assignment persistence ──────────────────────────────────

    def _prev_clustered_row_ids(self) -> set:
        if self._conn is None:
            return set()
        try:
            cur = self._conn.execute(
                "SELECT id FROM behavior_trades WHERE cluster_id >= 0"
            )
            return {int(r[0]) for r in cur.fetchall()}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[behavior-discovery] _prev_clustered_row_ids failed: {}", exc)
            return set()

    def _persist_assignments(self, vectors: List[FeatureVector], labels: List[int]) -> None:
        if self._conn is None:
            return
        try:
            for fv, lab in zip(vectors, labels):
                rid = fv.raw.get("_row_id")
                if rid is None:
                    continue
                self._conn.execute(
                    "UPDATE behavior_trades SET cluster_id=? WHERE id=?",
                    (int(lab), int(rid)),
                )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[behavior-discovery] _persist_assignments failed: {}", exc)

    def _stability(
        self, vectors: List[FeatureVector], labels: List[int], prev_clustered: set,
    ) -> float:
        """Membership-retention proxy: of the trades clustered this pass, the
        fraction that were also clustered last pass. 1.0 on the first pass."""
        if not prev_clustered:
            return 1.0
        now_clustered = {
            int(fv.raw.get("_row_id"))
            for fv, lab in zip(vectors, labels)
            if lab >= 0 and fv.raw.get("_row_id") is not None
        }
        if not now_clustered:
            return 0.0
        retained = len(now_clustered & prev_clustered)
        return round(retained / len(now_clustered), 4)

    # ── Payload / history ───────────────────────────────────────────────────

    def _build_payload(
        self,
        behaviors: Dict[str, BehaviorRecord],
        trades_analyzed: int,
        cluster_count: int,
        stability: float,
    ) -> dict:
        rows = [b.to_dict() for b in behaviors.values()]
        rows.sort(key=lambda r: r["shrunk_expectancy"], reverse=True)
        counts = {SHADOW: 0, ACTIVE: 0, RETIRED: 0}
        for b in behaviors.values():
            counts[b.state] = counts.get(b.state, 0) + 1
        return {
            "computed_at": time.time(),
            "lookback": self._lookback,
            "trades_analyzed": trades_analyzed,
            "cluster_count": cluster_count,
            "behavior_count": len(rows),
            "stability": stability,
            "counts": counts,
            "behaviors": rows[:100],
        }

    def _empty_payload(self, trades: int) -> dict:
        return {
            "computed_at": None,
            "lookback": self._lookback,
            "trades_analyzed": trades,
            "cluster_count": 0,
            "behavior_count": 0,
            "stability": 1.0,
            "counts": {SHADOW: 0, ACTIVE: 0, RETIRED: 0},
            "behaviors": [],
        }

    def _write_history(self, payload: dict) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """INSERT INTO behavior_history
                   (computed_at, trades_analyzed, cluster_count, stability, payload)
                   VALUES (?,?,?,?,?)""",
                (
                    float(payload.get("computed_at") or time.time()),
                    int(payload.get("trades_analyzed", 0)),
                    int(payload.get("cluster_count", 0)),
                    float(payload.get("stability", 0.0)),
                    json.dumps(payload, default=str),
                ),
            )
            self._conn.execute(
                """DELETE FROM behavior_history WHERE id NOT IN
                   (SELECT id FROM behavior_history ORDER BY id DESC LIMIT 50)"""
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[behavior-discovery] _write_history failed: {}", exc)

    def get_cached(self) -> dict:
        empty = self._empty_payload(0)
        if self._conn is None:
            return empty
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT payload FROM behavior_history ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[behavior-discovery] get_cached failed: {}", exc)
                return empty
        if not row:
            return empty
        try:
            val = json.loads(row[0])
            return val if isinstance(val, dict) else empty
        except (TypeError, ValueError):
            return empty

    def get_dashboard_data(self) -> dict:
        return self.get_state()

    def get_state(self) -> dict:
        cached = self.get_cached()
        return {
            "enabled": bool(self.enabled),
            "lookback": self._lookback,
            "interval": self._interval,
            "min_trades_to_cluster": self._min_trades_to_cluster,
            "min_cluster_size": self._min_cluster_size,
            "max_clusters": self._max_clusters,
            "cluster_eps": self._cluster_eps,
            "promote_threshold": self._promote_threshold,
            "retire_threshold": self._retire_threshold,
            "total_trades": self.total_trades(),
            **cached,
        }


__all__ = [
    "SHADOW",
    "ACTIVE",
    "RETIRED",
    "FeatureVector",
    "TradeFeatureExtractor",
    "feature_distance",
    "BehaviorClusterer",
    "BehaviorScore",
    "BehaviorScorer",
    "BehaviorRecord",
    "BehaviorDiscoveryEngine",
]
