"""End-to-end API tests via FastAPI TestClient.

Covers the full user journey: register → authenticate → store broker
credentials (masked on read) → configure → dashboard data → instance control.
The process manager is replaced with a fake so no real subprocess is spawned.
"""

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.auth import get_process_manager
from api.config import ApiConfig


class _FakePM:
    """Stand-in process manager that records calls without spawning anything."""

    def __init__(self):
        self._status = {}

    def reconcile_on_startup(self):
        pass

    def start_monitor(self):
        pass

    def shutdown(self):
        pass

    def instance_status(self, user_id):
        return self._status.get(
            user_id,
            {
                "user_id": user_id,
                "status": "STOPPED",
                "pid": None,
                "alive": False,
                "restarts": 0,
                "last_error": "",
                "started_at": None,
                "stopped_at": None,
            },
        )

    def start_instance(self, user_id, creds):
        self._status[user_id] = {
            "user_id": user_id,
            "status": "RUNNING",
            "pid": 4242,
            "alive": True,
            "restarts": 0,
            "last_error": "",
            "started_at": "2026-01-01T00:00:00+00:00",
            "stopped_at": None,
        }
        return self._status[user_id]

    def stop_instance(self, user_id, user_initiated=True):
        self._status[user_id] = self.instance_status(user_id) | {
            "status": "STOPPED",
            "alive": False,
            "pid": None,
        }
        return self._status[user_id]

    def restart_instance(self, user_id, creds):
        return self.start_instance(user_id, creds)

    def tail_log(self, user_id, lines=200):
        return ["log line 1", "log line 2"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_API_DATA_DIR", str(tmp_path / "api_data"))
    monkeypatch.setenv("APEX_API_JWT_SECRET", "endpoint-test-secret")
    monkeypatch.setenv("APEX_API_VAULT_KEY", "endpoint-test-vault-key")
    app = create_app(ApiConfig())
    app.dependency_overrides[get_process_manager] = lambda: _FakePM()
    with TestClient(app) as c:
        yield c


def _register(client, email="user@example.com", password="password123"):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def _auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_register_login_and_me(client):
    r = _register(client)
    assert r.status_code == 201
    tokens = r.json()
    assert tokens["access_token"] and tokens["refresh_token"]

    # First user is bootstrapped as admin.
    me = client.get("/api/users/me", headers=_auth_header(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["is_admin"] is True
    assert me.json()["email"] == "user@example.com"

    # Duplicate registration is rejected.
    assert _register(client).status_code == 409

    # Login works; wrong password is 401.
    ok = client.post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "password123"},
    )
    assert ok.status_code == 200
    bad = client.post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "wrong"},
    )
    assert bad.status_code == 401


def test_refresh_token_flow(client):
    tokens = _register(client).json()
    r = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    assert r.json()["access_token"]
    # An access token is not accepted at the refresh endpoint.
    bad = client.post("/api/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert bad.status_code == 401


def test_requires_auth(client):
    assert client.get("/api/users/me").status_code == 401
    assert client.get("/api/dashboard/summary").status_code == 401


def test_broker_credentials_masked_roundtrip(client):
    token = _register(client).json()["access_token"]
    h = _auth_header(token)

    r = client.put(
        "/api/broker/mt5",
        headers=h,
        json={"login": 12345678, "password": "supersecret", "server": "Broker-Live"},
    )
    assert r.status_code == 200

    listed = client.get("/api/broker", headers=h).json()
    assert len(listed) == 1
    masked = listed[0]["masked"]
    assert masked["login"] == 12345678
    assert masked["server"] == "Broker-Live"
    assert "supersecret" not in str(masked)
    assert masked["password"].startswith("****")

    # Invalid creds rejected (missing server enforced by schema → 422).
    bad = client.put("/api/broker/mt5", headers=h, json={"login": 1, "password": "p"})
    assert bad.status_code == 422

    # Delete.
    assert client.delete("/api/broker/mt5", headers=h).status_code == 200
    assert client.delete("/api/broker/mt5", headers=h).status_code == 404


def test_config_get_update(client):
    token = _register(client).json()["access_token"]
    h = _auth_header(token)
    upd = client.put(
        "/api/config",
        headers=h,
        json={"risk_per_trade_pct": 1.0, "max_open_trades": 4},
    )
    assert upd.status_code == 200
    assert upd.json()["config"]["risk_per_trade_pct"] == 1.0
    got = client.get("/api/config", headers=h).json()
    assert got["max_open_trades"] == 4


def test_trading_start_requires_credentials(client):
    token = _register(client).json()["access_token"]
    h = _auth_header(token)
    # No creds yet → 400.
    assert client.post("/api/trading/start", headers=h).status_code == 400


def test_trading_lifecycle_with_credentials(client):
    token = _register(client).json()["access_token"]
    h = _auth_header(token)
    client.put(
        "/api/broker/mt5",
        headers=h,
        json={"login": 1, "password": "p", "server": "s"},
    )
    started = client.post("/api/trading/start", headers=h)
    assert started.status_code == 200
    assert started.json()["status"] == "RUNNING"

    status = client.get("/api/trading/status", headers=h).json()
    assert status["status"] == "RUNNING"

    logs = client.get("/api/trading/logs", headers=h).json()
    assert logs["lines"]

    stopped = client.post("/api/trading/stop", headers=h)
    assert stopped.json()["status"] == "STOPPED"


def test_dashboard_summary_empty(client):
    token = _register(client).json()["access_token"]
    h = _auth_header(token)
    summary = client.get("/api/dashboard/summary", headers=h).json()
    assert summary["total_trades"] == 0
    assert summary["instance_status"] == "STOPPED"
    assert client.get("/api/dashboard/positions", headers=h).json() == []
