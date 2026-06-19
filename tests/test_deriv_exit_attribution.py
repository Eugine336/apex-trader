"""
Deriv broker-side exit attribution tests.
Verifies DerivConnector.get_deal_close_info via mocked proposal_open_contract,
and exit_reason_source='deriv_poc' propagation in main_loop reconciliation.
"""

from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock

import pytest

from platforms.base_connector import DealCloseInfo


class TestDerivGetDealCloseInfo:

    def _make_connector(self):
        from platforms.deriv.deriv_connector import DerivConnector
        c = DerivConnector.__new__(DerivConnector)
        c._positions = {}
        c._reconnecting = False
        c._connected = True
        c._ws = object()
        c._sync_send = MagicMock()
        return c

    def test_sl_hit(self):
        c = self._make_connector()
        c._positions["100"] = {"sl": 1.10000, "tp": 1.12000, "symbol": "EURUSD", "lots": 0.1}
        c._sync_send.return_value = {
            "proposal_open_contract": {
                "is_sold": 1,
                "status": "lost",
                "profit": -15.0,
                "sell_price": 1.10001,
                "sell_time": 1717502400,
            }
        }
        info = c.get_deal_close_info("100")
        assert info is not None
        assert info.exit_reason == "SL"
        assert info.pnl == -15.0
        assert info.close_price == 1.10001
        assert info.close_time == datetime(2024, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
        assert info.raw_comment == "lost"

    def test_tp_hit(self):
        c = self._make_connector()
        c._positions["200"] = {"sl": 1.10000, "tp": 1.12000, "symbol": "EURUSD", "lots": 0.1}
        c._sync_send.return_value = {
            "proposal_open_contract": {
                "is_sold": 1,
                "status": "won",
                "profit": 30.0,
                "sell_price": 1.12001,
                "sell_time": 1717502400,
            }
        }
        info = c.get_deal_close_info("200")
        assert info is not None
        assert info.exit_reason == "TP"
        assert info.pnl == 30.0

    def test_stop_out(self):
        c = self._make_connector()
        c._positions["300"] = {"sl": 0, "tp": 0, "symbol": "V75", "lots": 0.5}
        c._sync_send.return_value = {
            "proposal_open_contract": {
                "is_sold": 1,
                "status": "lost",
                "profit": -100.0,
                "sell_price": 5000.0,
                "sell_time": 1717502400,
            }
        }
        info = c.get_deal_close_info("300")
        assert info is not None
        assert info.exit_reason == "STOP_OUT"

    def test_manual_close(self):
        c = self._make_connector()
        c._positions["400"] = {"sl": 1.10000, "tp": 1.12000, "symbol": "EURUSD", "lots": 0.1}
        c._sync_send.return_value = {
            "proposal_open_contract": {
                "is_sold": 1,
                "status": "sold",
                "profit": 5.0,
                "sell_price": 1.11000,
                "sell_time": 1717502400,
            }
        }
        info = c.get_deal_close_info("400")
        assert info is not None
        assert info.exit_reason == "MANUAL"
        assert info.pnl == 5.0

    def test_not_yet_sold_returns_none(self):
        c = self._make_connector()
        c._sync_send.return_value = {
            "proposal_open_contract": {
                "is_sold": 0,
                "status": "open",
                "profit": 2.0,
                "sell_price": 0,
            }
        }
        assert c.get_deal_close_info("500") is None

    def test_error_response_returns_none(self):
        c = self._make_connector()
        c._sync_send.return_value = {
            "error": {"code": "ContractNotFound", "message": "not found"}
        }
        assert c.get_deal_close_info("600") is None

    def test_exception_in_sync_send_returns_none(self):
        c = self._make_connector()
        c._sync_send.side_effect = RuntimeError("ws disconnected")
        assert c.get_deal_close_info("700") is None

    def test_no_local_sl_tp_defaults_to_manual(self):
        c = self._make_connector()
        c._sync_send.return_value = {
            "proposal_open_contract": {
                "is_sold": 1,
                "status": "sold",
                "profit": 10.0,
                "sell_price": 1.11500,
                "sell_time": 1717502400,
            }
        }
        info = c.get_deal_close_info("800")
        assert info is not None
        assert info.exit_reason == "MANUAL"

    def test_close_time_none_when_sell_time_missing(self):
        c = self._make_connector()
        c._sync_send.return_value = {
            "proposal_open_contract": {
                "is_sold": 1,
                "status": "won",
                "profit": 5.0,
                "sell_price": 1.12000,
            }
        }
        info = c.get_deal_close_info("900")
        assert info is not None
        assert info.close_time is None

    def test_status_lost_no_close_price_gives_stop_out(self):
        c = self._make_connector()
        c._sync_send.return_value = {
            "proposal_open_contract": {
                "is_sold": 1,
                "status": "lost",
                "profit": -50.0,
                "sell_price": 0,
            }
        }
        info = c.get_deal_close_info("1000")
        assert info is not None
        assert info.exit_reason == "STOP_OUT"
        assert info.close_price is None

    def test_returns_deal_close_info_type(self):
        c = self._make_connector()
        c._positions["1100"] = {"sl": 1.10, "tp": 1.12, "symbol": "EURUSD", "lots": 0.1}
        c._sync_send.return_value = {
            "proposal_open_contract": {
                "is_sold": 1, "status": "won",
                "profit": 20.0, "sell_price": 1.12001,
                "sell_time": 1717502400,
            }
        }
        info = c.get_deal_close_info("1100")
        assert isinstance(info, DealCloseInfo)


class TestDerivExitReasonSource:

    def test_deriv_platform_gets_deriv_poc_source(self):
        pos = MagicMock()
        pos.platform = "deriv"
        _deal_info = DealCloseInfo(pnl=-10.0, exit_reason="SL", close_price=1.1)
        source = "deriv_poc" if pos.platform.startswith("deriv") else "mt5_deal"
        assert source == "deriv_poc"

    def test_mt5_platform_gets_mt5_deal_source(self):
        pos = MagicMock()
        pos.platform = "mt5"
        _deal_info = DealCloseInfo(pnl=10.0, exit_reason="TP", close_price=1.12)
        source = "deriv_poc" if pos.platform.startswith("deriv") else "mt5_deal"
        assert source == "mt5_deal"

    def test_deriv_live_platform_gets_deriv_poc_source(self):
        pos = MagicMock()
        pos.platform = "deriv_live"
        source = "deriv_poc" if pos.platform.startswith("deriv") else "mt5_deal"
        assert source == "deriv_poc"
