from types import SimpleNamespace

import pytest


def test_symbol_map_accepts_exact_broker_native_symbol(monkeypatch):
    pytest.importorskip("pandas")
    import platforms.mt5.mt5_connector as module

    calls = []

    class FakeMT5:
        @staticmethod
        def symbol_info(symbol):
            calls.append(("info", symbol))
            return SimpleNamespace(visible=False) if symbol == "EURUSD.a" else None

        @staticmethod
        def symbol_select(symbol, enabled):
            calls.append(("select", symbol, enabled))
            return True

    monkeypatch.setattr(module, "mt5", FakeMT5())
    monkeypatch.setattr(module, "_MT5_AVAILABLE", True)
    connector = module.MT5Connector()
    connector._connected = True

    assert connector.symbol_map("EURUSD.a") == "EURUSD.a"
    assert ("info", "EURUSD.a") in calls
    assert ("select", "EURUSD.a", True) in calls
