"""
Tests for PR-E: Deriv error surfacing + enriched empty-data diagnostics.

Verifies:
  1. get_account_info raises on error responses instead of returning $0.00.
  2. get_account_info still returns valid balances on success.
  3. get_ohlcv raises an enriched message on empty-but-no-error responses.
  4. get_price raises an enriched message on empty-but-no-error responses.

NOTE: CI is the runtime authority — local sandbox cannot run this suite
(requires websockets, pandas, loguru).
"""

from unittest.mock import MagicMock, patch


def _make_connector():
    """Create a DerivConnector with connection checks bypassed."""
    with patch.dict("sys.modules", {"websockets": MagicMock(), "websockets.client": MagicMock()}):
        from platforms.deriv.deriv_connector import DerivConnector
    conn = DerivConnector.__new__(DerivConnector)
    conn._connected = True
    conn._reconnecting = False
    conn._ws = MagicMock()
    conn._thread_lock = MagicMock()
    conn._thread_lock.__enter__ = MagicMock(return_value=None)
    conn._thread_lock.__exit__ = MagicMock(return_value=False)
    conn._mapper = MagicMock()
    conn._mapper.to_broker = MagicMock(side_effect=lambda s: s)
    conn._last_history_request = 0.0
    return conn


# ═══════════════════════════════════════════════════════════════════════════════
# 1. get_account_info — error surfacing
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetAccountInfoErrorSurfacing:

    def test_error_response_raises(self):
        conn = _make_connector()
        error_resp = {
            "error": {
                "code": "AuthorizationRequired",
                "message": "Please log in.",
            }
        }
        conn._sync_send = MagicMock(return_value=error_resp)

        try:
            conn.get_account_info()
            assert False, "Expected RuntimeError"
        except RuntimeError as exc:
            msg = str(exc)
            assert "AuthorizationRequired" in msg
            assert "Please log in." in msg

    def test_valid_balance_returned(self):
        conn = _make_connector()
        ok_resp = {
            "balance": {
                "balance": 205.44,
                "currency": "USD",
            }
        }
        conn._sync_send = MagicMock(return_value=ok_resp)
        info = conn.get_account_info()
        assert info.balance == 205.44
        assert info.currency == "USD"

    def test_subscribe_zero_removed(self):
        """The balance request must not include subscribe=0."""
        conn = _make_connector()
        conn._sync_send = MagicMock(return_value={
            "balance": {"balance": 100.0, "currency": "USD"}
        })
        conn.get_account_info()
        sent_payload = conn._sync_send.call_args[0][0]
        assert "subscribe" not in sent_payload


# ═══════════════════════════════════════════════════════════════════════════════
# 2. get_ohlcv — enriched empty-data message
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetOhlcvEnrichedDiagnostics:

    def test_empty_candles_message_contains_symbol(self):
        conn = _make_connector()
        conn.symbol_map = MagicMock(return_value="1HZ100V")
        conn.timeframe_map = MagicMock(return_value=14400)
        empty_resp = {
            "candles": [],
            "echo_req": {"ticks_history": "1HZ100V", "granularity": 14400},
        }
        conn._sync_send = MagicMock(return_value=empty_resp)

        try:
            conn.get_ohlcv("V100_1S", "H4")
            assert False, "Expected RuntimeError"
        except RuntimeError as exc:
            msg = str(exc)
            assert "1HZ100V" in msg
            assert "H4" in msg
            assert "granularity=14400" in msg
            assert "echo_req" in msg


# ═══════════════════════════════════════════════════════════════════════════════
# 3. get_price — enriched empty-data message
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetPriceEnrichedDiagnostics:

    def test_empty_ticks_message_contains_symbol(self):
        conn = _make_connector()
        conn.symbol_map = MagicMock(return_value="1HZ100V")
        empty_resp = {
            "history": {"prices": [], "times": []},
            "echo_req": {"ticks_history": "1HZ100V"},
        }
        conn._sync_send = MagicMock(return_value=empty_resp)

        try:
            conn.get_price("V100_1S")
            assert False, "Expected RuntimeError"
        except RuntimeError as exc:
            msg = str(exc)
            assert "1HZ100V" in msg
            assert "echo_req" in msg
