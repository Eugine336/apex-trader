"""
APEX TRADER — Dashboard API Tests
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dashboard.api import app
from dashboard.state import STATE, BotStatus, load_demo_data


@pytest.fixture(autouse=True)
def setup_state():
    load_demo_data()
    yield


client = TestClient(app)


class TestStatusEndpoint:
    def test_status_returns_bot_info(self):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "bot_status" in data
        assert "win_rate" in data
        assert "risk_mode" in data
        assert "uptime_seconds" in data

    def test_status_has_pnl(self):
        resp = client.get("/api/status")
        data = resp.json()
        assert "daily_pnl" in data
        assert "total_pnl" in data


class TestTradesEndpoint:
    def test_active_trades_returned(self):
        resp = client.get("/api/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert "trades" in data
        assert "count" in data
        assert len(data["trades"]) == data["count"]

    def test_trade_has_required_fields(self):
        resp = client.get("/api/trades")
        trade = resp.json()["trades"][0]
        for field in ["instrument", "direction", "entry_price", "pnl_dollars",
                       "stop_loss", "tp1", "tp2", "stage", "score"]:
            assert field in trade, f"Missing field: {field}"


class TestHistoryEndpoint:
    def test_history_returned(self):
        resp = client.get("/api/history")
        assert resp.status_code == 200
        data = resp.json()
        assert "trades" in data
        assert len(data["trades"]) > 0

    def test_closed_trade_has_outcome(self):
        resp = client.get("/api/history")
        trade = resp.json()["trades"][0]
        assert "outcome" in trade
        assert trade["outcome"] in ("WIN", "LOSS")


class TestPerformanceEndpoint:
    def test_performance_metrics(self):
        resp = client.get("/api/performance")
        assert resp.status_code == 200
        data = resp.json()
        assert "win_rate" in data
        assert "equity_curve" in data
        assert "pnl_history" in data
        assert data["win_rate"] > 0

    def test_equity_curve_structure(self):
        resp = client.get("/api/performance")
        curve = resp.json()["equity_curve"]
        assert len(curve) > 0
        assert "date" in curve[0]
        assert "equity" in curve[0]


class TestScannerEndpoint:
    def test_scanner_returns_instruments(self):
        resp = client.get("/api/scanner")
        assert resp.status_code == 200
        data = resp.json()
        assert "instruments" in data
        assert "ready_count" in data
        assert "total_count" in data

    def test_instrument_has_score_and_factors(self):
        resp = client.get("/api/scanner")
        inst = resp.json()["instruments"][0]
        assert "score" in inst
        assert "factors" in inst
        assert "symbol" in inst


class TestRiskEndpoint:
    def test_risk_returns_mode(self):
        resp = client.get("/api/risk")
        assert resp.status_code == 200
        data = resp.json()
        assert data["risk_mode"] in ("NORMAL", "CAUTION", "RECOVERY", "FROZEN")
        assert "daily_loss_pct" in data
        assert "max_daily_loss_pct" in data


class TestMLEndpoint:
    def test_ml_returns_all_sections(self):
        resp = client.get("/api/ml")
        assert resp.status_code == 200
        data = resp.json()
        assert "score_adjustments" in data
        assert "regime_stats" in data
        assert "session_stats" in data
        assert "pair_stats" in data

    def test_score_adjustments_are_numbers(self):
        resp = client.get("/api/ml")
        for key, val in resp.json()["score_adjustments"].items():
            assert isinstance(val, (int, float))


class TestInstrumentsEndpoint:
    def test_instruments_list(self):
        resp = client.get("/api/instruments")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 59

    def test_instrument_has_pip_size(self):
        resp = client.get("/api/instruments")
        inst = resp.json()["instruments"][0]
        assert "pip_size" in inst
        assert "platform" in inst


class TestControlEndpoint:
    def test_start_bot(self):
        resp = client.post("/api/control", json={"action": "start"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert STATE.bot_status == BotStatus.RUNNING.value

    def test_stop_bot(self):
        resp = client.post("/api/control", json={"action": "stop"})
        assert resp.status_code == 200
        assert STATE.bot_status == BotStatus.STOPPED.value

    def test_pause_bot(self):
        resp = client.post("/api/control", json={"action": "pause"})
        assert resp.status_code == 200
        assert STATE.bot_status == BotStatus.PAUSED.value

    def test_set_risk_mode(self):
        resp = client.post("/api/control",
                           json={"action": "risk_mode", "value": "CAUTION"})
        assert resp.status_code == 200
        assert STATE.risk_mode == "CAUTION"

    def test_close_all(self):
        resp = client.post("/api/control", json={"action": "close_all"})
        assert resp.status_code == 200
        assert len(STATE.active_trades) == 0

    def test_invalid_action(self):
        resp = client.post("/api/control", json={"action": "explode"})
        data = resp.json()
        assert data["ok"] is False

    def test_invalid_risk_mode(self):
        resp = client.post("/api/control",
                           json={"action": "risk_mode", "value": "YOLO"})
        data = resp.json()
        assert data["ok"] is False


class TestWebSocket:
    def test_ws_connects_and_receives_snapshot(self):
        with client.websocket_connect("/ws/live") as ws:
            data = ws.receive_json()
            assert data["type"] == "snapshot"
            assert "bot_status" in data["data"]
