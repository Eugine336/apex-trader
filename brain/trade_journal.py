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
                except Exception:
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
                    outcome, pnl_dollars, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                SELECT pair, session, pnl, time_to_exit, outcome, pnl_dollars
                FROM trades
                ORDER BY timestamp ASC
                """
            )
            rows = await cursor.fetchall()

        if not rows:
            return {
                "win_rate": 0.0,
                "avg_rr": 0.0,
                "profit_factor": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0,
                "best_pair": None,
                "best_session": None,
                "avg_hold_time": 0.0,
            }

        pnl_dollars = [float(row[5]) if row[5] is not None else float(row[2]) for row in rows]
        wins = [p for p in pnl_dollars if p > 0]
        losses = [p for p in pnl_dollars if p < 0]
        win_rate = (len(wins) / len(pnl_dollars)) * 100
        avg_rr = float(np.mean(pnl_dollars))
        profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf")
        sharpe_ratio = self._sharpe_ratio(pnl_dollars)
        max_drawdown = self._max_drawdown(pnl_dollars)
        avg_hold_time = float(np.mean([r[3] for r in rows if r[3] is not None])) if rows else 0.0

        best_pair = self._best_dimension(rows, dimension="pair")
        best_session = self._best_dimension(rows, dimension="session")

        return {
            "win_rate": round(win_rate, 2),
            "avg_rr": round(avg_rr, 4),
            "profit_factor": round(profit_factor, 4) if np.isfinite(profit_factor) else float("inf"),
            "sharpe_ratio": round(sharpe_ratio, 4),
            "max_drawdown": round(max_drawdown, 4),
            "best_pair": best_pair,
            "best_session": best_session,
            "avg_hold_time": round(avg_hold_time, 2),
        }

    async def get_all_trades_as_dicts(self) -> list[dict]:
        """Return all trades as plain dicts for ML consumption."""
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cursor = await db.execute(
                    "SELECT pair, direction, pnl, score, confluences, regime, "
                    "session, spread, entry_type, time_to_exit, outcome, pnl_dollars FROM trades"
                )
            except Exception:
                cursor = await db.execute(
                    "SELECT pair, direction, pnl, score, confluences, regime, "
                    "session, spread, entry_type, time_to_exit, outcome FROM trades"
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
            })
        return result

    async def get_win_rate(self) -> float:
        stats = await self.get_performance_stats()
        return float(stats["win_rate"])

    async def get_best_session(self) -> Optional[str]:
        stats = await self.get_performance_stats()
        return stats["best_session"]

    async def get_best_pair(self) -> Optional[str]:
        stats = await self.get_performance_stats()
        return stats["best_pair"]

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

    def _best_dimension(self, rows: list[tuple], dimension: str) -> Optional[str]:
        idx = 0 if dimension == "pair" else 1
        grouped: dict[str, list[float]] = {}
        for row in rows:
            grouped.setdefault(row[idx], []).append(float(row[2]))
        if not grouped:
            return None
        ranked = sorted(grouped.items(), key=lambda item: np.mean(item[1]), reverse=True)
        return ranked[0][0]
