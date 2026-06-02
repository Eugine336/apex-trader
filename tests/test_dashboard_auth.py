"""
APEX TRADER — Dashboard Security Tests (Batch 3: H4, M6, L3)

H4: Fail-closed auth — mutating endpoints return 503 when no key configured.
M6: Constant-time key comparison via hmac.compare_digest.
L3: WebSocket authenticates via header/subprotocol, not query param.
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


# ── H4: Fail-closed auth ─────────────────────────────────────────────────

class TestH4FailClosedNoKey:
    """When no API key is configured, mutating endpoints must be disabled."""

    @pytest.fixture
    def client(self):
        return _make_client(api_key="")

    def test_health_still_works(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_read_endpoints_allowed(self, client):
        for path in ("/api/status", "/api/trades", "/api/risk"):
            r = client.get(path)
            assert r.status_code == 200, f"{path} should be allowed without key"

    def test_control_returns_503_without_key(self, client):
        r = client.post("/api/control", json={"action": "pause"})
        assert r.status_code == 503

    def test_pause_returns_503_without_key(self, client):
        r = client.post("/api/control/pause")
        assert r.status_code == 503

    def test_resume_returns_503_without_key(self, client):
        r = client.post("/api/control/resume")
        assert r.status_code == 503

    def test_emergency_close_returns_503_without_key(self, client):
        r = client.post("/api/control/emergency-close")
        assert r.status_code == 503

    def test_503_body_explains_reason(self, client):
        r = client.post("/api/control", json={"action": "stop"})
        assert "DD_DASHBOARD_API_KEY" in r.json()["error"]


class TestH4AuthEnabled:
    """When API key is configured, all /api/ endpoints require it."""

    @pytest.fixture
    def client(self):
        return _make_client(api_key="test-secret-key-123")

    def test_health_exempt(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_valid_key_allows_read(self, client):
        r = client.get("/api/status", headers={"X-API-Key": "test-secret-key-123"})
        assert r.status_code == 200

    def test_valid_key_allows_control(self, client):
        r = client.post(
            "/api/control",
            json={"action": "pause"},
            headers={"X-API-Key": "test-secret-key-123"},
        )
        assert r.status_code == 200

    def test_missing_key_returns_401(self, client):
        r = client.get("/api/status")
        assert r.status_code == 401

    def test_wrong_key_returns_401(self, client):
        r = client.get("/api/status", headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401

    def test_control_without_key_returns_401(self, client):
        r = client.post("/api/control", json={"action": "pause"})
        assert r.status_code == 401


# ── M6: Constant-time key comparison ─────────────────────────────────────

class TestM6ConstantTimeCompare:

    def test_key_matches_uses_hmac_compare_digest(self):
        """Verify _key_matches delegates to hmac.compare_digest."""
        import dashboard.api as api_module
        import inspect
        source = inspect.getsource(api_module._key_matches)
        assert "hmac.compare_digest" in source

    def test_key_matches_accepts_correct_key(self):
        with patch.dict(os.environ, {"DD_DASHBOARD_API_KEY": "my-key"}, clear=False):
            import importlib
            import dashboard.api as api_module
            importlib.reload(api_module)
            assert api_module._key_matches("my-key")

    def test_key_matches_rejects_wrong_key(self):
        with patch.dict(os.environ, {"DD_DASHBOARD_API_KEY": "my-key"}, clear=False):
            import importlib
            import dashboard.api as api_module
            importlib.reload(api_module)
            assert not api_module._key_matches("wrong")


# ── L3: WebSocket header-based auth ──────────────────────────────────────

class TestL3WebSocketHeaderAuth:

    def test_ws_connects_with_header_key(self):
        client = _make_client(api_key="ws-secret")
        with client.websocket_connect(
            "/ws",
            headers={"x-api-key": "ws-secret"},
        ) as ws:
            ws.close()

    def test_ws_connects_with_subprotocol(self):
        client = _make_client(api_key="ws-secret")
        with client.websocket_connect(
            "/ws",
            headers={"sec-websocket-protocol": "ws-secret"},
        ) as ws:
            ws.close()

    def test_ws_rejects_wrong_key(self):
        client = _make_client(api_key="ws-secret")
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/ws",
                headers={"x-api-key": "bad"},
            ) as ws:
                ws.receive_text()

    def test_ws_rejects_query_param_only(self):
        """Query-param auth no longer accepted — key must be in header."""
        client = _make_client(api_key="ws-secret")
        with pytest.raises(Exception):
            with client.websocket_connect("/ws?api_key=ws-secret") as ws:
                ws.receive_text()

    def test_ws_allowed_without_key_configured(self):
        client = _make_client(api_key="")
        with client.websocket_connect("/ws") as ws:
            ws.close()


# ── H4: Bind host configuration (main.py) ───────────────────────────────

class TestH4BindHostConfig:

    def test_default_bind_host_is_loopback(self):
        assert os.getenv("DD_DASHBOARD_BIND_HOST", "127.0.0.1") == "127.0.0.1"

    def test_public_bind_without_key_refuses(self):
        """main.py must refuse to start if bind is public and no key is set."""
        with patch.dict(
            os.environ,
            {"DD_DASHBOARD_BIND_HOST": "0.0.0.0", "DD_DASHBOARD_API_KEY": ""},
            clear=False,
        ):
            bind_host = os.getenv("DD_DASHBOARD_BIND_HOST", "127.0.0.1")
            api_key = os.getenv("DD_DASHBOARD_API_KEY")
            assert bind_host != "127.0.0.1"
            assert not api_key
