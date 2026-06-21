"""
APEX TRADER — FastAPI Dashboard Backend
REST endpoints + WebSocket serving live trading data to the frontend.
"""

import asyncio
import hmac
import ipaddress
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

_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "DD_DASHBOARD_ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if o.strip()
]

_MUTATING_PATHS = {
    "/api/control",
    "/api/control/pause",
    "/api/control/resume",
    "/api/control/emergency-close",
}


def _is_mutating_request(path: str) -> bool:
    return path in _MUTATING_PATHS


def _key_matches(provided: str) -> bool:
    return hmac.compare_digest(provided.encode(), _API_KEY.encode())


# Trust loopback (localhost) connections without an API key. The dashboard is
# typically bound to 0.0.0.0 but driven from the same machine (127.0.0.1 / ::1),
# where the operator already has host access. This lets the local browser work
# WITHOUT baking the key into the frontend bundle, while remote (non-loopback)
# clients still require the key. Disable with DD_DASHBOARD_TRUST_LOOPBACK=0.
# Caveat: behind a reverse proxy every request appears to originate from the
# proxy's (often loopback) address — turn this off if you front the dashboard
# with a proxy, and authenticate at the proxy instead.
# Default OFF: behind a reverse proxy every request appears to originate from
# the proxy's (often loopback) address, so trusting loopback would expose the
# control endpoints (close_all, emergency-close, risk_mode) with no auth.
# Opt in explicitly with DD_DASHBOARD_TRUST_LOOPBACK=1 only when the dashboard
# is bound directly (no proxy in front).
_TRUST_LOOPBACK = os.getenv("DD_DASHBOARD_TRUST_LOOPBACK", "0").strip().lower() in (
    "1", "true", "yes", "on",
)


def _is_loopback(host: str) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) before the loopback check.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _request_is_trusted_local(client) -> bool:
    return bool(_TRUST_LOOPBACK and client and _is_loopback(client.host))


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
        allow_origins=_ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if _API_KEY:
        logger.info(
            "Dashboard API key authentication ENABLED (loopback trust: {})",
            "ON — local browser needs no key" if _TRUST_LOOPBACK
            else "OFF — all clients need the key",
        )
    else:
        logger.warning(
            "No DD_DASHBOARD_API_KEY set — mutating endpoints DISABLED (read-only mode)"
        )

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next):
        path = request.url.path

        if path == "/api/health" or not path.startswith("/api/"):
            return await call_next(request)

        if not _API_KEY:
            if _is_mutating_request(path) and request.method in ("POST", "PUT", "PATCH", "DELETE"):
                return JSONResponse(
                    {"error": "Mutating endpoints disabled — set DD_DASHBOARD_API_KEY"},
                    status_code=503,
                )
            return await call_next(request)

        provided = request.headers.get("X-API-Key", "")
        if not _key_matches(provided):
            if _request_is_trusted_local(request.client):
                return await call_next(request)
            return JSONResponse(
                {"error": "Invalid or missing API key"},
                status_code=401,
            )
        return await call_next(request)

    @app.get("/api/health")
    def health():
        """Aggregate system health — broker, adaptive layers, drawdown, tick age.

        Stays unauthenticated (liveness probes hit it) and always returns 200 so
        a probe can read ``status`` from the body rather than the HTTP code.
        """
        try:
            return _state.get_health()
        except Exception:  # noqa: BLE001
            return {"status": "ok", "ops_enabled": False}

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

    @app.get("/api/system-performance")
    def system_performance():
        return _state.get_system_performance()

    @app.get("/api/ml")
    def ml_insights():
        return _state.get_ml_insights()

    @app.get("/api/activity")
    def activity():
        """Live feed of rejections, warnings, and system events."""
        return _state.get_activity()

    @app.get("/api/events")
    def events(
        severity_min: str = "INFO",
        event_type: Optional[str] = None,
        symbol: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ):
        """Persistent activity feed from the event store (all severities incl. DEBUG)."""
        types = [event_type] if event_type else None
        return _state.get_events(
            severity_min=severity_min,
            event_types=types,
            symbol=symbol,
            correlation_id=correlation_id,
            limit=min(limit, 500),
            offset=offset,
        )

    @app.get("/api/shadow")
    def shadow_outcomes():
        """Rejected/skipped setup outcomes grouped by rejecting gate."""
        return _state.get_shadow_outcomes()

    @app.get("/api/reversal-breakdown")
    def reversal_breakdown():
        """Realized EV/win-rate of counter-trend REVERSAL trades vs continuation."""
        return _state.get_reversal_breakdown()

    @app.get("/api/reconciliation")
    def reconciliation():
        """Broker-vs-derived exit reason discrepancies."""
        return _state.get_reconciliation()

    @app.get("/api/decisions")
    def decisions(
        limit: int = 50,
        decision_type: str = "",
        symbol: str = "",
    ):
        """Recent decisions from the Decision Intelligence layer."""
        return _state.get_decisions(limit=limit, decision_type=decision_type, symbol=symbol)

    @app.get("/api/decisions/stats")
    def decision_stats():
        """Aggregated decision statistics — action counts, situation breakdown, governor vetoes."""
        return _state.get_decision_stats()

    @app.get("/api/planner")
    def planner(limit: int = 50, symbol: str = ""):
        """Recent trade plans from the Trade Planner with their outcomes."""
        return _state.get_planner(limit=limit, symbol=symbol)

    @app.get("/api/planner/stats")
    def planner_stats():
        """Aggregated planner statistics — action / entry-mode / SL / TP strategy win rates."""
        return _state.get_planner_stats()

    @app.get("/api/governor")
    def governor():
        """Portfolio Governor state — daily loss cap, exposure breakdowns, recent blocks."""
        return _state.get_governor()

    @app.get("/api/decision-trace")
    def decision_trace(limit: int = 100, symbol: str = ""):
        """Pipeline awareness traces — full stage chains, justifications, and challenges."""
        return _state.get_decision_traces(limit=min(limit, 500), symbol=symbol)

    @app.get("/api/decision-trace/stats")
    def decision_trace_stats():
        """Aggregated pipeline stats — funnel, rejection breakdown, challenges, confidence."""
        return _state.get_decision_trace_stats()

    @app.get("/api/module-votes")
    def module_votes():
        """Per-pair grid of the 9 brain modules' directional votes + per-module stats."""
        return _state.get_module_votes()

    @app.get("/api/ranker")
    def ranker():
        """Ranked opportunities (coherent vote clusters) per pair with EV/horizon aggregates."""
        return _state.get_ranker()

    @app.get("/api/orchestrator")
    def orchestrator(limit: int = 100, symbol: str = ""):
        """Round-table graded-sizing proposals — per-dimension multipliers + applied size."""
        return _state.get_orchestrator(limit=min(limit, 500), symbol=symbol)

    @app.get("/api/outcome-feedback")
    def outcome_feedback():
        """Per-module / per-horizon accuracy + confidence calibration from closed trades."""
        return _state.get_outcome_feedback()

    @app.get("/api/position-health")
    def position_health(limit: int = 200, symbol: str = ""):
        """Live-management round table — open-position health scores, per-dimension breakdown + action log."""
        return _state.get_position_health(limit=min(limit, 1000), symbol=symbol)

    @app.get("/api/module-governor")
    def module_governor(limit: int = 100):
        """Module shadow-mode governor — per-module ACTIVE/SHADOW/DISABLED state + transition history."""
        return _state.get_module_governor(limit=min(limit, 500))
    @app.get("/api/learning")
    def learning():
        """Adaptive learning layer — signal ledger, emitter feedback, vote calibration, per-class weights, pair learner, tuner agent."""
        return _state.get_learning()

    @app.get("/api/operations")
    def operations():
        """Ops control room — health, drawdown, equity, open positions, risk events, regime map, exposure, layer pulse, governor actions, watchdog, and tick-latency profile."""
        return _state.get_operations()

    @app.get("/api/departments")
    def departments():
        """9-department organisation health summary — the org-chart home board."""
        return _state.get_departments()

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
        if _API_KEY:
            key = (
                websocket.headers.get("x-api-key", "")
                or websocket.headers.get("sec-websocket-protocol", "")
            )
            if not _key_matches(key):
                if _request_is_trusted_local(websocket.client):
                    await websocket.accept()
                else:
                    await websocket.close(code=1008, reason="Invalid API key")
                    return
            elif websocket.headers.get("sec-websocket-protocol"):
                await websocket.accept(subprotocol=websocket.headers["sec-websocket-protocol"])
            else:
                await websocket.accept()
        else:
            await websocket.accept()
        manager.active.append(websocket)
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

        # slow channel: scanner + performance + shadow + decisions every 5s
        async def _slow_loop():
            while True:
                await asyncio.sleep(5)
                if manager.active:
                    try:
                        payload = {
                            "type": "scanner_update",
                            "scanner": _state.get_scanner_results(),
                            "performance": _state.get_performance(),
                            "shadow": _state.get_shadow_outcomes(),
                            "reversal": _state.get_reversal_breakdown(),
                            "decisions": _state.get_decisions(limit=20),
                            "departments": _state.get_departments(),
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
