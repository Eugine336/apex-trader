"""
APEX TRADER — Dashboard API
FastAPI backend exposing the bot's internal state for the React dashboard.
"""

import sys
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    INSTRUMENT_REGISTRY,
    AppConfig,
    get_instruments_by_category,
)
from dashboard.state import STATE, BotStatus, RiskMode, load_demo_data


class ControlRequest(BaseModel):
    action: str  # start | stop | pause | risk_mode
    value: str | None = None


connected_ws: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_demo_data()
    yield


app = FastAPI(
    title="Apex Trader Dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Status ────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return STATE.to_dict()


# ── Active trades ─────────────────────────────────────────────────────────

@app.get("/api/trades")
async def get_trades():
    return {"trades": STATE.active_trades, "count": len(STATE.active_trades)}


# ── Trade history ─────────────────────────────────────────────────────────

@app.get("/api/history")
async def get_history():
    return {"trades": STATE.closed_trades, "count": len(STATE.closed_trades)}


# ── Performance ───────────────────────────────────────────────────────────

@app.get("/api/performance")
async def get_performance():
    return {
        "win_rate": STATE.win_rate,
        "total_trades": STATE.total_trades,
        "win_count": STATE.win_count,
        "loss_count": STATE.loss_count,
        "daily_pnl": STATE.daily_pnl,
        "weekly_pnl": STATE.weekly_pnl,
        "monthly_pnl": STATE.monthly_pnl,
        "total_pnl": STATE.total_pnl,
        "account_balance": STATE.account_balance,
        "equity_curve": STATE.equity_curve,
        "pnl_history": STATE.pnl_history,
    }


# ── Scanner ───────────────────────────────────────────────────────────────

@app.get("/api/scanner")
async def get_scanner():
    ready = [s for s in STATE.instrument_scores if s.get("status") == "READY"]
    watchlist = [s for s in STATE.instrument_scores
                 if s.get("status") == "WATCHLIST"]
    return {
        "instruments": STATE.instrument_scores,
        "ready_count": len(ready),
        "watchlist_count": len(watchlist),
        "total_count": len(STATE.instrument_scores),
    }


# ── Risk ──────────────────────────────────────────────────────────────────

@app.get("/api/risk")
async def get_risk():
    return {
        "risk_mode": STATE.risk_mode,
        "daily_loss_pct": STATE.daily_loss_pct,
        "max_daily_loss_pct": STATE.max_daily_loss_pct,
        "exposure_pct": STATE.exposure_pct,
        "consecutive_losses": STATE.consecutive_losses,
        "consecutive_wins": STATE.consecutive_wins,
        "open_trade_count": len(STATE.active_trades),
        "max_open_trades": 6,
        "account_balance": STATE.account_balance,
    }


# ── ML Insights ───────────────────────────────────────────────────────────

@app.get("/api/ml")
async def get_ml():
    return {
        "score_adjustments": STATE.ml_score_adjustments,
        "regime_stats": STATE.ml_regime_stats,
        "session_stats": STATE.ml_session_stats,
        "pair_stats": STATE.ml_pair_stats,
    }


# ── Instruments ───────────────────────────────────────────────────────────

@app.get("/api/instruments")
async def get_instruments():
    instruments = []
    for sym, info in INSTRUMENT_REGISTRY.items():
        score_entry = next(
            (s for s in STATE.instrument_scores if s.get("symbol") == sym),
            None,
        )
        instruments.append({
            "symbol": sym,
            "name": info.name,
            "category": info.category.value,
            "platform": info.platform.value,
            "pip_size": info.pip_size,
            "typical_spread": info.typical_spread_pips,
            "score": score_entry.get("score", 0) if score_entry else 0,
            "status": score_entry.get("status", "INACTIVE") if score_entry else "INACTIVE",
        })
    return {"instruments": instruments, "total": len(instruments)}


# ── Control ───────────────────────────────────────────────────────────────

@app.post("/api/control")
async def control_bot(req: ControlRequest):
    if req.action == "start":
        STATE.bot_status = BotStatus.RUNNING.value
        STATE.started_at = time.time()
        msg = "Bot started"
    elif req.action == "stop":
        STATE.bot_status = BotStatus.STOPPED.value
        STATE.started_at = 0
        msg = "Bot stopped"
    elif req.action == "pause":
        STATE.bot_status = BotStatus.PAUSED.value
        msg = "Bot paused"
    elif req.action == "risk_mode" and req.value:
        try:
            STATE.risk_mode = RiskMode(req.value).value
            msg = f"Risk mode set to {req.value}"
        except ValueError:
            return {"ok": False, "message": f"Invalid risk mode: {req.value}"}
    elif req.action == "close_all":
        STATE.active_trades = []
        msg = "All positions closed"
    else:
        return {"ok": False, "message": f"Unknown action: {req.action}"}

    await broadcast({"type": "status", "data": STATE.to_dict()})
    return {"ok": True, "message": msg}


# ── WebSocket ─────────────────────────────────────────────────────────────

async def broadcast(data: dict):
    import json
    payload = json.dumps(data)
    dead = set()
    for ws in connected_ws:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    connected_ws.difference_update(dead)


@app.websocket("/ws/live")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_ws.add(ws)
    try:
        await ws.send_json({"type": "snapshot", "data": STATE.to_dict()})
        while True:
            data = await ws.receive_text()
            await ws.send_json({"type": "ack", "received": data})
    except WebSocketDisconnect:
        pass
    finally:
        connected_ws.discard(ws)


# ── Static files (production build) ──────────────────────────────────────

_build_dir = Path(__file__).parent / "frontend" / "build"
if _build_dir.exists():
    app.mount("/", StaticFiles(directory=str(_build_dir), html=True), name="spa")
