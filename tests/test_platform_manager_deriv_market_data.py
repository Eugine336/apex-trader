from __future__ import annotations

import pytest

from platforms.platform_manager import PlatformManager


class _Deriv:
    def __init__(self):
        self.calls = []

    def get_price(self, symbol):
        self.calls.append(symbol)
        return symbol


def test_explicit_deriv_price_route_never_falls_back_when_offline():
    manager = PlatformManager.__new__(PlatformManager)
    manager._deriv_connected = False
    manager.deriv = _Deriv()

    with pytest.raises(ConnectionError, match="Deriv not connected"):
        manager.get_deriv_price("EURUSD")
    assert manager.deriv.calls == []


def test_explicit_deriv_price_route_uses_deriv_even_for_dual_venue_symbol():
    manager = PlatformManager.__new__(PlatformManager)
    manager._deriv_connected = True
    manager.deriv = _Deriv()

    assert manager.get_deriv_price("EURUSD") == "EURUSD"
    assert manager.deriv.calls == ["EURUSD"]



def test_deriv_closed_bar_fetch_attests_utc_completion(monkeypatch):
    from platforms.deriv.deriv_connector import DerivConnector

    connector = DerivConnector.__new__(DerivConnector)
    connector._require_connection = lambda: None
    connector.symbol_map = lambda symbol: symbol
    connector.timeframe_map = lambda timeframe: 60
    connector._sync_send = lambda request: {
        "candles": [
            {"epoch": 1_800_000_000, "open": "1.0", "high": "1.2",
             "low": "0.9", "close": "1.1", "volume": 10},
            {"epoch": 1_800_000_060, "open": "1.1", "high": "1.3",
             "low": "1.0", "close": "1.2", "volume": 11},
        ],
    }

    closed = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=False)
    forming = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=True)

    assert len(closed) == 1
    assert closed.attrs["source_timezone"] == "UTC"
    assert closed.attrs["completed_bars_verified"] is True
    assert closed.attrs["forming_bar_included"] is False
    assert closed["time_source_epoch"].tolist() == [1_800_000_000]
    assert len(forming) == 2
    assert forming.attrs["completed_bars_verified"] is False
    assert forming.attrs["forming_bar_included"] is True
