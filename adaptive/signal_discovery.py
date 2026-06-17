"""
APEX TRADER — Synthetic Signal Discovery (L5c)

The 9 modules produce rich reads, but they are combined by *fixed* consensus
logic.  This engine asks the generative question L1–L4 never do: are there
*combinations* of module conditions — rules nobody wrote — whose edge shows up
in the data?  e.g. "structure LONG AND liquidity LONG AND momentum ABSENT →
+0.6R/trade above baseline".

It mines the recorded vote panels + realised outcomes:

  * **conditions** — each observed (module, direction) where direction is LONG /
    SHORT / ABSENT (the module cast no directional vote).
  * **rules** — apriori-style level-wise combinations of conditions (size 1 …
    ``max_rule_conditions``), pruned by a minimum-support floor so the search
    stays tractable; two conditions on the same module are mutually exclusive
    and never combined.
  * **edge** — mean realised R of the trades a rule fires on, minus the baseline
    mean over all trades.

Overfitting is the danger of any pattern miner, so a rule is only *recommended*
when its edge persists in BOTH halves of a time-ordered train/test split and it
clears a minimum-support floor.  Discovery is purely advisory — it never creates
a live signal; it surfaces candidate rules with out-of-sample-validated edge for
a human (or a later layer) to adopt.

Leaf-ish module — stdlib + loguru only (it reads the vote panels the
counterfactual store already captured).  Exception-safe throughout.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Optional

from loguru import logger

from adaptive.counterfactual import TradeAttribution

_DB_DIR = Path(__file__).parent.parent / "data"
_DB_PATH = _DB_DIR / "signal_discovery.db"

ABSENT = "ABSENT"

_CREATE_CACHE = """
CREATE TABLE IF NOT EXISTS discovery_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at     REAL NOT NULL,
    lookback        INTEGER NOT NULL,
    trades_analyzed INTEGER NOT NULL,
    payload         TEXT NOT NULL
)
"""

# A condition is (module, direction) with direction in LONG / SHORT / ABSENT.
Condition = tuple[str, str]


@dataclass
class DiscoveredRule:
    """A combination of module conditions with its in/out-of-sample edge."""

    conditions: list[Condition]
    support: int = 0                 # trades the rule fires on (full window)
    win_rate: float = 0.0
    expectancy: float = 0.0          # mean realised R when the rule fires
    edge: float = 0.0                # expectancy − baseline expectancy
    train_support: int = 0
    test_support: int = 0
    train_edge: float = 0.0
    test_edge: float = 0.0
    qualifies: bool = False

    @property
    def label(self) -> str:
        return " AND ".join(f"{m}:{d}" for m, d in self.conditions)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "conditions": [list(c) for c in self.conditions],
            "size": len(self.conditions),
            "support": self.support,
            "win_rate": round(self.win_rate, 4),
            "expectancy": round(self.expectancy, 4),
            "edge": round(self.edge, 4),
            "train_support": self.train_support,
            "test_support": self.test_support,
            "train_edge": round(self.train_edge, 4),
            "test_edge": round(self.test_edge, 4),
            "qualifies": bool(self.qualifies),
        }


# ── Pure mining helpers ────────────────────────────────────────────────────


def _trade_conditions(attr: TradeAttribution, modules: set[str]) -> set[Condition]:
    """The condition set a single trade's vote panel satisfies.

    Every module known to the window contributes exactly one condition per
    trade: its directional vote (LONG/SHORT) when it cast one, else ABSENT.
    """
    voted: dict[str, str] = {}
    for v in attr.votes:
        m = v.get("module")
        d = v.get("direction")
        if not m or d not in ("LONG", "SHORT"):
            continue
        try:
            if float(v.get("confidence", 0) or 0) <= 0 or float(v.get("weight", 0) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        voted[str(m)] = str(d)
    out: set[Condition] = set()
    for m in modules:
        out.add((m, voted.get(m, ABSENT)))
    return out


def _expectancy(rs: list[float]) -> float:
    return (sum(rs) / len(rs)) if rs else 0.0


def _win_rate(rs: list[float]) -> float:
    return (sum(1 for r in rs if r > 0) / len(rs)) if rs else 0.0


class SignalDiscoveryEngine:
    """Mines OOS-validated module-combination rules from closed-trade panels.

    Reads closed-trade snapshots from an injected ``CounterfactualEngine`` (it
    owns the vote panels + realised R).  Pure analysis + persistence; every
    public method is exception-safe.
    """

    def __init__(
        self,
        counterfactual_engine,
        *,
        enabled: bool = False,
        db_path: Optional[Path | str] = None,
        lookback: int = 1000,
        interval: int = 200,
        min_trades: int = 100,
        min_support: int = 15,
        max_conditions: int = 3,
        min_edge_r: float = 0.10,
        walk_forward_split: float = 0.7,
    ) -> None:
        self.enabled = bool(enabled)
        self._engine = counterfactual_engine
        self._lookback = max(1, int(lookback))
        self._interval = max(1, int(interval))
        self._min_trades = max(1, int(min_trades))
        self._min_support = max(1, int(min_support))
        self._max_conditions = max(1, min(5, int(max_conditions)))
        self._min_edge_r = float(min_edge_r)
        self._split = min(0.95, max(0.5, float(walk_forward_split)))
        self._last_computed_trades = 0
        self._lock = threading.RLock()
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[signal-discovery] could not create db dir: {}", exc)
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
            logger.warning("[signal-discovery] DB init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[signal-discovery] conn close failed")
                self._conn = None

    # ── Mining ────────────────────────────────────────────────────────────

    def _trades(self) -> list[TradeAttribution]:
        if self._engine is None:
            return []
        try:
            trades = self._engine.get_closed_attributions(self._lookback)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[signal-discovery] read attributions failed: {}", exc)
            return []
        return [
            t for t in trades
            if t.pnl_r is not None and t.direction in ("LONG", "SHORT")
        ]

    def mine(self, trades: Optional[list[TradeAttribution]] = None) -> dict:
        """Discover + OOS-validate candidate rules (uncached)."""
        rows = trades if trades is not None else self._trades()
        if len(rows) < 2:
            return self._empty_payload(len(rows))
        ordered = sorted(rows, key=lambda t: t.timestamp_open)

        modules = self._modules_in(ordered)
        if not modules:
            return self._empty_payload(len(ordered))

        # Precompute each trade's (condition set, R) once.
        rl = [
            (_trade_conditions(t, modules), float(t.pnl_r))
            for t in ordered
        ]
        baseline = _expectancy([r for _, r in rl])

        # Level-wise apriori search over conditions, pruned by support.
        rules = self._mine_rules(rl)

        # OOS validation on a time-ordered split.
        split = max(1, int(len(rl) * self._split))
        train, test = rl[:split], rl[split:]
        base_train = _expectancy([r for _, r in train])
        base_test = _expectancy([r for _, r in test])

        discovered: list[DiscoveredRule] = []
        for conds in rules:
            rule = self._evaluate_rule(conds, rl, baseline, train, test, base_train, base_test)
            if rule is not None:
                discovered.append(rule)

        discovered.sort(
            key=lambda r: (r.qualifies, min(r.train_edge, r.test_edge), r.support),
            reverse=True,
        )
        qualifying = [r for r in discovered if r.qualifies]
        payload = {
            "computed_at": time.time(),
            "lookback": self._lookback,
            "trades_analyzed": len(ordered),
            "baseline_expectancy": round(baseline, 4),
            "rule_count": len(discovered),
            "qualifying_count": len(qualifying),
            "rules": [r.to_dict() for r in discovered[:100]],
        }
        return payload

    @staticmethod
    def _modules_in(trades: list[TradeAttribution]) -> set[str]:
        out: set[str] = set()
        for t in trades:
            for v in t.votes:
                m = v.get("module")
                if m and v.get("direction") in ("LONG", "SHORT"):
                    out.add(str(m))
        return out

    def _mine_rules(
        self, rl: list[tuple[set[Condition], float]],
    ) -> list[list[Condition]]:
        """Apriori-style level-wise generation of condition combinations whose
        support clears ``min_support``.  Same-module conditions never combine."""
        # Level-1 frequent conditions.
        support1: dict[Condition, int] = {}
        for conds, _ in rl:
            for c in conds:
                support1[c] = support1.get(c, 0) + 1
        frequent = [c for c, n in support1.items() if n >= self._min_support]
        frequent.sort(key=lambda c: support1[c], reverse=True)

        all_rules: list[list[Condition]] = [[c] for c in frequent]
        current = [[c] for c in frequent]
        for _size in range(2, self._max_conditions + 1):
            nxt: list[list[Condition]] = []
            seen: set[frozenset] = set()
            for rule in current:
                for c in frequent:
                    mod = c[0]
                    if any(existing[0] == mod for existing in rule):
                        continue  # same module already constrained
                    cand = rule + [c]
                    key = frozenset(cand)
                    if key in seen:
                        continue
                    seen.add(key)
                    support = sum(1 for conds, _ in rl if key <= conds)
                    if support >= self._min_support:
                        nxt.append(cand)
            all_rules.extend(nxt)
            current = nxt
            if not current:
                break
        return all_rules

    def _evaluate_rule(
        self,
        conds: list[Condition],
        rl: list[tuple[set[Condition], float]],
        baseline: float,
        train: list[tuple[set[Condition], float]],
        test: list[tuple[set[Condition], float]],
        base_train: float,
        base_test: float,
    ) -> Optional[DiscoveredRule]:
        key = frozenset(conds)
        full_r = [r for c, r in rl if key <= c]
        if len(full_r) < self._min_support:
            return None
        train_r = [r for c, r in train if key <= c]
        test_r = [r for c, r in test if key <= c]
        rule = DiscoveredRule(
            conditions=list(conds),
            support=len(full_r),
            win_rate=_win_rate(full_r),
            expectancy=_expectancy(full_r),
            edge=_expectancy(full_r) - baseline,
            train_support=len(train_r),
            test_support=len(test_r),
            train_edge=(_expectancy(train_r) - base_train) if train_r else 0.0,
            test_edge=(_expectancy(test_r) - base_test) if test_r else 0.0,
        )
        # Qualify only when the edge persists out-of-sample with real support.
        rule.qualifies = (
            rule.train_support >= self._min_support
            and rule.test_support >= max(3, self._min_support // 3)
            and rule.train_edge >= self._min_edge_r
            and rule.test_edge >= self._min_edge_r
        )
        return rule

    # ── Periodic compute + cache ──────────────────────────────────────────

    def compute_and_cache(self) -> dict:
        payload = self.mine()
        self._write_cache(payload)
        return payload

    def maybe_recompute(self, total_trades: int) -> Optional[dict]:
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
            "baseline_expectancy": 0.0,
            "rule_count": 0,
            "qualifying_count": 0,
            "rules": [],
        }

    def _write_cache(self, payload: dict) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO discovery_cache
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
                    """DELETE FROM discovery_cache WHERE id NOT IN
                       (SELECT id FROM discovery_cache ORDER BY id DESC LIMIT 50)"""
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[signal-discovery] cache write failed: {}", exc)

    def get_cached(self) -> dict:
        empty = self._empty_payload(0)
        if self._conn is None:
            return empty
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT payload FROM discovery_cache ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[signal-discovery] cache read failed: {}", exc)
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
            "min_support": self._min_support,
            "max_conditions": self._max_conditions,
            "min_edge_r": self._min_edge_r,
            **cached,
        }


__all__ = [
    "ABSENT",
    "Condition",
    "DiscoveredRule",
    "SignalDiscoveryEngine",
]
