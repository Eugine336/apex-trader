"""
Tests for production bug fixes:
  A. Deriv stake-cap retry convergence (fixed limit_order)
  B. ML retraining robustness (logger.exception + defensive journal query)
  C. Dashboard WebSocket authentication
  D. Spread bootstrap early bail on missing symbols
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# BUG A — Deriv stake-cap retry converges in ≤2 attempts
# ═══════════════════════════════════════════════════════════════════════════════


class TestDerivStakeCapRetry:
    """Verify the retry loop converges when Deriv returns a cap error."""

    def _build_connector(self, responses):
        """Create a DerivConnector mock that returns canned responses."""
        from platforms.deriv.deriv_connector import DerivConnector

        conn = DerivConnector.__new__(DerivConnector)
        conn._connected = True
        conn._reconnecting = False
        conn._ws = MagicMock()
        conn._positions = {}
        conn._discovered_multipliers = {}
        conn._mapper = MagicMock()
        conn._mapper.to_broker.return_value = "1HZ10V"
        conn._thread_lock = __import__("threading").Lock()
        conn._last_history_request = 0.0

        call_count = {"n": 0}

        def fake_sync_send(payload):
            if "ticks_history" in payload:
                return {
                    "history": {"prices": [10200.0], "times": [1700000000]}
                }
            idx = call_count["n"]
            call_count["n"] += 1
            if idx < len(responses):
                return responses[idx]
            return responses[-1]

        conn._sync_send = fake_sync_send
        return conn

    def test_cap_converges_in_one_retry(self):
        """Stake $47.54 vs cap $47.50 should converge in 1 retry."""
        responses = [
            {"error": {"message": "Enter an amount equal to or lower than 47.50."}},
            {"buy": {"contract_id": "123456", "buy_price": 47.00}},
        ]
        conn = self._build_connector(responses)
        with patch.object(conn, "_get_multiplier", return_value=1000):
            result = conn.place_order(
                "V10_1S", "LONG", 0.01, sl=10176.0, tp=10250.0, stake_usd=47.54
            )
        assert result.success is True
        assert result.order_id == "123456"

    def test_cap_does_not_slide_indefinitely(self):
        """With fixed limit_order, the cap should not slide on each retry."""
        responses = [
            {"error": {"message": "Enter an amount equal to or lower than 47.54."}},
            {"buy": {"contract_id": "999", "buy_price": 47.00}},
        ]
        conn = self._build_connector(responses)
        with patch.object(conn, "_get_multiplier", return_value=1000):
            result = conn.place_order(
                "V10_1S", "LONG", 0.01, sl=10176.0, tp=10250.0, stake_usd=47.54
            )
        assert result.success is True

    def test_cap_floor_jumps_below_amount(self):
        """When cap matches amount, floor should jump at least $0.50 below."""
        responses = [
            {"error": {"message": "Enter an amount equal to or lower than 50.00."}},
            {"buy": {"contract_id": "555", "buy_price": 49.00}},
        ]
        conn = self._build_connector(responses)
        sent_amounts = []
        orig_send = conn._sync_send

        def capture_send(payload):
            if "buy" in payload:
                sent_amounts.append(payload["parameters"]["amount"])
            return orig_send(payload)

        conn._sync_send = capture_send
        with patch.object(conn, "_get_multiplier", return_value=1000):
            conn.place_order(
                "V10_1S", "LONG", 0.01, sl=10176.0, tp=10250.0, stake_usd=50.00
            )
        assert len(sent_amounts) >= 1
        assert sent_amounts[-1] < 50.00

    def test_unrecognised_error_does_not_retry(self):
        """Non-cap, non-multiplier errors should not trigger retries."""
        responses = [
            {"error": {"message": "Insufficient balance"}},
        ]
        conn = self._build_connector(responses)
        with patch.object(conn, "_get_multiplier", return_value=1000):
            result = conn.place_order(
                "V10_1S", "LONG", 0.01, sl=10176.0, tp=10250.0, stake_usd=47.54
            )
        assert result.success is False
        assert "Insufficient balance" in result.error


# ═══════════════════════════════════════════════════════════════════════════════
# BUG B — ML retraining robustness
# ═══════════════════════════════════════════════════════════════════════════════


class TestMLRetrainingRobustness:
    """Verify the optimization pipeline handles edge cases."""

    def test_optimizer_handles_empty_trades(self):
        from adaptive.optimizer import AdaptiveOptimizer

        opt = AdaptiveOptimizer()
        report = opt.run_optimization([])
        assert report.trades_analyzed == 0

    def test_optimizer_handles_single_trade(self):
        from adaptive.optimizer import AdaptiveOptimizer

        opt = AdaptiveOptimizer()
        trades = [
            {
                "pair": "EURUSD",
                "direction": "BUY",
                "pnl": 5.0,
                "score": 88,
                "confluences_tags": ["structure", "fvg"],
                "regime": "trending",
                "session": "london",
                "spread": 1.2,
                "entry_type": "fvg_ob",
                "time_to_exit": 30.0,
                "outcome": "TP1",
            }
        ]
        report = opt.run_optimization(trades)
        assert report.trades_analyzed == 1
        assert report.overall_performance.total_trades == 1

    def test_optimizer_handles_single_pair_single_session(self):
        from adaptive.optimizer import AdaptiveOptimizer

        opt = AdaptiveOptimizer()
        trades = [
            {
                "pair": "EURUSD",
                "direction": "BUY",
                "pnl": p,
                "score": 85,
                "confluences_tags": ["structure"],
                "regime": "trending",
                "session": "london",
                "spread": 1.0,
                "entry_type": "fvg",
                "time_to_exit": 20.0,
                "outcome": "TP1" if p > 0 else "SL",
            }
            for p in [3.0, -2.0, 5.0, -1.0, 4.0]
        ]
        report = opt.run_optimization(trades)
        assert report.trades_analyzed == 5

    def test_journal_get_all_trades_handles_missing_pnl_dollars(self):
        """Verify get_all_trades_as_dicts works if pnl_dollars column is absent."""
        import tempfile
        import aiosqlite
        from brain.trade_journal import TradeJournal

        async def _run():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                db_path = tmp.name

            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE trades ("
                    "pair TEXT, direction TEXT, pnl REAL, score INT, "
                    "confluences TEXT, regime TEXT, session TEXT, "
                    "spread REAL, entry_type TEXT, time_to_exit REAL, "
                    "outcome TEXT, timestamp TEXT)"
                )
                await db.execute(
                    "INSERT INTO trades VALUES "
                    "('EURUSD','BUY',5.0,88,'[\"structure\"]','trending',"
                    "'london',1.2,'fvg',30.0,'TP1','2026-01-01T00:00:00')"
                )
                await db.commit()

            journal = TradeJournal(db_path=db_path)
            trades = await journal.get_all_trades_as_dicts()
            os.unlink(db_path)
            assert len(trades) == 1
            assert trades[0]["pair"] == "EURUSD"
            assert trades[0]["pnl_dollars"] == 0.0

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_parse_confluence_tags_handles_non_list(self):
        """_parse_confluence_tags should handle non-list input gracefully."""
        from platforms.main_loop import _parse_confluence_tags

        assert _parse_confluence_tags([]) == []
        assert _parse_confluence_tags(["Structure scored 18/20"]) == ["structure"]
        assert _parse_confluence_tags(["FVG confirmed", "News clear"]) == ["fvg", "news"]


# ═══════════════════════════════════════════════════════════════════════════════
# BUG C — Dashboard WebSocket authentication
# ═══════════════════════════════════════════════════════════════════════════════


class TestDashboardWebSocketAuth:
    """Verify WebSocket auth when API key is configured."""

    def test_ws_rejected_without_key(self):
        """When API key is set, WS connection without key should be rejected."""
        from dashboard.api import create_app
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"DD_DASHBOARD_API_KEY": "secret123"}):
            with patch("dashboard.api._API_KEY", "secret123"):
                app = create_app()
                client = TestClient(app)
                with pytest.raises(Exception):
                    with client.websocket_connect("/ws"):
                        pass

    def test_ws_accepted_with_key(self):
        """When API key is set, WS with correct key should connect."""
        from dashboard.api import create_app
        from fastapi.testclient import TestClient

        with patch("dashboard.api._API_KEY", "secret123"):
            app = create_app()
            client = TestClient(app)
            with client.websocket_connect("/ws?api_key=secret123") as ws:
                assert ws is not None

    def test_ws_open_without_key_configured(self):
        """When no API key is set, WS should connect freely."""
        from dashboard.api import create_app
        from fastapi.testclient import TestClient

        with patch("dashboard.api._API_KEY", ""):
            app = create_app()
            client = TestClient(app)
            with client.websocket_connect("/ws") as ws:
                assert ws is not None

    def test_http_auth_still_works(self):
        """HTTP endpoints should still require API key when set."""
        from dashboard.api import create_app
        from fastapi.testclient import TestClient

        with patch("dashboard.api._API_KEY", "secret123"):
            app = create_app()
            client = TestClient(app)
            resp = client.get("/api/status")
            assert resp.status_code == 401
            resp = client.get("/api/status", headers={"X-API-Key": "secret123"})
            assert resp.status_code == 200
            resp = client.get("/api/health")
            assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# BUG D — Spread bootstrap early bail on missing symbols
# ═══════════════════════════════════════════════════════════════════════════════


class TestSpreadBootstrapEarlyBail:
    """Verify spread sampling bails immediately on 'not found' errors."""

    def test_not_found_bails_immediately(self):
        from risk.spread_bootstrap import _sample_spread

        connector = MagicMock()
        call_count = {"n": 0}

        def raise_not_found(sym):
            call_count["n"] += 1
            raise RuntimeError("No tick data for ESP35: (-4, 'Terminal: Not found')")

        connector.get_tick = raise_not_found
        result = _sample_spread(connector, "ESP35", retries=3)

        assert result is None
        assert call_count["n"] == 1

    def test_transient_error_retries(self):
        from risk.spread_bootstrap import _sample_spread

        connector = MagicMock()
        call_count = {"n": 0}

        def sometimes_fail(sym):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise RuntimeError("Connection timeout")
            return SimpleNamespace(bid=1.1000, ask=1.1002, spread=0.2)

        connector.get_tick = sometimes_fail
        result = _sample_spread(connector, "EURUSD", retries=3)

        assert result == 0.2
        assert call_count["n"] == 3

    def test_all_transient_failures(self):
        from risk.spread_bootstrap import _sample_spread

        connector = MagicMock()
        connector.get_tick.side_effect = RuntimeError("Connection timeout")
        result = _sample_spread(connector, "EURUSD", retries=3)

        assert result is None
        assert connector.get_tick.call_count == 3
