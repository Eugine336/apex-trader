"""
APEX TRADER — Dashboard Authentication Tests
Verifies API key middleware: with key, without key, wrong key, health exempt.
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _make_client(api_key: str = ""):
    """Create a fresh FastAPI test client with optional API key."""
    with patch.dict(os.environ, {"DD_DASHBOARD_API_KEY": api_key}, clear=False):
        import importlib
        import dashboard.api as api_module
        importlib.reload(api_module)
        app = api_module.create_app()
        return TestClient(app)


class TestDashboardAuthEnabled:

    @pytest.fixture
    def client(self):
        return _make_client(api_key="test-secret-key-123")

    def test_health_exempt(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_valid_key_allows_access(self, client):
        r = client.get("/api/status", headers={"X-API-Key": "test-secret-key-123"})
        assert r.status_code == 200

    def test_missing_key_returns_401(self, client):
        r = client.get("/api/status")
        assert r.status_code == 401
        assert r.json()["error"] == "Invalid or missing API key"

    def test_wrong_key_returns_401(self, client):
        r = client.get("/api/status", headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401

    def test_control_endpoint_requires_auth(self, client):
        r = client.post("/api/control", json={"action": "pause"})
        assert r.status_code == 401

    def test_control_endpoint_with_key_works(self, client):
        r = client.post(
            "/api/control",
            json={"action": "pause"},
            headers={"X-API-Key": "test-secret-key-123"},
        )
        assert r.status_code == 200


class TestDashboardAuthDisabled:

    @pytest.fixture
    def client(self):
        return _make_client(api_key="")

    def test_no_key_allows_all(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200

    def test_health_still_works(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_control_works_without_key(self, client):
        r = client.post("/api/control", json={"action": "pause"})
        assert r.status_code == 200
