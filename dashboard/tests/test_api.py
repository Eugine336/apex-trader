"""
APEX TRADER — Dashboard API smoke tests.

The dashboard moved from a mutable ``STATE`` singleton (with ``BotStatus`` and
``load_demo_data``) to a ``create_app()`` factory over the ``LiveState`` mixin
orchestrator, which serves live data when a system is attached and stable
fallback shapes otherwise. These tests exercise the factory and the read-only
endpoints against those fallback shapes (nothing attached), so they stay valid
without a running engine. Mutating control endpoints are intentionally not
covered here — they are disabled unless ``DD_DASHBOARD_API_KEY`` is set.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dashboard.api import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def test_app_builds_and_serves_schema(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "Apex Trader"


def test_health_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/status",
        "/api/trades",
        "/api/history",
        "/api/performance",
        "/api/scanner",
        "/api/risk",
        "/api/ml",
    ],
)
def test_read_endpoints_serve_fallback_shapes(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)
