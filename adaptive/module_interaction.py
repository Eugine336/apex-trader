"""
APEX TRADER — Module Interaction Discovery (L5b)

Leave-one-out attribution (L4) measures each module *alone*.  But module effects
are non-linear: two modules can each look fine individually yet be jointly toxic
(they reinforce each other's bad trades), or jointly synergistic (trades that
needed BOTH).  Removing A costs −5R, removing B costs −3R, but removing both
GAINS +20R — because together they kept opening losers neither would open alone.

This engine answers that with the SAME decision-replay foundation as L4
(``replay_consensus`` over the exact vote panel that opened each trade):

  * **subset replay** — re-run the entry decision using only a subset of the
    modules' votes, and total the realised R of the trades that still open in
    the same direction.  ``baseline`` is the full panel.
  * **pairwise interaction** — for every module pair (a, b):
        effect(x)   = subset_R(all − x) − baseline       (R change from removing x)
        interaction = effect(a, b) − (effect(a) + effect(b))
    A positive interaction means removing both helps *more* than the parts —
    a **toxic / reinforcing** pair.  A negative interaction means the two
    overlap (**redundant** coverage).
  * **optimal active subset** — greedily drop the module whose removal most
    raises retained R, repeat while it still helps.  The surviving subset is the
    set the data says to keep.

Purely analytical — it never changes a weight, a mode, or a decision; it only
measures and recommends.  Storage mirrors the proven counterfactual/signal-ledger
pattern (sync sqlite3 WAL, exception-safe, survives restarts).  The combinatorial
pass is expensive, so it runs periodically and caches a ranked table the
dashboard reads.

Leaf-ish module — stdlib + loguru + the pure ``adaptive.counterfactual`` replay
helpers only.  Exception-safe throughout; a fault here never blocks trading.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Optional

from loguru import logger

from adaptive.counterfactual import TradeAttribution, _votes_from_dicts, replay_consensus

_DB_DIR = Path(__file__).parent.parent / "data"
_DB_PATH = _DB_DIR / "module_interaction.db"

TOXIC = "TOXIC"            # removing both helps MORE than the sum of parts
SYNERGISTIC = "SYNERGISTIC"  # the pair enables shared value (removal hurts jointly)
REDUNDANT = "REDUNDANT"    # overlapping coverage (sub-additive)
NEUTRAL = "NEUTRAL"

_REPLAY_LOGGERS = ("brain.directional_consensus", "brain.opportunity_ranker")


@contextlib.contextmanager
def _quiet_replay():
    for name in _REPLAY_LOGGERS:
        logger.disable(name)
    try:
        yield
    finally:
        for name in _REPLAY_LOGGERS:
            logger.enable(name)


_CREATE_CACHE = """
CREATE TABLE IF NOT EXISTS interaction_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at     REAL NOT NULL,
    lookback        INTEGER NOT NULL,
    trades_analyzed INTEGER NOT NULL,
    payload         TEXT NOT NULL
)
"""


@dataclass
class PairInteraction:
    """The joint effect of removing two modules together vs separately."""

    module_a: str
    module_b: str
    effect_a: float = 0.0       # R change from removing a alone
    effect_b: float = 0.0       # R change from removing b alone
    effect_joint: float = 0.0   # R change from removing both
    interaction: float = 0.0    # effect_joint − (effect_a + effect_b)
    classification: str = NEUTRAL

    def to_dict(self) -> dict:
        return {
            "module_a": self.module_a,
            "module_b": self.module_b,
            "effect_a": round(self.effect_a, 4),
            "effect_b": round(self.effect_b, 4),
            "effect_joint": round(self.effect_joint, 4),
            "interaction": round(self.interaction, 4),
            "classification": self.classification,
        }


# ── Pure subset replay ─────────────────────────────────────────────────────


def subset_retains_trade(
    attribution: TradeAttribution, active_modules: set[str],
) -> bool:
    """True if the trade still opens in the same direction using only the votes
    cast by ``active_modules`` (the rest of the panel removed).  Never raises."""
    if attribution.direction not in ("LONG", "SHORT"):
        return False
    remaining = [
        v for v in attribution.votes if v.get("module") in active_modules
    ]
    votes = _votes_from_dicts(remaining)
    try:
        cf_dir, _ = replay_consensus(
            votes, attribution.thresholds, attribution.ranker_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[module-interaction] subset replay failed: {}", exc)
        return True
    return cf_dir == attribution.direction


def subset_total_r(
    trades: list[TradeAttribution], active_modules: set[str],
) -> float:
    """Total realised R of trades that still open under ``active_modules``."""
    total = 0.0
    for t in trades:
        if t.pnl_r is None or t.direction not in ("LONG", "SHORT"):
            continue
        if subset_retains_trade(t, active_modules):
            total += float(t.pnl_r)
    return total


class ModuleInteractionEngine:
    """Computes pairwise module interactions + the optimal active subset.

    Pure analysis over closed-trade snapshots read from an injected
    ``CounterfactualEngine`` (it owns the per-trade vote panels).  Construction
    and every public method are exception-safe.
    """

    def __init__(
        self,
        counterfactual_engine,
        *,
        enabled: bool = False,
        db_path: Optional[Path | str] = None,
        lookback: int = 500,
        interval: int = 100,
        min_trades: int = 50,
        significance_r: float = 1.0,
        max_modules: int = 12,
    ) -> None:
        self.enabled = bool(enabled)
        self._engine = counterfactual_engine
        self._lookback = max(1, int(lookback))
        self._interval = max(1, int(interval))
        self._min_trades = max(1, int(min_trades))
        self._significance_r = float(significance_r)
        self._max_modules = max(2, int(max_modules))
        self._last_computed_trades = 0
        self._lock = threading.RLock()
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[module-interaction] could not create db dir: {}", exc)
        self._connect()

    @property
    def lookback(self) -> int:
        return self._lookback

    @property
    def interval(self) -> int:
        return self._interval

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
            logger.warning("[module-interaction] DB init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[module-interaction] conn close failed")
                self._conn = None

    # ── Core computation ──────────────────────────────────────────────────

    def _trades(self) -> list[TradeAttribution]:
        if self._engine is None:
            return []
        try:
            trades = self._engine.get_closed_attributions(self._lookback)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[module-interaction] read attributions failed: {}", exc)
            return []
        return [t for t in trades if t.pnl_r is not None and t.direction in ("LONG", "SHORT")]

    @staticmethod
    def _modules_in(trades: list[TradeAttribution]) -> list[str]:
        seen: dict[str, int] = {}
        for t in trades:
            for v in t.votes:
                m = v.get("module")
                if m and v.get("direction") in ("LONG", "SHORT"):
                    seen[str(m)] = seen.get(str(m), 0) + 1
        # Most-active modules first (so the max_modules cap keeps the busiest).
        return [m for m, _ in sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]

    def compute(self, trades: Optional[list[TradeAttribution]] = None) -> dict:
        """Full pairwise-interaction table + greedy optimal subset (uncached)."""
        rows = trades if trades is not None else self._trades()
        if not rows:
            return self._empty_payload(0)
        all_modules = self._modules_in(rows)
        modules = all_modules[: self._max_modules]
        modset = set(modules)

        with _quiet_replay():
            baseline = subset_total_r(rows, modset)
            # Single-module removal effects (cache for the pair math).
            effect: dict[str, float] = {}
            for m in modules:
                effect[m] = subset_total_r(rows, modset - {m}) - baseline

            pairs: list[PairInteraction] = []
            for a, b in combinations(modules, 2):
                joint = subset_total_r(rows, modset - {a, b}) - baseline
                inter = joint - (effect[a] + effect[b])
                pairs.append(PairInteraction(
                    module_a=a, module_b=b,
                    effect_a=effect[a], effect_b=effect[b],
                    effect_joint=joint, interaction=inter,
                    classification=self._classify(joint, effect[a], effect[b], inter),
                ))
            optimal = self._greedy_subset(rows, modset, baseline)

        pairs.sort(key=lambda p: abs(p.interaction), reverse=True)
        payload = {
            "computed_at": time.time(),
            "lookback": self._lookback,
            "trades_analyzed": len(rows),
            "baseline_total_r": round(baseline, 4),
            "module_effects": {m: round(effect[m], 4) for m in modules},
            "pairs": [p.to_dict() for p in pairs],
            "optimal_subset": optimal,
        }
        return payload

    def _classify(
        self, joint: float, ea: float, eb: float, interaction: float,
    ) -> str:
        """Classify a pair from its joint removal effect + interaction term.

        ``joint`` (= effect of removing both) is the deciding signal:
          * ``joint > 0`` — removing the pair *raises* retained R → the pair is
            genuinely **TOXIC** (together they keep opening net-losers).
          * ``joint < 0`` — the pair is net valuable; the interaction sign then
            says whether the value is **SYNERGISTIC** (they need each other:
            removing both loses MORE than the parts, interaction < 0) or merely
            **REDUNDANT** overlap (removing both loses LESS than the parts,
            interaction > 0).
        """
        if abs(interaction) < self._significance_r:
            return NEUTRAL
        if joint > self._significance_r:
            return TOXIC
        if interaction < 0 and joint < -self._significance_r:
            return SYNERGISTIC
        return REDUNDANT

    def _greedy_subset(
        self, trades: list[TradeAttribution], modules: set[str], baseline: float,
    ) -> dict:
        """Greedily remove the module whose exclusion most raises retained R."""
        active = set(modules)
        current = baseline
        removed: list[dict] = []
        # Bound the loop to the module count; each step is one full scan.
        for _ in range(len(modules)):
            best_mod = None
            best_total = current
            for m in list(active):
                if len(active) <= 1:
                    break
                total = subset_total_r(trades, active - {m})
                if total > best_total + 1e-9:
                    best_total = total
                    best_mod = m
            if best_mod is None:
                break
            active.discard(best_mod)
            removed.append({"module": best_mod, "total_r_after": round(best_total, 4)})
            current = best_total
        return {
            "kept_modules": sorted(active),
            "removed_modules": removed,
            "baseline_total_r": round(baseline, 4),
            "optimal_total_r": round(current, 4),
            "improvement_r": round(current - baseline, 4),
        }

    # ── Periodic compute + cache ──────────────────────────────────────────

    def compute_and_cache(self) -> dict:
        payload = self.compute()
        self._write_cache(payload)
        return payload

    def maybe_recompute(self, total_trades: int) -> Optional[dict]:
        """Recompute when enough new trades have closed (mirrors L4 cadence)."""
        tt = int(total_trades or 0)
        if tt < self._min_trades:
            return None
        if self._last_computed_trades and (tt - self._last_computed_trades) < self._interval:
            return None
        self._last_computed_trades = tt
        return self.compute_and_cache()

    def _empty_payload(self, trades: int) -> dict:
        return {
            "computed_at": None,
            "lookback": self._lookback,
            "trades_analyzed": trades,
            "baseline_total_r": 0.0,
            "module_effects": {},
            "pairs": [],
            "optimal_subset": {},
        }

    def _write_cache(self, payload: dict) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO interaction_cache
                       (computed_at, lookback, trades_analyzed, payload)
                       VALUES (?,?,?,?)""",
                    (
                        float(payload.get("computed_at") or time.time()),
                        int(payload.get("lookback", self._lookback)),
                        int(payload.get("trades_analyzed", 0)),
                        json.dumps(payload, default=str),
                    ),
                )
                self._conn.execute(
                    """DELETE FROM interaction_cache WHERE id NOT IN
                       (SELECT id FROM interaction_cache ORDER BY id DESC LIMIT 50)"""
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[module-interaction] cache write failed: {}", exc)

    def get_cached(self) -> dict:
        empty = self._empty_payload(0)
        if self._conn is None:
            return empty
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT payload FROM interaction_cache ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[module-interaction] cache read failed: {}", exc)
                return empty
        if not row:
            return empty
        try:
            val = json.loads(row[0])
            return val if isinstance(val, dict) else empty
        except (TypeError, ValueError):
            return empty

    def get_state(self) -> dict:
        cached = self.get_cached()
        return {
            "enabled": bool(self.enabled),
            "lookback": self._lookback,
            "interval": self._interval,
            "min_trades": self._min_trades,
            "significance_r": self._significance_r,
            "max_modules": self._max_modules,
            **cached,
        }


__all__ = [
    "TOXIC",
    "SYNERGISTIC",
    "REDUNDANT",
    "NEUTRAL",
    "PairInteraction",
    "ModuleInteractionEngine",
    "subset_retains_trade",
    "subset_total_r",
]
