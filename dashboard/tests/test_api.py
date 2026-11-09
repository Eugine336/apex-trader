"""Contract tests for the current FastAPI application factory.

The dashboard is factory-based and exposes read-only state without a demo
singleton. Mutating routes remain guarded by the API-key middleware.
"""

from fastapi.testclient import TestClient

from dashboard.api import create_app
from dashboard.state import LiveState


def _client() -> TestClient:
    return TestClient(create_app(LiveState()))


def test_app_factory_builds_read_only_dashboard():
    client = _client()
    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "simulated"
    assert payload["bot_status"] == "running"
    assert "uptime_seconds" in payload


def test_health_endpoint_reports_health_without_engine():
    response = _client().get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert "status" in payload


def test_read_only_dashboard_routes_are_registered():
    app = create_app(LiveState())
    paths = {route.path for route in app.routes}

    assert {
        "/api/status",
        "/api/health",
        "/api/trades",
        "/api/history",
        "/api/risk",
        "/api/decisions",
        "/api/decision-trace",
        "/api/module-votes",
        "/api/ranker",
        "/api/market-model",
        "/api/operations",
    } <= paths
