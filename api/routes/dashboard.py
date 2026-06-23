"""APEX TRADER — Dashboard data routes.

Read-only endpoints that serve everything a frontend needs, scoped to the
authenticated user:

* summary / stats / equity-curve / history — served from the API's
  ``trade_history`` table (populated by the per-user instance reporting hook).
* open positions — read live from the user's isolated ``apex_positions.db``
  (the engine's :class:`persistence.position_store.PositionStore` schema),
  opened read-only so the API never interferes with the trading process.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from loguru import logger

from api.auth import get_config, get_current_user, get_db, get_process_manager
from api.config import ApiConfig
from api.database import Database
from api.models import (
    DashboardSummary,
    EquityPoint,
    PositionResponse,
    TradeResponse,
)
from api.process_manager import ProcessManager

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def summary(
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    pm: ProcessManager = Depends(get_process_manager),
) -> DashboardSummary:
    stats = db.trade_stats(int(user["id"]))
    inst = pm.instance_status(int(user["id"]))
    return DashboardSummary(
        total_trades=int(stats.get("total", 0) or 0),
        total_pnl=round(float(stats.get("total_pnl", 0.0) or 0.0), 2),
        win_rate=float(stats.get("win_rate", 0.0) or 0.0),
        wins=int(stats.get("wins", 0) or 0),
        losses=int(stats.get("losses", 0) or 0),
        best_trade=round(float(stats.get("best", 0.0) or 0.0), 2),
        worst_trade=round(float(stats.get("worst", 0.0) or 0.0), 2),
        instance_status=inst.get("status", "STOPPED"),
        instance_alive=bool(inst.get("alive", False)),
    )


@router.get("/history", response_model=list[TradeResponse])
async def history(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> list[TradeResponse]:
    rows = db.list_trades(
        int(user["id"]),
        limit=limit,
        offset=offset,
        symbol=symbol,
        direction=direction,
        since=since,
        until=until,
    )
    return [
        TradeResponse(
            id=int(r["id"]),
            ticket=r["ticket"],
            symbol=r["symbol"],
            direction=r["direction"],
            entry_price=r["entry_price"],
            exit_price=r["exit_price"],
            pnl=r["pnl"],
            pnl_pips=r["pnl_pips"],
            exit_reason=r["exit_reason"],
            opened_at=r["opened_at"],
            closed_at=r["closed_at"],
        )
        for r in rows
    ]


@router.get("/equity-curve", response_model=list[EquityPoint])
async def equity_curve(
    limit: int = Query(default=500, ge=1, le=5000),
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> list[EquityPoint]:
    return [EquityPoint(**p) for p in db.equity_curve(int(user["id"]), limit=limit)]


@router.get("/stats")
async def detailed_stats(
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    base = db.trade_stats(int(user["id"]))
    # Per-symbol win-rate breakdown from the full (capped) trade list.
    trades = db.list_trades(int(user["id"]), limit=1000)
    by_symbol: dict[str, dict[str, Any]] = {}
    for t in trades:
        sym = t["symbol"]
        bucket = by_symbol.setdefault(sym, {"trades": 0, "wins": 0, "pnl": 0.0})
        bucket["trades"] += 1
        bucket["pnl"] = round(bucket["pnl"] + float(t["pnl"] or 0.0), 2)
        if float(t["pnl"] or 0.0) > 0:
            bucket["wins"] += 1
    for bucket in by_symbol.values():
        bucket["win_rate"] = (
            round(bucket["wins"] / bucket["trades"], 4) if bucket["trades"] else 0.0
        )
    return {
        "total_trades": int(base.get("total", 0) or 0),
        "total_pnl": round(float(base.get("total_pnl", 0.0) or 0.0), 2),
        "win_rate": float(base.get("win_rate", 0.0) or 0.0),
        "best_trade": round(float(base.get("best", 0.0) or 0.0), 2),
        "worst_trade": round(float(base.get("worst", 0.0) or 0.0), 2),
        "by_symbol": by_symbol,
    }


_POSITION_COLUMNS = (
    "order_id, symbol, direction, lots, entry_price, sl, tp1, tp2, open_time"
)


@router.get("/positions", response_model=list[PositionResponse])
async def open_positions(
    user: dict[str, Any] = Depends(get_current_user),
    config: ApiConfig = Depends(get_config),
) -> list[PositionResponse]:
    """Read the user's currently-managed open positions (read-only)."""
    db_path = config.user_workdir(int(user["id"])) / "data" / "apex_positions.db"
    if not db_path.exists():
        return []
    positions: list[PositionResponse] = []
    try:
        # Open read-only via URI so we never write/lock the engine's DB.
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5
        )
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT {_POSITION_COLUMNS} FROM managed_positions"
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            positions.append(
                PositionResponse(
                    order_id=str(r["order_id"]),
                    symbol=r["symbol"],
                    direction=r["direction"],
                    lots=float(r["lots"] or 0.0),
                    entry_price=float(r["entry_price"] or 0.0),
                    sl=float(r["sl"] or 0.0),
                    tp1=float(r["tp1"] or 0.0),
                    tp2=float(r["tp2"] or 0.0),
                    open_time=str(r["open_time"] or ""),
                )
            )
    except sqlite3.Error as exc:
        logger.debug("[dashboard] positions read failed for {}: {}", user["id"], exc)
        return []
    return positions
