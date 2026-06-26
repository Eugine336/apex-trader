"""Position reporting must use canonical registry symbols, not broker aliases.

A GER40 trade closed at the broker (SL/TP/manual) is reported by MT5 under the
broker-native alias ``DE40``. Before the fix the close path recorded the trade
under ``DE40`` in the trade journal and per-symbol learners (PairLearner,
RegimeLearner, SessionLearner), orphaning it from the ``GER40`` registry key.

The connector now resolves the broker-native symbol to the internal registry
key in its position-reporting boundary, so every downstream consumer keys on
``GER40``. Order/modify/close are unaffected — they route off the ticket and the
raw broker position object, never ``PositionInfo.symbol``.
"""

from types import SimpleNamespace

import pytest

from brain.symbol_mapper import resolve_to_internal


def _fake_mt5_position(symbol: str) -> SimpleNamespace:
    """Minimal stand-in for a raw MT5 position object."""
    return SimpleNamespace(
        ticket=123456,
        symbol=symbol,
        type=0,  # ORDER_TYPE_BUY in the MetaTrader5 stub
        volume=0.10,
        price_open=24967.9,
        price_current=24960.0,
        sl=24930.0,
        tp=25050.0,
        profit=-0.43,
        swap=0.0,
        time=1_700_000_000,
    )


class TestSymbolMapperResolution:
    def test_de40_alias_resolves_to_ger40(self):
        assert resolve_to_internal("DE40") == "GER40"

    def test_registry_symbol_passthrough(self):
        assert resolve_to_internal("GER40") == "GER40"
        assert resolve_to_internal("EURUSD") == "EURUSD"

    def test_empty_symbol_is_safe(self):
        assert resolve_to_internal("") == ""


class TestMT5PositionReporting:
    def test_broker_alias_reported_as_registry_symbol(self):
        pytest.importorskip("pandas")  # connector imports pandas at module level
        from platforms.mt5.mt5_connector import MT5Connector

        info = MT5Connector._to_position_info(
            MT5Connector(), _fake_mt5_position("DE40"),
        )
        # Recorded under the canonical registry key, not the broker alias.
        assert info.symbol == "GER40"
        # The ticket (used for modify/close routing) is untouched.
        assert info.order_id == "123456"

    def test_registry_symbol_passes_through(self):
        pytest.importorskip("pandas")
        from platforms.mt5.mt5_connector import MT5Connector

        info = MT5Connector._to_position_info(
            MT5Connector(), _fake_mt5_position("EURUSD"),
        )
        assert info.symbol == "EURUSD"
