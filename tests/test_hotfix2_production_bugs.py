"""
APEX TRADER — Hotfix 2: Production Bug Tests
Tests for multiplier discovery, Deriv fill_price fix, and timeframe warning dedup.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from platforms.base_connector import OrderResult, TickData
from platforms.deriv.deriv_connector import DerivConnector
from platforms.platform_manager import PlatformManager
from platforms.mt5.mt5_connector import MT5Connector


# ═══════════════════════════════════════════════════════════════════════
# Bug 1 — Multiplier Discovery
# ═══════════════════════════════════════════════════════════════════════

class TestMultiplierDiscovery:

    def _make_connector(self):
        c = DerivConnector.__new__(DerivConnector)
        c._api_token = "tok"
        c._app_id = "999"
        c._ws = None
        c._connected = False
        c._reconnecting = False
        c._authorized = False
        c._account_id = ""
        c._req_id = 0
        c._positions = {}
        c._mapper = MagicMock()
        c._discovered_multipliers = {}
        c._loop = MagicMock()
        c._loop_thread = MagicMock()
        c._lock = MagicMock()
        c._thread_lock = MagicMock()
        c._last_history_request = 0.0
        return c

    def test_discovered_multipliers_initialized_empty(self):
        c = DerivConnector()
        assert c._discovered_multipliers == {}

    def test_get_multiplier_prefers_discovered(self):
        c = self._make_connector()
        c._discovered_multipliers["1HZ50V"] = [80, 200, 400, 600, 800]
        result = c._get_multiplier("1HZ50V")
        assert result in [80, 200, 400, 600, 800]

    def test_get_multiplier_discovered_snaps_to_nearest(self):
        c = self._make_connector()
        c._discovered_multipliers["1HZ50V"] = [80, 200, 400, 600, 800]
        result = c._get_multiplier("1HZ50V")
        assert result == 200

    def test_get_multiplier_falls_back_to_json_when_not_discovered(self):
        c = self._make_connector()
        result = c._get_multiplier("1HZ100V")
        assert isinstance(result, int)
        assert result > 0

    def test_get_multiplier_handles_missing_config(self):
        c = self._make_connector()
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = c._get_multiplier("UNKNOWN_SYMBOL")
        assert result == 1000

    @pytest.mark.asyncio
    async def test_discover_multipliers_parses_response(self):
        c = self._make_connector()

        mock_config = {
            "multipliers": {
                "1HZ50V": {"default": 100, "accepted": [40, 100, 200]},
            }
        }

        mock_response = {
            "contracts_for": {
                "available": [
                    {
                        "contract_type": "MULTUP",
                        "multipliers": [80, 200, 400, 600, 800],
                    },
                    {
                        "contract_type": "MULTDOWN",
                        "multipliers": [80, 200, 400, 600, 800],
                    },
                    {
                        "contract_type": "CALL",
                    },
                ]
            }
        }

        import asyncio
        c._lock = asyncio.Lock()
        c._send = AsyncMock(return_value=mock_response)

        with patch("builtins.open", MagicMock()):
            with patch("json.load", return_value=mock_config):
                await c._discover_multipliers()

        assert "1HZ50V" in c._discovered_multipliers
        assert c._discovered_multipliers["1HZ50V"] == [80, 200, 400, 600, 800]

    @pytest.mark.asyncio
    async def test_discover_multipliers_skips_default_key(self):
        c = self._make_connector()

        mock_config = {
            "multipliers": {
                "_default": {"default": 1000, "accepted": [400, 1000]},
                "1HZ75V": {"default": 100, "accepted": [40, 100]},
            }
        }

        import asyncio
        c._lock = asyncio.Lock()
        c._send = AsyncMock(return_value={
            "contracts_for": {
                "available": [
                    {"contract_type": "MULTUP", "multipliers": [40, 100, 200]},
                ]
            }
        })

        with patch("builtins.open", MagicMock()):
            with patch("json.load", return_value=mock_config):
                await c._discover_multipliers()

        assert "_default" not in c._discovered_multipliers
        assert "1HZ75V" in c._discovered_multipliers

    @pytest.mark.asyncio
    async def test_discover_multipliers_handles_api_error(self):
        c = self._make_connector()

        mock_config = {
            "multipliers": {"1HZ50V": {"default": 100, "accepted": [40, 100]}},
        }

        import asyncio
        c._lock = asyncio.Lock()
        c._send = AsyncMock(return_value={"error": {"message": "Not found"}})

        with patch("builtins.open", MagicMock()):
            with patch("json.load", return_value=mock_config):
                await c._discover_multipliers()

        assert "1HZ50V" not in c._discovered_multipliers

    @pytest.mark.asyncio
    async def test_discover_multipliers_handles_config_missing(self):
        c = self._make_connector()

        import asyncio
        c._lock = asyncio.Lock()

        with patch("builtins.open", side_effect=FileNotFoundError):
            await c._discover_multipliers()

        assert c._discovered_multipliers == {}


# ═══════════════════════════════════════════════════════════════════════
# Bug 2 — Deriv fill_price / slippage fix
# ═══════════════════════════════════════════════════════════════════════

class TestDerivFillPrice:

    def test_order_result_fill_price_is_instrument_price(self):
        """fill_price should be the instrument price, not the stake (buy_price)."""
        c = DerivConnector.__new__(DerivConnector)
        c._connected = True
        c._reconnecting = False
        c._ws = MagicMock()
        c._mapper = MagicMock()
        c._mapper.to_broker.return_value = "1HZ50V"
        c._discovered_multipliers = {"1HZ50V": [80, 200, 400, 600, 800]}
        c._positions = {}
        c._thread_lock = MagicMock()
        c._last_history_request = 0.0
        c._req_id = 0

        instrument_price = 309500.0
        buy_price = 193.40

        tick_response = {
            "history": {"prices": [instrument_price], "times": [1717200000]},
        }
        buy_response = {
            "buy": {"contract_id": 12345, "buy_price": buy_price},
        }

        call_count = 0
        def mock_sync_send(payload):
            nonlocal call_count
            call_count += 1
            if "ticks_history" in payload:
                return tick_response
            return buy_response

        c._sync_send = mock_sync_send

        result = c.place_order(
            "V50_1S", "SELL", 0.01, 309600.0, 308000.0,
            stake_usd=193.40, multiplier=800,
        )

        assert result.success is True
        assert result.fill_price == instrument_price
        assert result.slippage_pips == 0.0
        assert result.requested_price == instrument_price

    def test_order_result_slippage_is_zero_for_deriv(self):
        """Deriv contracts are purchases, not market orders — slippage is always 0."""
        c = DerivConnector.__new__(DerivConnector)
        c._connected = True
        c._reconnecting = False
        c._ws = MagicMock()
        c._mapper = MagicMock()
        c._mapper.to_broker.return_value = "1HZ100V"
        c._discovered_multipliers = {}
        c._positions = {}
        c._thread_lock = MagicMock()
        c._last_history_request = 0.0
        c._req_id = 0

        def mock_sync_send(payload):
            if "ticks_history" in payload:
                return {"history": {"prices": [850.0], "times": [1717200000]}}
            return {"buy": {"contract_id": 99, "buy_price": 50.0}}

        c._sync_send = mock_sync_send

        result = c.place_order(
            "V100_1S", "SELL", 0.01, 860.0, 840.0,
            stake_usd=50.0, multiplier=100,
        )

        assert result.slippage_pips == 0.0


# ═══════════════════════════════════════════════════════════════════════
# Bug 3 — Failed timeframe warning deduplication
# ═══════════════════════════════════════════════════════════════════════

class TestFailedTimeframeDedup:

    def _make_manager(self):
        mgr = PlatformManager.__new__(PlatformManager)
        mgr.config = MagicMock()
        mgr.mt5 = MagicMock(spec=MT5Connector)
        mgr.deriv = MagicMock(spec=DerivConnector)
        mgr._mt5_connected = True
        mgr._deriv_connected = True
        mgr._mt5_was_connected = True
        mgr._deriv_was_connected = True
        mgr._reconnect_delays = [5, 10, 20, 40, 60]
        mgr._mt5_reconnect_attempt = 0
        mgr._deriv_reconnect_attempt = 0
        mgr._mt5_next_reconnect = 0.0
        mgr._deriv_next_reconnect = 0.0
        return mgr

    def test_first_no_candle_data_logs_warning(self):
        mgr = self._make_manager()
        mgr.deriv.get_ohlcv.side_effect = RuntimeError("No candle data for 1HZ50V/H4")

        with patch.object(PlatformManager, "get_connector", return_value=mgr.deriv):
            data = mgr.fetch_market_data("V50_1S", ["H4"])

        assert ("V50_1S", "H4") in mgr._failed_timeframes
        assert data == {}

    def test_second_no_candle_data_suppressed_to_debug(self):
        mgr = self._make_manager()
        mgr.deriv.get_ohlcv.side_effect = RuntimeError("No candle data for 1HZ50V/H4")

        with patch.object(PlatformManager, "get_connector", return_value=mgr.deriv):
            mgr.fetch_market_data("V50_1S", ["H4"])
            mgr.fetch_market_data("V50_1S", ["H4"])

        assert ("V50_1S", "H4") in mgr._failed_timeframes

    def test_different_timeframes_tracked_separately(self):
        mgr = self._make_manager()

        call_count = {"H4": 0, "H1": 0}
        def side_effect(sym, tf, count):
            if tf in ("H4", "H1"):
                raise RuntimeError(f"No candle data for test/{tf}")
            import pandas as pd
            return pd.DataFrame({"close": [1.0]})

        mgr.deriv.get_ohlcv.side_effect = side_effect

        with patch.object(PlatformManager, "get_connector", return_value=mgr.deriv):
            mgr.fetch_market_data("V50_1S", ["H4", "H1", "M5"])

        assert ("V50_1S", "H4") in mgr._failed_timeframes
        assert ("V50_1S", "H1") in mgr._failed_timeframes

    def test_non_candle_errors_always_warn(self):
        mgr = self._make_manager()
        mgr.deriv.get_ohlcv.side_effect = RuntimeError("Some other error")

        with patch.object(PlatformManager, "get_connector", return_value=mgr.deriv):
            mgr.fetch_market_data("V50_1S", ["H4"])
            mgr.fetch_market_data("V50_1S", ["H4"])

        assert ("V50_1S", "H4") not in mgr._failed_timeframes

    def test_failed_timeframes_set_initialized_on_demand(self):
        mgr = self._make_manager()
        assert not hasattr(mgr, "_failed_timeframes")

        mgr.deriv.get_ohlcv.side_effect = RuntimeError("No candle data for test/H4")
        with patch.object(PlatformManager, "get_connector", return_value=mgr.deriv):
            mgr.fetch_market_data("V50_1S", ["H4"])

        assert hasattr(mgr, "_failed_timeframes")
