"""
Phase 3 — True exit attribution tests.
Verifies MT5 deal-reason mapping, DealCloseInfo construction,
TRADE_CLOSE event emission, and exit_reason_source propagation.
"""

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


from persistence.domain_events import TRADE_CLOSE
from persistence.event_store import EventStore
from platforms.base_connector import DealCloseInfo


# ── MT5 deal reason mapping tests ───────────────────────────────────────────

class TestMT5DealReasonMapping:
    """Verify the DEAL_REASON integer → human reason mapping."""

    def _get_map(self):
        from platforms.mt5.mt5_connector import _MT5_DEAL_REASON_MAP
        return _MT5_DEAL_REASON_MAP

    def test_deal_reason_sl(self):
        m = self._get_map()
        assert m[4] == "SL"

    def test_deal_reason_tp(self):
        m = self._get_map()
        assert m[5] == "TP"

    def test_deal_reason_stop_out(self):
        m = self._get_map()
        assert m[6] == "STOP_OUT"

    def test_deal_reason_client_is_manual(self):
        m = self._get_map()
        assert m[0] == "MANUAL"

    def test_deal_reason_mobile_is_manual(self):
        m = self._get_map()
        assert m[1] == "MANUAL"

    def test_deal_reason_web_is_manual(self):
        m = self._get_map()
        assert m[2] == "MANUAL"

    def test_deal_reason_expert_is_algo(self):
        m = self._get_map()
        assert m[3] == "ALGO"

    def test_unknown_reason_not_in_map(self):
        m = self._get_map()
        assert 999 not in m
        assert m.get(999, "BROKER_CLOSED_UNKNOWN") == "BROKER_CLOSED_UNKNOWN"


# ── DealCloseInfo dataclass tests ───────────────────────────────────────────

class TestDealCloseInfo:

    def test_construction_with_all_fields(self):
        info = DealCloseInfo(
            pnl=-12.50,
            exit_reason="SL",
            raw_reason_code=4,
            raw_comment="sl",
            close_price=1.10234,
            close_time=datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert info.exit_reason == "SL"
        assert info.raw_reason_code == 4
        assert info.raw_comment == "sl"
        assert info.close_price == 1.10234
        assert info.pnl == -12.50

    def test_construction_minimal(self):
        info = DealCloseInfo(pnl=0.0, exit_reason="BROKER_CLOSED_UNKNOWN")
        assert info.raw_reason_code is None
        assert info.raw_comment is None
        assert info.close_price is None
        assert info.close_time is None

    def test_unknown_reason_preserves_raw(self):
        info = DealCloseInfo(
            pnl=5.0,
            exit_reason="BROKER_CLOSED_UNKNOWN",
            raw_reason_code=42,
            raw_comment="mystery",
        )
        assert info.exit_reason == "BROKER_CLOSED_UNKNOWN"
        assert info.raw_reason_code == 42
        assert info.raw_comment == "mystery"


# ── MT5 get_deal_close_info integration tests (mocked MT5) ─────────────────

@dataclass
class _FakeDeal:
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0
    entry: int = 1  # DEAL_ENTRY_OUT
    reason: int = 4  # DEAL_REASON_SL
    comment: str = "sl"
    price: float = 1.10234
    time: int = 1717502400  # 2024-06-04T12:00:00Z


class TestMT5GetDealCloseInfo:

    def _make_connector(self):
        from platforms.mt5.mt5_connector import MT5Connector
        c = MT5Connector.__new__(MT5Connector)
        c._connected = True
        c._symbol_cache = {}
        c._mapper = None
        return c

    @patch("platforms.mt5.mt5_connector._MT5_AVAILABLE", True)
    @patch("platforms.mt5.mt5_connector.mt5")
    def test_sl_deal(self, mock_mt5):
        mock_mt5.history_deals_get.return_value = [
            _FakeDeal(profit=-12.50, entry=0, reason=0),  # entry deal
            _FakeDeal(profit=-12.50, entry=1, reason=4, comment="sl", price=1.10234, time=1717502400),
        ]
        c = self._make_connector()
        info = c.get_deal_close_info("12345")
        assert info is not None
        assert info.exit_reason == "SL"
        assert info.raw_reason_code == 4
        assert info.raw_comment == "sl"
        assert info.close_price == 1.10234

    @patch("platforms.mt5.mt5_connector._MT5_AVAILABLE", True)
    @patch("platforms.mt5.mt5_connector.mt5")
    def test_tp_deal(self, mock_mt5):
        mock_mt5.history_deals_get.return_value = [
            _FakeDeal(profit=50.0, entry=1, reason=5, comment="tp", price=1.11000),
        ]
        c = self._make_connector()
        info = c.get_deal_close_info("12345")
        assert info is not None
        assert info.exit_reason == "TP"
        assert info.raw_reason_code == 5

    @patch("platforms.mt5.mt5_connector._MT5_AVAILABLE", True)
    @patch("platforms.mt5.mt5_connector.mt5")
    def test_unknown_reason(self, mock_mt5):
        mock_mt5.history_deals_get.return_value = [
            _FakeDeal(profit=0.0, entry=1, reason=99, comment="custom"),
        ]
        c = self._make_connector()
        info = c.get_deal_close_info("12345")
        assert info is not None
        assert info.exit_reason == "BROKER_CLOSED_UNKNOWN"
        assert info.raw_reason_code == 99
        assert info.raw_comment == "custom"

    @patch("platforms.mt5.mt5_connector._MT5_AVAILABLE", True)
    @patch("platforms.mt5.mt5_connector.mt5")
    def test_no_deals_returns_none(self, mock_mt5):
        mock_mt5.history_deals_get.return_value = None
        c = self._make_connector()
        assert c.get_deal_close_info("12345") is None

    @patch("platforms.mt5.mt5_connector._MT5_AVAILABLE", True)
    @patch("platforms.mt5.mt5_connector.mt5")
    def test_empty_deals_returns_none(self, mock_mt5):
        mock_mt5.history_deals_get.return_value = []
        c = self._make_connector()
        assert c.get_deal_close_info("12345") is None

    @patch("platforms.mt5.mt5_connector._MT5_AVAILABLE", True)
    @patch("platforms.mt5.mt5_connector.mt5")
    def test_exception_returns_none(self, mock_mt5):
        mock_mt5.history_deals_get.side_effect = RuntimeError("connection lost")
        c = self._make_connector()
        assert c.get_deal_close_info("12345") is None

    @patch("platforms.mt5.mt5_connector._MT5_AVAILABLE", True)
    @patch("platforms.mt5.mt5_connector.mt5")
    def test_pnl_aggregates_all_deals(self, mock_mt5):
        mock_mt5.history_deals_get.return_value = [
            _FakeDeal(profit=0.0, commission=-2.0, swap=0.0, fee=0.0, entry=0),
            _FakeDeal(profit=50.0, commission=-2.0, swap=-1.5, fee=0.0, entry=1, reason=5),
        ]
        c = self._make_connector()
        info = c.get_deal_close_info("12345")
        assert info is not None
        assert info.pnl == 44.50  # (0-2+0+0) + (50-2-1.5+0)

    @patch("platforms.mt5.mt5_connector._MT5_AVAILABLE", True)
    @patch("platforms.mt5.mt5_connector.mt5")
    def test_fallback_to_last_deal_when_no_exit(self, mock_mt5):
        mock_mt5.history_deals_get.return_value = [
            _FakeDeal(profit=10.0, entry=0, reason=5, comment="tp", price=1.12),
        ]
        c = self._make_connector()
        info = c.get_deal_close_info("12345")
        assert info is not None
        assert info.exit_reason == "TP"


# ── Deriv close path — must be "unknown", not "BROKER_CLOSED" ──────────────

class TestDerivCloseInfo:

    def test_deriv_returns_none(self):
        from platforms.base_connector import BaseConnector
        class DummyConnector(BaseConnector):
            def connect(self) -> bool: return True
            def disconnect(self): pass
            def is_connected(self) -> bool: return True
            def get_account_info(self): return None
            def place_order(self, *a, **kw): pass
            def place_pending_order(self, *a, **kw): pass
            def modify_order(self, *a, **kw): pass
            def close_order(self, *a, **kw): pass
            def get_open_positions(self): return []
            def get_position_info(self, oid): return None
            def get_spread(self, s): return 0.0
            def get_tick(self, s): pass
            def get_price(self, s): pass
            def get_ohlcv(self, *a, **kw): pass
        c = DummyConnector()
        assert c.get_deal_close_info("12345") is None


# ── TRADE_CLOSE event emission tests ───────────────────────────────────────

class TestTradeCloseEventEmission:

    def _make_store(self, tmp_path):
        db_path = str(tmp_path / "test_events.db")
        store = EventStore(db_path=db_path, max_queue=1000)
        return store

    def _wait_drain(self, store, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if store._queue.empty():
                time.sleep(0.2)
                return
            time.sleep(0.05)

    def test_trade_close_event_has_exit_reason(self, tmp_path):
        store = self._make_store(tmp_path)
        try:
            store.emit(
                event_type=TRADE_CLOSE,
                severity="INFO",
                symbol="EURUSD",
                correlation_id="cycle-abc",
                source_module="platforms.main_loop",
                payload={
                    "order_id": "12345",
                    "direction": "BUY",
                    "entry_price": 1.10000,
                    "close_price": 1.09500,
                    "exit_reason": "SL",
                    "exit_reason_source": "mt5_deal",
                    "raw_broker_reason": 4,
                    "raw_broker_comment": "sl",
                    "pnl_pips": -50.0,
                    "pnl_dollars": -50.0,
                    "hold_seconds": 3600.0,
                    "lots": 0.1,
                    "platform": "mt5",
                },
            )
            self._wait_drain(store)
            conn = sqlite3.connect(str(tmp_path / "test_events.db"))
            rows = conn.execute(
                "SELECT event_type, symbol, correlation_id, payload_json FROM events WHERE event_type = ?",
                (TRADE_CLOSE,),
            ).fetchall()
            conn.close()
            assert len(rows) == 1
            assert rows[0][0] == TRADE_CLOSE
            assert rows[0][1] == "EURUSD"
            assert rows[0][2] == "cycle-abc"
            import json
            payload = json.loads(rows[0][3])
            assert payload["exit_reason"] == "SL"
            assert payload["exit_reason_source"] == "mt5_deal"
            assert payload["raw_broker_reason"] == 4
        finally:
            store.close()

    def test_trade_close_event_with_trade_manager_source(self, tmp_path):
        store = self._make_store(tmp_path)
        try:
            store.emit(
                event_type=TRADE_CLOSE,
                severity="INFO",
                symbol="GBPUSD",
                correlation_id="cycle-xyz",
                source_module="platforms.main_loop",
                payload={
                    "exit_reason": "Structure exit — bearish shift after 45min",
                    "exit_reason_source": "trade_manager",
                    "raw_broker_reason": None,
                    "raw_broker_comment": None,
                },
            )
            self._wait_drain(store)
            conn = sqlite3.connect(str(tmp_path / "test_events.db"))
            rows = conn.execute(
                "SELECT payload_json FROM events WHERE event_type = ?",
                (TRADE_CLOSE,),
            ).fetchall()
            conn.close()
            assert len(rows) == 1
            import json
            payload = json.loads(rows[0][0])
            assert payload["exit_reason_source"] == "trade_manager"
            assert payload["raw_broker_reason"] is None
        finally:
            store.close()

    def test_trade_close_event_deriv_unknown_source(self, tmp_path):
        store = self._make_store(tmp_path)
        try:
            store.emit(
                event_type=TRADE_CLOSE,
                severity="INFO",
                symbol="Volatility_75_Index",
                source_module="platforms.main_loop",
                payload={
                    "exit_reason": "BROKER_CLOSED_UNKNOWN",
                    "exit_reason_source": "unknown",
                    "raw_broker_reason": None,
                    "raw_broker_comment": None,
                },
            )
            self._wait_drain(store)
            conn = sqlite3.connect(str(tmp_path / "test_events.db"))
            rows = conn.execute(
                "SELECT payload_json FROM events WHERE event_type = ?",
                (TRADE_CLOSE,),
            ).fetchall()
            conn.close()
            assert len(rows) == 1
            import json
            payload = json.loads(rows[0][0])
            assert payload["exit_reason"] == "BROKER_CLOSED_UNKNOWN"
            assert payload["exit_reason_source"] == "unknown"
        finally:
            store.close()

    def test_trade_close_correlation_id_non_null(self, tmp_path):
        store = self._make_store(tmp_path)
        try:
            store.emit(
                event_type=TRADE_CLOSE,
                severity="INFO",
                symbol="XAUUSD",
                correlation_id="cycle-gold-123",
                source_module="platforms.main_loop",
                payload={"exit_reason": "TP", "exit_reason_source": "mt5_deal"},
            )
            self._wait_drain(store)
            conn = sqlite3.connect(str(tmp_path / "test_events.db"))
            rows = conn.execute(
                "SELECT correlation_id FROM events WHERE event_type = ?",
                (TRADE_CLOSE,),
            ).fetchall()
            conn.close()
            assert len(rows) == 1
            assert rows[0][0] == "cycle-gold-123"
        finally:
            store.close()


# ── PlatformManager proxy test ─────────────────────────────────────────────

class TestPlatformManagerProxy:

    def test_get_deal_close_info_delegates(self):
        from platforms.platform_manager import PlatformManager
        mock_connector = MagicMock()
        expected = DealCloseInfo(pnl=10.0, exit_reason="TP")
        mock_connector.get_deal_close_info.return_value = expected
        pm = PlatformManager.__new__(PlatformManager)
        pm.mt5_connectors = [mock_connector]
        pm._mt5_connected_flags = [True]
        pm.deriv = MagicMock()
        with patch.object(pm, "_connector_by_platform_str", return_value=mock_connector):
            result = pm.get_deal_close_info("12345", "mt5")
        assert result is expected
        mock_connector.get_deal_close_info.assert_called_once_with("12345")
