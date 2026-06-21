"""
Tick freshness & positivity guard — regression tests.

Verifies that get_price on both MT5 and Deriv connectors rejects stale,
zero/negative, and invalid-timestamp ticks with RuntimeError (fail-closed),
while passing fresh positive ticks unchanged.
"""

import sys
import time as _time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ── Stub MetaTrader5 before any app import touches it ─────────────────────
sys.modules.setdefault("MetaTrader5", MagicMock())

from platforms.base_connector import TickData  # noqa: E402
from platforms.mt5.mt5_connector import MT5Connector  # noqa: E402
from platforms.deriv.deriv_connector import DerivConnector  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _now_epoch() -> int:
    return int(_time.time())


def _fresh_mt5_tick(bid=1.1000, ask=1.1002, epoch=None):
    return SimpleNamespace(
        bid=bid,
        ask=ask,
        time=epoch if epoch is not None else _now_epoch(),
    )


def _make_mt5(max_tick_age_seconds=120.0):
    mock_mt5 = MagicMock()
    mock_mt5.last_error.return_value = (0, "ok")
    with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
        c = MT5Connector.__new__(MT5Connector)
        c._connected = True
        c._connection_time = None
        c._mapper = MagicMock()
        c._mapper.to_broker.return_value = "EURUSD"
        c._symbol_cache = {}
        c._max_tick_age_seconds = max_tick_age_seconds
    return c, mock_mt5


def _make_deriv(max_tick_age_seconds=120.0):
    c = DerivConnector.__new__(DerivConnector)
    c._connected = True
    c._reconnecting = False
    c._ws = object()
    c._mapper = MagicMock()
    c._mapper.to_broker.return_value = "R_75"
    c._sync_send = MagicMock()
    c._max_tick_age_seconds = max_tick_age_seconds
    return c


def _deriv_tick_response(quote=1500.0, epoch=None):
    return {
        "history": {
            "prices": [quote],
            "times": [epoch if epoch is not None else _now_epoch()],
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# MT5 — fresh / positive tick passes
# ═══════════════════════════════════════════════════════════════════════════

class TestMT5FreshTick:

    @patch("platforms.mt5.mt5_connector.mt5")
    def test_fresh_positive_tick_returns_tickdata(self, mock_mt5):
        c, _ = _make_mt5()
        mock_mt5.symbol_info_tick.return_value = _fresh_mt5_tick()
        with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
            result = c.get_price("EURUSD")
        assert isinstance(result, TickData)
        assert result.bid == 1.1000
        assert result.ask == 1.1002


# ═══════════════════════════════════════════════════════════════════════════
# MT5 — stale tick rejected
# ═══════════════════════════════════════════════════════════════════════════

class TestMT5StaleTick:

    @patch("platforms.mt5.mt5_connector.mt5")
    def test_stale_tick_raises(self, mock_mt5):
        c, _ = _make_mt5(max_tick_age_seconds=60.0)
        stale_epoch = _now_epoch() - 300
        mock_mt5.symbol_info_tick.return_value = _fresh_mt5_tick(epoch=stale_epoch)
        with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
            with pytest.raises(RuntimeError, match="Stale tick"):
                c.get_price("EURUSD")


# ═══════════════════════════════════════════════════════════════════════════
# MT5 — zero / negative bid/ask rejected
# ═══════════════════════════════════════════════════════════════════════════

class TestMT5NonPositiveTick:

    @patch("platforms.mt5.mt5_connector.mt5")
    def test_zero_bid_raises(self, mock_mt5):
        c, _ = _make_mt5()
        mock_mt5.symbol_info_tick.return_value = _fresh_mt5_tick(bid=0.0, ask=1.10)
        with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
            with pytest.raises(RuntimeError, match="Non-positive tick"):
                c.get_price("EURUSD")

    @patch("platforms.mt5.mt5_connector.mt5")
    def test_negative_ask_raises(self, mock_mt5):
        c, _ = _make_mt5()
        mock_mt5.symbol_info_tick.return_value = _fresh_mt5_tick(bid=1.10, ask=-0.5)
        with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
            with pytest.raises(RuntimeError, match="Non-positive tick"):
                c.get_price("EURUSD")


# ═══════════════════════════════════════════════════════════════════════════
# MT5 — invalid timestamp (epoch <= 0) rejected
# ═══════════════════════════════════════════════════════════════════════════

class TestMT5InvalidTimestamp:

    @patch("platforms.mt5.mt5_connector.mt5")
    def test_zero_epoch_raises(self, mock_mt5):
        c, _ = _make_mt5()
        mock_mt5.symbol_info_tick.return_value = _fresh_mt5_tick(epoch=0)
        with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
            with pytest.raises(RuntimeError, match="Invalid tick timestamp"):
                c.get_price("EURUSD")

    @patch("platforms.mt5.mt5_connector.mt5")
    def test_negative_epoch_raises(self, mock_mt5):
        c, _ = _make_mt5()
        mock_mt5.symbol_info_tick.return_value = _fresh_mt5_tick(epoch=-100)
        with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
            with pytest.raises(RuntimeError, match="Invalid tick timestamp"):
                c.get_price("EURUSD")


# ═══════════════════════════════════════════════════════════════════════════
# MT5 — configurable max age
# ═══════════════════════════════════════════════════════════════════════════

class TestMT5ConfigurableAge:

    @patch("platforms.mt5.mt5_connector.mt5")
    def test_custom_age_allows_older_tick(self, mock_mt5):
        c, _ = _make_mt5(max_tick_age_seconds=600.0)
        old_epoch = _now_epoch() - 300
        mock_mt5.symbol_info_tick.return_value = _fresh_mt5_tick(epoch=old_epoch)
        with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
            result = c.get_price("EURUSD")
        assert isinstance(result, TickData)

    @patch("platforms.mt5.mt5_connector.mt5")
    def test_env_override(self, mock_mt5):
        with patch.dict("os.environ", {"MAX_TICK_AGE_SECONDS": "10"}):
            c = MT5Connector.__new__(MT5Connector)
            c._connected = True
            c._connection_time = None
            c._mapper = MagicMock()
            c._mapper.to_broker.return_value = "EURUSD"
            c._symbol_cache = {}
            c._max_tick_age_seconds = float("10")
        old_epoch = _now_epoch() - 30
        mock_mt5.symbol_info_tick.return_value = _fresh_mt5_tick(epoch=old_epoch)
        with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
            with pytest.raises(RuntimeError, match="Stale tick"):
                c.get_price("EURUSD")


# ═══════════════════════════════════════════════════════════════════════════
# Deriv — fresh / positive tick passes
# ═══════════════════════════════════════════════════════════════════════════

class TestDerivFreshTick:

    def test_fresh_positive_tick_returns_tickdata(self):
        c = _make_deriv()
        c._sync_send.return_value = _deriv_tick_response(quote=1500.0)
        result = c.get_price("R_75")
        assert isinstance(result, TickData)
        assert result.bid == 1500.0
        assert result.ask == 1500.0
        assert result.spread == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Deriv — stale tick rejected
# ═══════════════════════════════════════════════════════════════════════════

class TestDerivStaleTick:

    def test_stale_tick_raises(self):
        c = _make_deriv(max_tick_age_seconds=60.0)
        stale_epoch = _now_epoch() - 300
        c._sync_send.return_value = _deriv_tick_response(epoch=stale_epoch)
        with pytest.raises(RuntimeError, match="Stale tick"):
            c.get_price("R_75")


# ═══════════════════════════════════════════════════════════════════════════
# Deriv — zero / negative quote rejected
# ═══════════════════════════════════════════════════════════════════════════

class TestDerivNonPositiveQuote:

    def test_zero_quote_raises(self):
        c = _make_deriv()
        c._sync_send.return_value = _deriv_tick_response(quote=0.0)
        with pytest.raises(RuntimeError, match="Non-positive tick"):
            c.get_price("R_75")

    def test_negative_quote_raises(self):
        c = _make_deriv()
        c._sync_send.return_value = _deriv_tick_response(quote=-5.0)
        with pytest.raises(RuntimeError, match="Non-positive tick"):
            c.get_price("R_75")


# ═══════════════════════════════════════════════════════════════════════════
# Deriv — invalid timestamp (epoch <= 0) rejected
# ═══════════════════════════════════════════════════════════════════════════

class TestDerivInvalidTimestamp:

    def test_zero_epoch_raises(self):
        c = _make_deriv()
        c._sync_send.return_value = _deriv_tick_response(epoch=0)
        with pytest.raises(RuntimeError, match="Invalid tick timestamp"):
            c.get_price("R_75")

    def test_negative_epoch_raises(self):
        c = _make_deriv()
        c._sync_send.return_value = _deriv_tick_response(epoch=-100)
        with pytest.raises(RuntimeError, match="Invalid tick timestamp"):
            c.get_price("R_75")


# ═══════════════════════════════════════════════════════════════════════════
# Deriv — configurable max age
# ═══════════════════════════════════════════════════════════════════════════

class TestDerivConfigurableAge:

    def test_custom_age_allows_older_tick(self):
        c = _make_deriv(max_tick_age_seconds=600.0)
        old_epoch = _now_epoch() - 300
        c._sync_send.return_value = _deriv_tick_response(epoch=old_epoch)
        result = c.get_price("R_75")
        assert isinstance(result, TickData)

    def test_env_override_rejects_older_tick(self):
        with patch.dict("os.environ", {"MAX_TICK_AGE_SECONDS": "10"}):
            c = DerivConnector.__new__(DerivConnector)
            c._connected = True
            c._reconnecting = False
            c._ws = object()
            c._mapper = MagicMock()
            c._mapper.to_broker.return_value = "R_75"
            c._sync_send = MagicMock()
            c._max_tick_age_seconds = float("10")
        old_epoch = _now_epoch() - 30
        c._sync_send.return_value = _deriv_tick_response(epoch=old_epoch)
        with pytest.raises(RuntimeError, match="Stale tick"):
            c.get_price("R_75")


# ═══════════════════════════════════════════════════════════════════════════
# Config default
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskConfigDefault:

    def test_max_tick_age_defaults_120(self):
        from config import RiskConfig
        cfg = RiskConfig()
        assert cfg.max_tick_age_seconds == 120.0

    def test_env_override_applied_at_config_layer(self):
        """MAX_TICK_AGE_SECONDS is resolved once, explicitly, in RiskConfig —
        not silently inside each connector's __init__ (audit #14)."""
        from config import RiskConfig
        with patch.dict("os.environ", {"MAX_TICK_AGE_SECONDS": "45"}):
            cfg = RiskConfig()
        assert cfg.max_tick_age_seconds == 45.0

    def test_explicit_config_value_not_overridden_by_env(self):
        """An explicitly-configured non-default value wins over the env var —
        the env only fills in when the field is left at its default."""
        from config import RiskConfig
        with patch.dict("os.environ", {"MAX_TICK_AGE_SECONDS": "45"}):
            cfg = RiskConfig(max_tick_age_seconds=300.0)
        assert cfg.max_tick_age_seconds == 300.0

    def test_invalid_env_value_keeps_default(self):
        from config import RiskConfig
        with patch.dict("os.environ", {"MAX_TICK_AGE_SECONDS": "not-a-number"}):
            cfg = RiskConfig()
        assert cfg.max_tick_age_seconds == 120.0
