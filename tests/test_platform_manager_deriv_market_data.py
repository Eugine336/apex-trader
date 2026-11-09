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
