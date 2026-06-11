"""
APEX TRADER — Trade Journal
Every decision is recorded: taken trades and rejected setups.
The sniper improves because every shot is audited.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import numpy as np
from loguru import logger


@dataclass
class TradeRecord:
    pair: str
    direction: str
    entry: float
    exit: float
    pnl: float  # pips
    score: int
    confluences: list[Any]
    regime: str
    session: str
    spread: float
    slippage: float
    entry_type: str
    time_to_tp1: Optional[float]
    time_to_exit: Optional[float]
    outcome: str
    pnl_dollars: float = 0.0
    swap_modeled: Optional[float] = None
    swap_status: str = "unavailable"
    risk_dollars: Optional[float] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DecisionRecord:
    pair: str
    direction: str
    score: int
    reason_rejected: Optional[str]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TradeJournal:
    """
    SQLite-backed trade and decision logger.
    Designed for both live execution and backtest replay sessions.
    """

    def __init__(self, db_path: str = "data/trade_journal.db"):
        self.db_path = Path(db_path)
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pair TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        entry REAL NOT NULL,
                        exit REAL NOT NULL,
                        pnl REAL NOT NULL,
                        score INTEGER NOT NULL,
                        confluences TEXT NOT NULL,
                        regime TEXT NOT NULL,
                        session TEXT NOT NULL,
                        spread REAL NOT NULL,
                        slippage REAL NOT NULL,
                        entry_type TEXT NOT NULL,
                        time_to_tp1 REAL,
                        time_to_exit REAL,
                        outcome TEXT NOT NULL,
                        timestamp TEXT NOT NULL
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pair TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        score INTEGER NOT NULL,
                        reason_rejected TEXT,
                        timestamp TEXT NOT NULL
                    )
                    """
                )
                await db.commit()
                try:
                    await db.execute("ALTER TABLE trades ADD COLUMN pnl_dollars REAL DEFAULT 0.0")
                    await db.commit()
                except Exception as exc:
                    logger.debug("[trade_journal] pnl_dollars column migration skipped (likely already exists): {}", exc)
                    pass
                try:
                    await db.execute("ALTER TABLE trades ADD COLUMN swap_modeled REAL")
                    await db.commit()
                except Exception as exc:
                    logger.debug("[trade_journal] swap_modeled column migration skipped (likely already exists): {}", exc)
                    pass
                try:
                    await db.execute("ALTER TABLE trades ADD COLUMN swap_status TEXT DEFAULT 'unavailable'")
                    await db.commit()
                except Exception as exc:
                    logger.debug("[trade_journal] swap_status column migration skipped (likely already exists): {}", exc)
                    pass
                try:
                    await db.execute("ALTER TABLE trades ADD COLUMN risk_dollars REAL")
                    await db.commit()
                except Exception as exc:
                    logger.debug("[trade_journal] risk_dollars column migration skipped (likely already exists): {}", exc)
                    pass
            self._initialized = True

    async def log_trade(self, trade: TradeRecord) -> None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO trades (
                    pair, direction, entry, exit, pnl, score, confluences, regime,
                    session, spread, slippage, entry_type, time_to_tp1, time_to_exit,
                    outcome, pnl_dollars, swap_modeled, swap_status, risk_dollars,
                    timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.pair,
                    trade.direction,
                    trade.entry,
                    trade.exit,
                    trade.pnl,
                    trade.score,
                    json.dumps(trade.confluences),
                    trade.regime,
                    trade.session,
                    trade.spread,
                    trade.slippage,
                    trade.entry_type,
                    trade.time_to_tp1,
                    trade.time_to_exit,
                    trade.outcome,
                    trade.pnl_dollars,
                    trade.swap_modeled,
                    trade.swap_status,
                    trade.risk_dollars,
                    trade.timestamp.isoformat(),
                ),
            )
            await db.commit()

    async def log_decision(self, decision: DecisionRecord) -> None:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO decisions (pair, direction, score, reason_rejected, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision.pair,
                    decision.direction,
                    decision.score,
                    decision.reason_rejected,
                    decision.timestamp.isoformat(),
                ),
            )
            await db.commit()

    async def get_recent_decisions(self, limit: int = 50) -> list[dict]:
        """Return the most recent rejected decisions for dashboard display."""
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT pair, direction, score, reason_rejected, timestamp
                FROM decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "pair": row["pair"],
                "direction": row["direction"],
                "score": row["score"],
                "reason": row["reason_rejected"] or "",
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]

    async def get_performance_stats(self) -> dict[str, Any]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT pair, session, pnl, time_to_exit, outcome, pnl_dollars,
                       swap_modeled, swap_status, direction, risk_dollars
                FROM trades
                WHERE outcome != 'LEGACY'
                ORDER BY timestamp ASC
                """
            )
            raw_rows = await cursor.fetchall()

        rows = self._consolidate_partial_rows(raw_rows)

        if not rows:
            return {
                "win_rate": 0.0,
                "avg_rr": 0.0,  # mean dollar P&L, not R — kept for backward-compat
                "profit_factor": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0,
                "best_pair": None,
                "best_session": None,
                "avg_hold_time": 0.0,
                "mean_r": 0.0,
                "expectancy_r": 0.0,
                "r_sample_size": 0,
            }

        pnl_net = [self._swap_net_pnl(row) for row in rows]
        wins = [p for p in pnl_net if p > 0]
        losses = [p for p in pnl_net if p < 0]
        win_rate = (len(wins) / len(pnl_net)) * 100
        avg_rr = float(np.mean(pnl_net))  # mean dollar P&L — NOT reward/risk
        profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf")
        sharpe_ratio = self._sharpe_ratio(pnl_net)
        max_drawdown = self._max_drawdown(pnl_net)
        avg_hold_time = float(np.mean([r[3] for r in rows if r[3] is not None])) if rows else 0.0

        best_pair = self._best_dimension(rows, pnl_net, dimension="pair")
        best_session = self._best_dimension(rows, pnl_net, dimension="session")

        _RISK_D = 9
        r_values = []
        for row, net_pnl in zip(rows, pnl_net):
            risk_d = row[_RISK_D] if len(row) > _RISK_D else None
            if risk_d is not None and risk_d > 0:
                r_values.append(net_pnl / risk_d)

        r_sample_size = len(r_values)
        if r_values:
            mean_r = float(np.mean(r_values))
            r_wins = [rv for rv in r_values if rv > 0]
            r_losses = [rv for rv in r_values if rv <= 0]
            win_pct = len(r_wins) / len(r_values)
            loss_pct = 1.0 - win_pct
            avg_win_r = float(np.mean(r_wins)) if r_wins else 0.0
            avg_loss_r = float(np.mean(r_losses)) if r_losses else 0.0
            expectancy_r = win_pct * avg_win_r + loss_pct * avg_loss_r
        else:
            mean_r = 0.0
            expectancy_r = 0.0

        return {
            "win_rate": round(win_rate, 2),
            "avg_rr": round(avg_rr, 4),  # mean dollar P&L — NOT reward/risk
            "profit_factor": round(profit_factor, 4) if np.isfinite(profit_factor) else float("inf"),
            "sharpe_ratio": round(sharpe_ratio, 4),
            "max_drawdown": round(max_drawdown, 4),
            "best_pair": best_pair,
            "best_session": best_session,
            "avg_hold_time": round(avg_hold_time, 2),
            "mean_r": round(mean_r, 4),
            "expectancy_r": round(expectancy_r, 4),
            "r_sample_size": r_sample_size,
        }

    async def get_all_trades_as_dicts(self) -> list[dict]:
        """Return all trades as plain dicts for ML consumption."""
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cursor = await db.execute(
                    "SELECT pair, direction, pnl, score, confluences, regime, "
                    "session, spread, entry_type, time_to_exit, outcome, pnl_dollars, "
                    "timestamp, swap_modeled, swap_status, risk_dollars, id, entry, exit FROM trades"
                )
            except Exception:
                cursor = await db.execute(
                    "SELECT pair, direction, pnl, score, confluences, regime, "
                    "session, spread, entry_type, time_to_exit, outcome, timestamp, "
                    "NULL as swap_modeled, 'unavailable' as swap_status, NULL as risk_dollars, "
                    "rowid as id, NULL as entry, NULL as exit FROM trades"
                )
            rows = await cursor.fetchall()
        result = []
        for r in rows:
            result.append({
                "pair": r[0],
                "direction": r[1],
                "pnl": r[2],
                "score": r[3],
                "confluences_raw": json.loads(r[4]) if r[4] else [],
                "regime": r[5],
                "session": r[6],
                "spread": r[7],
                "entry_type": r[8],
                "time_to_exit": r[9],
                "outcome": r[10],
                "pnl_dollars": r[11] if len(r) > 11 and r[11] is not None else 0.0,
                "timestamp": r[12] if len(r) > 12 else None,
                "swap_modeled": r[13] if len(r) > 13 else None,
                "swap_status": r[14] if len(r) > 14 else "unavailable",
                "risk_dollars": r[15] if len(r) > 15 else None,
                "id": r[16] if len(r) > 16 else None,
                "entry": r[17] if len(r) > 17 else None,
                "exit": r[18] if len(r) > 18 else None,
            })
        return self._consolidate_partial_dicts(result)

    async def get_win_rate(self) -> float:
        stats = await self.get_performance_stats()
        return float(stats["win_rate"])

    async def get_best_session(self) -> Optional[str]:
        stats = await self.get_performance_stats()
        return stats["best_session"]

    async def get_best_pair(self) -> Optional[str]:
        stats = await self.get_performance_stats()
        return stats["best_pair"]

    # ── Partial-trade consolidation ──────────────────────────────────────

    _PARTIAL_OUTCOME = "TP1_FULL_CLOSE_REOPEN"

    @staticmethod
    def _consolidate_partial_rows(rows: list[tuple]) -> list[tuple]:
        """Merge TP1_FULL_CLOSE_REOPEN rows with their continuation trade.

        Column layout assumed (get_performance_stats SELECT):
          0=pair, 1=session, 2=pnl, 3=time_to_exit, 4=outcome,
          5=pnl_dollars, 6=swap_modeled, 7=swap_status, 8=direction,
          9=risk_dollars

        Rows are already sorted by timestamp ASC.  For each partial-close
        record, the next row with the same pair+direction is the continuation.
        When partial-close records chain (a reopened half-stake position
        itself hits TP1 and reopens again), the entire chain is collapsed
        into a single surviving row: pnl and pnl_dollars are summed across
        all legs, and every marker row is dropped.  The surviving row is
        the final non-marker continuation, or the last marker if no
        non-marker continuation exists (orphaned chain — kept because it
        represents real realized P&L).

        risk_dollars on the surviving row is set to the FIRST (earliest)
        leg's risk_dollars, because R-multiple is measured against the
        capital originally risked at the initial entry.
        """
        _PAIR = 0
        _PNL = 2
        _OUTCOME = 4
        _PNL_D = 5
        _SWAP = 6
        _SWAP_ST = 7
        _DIR = 8
        _RISK_D = 9
        MARKER = TradeJournal._PARTIAL_OUTCOME

        if not rows:
            return rows

        skip: set[int] = set()
        merge_into: dict[int, int] = {}

        for i, row in enumerate(rows):
            if row[_OUTCOME] != MARKER:
                continue
            pair = row[_PAIR]
            direction = row[_DIR] if len(row) > _DIR else None
            for j in range(i + 1, len(rows)):
                if j in skip:
                    continue
                cand = rows[j]
                if cand[_PAIR] != pair:
                    continue
                cand_dir = cand[_DIR] if len(cand) > _DIR else None
                if direction is not None and cand_dir is not None and direction != cand_dir:
                    continue
                merge_into[i] = j
                skip.add(i)
                break

        if not skip:
            return rows

        def _resolve(idx: int) -> int:
            while idx in merge_into:
                idx = merge_into[idx]
            return idx

        extra: dict[int, tuple[float, float, float, bool, object]] = {}
        for pi in sorted(skip):
            target = _resolve(pi)
            p = rows[pi]
            pnl_add = float(p[_PNL])
            pnl_d_add = float(p[_PNL_D]) if p[_PNL_D] is not None else float(p[_PNL])
            leg_modeled = len(p) > _SWAP_ST and p[_SWAP_ST] == "modeled"
            swap_add = float(p[_SWAP]) if leg_modeled and p[_SWAP] is not None else 0.0
            leg_risk = p[_RISK_D] if len(p) > _RISK_D else None
            prev = extra.get(target, (0.0, 0.0, 0.0, False, None))
            first_risk = prev[4] if prev[4] is not None else leg_risk
            extra[target] = (prev[0] + pnl_add, prev[1] + pnl_d_add,
                             prev[2] + swap_add, prev[3] or leg_modeled, first_risk)

        result: list[tuple] = []
        for i, row in enumerate(rows):
            if i in skip:
                continue
            if i in extra:
                row = list(row)
                row[_PNL] = float(row[_PNL]) + extra[i][0]
                pnl_d = float(row[_PNL_D]) if row[_PNL_D] is not None else float(row[_PNL] - extra[i][0])
                row[_PNL_D] = pnl_d + extra[i][1]
                extra_swap, extra_any_modeled = extra[i][2], extra[i][3]
                surv_modeled = len(row) > _SWAP_ST and row[_SWAP_ST] == "modeled"
                surv_swap = float(row[_SWAP]) if surv_modeled and row[_SWAP] is not None else 0.0
                if extra_any_modeled or surv_modeled:
                    row[_SWAP] = surv_swap + extra_swap
                    row[_SWAP_ST] = "modeled"
                first_risk = extra[i][4]
                if first_risk is not None and len(row) > _RISK_D:
                    row[_RISK_D] = first_risk
                row = tuple(row)
            result.append(row)
        return result

    @staticmethod
    def _consolidate_partial_dicts(trades: list[dict]) -> list[dict]:
        """Merge TP1_FULL_CLOSE_REOPEN dicts with their continuation trade.

        Same algorithm as _consolidate_partial_rows but operates on the dict
        representation returned by get_all_trades_as_dicts.  Trades are assumed
        to be in insertion (chronological) order.  Handles chained reopens
        (3+ legs) by resolving merge targets to their ultimate destination,
        ensuring no P&L is lost regardless of chain length.

        risk_dollars on the surviving dict is set to the FIRST (earliest)
        leg's risk_dollars for correct R-multiple calculation.
        """
        MARKER = TradeJournal._PARTIAL_OUTCOME
        if not trades:
            return trades

        skip: set[int] = set()
        merge_into: dict[int, int] = {}

        for i, t in enumerate(trades):
            if t.get("outcome") != MARKER:
                continue
            pair = t.get("pair")
            direction = t.get("direction")
            for j in range(i + 1, len(trades)):
                if j in skip:
                    continue
                cand = trades[j]
                if cand.get("pair") != pair:
                    continue
                if direction and cand.get("direction") and direction != cand.get("direction"):
                    continue
                merge_into[i] = j
                skip.add(i)
                break

        if not skip:
            return trades

        def _resolve(idx: int) -> int:
            while idx in merge_into:
                idx = merge_into[idx]
            return idx

        extra: dict[int, tuple[float, float, float, bool, object]] = {}
        for pi in sorted(skip):
            target = _resolve(pi)
            p = trades[pi]
            pnl_add = float(p.get("pnl", 0))
            pnl_d_add = float(p.get("pnl_dollars", 0))
            leg_modeled = p.get("swap_status") == "modeled"
            swap_add = float(p.get("swap_modeled") or 0) if leg_modeled else 0.0
            leg_risk = p.get("risk_dollars")
            prev = extra.get(target, (0.0, 0.0, 0.0, False, None))
            first_risk = prev[4] if prev[4] is not None else leg_risk
            extra[target] = (prev[0] + pnl_add, prev[1] + pnl_d_add,
                             prev[2] + swap_add, prev[3] or leg_modeled, first_risk)

        result: list[dict] = []
        for i, t in enumerate(trades):
            if i in skip:
                continue
            if i in extra:
                t = dict(t)
                t["pnl"] = float(t.get("pnl", 0)) + extra[i][0]
                t["pnl_dollars"] = float(t.get("pnl_dollars", 0)) + extra[i][1]
                extra_swap, extra_any_modeled = extra[i][2], extra[i][3]
                surv_modeled = t.get("swap_status") == "modeled"
                surv_swap = float(t.get("swap_modeled") or 0) if surv_modeled else 0.0
                if extra_any_modeled or surv_modeled:
                    t["swap_modeled"] = surv_swap + extra_swap
                    t["swap_status"] = "modeled"
                first_risk = extra[i][4]
                if first_risk is not None:
                    t["risk_dollars"] = first_risk
            result.append(t)
        return result

    @staticmethod
    def _swap_net_pnl(row: tuple) -> float:
        """Compute swap-netted P&L for a single consolidated row.

        Sign convention (from swap_model.py): positive = credit (money
        received), negative = cost (money paid).  Netting is additive:
        ``net = pnl_dollars + swap_modeled``.  Rows with swap_status !=
        "modeled" are returned at their raw dollar value — unknown swap
        is never treated as zero.
        """
        raw = float(row[5]) if row[5] is not None else float(row[2])
        if len(row) > 7 and row[7] == "modeled" and row[6] is not None:
            return raw + float(row[6])
        return raw

    def _sharpe_ratio(self, returns: list[float]) -> float:
        if len(returns) < 2:
            return 0.0
        std = float(np.std(returns, ddof=1))
        if std == 0:
            return 0.0
        return float(np.mean(returns) / std * np.sqrt(len(returns)))

    def _max_drawdown(self, returns: list[float]) -> float:
        equity = np.cumsum(returns)
        peaks = np.maximum.accumulate(equity)
        drawdowns = peaks - equity
        return float(np.max(drawdowns)) if len(drawdowns) else 0.0

    def _best_dimension(self, rows: list[tuple], pnl_net: list[float], dimension: str) -> Optional[str]:
        idx = 0 if dimension == "pair" else 1
        grouped: dict[str, list[float]] = {}
        for row, pnl in zip(rows, pnl_net):
            grouped.setdefault(row[idx], []).append(pnl)
        if not grouped:
            return None
        ranked = sorted(grouped.items(), key=lambda item: np.mean(item[1]), reverse=True)
        return ranked[0][0]
