"""
APEX TRADER — Counterfactual Attribution Engine (L4)

The system records every signal, weights every module's vote, and collapses the
panel into one entry decision.  But it never asks the only question that tells
you which modules *deserve to exist*:

    For each module, what is its marginal contribution to the decisions taken?

This is NOT backtesting against price data.  It is **decision replay** — taking
the exact vote snapshot that opened each trade and re-running the SAME consensus
math (``decide`` + the ranker's NEUTRAL rescue) with one module removed at a
time.  That answers, per module:

  * **DECISIVE** — the trade only happened *because* of this module (removing it
    collapses the panel to NEUTRAL or flips the direction).
  * **SUPPORTING** — the trade would have happened anyway; the module only added
    weight to a decision the rest of the panel already carried.
  * **OPPOSING** — the module voted *against* the winning direction (it was
    overruled).
  * **ABSENT** — the module did not cast a directional vote on this trade.

Joining those classifications to the realised R of each trade yields each
module's *marginal* expectancy, Sharpe contribution and drawdown contribution —
and the bottom-line question: *would we have been better off without it?*

It is purely observational.  It never changes a weight, a mode, or a decision —
it only measures.  Storage mirrors the proven ``persistence.shadow_store`` /
``signal_ledger`` pattern: sync sqlite3 in WAL mode, exception-safe, survives
restarts.  Attribution is expensive, so it is computed periodically (every N
closed trades) and the result is cached; the dashboard reads the cache.

Leaf-ish module — stdlib + loguru + the pure ``brain`` consensus math only.  It
imports nothing from the live loop, so it can be unit-tested in isolation.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

from brain.directional_consensus import Vote, decide
from brain.opportunity_ranker import rank_opportunities

# ── Trade classifications ──────────────────────────────────────────────────
DECISIVE = "DECISIVE"
SUPPORTING = "SUPPORTING"
OPPOSING = "OPPOSING"
ABSENT = "ABSENT"

from runtime_paths import data_dir as _data_dir  # noqa: E402

_DB_DIR = _data_dir()
_DB_PATH = _DB_DIR / "counterfactual.db"

_CREATE_ATTRIBUTION = """
CREATE TABLE IF NOT EXISTS trade_attribution (
    trade_id             TEXT PRIMARY KEY,
    pair                 TEXT NOT NULL,
    direction            TEXT NOT NULL,
    timeframe_class      TEXT,
    timestamp_open       REAL NOT NULL,
    timestamp_close      REAL,
    votes                TEXT NOT NULL,
    consensus_direction  TEXT,
    consensus_net        REAL NOT NULL DEFAULT 0.0,
    consensus_agreement  REAL NOT NULL DEFAULT 0.0,
    thresholds           TEXT NOT NULL,
    ranker_kwargs        TEXT NOT NULL,
    pnl_r                REAL,
    won                  INTEGER,
    outcome              TEXT,
    exit_cause           TEXT,
    closed               INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_CACHE = """
CREATE TABLE IF NOT EXISTS attribution_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at     REAL NOT NULL,
    lookback        INTEGER NOT NULL,
    trades_analyzed INTEGER NOT NULL,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    payload         TEXT NOT NULL
)
"""

_CREATE_IDX_CLOSED = (
    "CREATE INDEX IF NOT EXISTS idx_attr_closed "
    "ON trade_attribution (closed, timestamp_open)"
)


# ── Records ──────────────────────────────────────────────────────────────


@dataclass
class TradeAttribution:
    """A snapshot of the decision that opened one trade, plus its outcome.

    Captured at entry (votes + the exact consensus thresholds + ranker config
    that produced the direction) and completed at close (realised R).  Holding
    the full snapshot is what lets the replay use the *identical* math the live
    system used, with one module removed.
    """

    trade_id: str
    pair: str
    direction: str                       # actual trade direction: LONG / SHORT
    timeframe_class: str = ""            # ranker selected_horizon (SCALP/SWING)
    timestamp_open: float = 0.0
    timestamp_close: Optional[float] = None
    # [{"module", "direction", "confidence", "weight"}] — the entry vote panel.
    votes: list[dict] = field(default_factory=list)
    consensus_direction: str = ""
    consensus_net: float = 0.0
    consensus_agreement: float = 0.0
    # decide() kwargs that were in force at entry.
    thresholds: dict = field(default_factory=dict)
    # rank_opportunities() + NEUTRAL-rescue kwargs in force at entry.
    ranker_kwargs: dict = field(default_factory=dict)
    pnl_r: Optional[float] = None
    won: Optional[bool] = None
    outcome: str = ""
    exit_cause: str = ""
    closed: bool = False

    def __post_init__(self) -> None:
        if not self.timestamp_open:
            self.timestamp_open = time.time()


@dataclass
class CounterfactualResult:
    """The result of replaying the consensus with one module removed."""

    excluded_module: str
    original_direction: str
    counterfactual_direction: str
    would_trade: bool
    counterfactual_net: float
    margin: float                 # |net| − min_net_score (distance past gate)
    classification: str           # DECISIVE / SUPPORTING / OPPOSING / ABSENT


@dataclass
class ModuleAttribution:
    """One module's marginal contribution across the analysed trades."""

    module: str
    trades_involved: int = 0          # trades where it cast a directional vote
    decisive_trades: int = 0
    decisive_r: float = 0.0
    supporting_trades: int = 0
    supporting_r: float = 0.0
    opposing_trades: int = 0
    opposing_r: float = 0.0
    absent_trades: int = 0
    marginal_r: float = 0.0           # = decisive_r (the unique contribution)
    expectancy_when_decisive: float = 0.0
    sharpe_contribution: float = 0.0
    drawdown_contribution: float = 0.0
    better_off_without: bool = False
    r_difference: float = 0.0         # P&L delta if the module were removed

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "trades_involved": self.trades_involved,
            "decisive_trades": self.decisive_trades,
            "decisive_r": round(self.decisive_r, 4),
            "supporting_trades": self.supporting_trades,
            "supporting_r": round(self.supporting_r, 4),
            "opposing_trades": self.opposing_trades,
            "opposing_r": round(self.opposing_r, 4),
            "absent_trades": self.absent_trades,
            "marginal_r": round(self.marginal_r, 4),
            "expectancy_when_decisive": round(self.expectancy_when_decisive, 4),
            "sharpe_contribution": round(self.sharpe_contribution, 4),
            "drawdown_contribution": round(self.drawdown_contribution, 4),
            "better_off_without": bool(self.better_off_without),
            "r_difference": round(self.r_difference, 4),
        }


# ── Pure replay helpers ─────────────────────────────────────────────────


def _votes_from_dicts(rows: list[dict]) -> list[Vote]:
    """Rebuild ``Vote`` objects from a serialised vote snapshot."""
    out: list[Vote] = []
    for r in rows or []:
        try:
            out.append(
                Vote(
                    module=str(r.get("module", "")),
                    direction=str(r.get("direction", "NEUTRAL")),
                    confidence=float(r.get("confidence", 0.0) or 0.0),
                    weight=float(r.get("weight", 0.0) or 0.0),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[counterfactual] vote rebuild failed: {}", exc)
    return out


# Only the rank_opportunities kwargs (the rescue flags are read separately).
_RANK_KWARG_KEYS = (
    "scalp_modules",
    "swing_modules",
    "scalp_reward_risk",
    "swing_reward_risk",
    "base_win_rate",
    "confidence_win_rate_gain",
    "min_expected_value",
    "min_cluster_confidence",
    "min_cluster_contributors",
)


def replay_consensus(
    votes: list[Vote], thresholds: dict, ranker_kwargs: dict,
) -> tuple[str, float]:
    """Re-run the live entry-direction math on a vote panel.

    Mirrors ``scanner.pair_scanner`` exactly: the scalar ``decide`` runs first,
    and when it returns NEUTRAL *and* the ranker is live with rescue enabled,
    the best-EV ranked opportunity promotes the direction (the same condition as
    ``rescue_neutral_direction``).  Returns ``(direction, net_score)``.  Never
    raises and never logs the suppressed-minority spam (quiet replay).
    """
    th = thresholds or {}
    decision = decide(
        votes,
        min_net_score=float(th.get("min_net_score", 1.5)),
        min_agreement=float(th.get("min_agreement", 0.55)),
        high_authority_modules=list(th.get("high_authority_modules", []) or []),
        high_authority_oppose_confidence=float(
            th.get("high_authority_oppose_confidence", 0.6)
        ),
        min_contributors=int(th.get("min_contributors", 1) or 1),
        log_suppressed_minorities=False,
    )
    direction = decision.direction
    net = decision.net_score

    rk = ranker_kwargs or {}
    if (
        direction == "NEUTRAL"
        and bool(rk.get("execute", False))
        and bool(rk.get("rescue_neutral_consensus", False))
    ):
        rank_kwargs = {k: rk[k] for k in _RANK_KWARG_KEYS if k in rk}
        try:
            candidates = rank_opportunities(votes, **rank_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[counterfactual] rank replay failed: {}", exc)
            candidates = []
        if candidates and getattr(candidates[0], "direction", "NEUTRAL") in (
            "LONG",
            "SHORT",
        ):
            direction = candidates[0].direction
    return direction, net


def replay_without(
    attribution: TradeAttribution, excluded_module: str,
) -> CounterfactualResult:
    """Replay the consensus for one trade with ``excluded_module`` removed."""
    original = attribution.direction
    remaining = [
        v for v in attribution.votes if v.get("module") != excluded_module
    ]
    votes = _votes_from_dicts(remaining)
    cf_dir, cf_net = replay_consensus(
        votes, attribution.thresholds, attribution.ranker_kwargs,
    )
    would_trade = cf_dir in ("LONG", "SHORT")
    min_net = float((attribution.thresholds or {}).get("min_net_score", 1.5))
    margin = abs(cf_net) - min_net

    classification = _classify(attribution, excluded_module, cf_dir, would_trade)
    return CounterfactualResult(
        excluded_module=excluded_module,
        original_direction=original,
        counterfactual_direction=cf_dir,
        would_trade=would_trade,
        counterfactual_net=cf_net,
        margin=margin,
        classification=classification,
    )


def _module_vote(attribution: TradeAttribution, module: str) -> Optional[dict]:
    for v in attribution.votes:
        if v.get("module") == module:
            return v
    return None


def _classify(
    attribution: TradeAttribution,
    module: str,
    cf_direction: str,
    would_trade: bool,
) -> str:
    """Classify a module's role in one trade (see module docstring)."""
    mvote = _module_vote(attribution, module)
    if mvote is None or mvote.get("direction") not in ("LONG", "SHORT"):
        return ABSENT
    if mvote.get("direction") != attribution.direction:
        return OPPOSING
    # Module voted WITH the winning direction — was it necessary?
    if would_trade and cf_direction == attribution.direction:
        return SUPPORTING
    return DECISIVE


def _max_drawdown(r_sequence: list[float]) -> float:
    """Max peak-to-trough drawdown of the cumulative-R equity curve.

    Returns a non-negative magnitude (0.0 when the curve never retraces).
    """
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for r in r_sequence:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(var)


# ── Engine ──────────────────────────────────────────────────────────────


class CounterfactualEngine:
    """Records per-trade decision snapshots and computes module attribution.

    Two-phase capture: ``record_open`` at entry (vote snapshot + the exact
    consensus config), ``complete`` at close (realised R).  ``compute_and_cache``
    runs the leave-one-out replay across recent closed trades and caches the
    ranked module table for the dashboard.  Every public method is exception
    safe — a fault here must never block trading.
    """

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        *,
        enabled: bool = False,
        attribution_lookback: int = 500,
        attribution_interval: int = 100,
        min_trades_for_attribution: int = 50,
    ) -> None:
        self.enabled = bool(enabled)
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._lookback = max(1, int(attribution_lookback))
        self._interval = max(1, int(attribution_interval))
        self._min_trades = max(1, int(min_trades_for_attribution))
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._last_computed_trades: int = 0
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[counterfactual] could not create db dir: {}", exc)
        self._connect()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_ATTRIBUTION)
            self._conn.execute(_CREATE_CACHE)
            self._conn.execute(_CREATE_IDX_CLOSED)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[counterfactual] DB connect/init failed ({}): {}", self._db_path, exc,
            )
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[counterfactual] conn.close() failed")
                self._conn = None

    @property
    def lookback(self) -> int:
        return self._lookback

    @property
    def interval(self) -> int:
        return self._interval

    @property
    def min_trades_for_attribution(self) -> int:
        return self._min_trades

    def set_params(
        self,
        *,
        attribution_lookback: Optional[int] = None,
        attribution_interval: Optional[int] = None,
    ) -> None:
        """Update the tunable knobs (clamped to >= 1). Used by the TunerAgent."""
        with self._lock:
            if attribution_lookback is not None:
                self._lookback = max(1, int(attribution_lookback))
            if attribution_interval is not None:
                self._interval = max(1, int(attribution_interval))

    # ── Phase 1: capture at entry ─────────────────────────────────────────

    def record_open(self, attribution: TradeAttribution) -> bool:
        """Persist the decision snapshot that opened a trade. Returns success."""
        if self._conn is None or attribution is None or not attribution.trade_id:
            return False
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO trade_attribution
                       (trade_id, pair, direction, timeframe_class,
                        timestamp_open, timestamp_close, votes,
                        consensus_direction, consensus_net, consensus_agreement,
                        thresholds, ranker_kwargs, pnl_r, won, outcome,
                        exit_cause, closed)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(attribution.trade_id),
                        str(attribution.pair),
                        str(attribution.direction),
                        str(attribution.timeframe_class or ""),
                        float(attribution.timestamp_open or time.time()),
                        attribution.timestamp_close,
                        json.dumps(attribution.votes or [], default=str),
                        str(attribution.consensus_direction or ""),
                        float(attribution.consensus_net or 0.0),
                        float(attribution.consensus_agreement or 0.0),
                        json.dumps(attribution.thresholds or {}, default=str),
                        json.dumps(attribution.ranker_kwargs or {}, default=str),
                        attribution.pnl_r,
                        None if attribution.won is None else (1 if attribution.won else 0),
                        str(attribution.outcome or ""),
                        str(attribution.exit_cause or ""),
                        1 if attribution.closed else 0,
                    ),
                )
                self._conn.commit()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("[counterfactual] record_open failed: {}", exc)
                return False

    # ── Phase 2: complete at close ────────────────────────────────────────

    def complete(self, trade_id: str, outcome: dict) -> bool:
        """Attach the realised outcome (pnl_r, won, exit cause) to a snapshot."""
        if self._conn is None or not trade_id or not outcome:
            return False
        pnl_r = outcome.get("pnl_r")
        won = outcome.get("won")
        with self._lock:
            try:
                cur = self._conn.execute(
                    """UPDATE trade_attribution
                       SET pnl_r=?, won=?, outcome=?, exit_cause=?,
                           timestamp_close=?, closed=1
                       WHERE trade_id=?""",
                    (
                        None if pnl_r is None else float(pnl_r),
                        None if won is None else (1 if won else 0),
                        str(outcome.get("outcome", "")),
                        str(outcome.get("exit_cause", "")),
                        float(outcome.get("timestamp_close", time.time())),
                        str(trade_id),
                    ),
                )
                self._conn.commit()
                return cur.rowcount > 0
            except Exception as exc:  # noqa: BLE001
                logger.debug("[counterfactual] complete failed: {}", exc)
                return False

    # ── Reading ────────────────────────────────────────────────────────

    def _row_to_attribution(self, row: dict) -> TradeAttribution:
        return TradeAttribution(
            trade_id=str(row.get("trade_id", "")),
            pair=str(row.get("pair", "")),
            direction=str(row.get("direction", "")),
            timeframe_class=str(row.get("timeframe_class") or ""),
            timestamp_open=float(row.get("timestamp_open") or 0.0),
            timestamp_close=row.get("timestamp_close"),
            votes=_loads_list(row.get("votes")),
            consensus_direction=str(row.get("consensus_direction") or ""),
            consensus_net=float(row.get("consensus_net") or 0.0),
            consensus_agreement=float(row.get("consensus_agreement") or 0.0),
            thresholds=_loads_dict(row.get("thresholds")),
            ranker_kwargs=_loads_dict(row.get("ranker_kwargs")),
            pnl_r=row.get("pnl_r"),
            won=None if row.get("won") is None else bool(row.get("won")),
            outcome=str(row.get("outcome") or ""),
            exit_cause=str(row.get("exit_cause") or ""),
            closed=bool(row.get("closed")),
        )

    def get_closed_attributions(self, lookback: Optional[int] = None) -> list[TradeAttribution]:
        """Most-recent closed, graded (pnl_r present) trade snapshots."""
        if self._conn is None:
            return []
        lb = int(lookback) if lookback else self._lookback
        with self._lock:
            try:
                cur = self._conn.execute(
                    """SELECT * FROM trade_attribution
                       WHERE closed=1 AND pnl_r IS NOT NULL
                       ORDER BY timestamp_open DESC LIMIT ?""",
                    (int(lb),),
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[counterfactual] get_closed_attributions failed: {}", exc)
                return []
        return [self._row_to_attribution(r) for r in rows]

    def get_attribution(self, trade_id: str) -> Optional[TradeAttribution]:
        if self._conn is None or not trade_id:
            return None
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT * FROM trade_attribution WHERE trade_id=?", (str(trade_id),),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [d[0] for d in cur.description]
                return self._row_to_attribution(dict(zip(cols, row)))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[counterfactual] get_attribution failed: {}", exc)
                return None

    # ── Attribution computation ──────────────────────────────────────────

    def compute_module_attribution(
        self, module: str, lookback: Optional[int] = None,
        attributions: Optional[list[TradeAttribution]] = None,
    ) -> ModuleAttribution:
        """Leave-one-out attribution for a single module over recent trades."""
        trades = attributions if attributions is not None else self.get_closed_attributions(lookback)
        agg = ModuleAttribution(module=module)
        decisive_r_seq: list[tuple[float, float]] = []  # (timestamp_open, R)
        for attr in trades:
            if attr.pnl_r is None or attr.direction not in ("LONG", "SHORT"):
                continue
            cf = replay_without(attr, module)
            cls = cf.classification
            r = float(attr.pnl_r)
            if cls == ABSENT:
                agg.absent_trades += 1
                continue
            # Cast a directional vote on this trade.
            agg.trades_involved += 1
            if cls == OPPOSING:
                agg.opposing_trades += 1
                agg.opposing_r += r
            elif cls == SUPPORTING:
                agg.supporting_trades += 1
                agg.supporting_r += r
            elif cls == DECISIVE:
                agg.decisive_trades += 1
                agg.decisive_r += r
                decisive_r_seq.append((attr.timestamp_open, r))

        agg.marginal_r = agg.decisive_r
        if agg.decisive_trades > 0:
            agg.expectancy_when_decisive = agg.decisive_r / agg.decisive_trades
            dr = [r for _, r in decisive_r_seq]
            sd = _std(dr)
            agg.sharpe_contribution = (agg.expectancy_when_decisive / sd) if sd > 0 else 0.0
            ordered = [r for _, r in sorted(decisive_r_seq, key=lambda t: t[0])]
            agg.drawdown_contribution = _max_drawdown(ordered)
        # Removing the module deletes exactly its decisive trades (both wins and
        # losses); supporting/opposing trades would still have happened.
        agg.r_difference = -agg.marginal_r
        agg.better_off_without = agg.marginal_r < 0
        return agg

    def compute_all_attributions(
        self, lookback: Optional[int] = None,
    ) -> dict[str, ModuleAttribution]:
        """Leave-one-out attribution for every module that voted in the window."""
        trades = self.get_closed_attributions(lookback)
        modules: set[str] = set()
        for attr in trades:
            for v in attr.votes:
                m = v.get("module")
                if m:
                    modules.add(str(m))
        return {
            m: self.compute_module_attribution(m, attributions=trades)
            for m in sorted(modules)
        }

    # ── Periodic compute + cache ─────────────────────────────────────────

    def compute_and_cache(self, lookback: Optional[int] = None) -> dict:
        """Compute the full module ranking and persist it as a cache row."""
        lb = int(lookback) if lookback else self._lookback
        trades = self.get_closed_attributions(lb)
        attributions = {}
        modules: set[str] = set()
        for attr in trades:
            for v in attr.votes:
                m = v.get("module")
                if m:
                    modules.add(str(m))
        for m in sorted(modules):
            attributions[m] = self.compute_module_attribution(m, attributions=trades).to_dict()
        # Rank most valuable → least → actively harmful (by marginal R).
        ranked = sorted(
            attributions.values(), key=lambda a: a["marginal_r"], reverse=True,
        )
        payload = {
            "computed_at": time.time(),
            "lookback": lb,
            "trades_analyzed": len(trades),
            "module_count": len(ranked),
            "modules": ranked,
        }
        self._write_cache(payload, len(trades))
        return payload

    def _write_cache(self, payload: dict, trades_analyzed: int) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO attribution_cache
                       (computed_at, lookback, trades_analyzed, total_trades, payload)
                       VALUES (?,?,?,?,?)""",
                    (
                        float(payload.get("computed_at", time.time())),
                        int(payload.get("lookback", self._lookback)),
                        int(trades_analyzed),
                        int(self._last_computed_trades),
                        json.dumps(payload, default=str),
                    ),
                )
                # Keep the cache table bounded — only the latest 50 rows matter.
                self._conn.execute(
                    """DELETE FROM attribution_cache WHERE id NOT IN
                       (SELECT id FROM attribution_cache ORDER BY id DESC LIMIT 50)"""
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[counterfactual] cache write failed: {}", exc)

    def get_cached_attributions(self) -> dict:
        """Latest cached module ranking (empty shape when none computed yet)."""
        empty = {
            "computed_at": None,
            "lookback": self._lookback,
            "trades_analyzed": 0,
            "module_count": 0,
            "modules": [],
        }
        if self._conn is None:
            return empty
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT payload FROM attribution_cache ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[counterfactual] cache read failed: {}", exc)
                return empty
        if not row:
            return empty
        return _loads_dict(row[0]) or empty

    def maybe_recompute(self, total_trades: int) -> Optional[dict]:
        """Recompute + cache when enough new trades have closed since last run.

        Returns the fresh payload when it recomputed, else None.  Honours the
        ``min_trades_for_attribution`` floor and the ``attribution_interval``
        cadence so the expensive replay never runs on a thin / unchanged book.
        """
        tt = int(total_trades or 0)
        if tt < self._min_trades:
            return None
        # First qualifying run fires as soon as the min-trades floor is cleared;
        # thereafter the interval gates how often the expensive replay reruns.
        if self._last_computed_trades and (tt - self._last_computed_trades) < self._interval:
            return None
        self._last_computed_trades = tt
        return self.compute_and_cache()


# ── Helpers ──────────────────────────────────────────────────────────────


def _loads_dict(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


def _loads_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (TypeError, ValueError):
        return []


__all__ = [
    "DECISIVE",
    "SUPPORTING",
    "OPPOSING",
    "ABSENT",
    "TradeAttribution",
    "CounterfactualResult",
    "ModuleAttribution",
    "CounterfactualEngine",
    "replay_consensus",
    "replay_without",
]
