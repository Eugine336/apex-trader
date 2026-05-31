"""
APEX TRADER — FastAPI Dashboard Backend
REST endpoints + WebSocket serving live trading data to the frontend.
"""

import asyncio
import os
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from dashboard.state import LiveState

_state = LiveState()

_FRONTEND_BUILD = os.path.join(os.path.dirname(__file__), "frontend", "build")
_API_KEY = os.getenv("DD_DASHBOARD_API_KEY", "")


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


def create_app(state: Optional[LiveState] = None) -> FastAPI:
    global _state
    if state is not None:
        _state = state

    app = FastAPI(title="Apex Trader", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if _API_KEY:
        logger.info("Dashboard API key authentication ENABLED")
    else:
        logger.warning("Dashboard running without authentication — set DD_DASHBOARD_API_KEY")

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next):
        if not _API_KEY:
            return await call_next(request)
        path = request.url.path
        if path == "/api/health" or not path.startswith("/api/"):
            return await call_next(request)
        provided = request.headers.get("X-API-Key", "")
        if provided != _API_KEY:
            return JSONResponse(
                {"error": "Invalid or missing API key"},
                status_code=401,
            )
        return await call_next(request)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/status")
    def status():
        return _state.get_status()

    @app.get("/api/trades")
    def open_trades():
        return _state.get_open_trades()

    @app.get("/api/history")
    def trade_history():
        return _state.get_trade_history()

    @app.get("/api/scanner")
    def scanner():
        return _state.get_scanner_results()

    @app.get("/api/risk")
    def risk():
        return _state.get_risk_status()

    @app.get("/api/performance")
    def performance():
        return _state.get_performance()

    @app.get("/api/ml")
    def ml_insights():
        return _state.get_ml_insights()

    @app.get("/api/activity")
    def activity():
        """Live feed of rejections, warnings, and system events."""
        return _state.get_activity()

    @app.post("/api/control")
    async def control(body: dict):
        action = body.get("action", "")
        value = body.get("value")
        if action == "start":
            return _state.start_trading()
        if action == "pause":
            return _state.pause_trading()
        if action == "stop":
            return _state.stop_trading()
        if action == "resume":
            return _state.resume_trading()
        if action == "risk_mode":
            return _state.set_risk_mode(value)
        if action == "close_all":
            return _state.emergency_close_all()
        return {"status": "unknown_action", "action": action}

    @app.post("/api/control/pause")
    def pause():
        return _state.pause_trading()

    @app.post("/api/control/resume")
    def resume():
        return _state.resume_trading()

    @app.post("/api/control/emergency-close")
    def emergency_close():
        return _state.emergency_close_all()

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    @app.on_event("startup")
    async def start_broadcast_loop():
        # fast channel: status + trades + risk every 2s
        async def _fast_loop():
            while True:
                await asyncio.sleep(2)
                if manager.active:
                    try:
                        payload = {
                            "type": "state_update",
                            "status": _state.get_status(),
                            "open_trades": _state.get_open_trades(),
                            "risk": _state.get_risk_status(),
                            "activity": _state.get_activity(),
                        }
                        await manager.broadcast(payload)
                    except Exception as exc:
                        logger.warning(f"WS broadcast error: {exc}")

        # slow channel: scanner + performance every 5s
        async def _slow_loop():
            while True:
                await asyncio.sleep(5)
                if manager.active:
                    try:
                        payload = {
                            "type": "scanner_update",
                            "scanner": _state.get_scanner_results(),
                            "performance": _state.get_performance(),
                        }
                        await manager.broadcast(payload)
                    except Exception as exc:
                        logger.warning(f"WS slow broadcast error: {exc}")

        asyncio.create_task(_fast_loop())
        asyncio.create_task(_slow_loop())

    if os.path.exists(_FRONTEND_BUILD):
        # Vite outputs to build/assets/, CRA outputs to build/static/
        _assets_dir = os.path.join(_FRONTEND_BUILD, "assets")
        if os.path.exists(_assets_dir):
            app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")
        _static_dir = os.path.join(_FRONTEND_BUILD, "static")
        if os.path.exists(_static_dir):
            app.mount("/static", StaticFiles(directory=_static_dir), name="static")

        @app.get("/{full_path:path}")
        def serve_react(full_path: str):
            if full_path.startswith("api/"):
                return JSONResponse({"error": "not found"}, status_code=404)
            index = os.path.join(_FRONTEND_BUILD, "index.html")
            if os.path.exists(index):
                return FileResponse(index)
            return JSONResponse({"error": "frontend not built — run: cd dashboard/frontend && npm run build"}, status_code=404)

    return app
