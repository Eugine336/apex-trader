"""
APEX TRADER — FastAPI Dashboard Backend
REST endpoints serving live trading data to the frontend.
"""

from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from dashboard.state import LiveState

_state = LiveState()


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

    @app.post("/api/control/pause")
    def pause():
        return _state.pause_trading()

    @app.post("/api/control/resume")
    def resume():
        return _state.resume_trading()

    @app.post("/api/control/emergency-close")
    def emergency_close():
        return _state.emergency_close_all()

    return app
