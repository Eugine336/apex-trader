"""
APEX TRADER — FastAPI Dashboard Backend
REST endpoints serving live trading data to the frontend.
"""

import os
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dashboard.state import LiveState

_state = LiveState()

_FRONTEND_BUILD = os.path.join(os.path.dirname(__file__), "frontend", "build")


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

    if os.path.exists(_FRONTEND_BUILD):
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
            return JSONResponse({"error": "frontend not built"}, status_code=404)

    return app
