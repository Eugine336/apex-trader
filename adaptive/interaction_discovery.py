"""
APEX TRADER — Module Interaction Discovery (L5b: leave-K-out combinatorial)

The L4 :mod:`adaptive.counterfactual` engine answers *one* question per module:
remove it alone, replay, measure its marginal R.  But module interactions are
non-linear.  Removing module A alone might cost ``-5R``; removing module B alone
``-3R``; removing **both** might *gain* ``+20R`` — because together they were
reinforcing each other's bad trades, or (the opposite) each was silently backing
the other up on the trades that actually paid.  Leave-ONE-out can never see that.

This module extends the exact same decision-replay to **leave-K-out**:

  * **Pairwise interaction matrix** — for every module pair ``(A, B)`` it
    compares the joint removal delta against the sum of the individual removal
    deltas.  The non-additive remainder is the *interaction effect*:

        interaction(A, B) = Δ_remove(A, B) − Δ_remove(A) − Δ_remove(B)

    where ``Δ_remove(S)`` is the change in the book's realised R when the modules
    in ``S`` are removed (replay drops every trade whose direction no longer
    survives the smaller panel).  ``Δ_remove({X})`` is exactly L4's
    ``r_difference`` for module X, so the single-module case is consistent with
    the counterfactual engine.

    Per the configured thresholds: ``effect > synergy_threshold`` ⇒ SYNERGY,
    ``effect < toxic_threshold`` ⇒ TOXIC, otherwise INDEPENDENT.

  * **Optimal active subset** — replays the whole book under every candidate
    active subset (only that subset's modules vote) and ranks them by realised
    R, then Sharpe, then parsimony, then drawdown.  The winner is the module
    configuration that *would have* produced the best book — "shadow these,
    keep those".  Exhaustive while the voting-module count is small
    (``2^M`` subsets); greedy backward-elimination above
    ``exhaustive_search_max_modules``.

  * **Toxic / synergy pair extraction** — read-only recommendations the Module
    Governor (or a human) can act on.  This module never changes a weight, a
    mode, or a decision; it only measures and reports.

It reuses the L4 engine's stored trade snapshots (``get_closed_attributions``)
and its pure ``replay_consensus`` math verbatim — it never re-implements
consensus and never writes to the counterfactual store.  Its own result is
expensive, so it is computed periodically and cached (sqlite3 WAL, exception
safe, survives restarts), and the dashboard reads the cache.

Leaf-ish module — stdlib + loguru + the L4 replay + ``brain`` ``Vote`` only.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Optional

from loguru import logger

from adaptive.counterfactual import TradeAttribution, replay_consensus
from brain.directional_consensus import Vote

# ── Relationship classes ────────────────────────────────────────────────
SYNERGY = "SYNERGY"
TOXIC = "TOXIC"
INDEPENDENT = "INDEPENDENT"

_DB_DIR = Path(__file__).parent.parent / "data"
_DB_PATH = _DB_DIR / "interaction_discovery.db"

_CREATE_CACHE = """
CREATE TABLE IF NOT EXISTS interaction_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at     REAL NOT NULL,
    lookback        INTEGER NOT NULL,
    trades_analyzed INTEGER NOT NULL,
    module_count    INTEGER NOT NULL DEFAULT 0,
    payload         TEXT NOT NULL
)
"""


# ── Records ──────────────────────────────────────────────────────────────


@dataclass
class InteractionResult:
    """One module pair's non-additive interaction over the analysed trades."""

    module_a: str
    module_b: str
    removal_delta_a: float        # Δ book-R from removing A alone
    removal_delta_b: float        # Δ book-R from removing B alone
    removal_delta_ab: float       # Δ book-R from removing BOTH
    interaction_effect: float     # Δab − (Δa + Δb): the non-additive remainder
    relationship: str             # SYNERGY / TOXIC / INDEPENDENT

    def to_dict(self) -> dict:
        return {
            "module_a": self.module_a,
            "module_b": self.module_b,
            "removal_delta_a": round(self.removal_delta_a, 4),
            "removal_delta_b": round(self.removal_delta_b, 4),
            "removal_delta_ab": round(self.removal_delta_ab, 4),
            "interaction_effect": round(self.interaction_effect, 4),
            "relationship": self.relationship,
        }


@dataclass
class OptimalSubsetResult:
    """The active-module subset that maximises the replayed book."""

    active_modules: list[str] = field(default_factory=list)
    shadow_modules: list[str] = field(default_factory=list)
    total_r: float = 0.0
    baseline_r: float = 0.0          # book R with every module active
    improvement: float = 0.0         # total_r − baseline_r
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    trades_taken: int = 0
    search: str = "exhaustive"       # exhaustive | greedy

    def to_dict(self) -> dict:
        return {
            "active_modules": list(self.active_modules),
            "shadow_modules": list(self.shadow_modules),
            "total_r": round(self.total_r, 4),
            "baseline_r": round(self.baseline_r, 4),
            "improvement": round(self.improvement, 4),
            "sharpe": round(self.sharpe, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "trades_taken": int(self.trades_taken),
            "search": self.search,
        }


# ── Pure helpers ──────────────────────────────────────────────────────


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(var)


def _max_drawdown(r_sequence: list[float]) -> float:
    """Max peak-to-trough drawdown of the cumulative-R curve (non-negative)."""
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for r in r_sequence:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


@dataclass
class _PreparedTrade:
    """A trade snapshot pre-rebuilt into Vote objects for fast repeated replay."""

    direction: str
    pnl_r: float
    timestamp_open: float
    votes: list[Vote]
    thresholds: dict
    ranker_kwargs: dict
    modules: frozenset[str]               # modules that cast ANY vote here
    # Per-trade replay memo, keyed by the excluded modules that voted here.
    memo: dict = field(default_factory=dict)


def _prepare(trades: list[TradeAttribution]) -> list[_PreparedTrade]:
    out: list[_PreparedTrade] = []
    for attr in trades or []:
        if attr.pnl_r is None or attr.direction not in ("LONG", "SHORT"):
            continue
        votes: list[Vote] = []
        modules: set[str] = set()
        for v in attr.votes or []:
            mod = str(v.get("module", ""))
            if not mod:
                continue
            modules.add(mod)
            votes.append(
                Vote(
                    module=mod,
                    direction=str(v.get("direction", "NEUTRAL")),
                    confidence=float(v.get("confidence", 0.0) or 0.0),
                    weight=float(v.get("weight", 0.0) or 0.0),
                )
            )
        if not votes:
            continue
        out.append(
            _PreparedTrade(
                direction=str(attr.direction),
                pnl_r=float(attr.pnl_r),
                timestamp_open=float(attr.timestamp_open or 0.0),
                votes=votes,
                thresholds=dict(attr.thresholds or {}),
                ranker_kwargs=dict(attr.ranker_kwargs or {}),
                modules=frozenset(modules),
            )
        )
    return out


def _directional_modules(prepared: list[_PreparedTrade]) -> list[str]:
    """Modules that cast at least one LONG/SHORT vote — the only ones whose
    removal can change a decision (so the only ones worth searching over)."""
    mods: set[str] = set()
    for t in prepared:
        for v in t.votes:
            if v.direction in ("LONG", "SHORT"):
                mods.add(v.module)
    return sorted(mods)


# ── Engine ──────────────────────────────────────────────────────────────


class InteractionAnalyzer:
    """Leave-K-out module-interaction analysis over the L4 trade snapshots.

    Purely observational: reads closed-trade snapshots from the injected
    :class:`~adaptive.counterfactual.CounterfactualEngine`, replays the SAME
    consensus math with module subsets removed, and caches a ranked interaction
    matrix + optimal-subset recommendation for the dashboard.  Never raises from
    a public method — a fault here must never block trading.
    """

    def __init__(
        self,
        counterfactual_engine,
        *,
        enabled: bool = False,
        lookback: int = 500,
        interval: int = 500,
        toxic_threshold: float = -0.05,
        synergy_threshold: float = 0.05,
        exhaustive_search_max_modules: int = 12,
        min_trades: int = 50,
        db_path: Optional[Path | str] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self._cf = counterfactual_engine
        self._lookback = max(1, int(lookback))
        self._interval = max(1, int(interval))
        self._toxic_threshold = float(toxic_threshold)
        self._synergy_threshold = float(synergy_threshold)
        self._max_modules = max(1, int(exhaustive_search_max_modules))
        self._min_trades = max(1, int(min_trades))
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._last_computed_trades: int = 0
        # Optional Learning→Governance recommendation gateway. When wired, the
        # toxic module-pair findings are emitted as TOXIC_PAIR_BLOCK
        # recommendations for Governance (Phase 7) to act on. Observational only:
        # this engine never enforces — it reports.
        self._recommendation_gateway = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[interaction] could not create db dir: {}", exc)
        self._connect()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_CACHE)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[interaction] DB connect/init failed ({}): {}", self._db_path, exc,
            )
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[interaction] conn.close() failed")
                self._conn = None

    def set_recommendation_gateway(self, gateway) -> None:
        """Inject (or clear) the Learning→Governance recommendation gateway.

        When wired, each recompute emits the discovered toxic module pairs as
        TOXIC_PAIR_BLOCK recommendations for Governance to consume. The analyzer
        still enforces nothing — it only reports through the pipeline."""
        self._recommendation_gateway = gateway

    @property
    def lookback(self) -> int:
        return self._lookback

    @property
    def interval(self) -> int:
        return self._interval

    @property
    def toxic_threshold(self) -> float:
        return self._toxic_threshold

    @property
    def synergy_threshold(self) -> float:
        return self._synergy_threshold

    @property
    def min_trades(self) -> int:
        return self._min_trades

    def set_params(
        self,
        *,
        lookback: Optional[int] = None,
        interval: Optional[int] = None,
    ) -> None:
        """Update the tunable knobs (clamped to >= 1). Used by the TunerAgent."""
        with self._lock:
            if lookback is not None:
                self._lookback = max(1, int(lookback))
            if interval is not None:
                self._interval = max(1, int(interval))

    # ── Replay core (reuses L4 replay_consensus) ─────────────────────────

    @staticmethod
    def _replay_direction(
        trade: _PreparedTrade, excluded: frozenset[str], memo: dict,
    ) -> str:
        """Replayed entry direction for one trade with ``excluded`` removed.

        Memoised on the excluded modules that actually voted in THIS trade, so
        the same effective panel is never replayed twice across subsets.
        """
        key = excluded & trade.modules
        cached = memo.get(key)
        if cached is not None:
            return cached
        votes = [v for v in trade.votes if v.module not in excluded]
        direction, _ = replay_consensus(votes, trade.thresholds, trade.ranker_kwargs)
        memo[key] = direction
        return direction

    def _book(
        self, prepared: list[_PreparedTrade], excluded: frozenset[str],
    ) -> tuple[float, list[float], int]:
        """Replay the book with ``excluded`` removed.

        A trade is still 'taken as-is' only when its replayed direction matches
        the direction it was actually opened in; flipped / collapsed trades are
        conservatively dropped (we cannot know a hypothetical trade's outcome).
        Returns ``(total_r, ordered_R_sequence, trades_taken)``.
        """
        kept: list[tuple[float, float]] = []  # (timestamp_open, R)
        for t in prepared:
            if self._replay_direction(t, excluded, t.memo) == t.direction:
                kept.append((t.timestamp_open, t.pnl_r))
        total = sum(r for _, r in kept)
        ordered = [r for _, r in sorted(kept, key=lambda x: x[0])]
        return total, ordered, len(kept)

    # ── Pairwise interaction matrix ──────────────────────────────────────

    def compute_pairwise_interactions(
        self, prepared: list[_PreparedTrade], modules: Optional[list[str]] = None,
    ) -> dict[tuple[str, str], InteractionResult]:
        """Full symmetric interaction matrix over every directional-module pair."""
        mods = list(modules) if modules is not None else _directional_modules(prepared)
        if len(mods) < 2:
            return {}
        baseline, _, _ = self._book(prepared, frozenset())
        # Cache single-module removal deltas (reused across every pair).
        single: dict[str, float] = {}
        for m in mods:
            book_m, _, _ = self._book(prepared, frozenset((m,)))
            single[m] = book_m - baseline
        out: dict[tuple[str, str], InteractionResult] = {}
        for a, b in combinations(mods, 2):
            book_ab, _, _ = self._book(prepared, frozenset((a, b)))
            delta_ab = book_ab - baseline
            effect = delta_ab - (single[a] + single[b])
            out[(a, b)] = InteractionResult(
                module_a=a,
                module_b=b,
                removal_delta_a=single[a],
                removal_delta_b=single[b],
                removal_delta_ab=delta_ab,
                interaction_effect=effect,
                relationship=self._classify(effect),
            )
        return out

    def _classify(self, effect: float) -> str:
        if effect > self._synergy_threshold:
            return SYNERGY
        if effect < self._toxic_threshold:
            return TOXIC
        return INDEPENDENT

    # ── Optimal subset search ────────────────────────────────────────────

    def find_optimal_subset(
        self, prepared: list[_PreparedTrade], modules: Optional[list[str]] = None,
    ) -> OptimalSubsetResult:
        """Active-module subset that maximises the replayed book.

        Exhaustive over ``2^M − 1`` non-empty subsets while ``M`` (the number of
        directional voting modules) is at or below ``exhaustive_search_max_modules``;
        otherwise greedy backward-elimination.
        """
        mods = list(modules) if modules is not None else _directional_modules(prepared)
        baseline, base_seq, _ = self._book(prepared, frozenset())
        result = OptimalSubsetResult(
            active_modules=list(mods),
            shadow_modules=[],
            total_r=baseline,
            baseline_r=baseline,
            improvement=0.0,
            sharpe=(sum(base_seq) / len(base_seq) / _std(base_seq)) if _std(base_seq) > 0 else 0.0,
            max_drawdown=_max_drawdown(base_seq),
            trades_taken=len(base_seq),
            search="exhaustive",
        )
        if not mods:
            return result
        all_mods = set(mods)
        if len(mods) <= self._max_modules:
            best = self._search_exhaustive(prepared, mods, all_mods)
            best.search = "exhaustive"
        else:
            best = self._search_greedy(prepared, mods, all_mods)
            best.search = "greedy"
        best.baseline_r = baseline
        best.improvement = best.total_r - baseline
        return best

    def _score_subset(
        self, prepared: list[_PreparedTrade], active: frozenset[str], all_mods: set[str],
    ) -> tuple[float, float, float, int]:
        excluded = frozenset(all_mods - active)
        total, seq, n = self._book(prepared, excluded)
        sd = _std(seq)
        sharpe = (sum(seq) / len(seq) / sd) if (seq and sd > 0) else 0.0
        return total, sharpe, _max_drawdown(seq), n

    @staticmethod
    def _better(
        cand: tuple[float, float, float, int], best: tuple[float, float, float, int],
        cand_size: int, best_size: int,
    ) -> bool:
        """Rank by total R, then Sharpe, then fewer modules, then lower drawdown."""
        c_total, c_sharpe, c_dd, _ = cand
        b_total, b_sharpe, b_dd, _ = best
        if not math.isclose(c_total, b_total, abs_tol=1e-9):
            return c_total > b_total
        if not math.isclose(c_sharpe, b_sharpe, abs_tol=1e-9):
            return c_sharpe > b_sharpe
        if cand_size != best_size:
            return cand_size < best_size
        return c_dd < b_dd

    def _build_result(
        self, prepared: list[_PreparedTrade], active: frozenset[str], all_mods: set[str],
    ) -> OptimalSubsetResult:
        total, sharpe, dd, n = self._score_subset(prepared, active, all_mods)
        return OptimalSubsetResult(
            active_modules=sorted(active),
            shadow_modules=sorted(all_mods - active),
            total_r=total,
            sharpe=sharpe,
            max_drawdown=dd,
            trades_taken=n,
        )

    def _search_exhaustive(
        self, prepared: list[_PreparedTrade], mods: list[str], all_mods: set[str],
    ) -> OptimalSubsetResult:
        best_active: Optional[frozenset[str]] = None
        best_score: Optional[tuple[float, float, float, int]] = None
        best_size = 0
        m = len(mods)
        for k in range(1, m + 1):
            for combo in combinations(mods, k):
                active = frozenset(combo)
                score = self._score_subset(prepared, active, all_mods)
                if best_score is None or self._better(score, best_score, k, best_size):
                    best_score, best_active, best_size = score, active, k
        active = best_active if best_active is not None else frozenset(mods)
        return self._build_result(prepared, active, all_mods)

    def _search_greedy(
        self, prepared: list[_PreparedTrade], mods: list[str], all_mods: set[str],
    ) -> OptimalSubsetResult:
        """Backward elimination: drop the module whose removal most improves the
        book, repeat until no single removal helps."""
        active = set(mods)
        cur_score = self._score_subset(prepared, frozenset(active), all_mods)
        while len(active) > 1:
            best_drop: Optional[str] = None
            best_score = cur_score
            for mod in sorted(active):
                cand_active = frozenset(active - {mod})
                score = self._score_subset(prepared, cand_active, all_mods)
                if self._better(score, best_score, len(cand_active), len(active)):
                    best_score, best_drop = score, mod
            if best_drop is None:
                break
            active.discard(best_drop)
            cur_score = best_score
        return self._build_result(prepared, frozenset(active), all_mods)

    # ── Compute + cache ──────────────────────────────────────────────────

    def compute_and_cache(self, lookback: Optional[int] = None) -> dict:
        """Compute the full interaction analysis and persist it as a cache row."""
        lb = int(lookback) if lookback else self._lookback
        trades = self._closed_attributions(lb)
        prepared = _prepare(trades)
        mods = _directional_modules(prepared)
        # The leave-K-out replay calls decide() up to 2^M×N times; that path
        # logs participation at INFO on every call. Silence ONLY the consensus
        # replay noise for the duration of the (periodic, off-hot-path) compute,
        # then restore — the live scanner's consensus logging is unaffected
        # outside this window.
        logger.disable("brain.directional_consensus")
        try:
            matrix = self.compute_pairwise_interactions(prepared, mods)
            optimal = self.find_optimal_subset(prepared, mods)
        finally:
            logger.enable("brain.directional_consensus")

        pairs = [r.to_dict() for r in matrix.values()]
        toxic = sorted(
            (p for p in pairs if p["relationship"] == TOXIC),
            key=lambda p: p["interaction_effect"],
        )
        synergy = sorted(
            (p for p in pairs if p["relationship"] == SYNERGY),
            key=lambda p: p["interaction_effect"], reverse=True,
        )
        payload = {
            "computed_at": time.time(),
            "lookback": lb,
            "trades_analyzed": len(prepared),
            "modules": mods,
            "module_count": len(mods),
            "toxic_threshold": self._toxic_threshold,
            "synergy_threshold": self._synergy_threshold,
            "search": optimal.search,
            "pairs": pairs,
            "toxic_pairs": toxic,
            "synergy_pairs": synergy,
            "optimal_subset": optimal.to_dict(),
        }
        self._write_cache(payload, len(prepared), len(mods))

        # Surface the discovered relationships at INFO so the dormant
        # intelligence is visible in the operational log (not only the
        # dashboard / cache). Read-only — recommendations the operator (or a
        # future governor wire) may act on; the analyzer itself never shadows.
        try:
            shadow = list(optimal.shadow_modules or [])
            if toxic or shadow:
                top_toxic = ", ".join(
                    f"{p['module_a']}+{p['module_b']}({p['interaction_effect']:+.3f})"
                    for p in toxic[:3]
                ) or "none"
                logger.info(
                    "[interaction] {} trade(s) analysed — toxic pairs: [{}]; "
                    "optimal subset shadows: [{}] (improvement {:+.3f}R)",
                    len(prepared), top_toxic,
                    ", ".join(shadow) or "none",
                    float(optimal.improvement or 0.0),
                )
        except Exception as exc:  # noqa: BLE001 — logging must never break compute
            logger.debug("[interaction] summary log failed: {}", exc)

        # Emit toxic module-pair findings as recommendations for Governance
        # (Phase 7). Recorded through the gateway only — never enforced here, so
        # this changes no live behaviour.
        self._emit_toxic_recommendations(toxic)

        return payload

    def _emit_toxic_recommendations(self, toxic: list[dict]) -> None:
        """Submit each toxic module pair as a TOXIC_PAIR_BLOCK recommendation.

        No-op when no gateway is wired or there are no toxic pairs. Never raises
        — a fault here must not break the (periodic) compute pass.
        """
        gateway = self._recommendation_gateway
        if gateway is None or not toxic:
            return
        try:
            from adaptive.recommendations import (
                LearningRecommendation,
                RecommendationType,
            )
            for pair in toxic:
                rec = LearningRecommendation(
                    source="interaction_analyzer",
                    recommendation_type=RecommendationType.TOXIC_PAIR_BLOCK,
                    payload={
                        "module_a": pair.get("module_a"),
                        "module_b": pair.get("module_b"),
                        "interaction_effect": pair.get("interaction_effect"),
                    },
                    confidence=abs(float(pair.get("interaction_effect", 0.0) or 0.0)),
                    evidence={"relationship": pair.get("relationship")},
                )
                gateway.submit(rec)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[interaction] toxic recommendation emit failed: {}", exc)

    def _closed_attributions(self, lookback: int) -> list[TradeAttribution]:
        if self._cf is None:
            return []
        try:
            return self._cf.get_closed_attributions(lookback)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[interaction] fetch attributions failed: {}", exc)
            return []

    def _write_cache(self, payload: dict, trades_analyzed: int, module_count: int) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO interaction_cache
                       (computed_at, lookback, trades_analyzed, module_count, payload)
                       VALUES (?,?,?,?,?)""",
                    (
                        float(payload.get("computed_at", time.time())),
                        int(payload.get("lookback", self._lookback)),
                        int(trades_analyzed),
                        int(module_count),
                        json.dumps(payload, default=str),
                    ),
                )
                # Keep the cache table bounded — only the latest 50 rows matter.
                self._conn.execute(
                    """DELETE FROM interaction_cache WHERE id NOT IN
                       (SELECT id FROM interaction_cache ORDER BY id DESC LIMIT 50)"""
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[interaction] cache write failed: {}", exc)

    def get_cached(self) -> dict:
        """Latest cached interaction analysis (empty shape when none computed)."""
        empty = {
            "computed_at": None,
            "lookback": self._lookback,
            "trades_analyzed": 0,
            "modules": [],
            "module_count": 0,
            "toxic_threshold": self._toxic_threshold,
            "synergy_threshold": self._synergy_threshold,
            "search": "exhaustive",
            "pairs": [],
            "toxic_pairs": [],
            "synergy_pairs": [],
            "optimal_subset": OptimalSubsetResult().to_dict(),
        }
        if self._conn is None:
            return empty
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT payload FROM interaction_cache ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[interaction] cache read failed: {}", exc)
                return empty
        if not row:
            return empty
        return _loads_dict(row[0]) or empty

    def get_toxic_pairs(self) -> list[dict]:
        """Read-only recommendation: pairs the Governor (or a human) may shadow."""
        return list(self.get_cached().get("toxic_pairs", []) or [])

    def get_synergy_pairs(self) -> list[dict]:
        """Read-only recommendation: pairs that protect each other (don't shadow
        one without re-checking the other)."""
        return list(self.get_cached().get("synergy_pairs", []) or [])

    def maybe_recompute(self, total_trades: int) -> Optional[dict]:
        """Recompute + cache when enough new trades have closed since last run.

        Honours the ``min_trades`` floor and the ``interval`` cadence so the
        expensive leave-K-out replay never runs on a thin / unchanged book.
        Returns the fresh payload when it recomputed, else ``None``.
        """
        tt = int(total_trades or 0)
        if tt < self._min_trades:
            return None
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


__all__ = [
    "SYNERGY",
    "TOXIC",
    "INDEPENDENT",
    "InteractionResult",
    "OptimalSubsetResult",
    "InteractionAnalyzer",
]
