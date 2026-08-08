"""APEX TRADER — Real-time overview stream.

Provides the frontend with a single consolidated, live snapshot of the
authenticated user's trading instance so the institutional dashboard can render
in real time without fanning out a dozen polls per tick.

Two transports, same payload:

* ``GET /api/stream/snapshot`` — one JSON snapshot (JWT *bearer* auth). Used as
  the polling fallback when the browser's ``EventSource`` is unavailable or the
  stream drops.
* ``GET /api/stream/overview?token=<access>`` — a Server-Sent Events stream that
  pushes a fresh snapshot every ``interval`` seconds until the client
  disconnects. ``EventSource`` cannot send an ``Authorization`` header, so the
  access token is passed as a query parameter and validated here.

The snapshot is assembled from control-plane state (instance status + the API's
own trade-history tables) plus a *best-effort* read of the user's isolated
loopback engine dashboard (operations health + risk). Engine reads are wrapped
so a starting/stopped instance degrades gracefully to ``offline`` department
cards rather than erroring the whole stream.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from jose import JWTError
from loguru import logger

from api.auth import (
    TOKEN_ACCESS,
    decode_token,
    get_config,
    get_current_user,
    get_db,
    get_process_manager,
)
from api.config import ApiConfig
from api.database import Database
from api.process_manager import ProcessManager

router = APIRouter(prefix="/api/stream", tags=["stream"])

# Engine loopback reads must fail fast — a slow reply means the instance is busy
# or still binding, in which case we simply omit the live engine slice.
_ENGINE_TIMEOUT = httpx.Timeout(2.5, connect=1.0)

# SSE cadence + safety bounds (seconds).
_DEFAULT_INTERVAL = 2.0
_MIN_INTERVAL = 1.0
_MAX_INTERVAL = 10.0

_RUNNING_STATES = {"RUNNING", "STARTING"}


async def _engine_get(port: int, path: str) -> Optional[Any]:
    """Best-effort GET against the user's loopback engine dashboard."""
    try:
        async with httpx.AsyncClient(timeout=_ENGINE_TIMEOUT) as http:
            resp = await http.get(f"http://127.0.0.1:{port}{path}")
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


def _open_positions(config: ApiConfig, user_id: int) -> list[dict[str, Any]]:
    """Read the user's managed open positions read-only (never locks the engine)."""
    db_path = config.user_workdir(user_id) / "data" / "apex_positions.db"
    if not db_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT order_id, symbol, direction, lots, entry_price, sl, tp1, "
                "tp2, open_time FROM managed_positions"
            )
            for r in cursor.fetchall():
                rows.append(
                    {
                        "order_id": str(r["order_id"]),
                        "symbol": r["symbol"],
                        "direction": r["direction"],
                        "lots": float(r["lots"] or 0.0),
                        "entry_price": float(r["entry_price"] or 0.0),
                        "sl": float(r["sl"] or 0.0),
                        "tp1": float(r["tp1"] or 0.0),
                        "tp2": float(r["tp2"] or 0.0),
                        "open_time": str(r["open_time"] or ""),
                    }
                )
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return rows


def _derive_departments(
    *, running: bool, ops: Optional[dict], risk: Optional[dict]
) -> list[dict[str, str]]:
    """Map live engine signals onto the 9-department health grid.

    State is one of: ``active`` | ``degraded`` | ``error`` | ``offline``.
    """
    order = [
        ("intelligence", "Intelligence"),
        ("consensus", "Consensus"),
        ("compliance", "Compliance"),
        ("portfolio", "Portfolio"),
        ("execution", "Execution"),
        ("operations", "Operations"),
        ("learning", "Learning"),
        ("governance", "Governance"),
        ("command", "Command Center"),
    ]
    if not running:
        return [{"key": k, "name": n, "state": "offline", "detail": ""} for k, n in order]

    health = (ops or {}).get("health", {}) if isinstance(ops, dict) else {}
    watchdog = (ops or {}).get("watchdog", {}) if isinstance(ops, dict) else {}
    broker = health.get("broker", {}) if isinstance(health, dict) else {}
    broker_ok = bool(broker.get("any_connected", True))
    health_ok = str(health.get("status", "ok")).lower() == "ok"
    stalled = bool(watchdog.get("stalled", False))

    risk_mode = str((risk or {}).get("mode", "NORMAL") or "NORMAL").upper()
    halted = risk_mode in {"HALT", "HALTED", "LOCKED"}

    states: dict[str, tuple[str, str]] = {
        "intelligence": ("active", ""),
        "consensus": ("active", ""),
        "compliance": (
            "error" if halted else "active",
            f"mode {risk_mode}" if risk_mode != "NORMAL" else "",
        ),
        "portfolio": ("active", ""),
        "execution": (
            "active" if broker_ok else "error",
            "broker connected" if broker_ok else "broker offline",
        ),
        "operations": (
            "active" if (health_ok and not stalled) else "degraded",
            "loop stalled" if stalled else ("" if health_ok else "warnings"),
        ),
        "learning": ("active", ""),
        "governance": ("active", ""),
        "command": (
            "active" if (broker_ok and not stalled and not halted) else "degraded",
            "",
        ),
    }
    return [
        {"key": k, "name": n, "state": states[k][0], "detail": states[k][1]}
        for k, n in order
    ]


async def build_snapshot(
    user: dict[str, Any],
    pm: ProcessManager,
    db: Database,
    config: ApiConfig,
) -> dict[str, Any]:
    """Assemble the consolidated live overview snapshot for *user*."""
    uid = int(user["id"])
    inst = pm.instance_status(uid)
    running = str(inst.get("status", "STOPPED")).upper() in _RUNNING_STATES

    stats = db.trade_stats(uid)
    summary = {
        "total_trades": int(stats.get("total", 0) or 0),
        "total_pnl": round(float(stats.get("total_pnl", 0.0) or 0.0), 2),
        "win_rate": float(stats.get("win_rate", 0.0) or 0.0),
        "wins": int(stats.get("wins", 0) or 0),
        "losses": int(stats.get("losses", 0) or 0),
        "best_trade": round(float(stats.get("best", 0.0) or 0.0), 2),
        "worst_trade": round(float(stats.get("worst", 0.0) or 0.0), 2),
        "max_drawdown": db.realized_drawdown(uid),
    }

    ops: Optional[dict] = None
    risk: Optional[dict] = None
    port = pm.instance_dashboard_port(uid)
    if port:
        ops_raw, risk_raw = await asyncio.gather(
            _engine_get(port, "/api/operations"),
            _engine_get(port, "/api/risk"),
        )
        ops = ops_raw if isinstance(ops_raw, dict) else None
        risk = risk_raw if isinstance(risk_raw, dict) else None

    positions = _open_positions(config, uid)

    # Live open P&L (best-effort from the engine's operations payload).
    open_pnl = 0.0
    open_positions_live = (ops or {}).get("open_positions") if ops else None
    if isinstance(open_positions_live, list):
        for p in open_positions_live:
            try:
                open_pnl += float(p.get("pnl_dollars") or 0.0)
            except (TypeError, ValueError):
                continue

    health = (ops or {}).get("health", {}) if isinstance(ops, dict) else {}

    return {
        "ts": time.time(),
        "instance": {
            "status": inst.get("status", "STOPPED"),
            "alive": bool(inst.get("alive", False)),
            "uptime_seconds": inst.get("uptime_seconds", 0.0),
            "restarts": inst.get("restarts", 0),
        },
        "summary": summary,
        "open_pnl": round(open_pnl, 2),
        "open_positions_count": len(open_positions_live)
        if isinstance(open_positions_live, list)
        else len(positions),
        "positions": positions[:50],
        "broker_connected": bool(
            (health.get("broker", {}) or {}).get("any_connected", False)
        )
        if isinstance(health, dict)
        else False,
        "risk_mode": str((risk or {}).get("mode", "")) if risk else "",
        "departments": _derive_departments(running=running, ops=ops, risk=risk),
    }


@router.get("/snapshot")
async def snapshot(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
    db: Database = Depends(get_db),
    config: ApiConfig = Depends(get_config),
) -> dict[str, Any]:
    """One consolidated live snapshot (polling fallback for the SSE stream)."""
    return await build_snapshot(user, pm, db, config)


@router.get("/overview")
async def overview_stream(
    request: Request,
    token: str = Query(..., description="Access token (EventSource cannot set headers)"),
    interval: float = Query(default=_DEFAULT_INTERVAL, ge=_MIN_INTERVAL, le=_MAX_INTERVAL),
    pm: ProcessManager = Depends(get_process_manager),
    db: Database = Depends(get_db),
    config: ApiConfig = Depends(get_config),
) -> StreamingResponse:
    """Server-Sent Events stream of consolidated overview snapshots."""
    # Validate the query-param access token and resolve the user.
    try:
        payload = decode_token(token, TOKEN_ACCESS, config)
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
        ) from exc

    user = db.get_user_by_id(user_id)
    if user is None or not user.get("is_active", 1):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not found or inactive",
        )

    async def event_generator():
        # Prime the client immediately, then push on the interval.
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snap = await build_snapshot(user, pm, db, config)
                    yield f"data: {json.dumps(snap)}\n\n"
                except Exception as exc:  # noqa: BLE001 — never kill the stream
                    logger.debug("[stream] snapshot build failed for {}: {}", user_id, exc)
                    yield f"event: error\ndata: {json.dumps({'detail': 'snapshot failed'})}\n\n"
                await asyncio.sleep(interval)
        except asyncio.CancelledError:  # client closed the connection
            raise

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        # Disable proxy buffering (nginx/Cloudflare) so events flush promptly.
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=headers
    )
