from __future__ import annotations

from types import SimpleNamespace

from platforms.platform_manager import PlatformManager


class _MT5:
    def __init__(self, login: int, broker: str, server: str):
        self._login = login
        self._broker_name = broker
        self._server = server

    def get_open_positions(self):
        return [SimpleNamespace(symbol="EURUSD", direction="BUY", platform="mt5")]


class _Deriv:
    _account_id = "VRTC12345"

    def get_open_positions(self):
        return [SimpleNamespace(symbol="R_100", direction="SELL", platform="deriv")]


def _manager():
    manager = PlatformManager.__new__(PlatformManager)
    manager.mt5_connectors = []
    manager._mt5_connected_flags = []
    manager._mt5_was_connected = []
    manager._deriv_connected = False
    manager._deriv_was_connected = False
    manager.deriv = None
    manager._deriv_reconnect_warned = False
    return manager


def test_mt5_positions_keep_their_exact_connector_account_identity():
    manager = _manager()
    manager.mt5_connectors = [
        _MT5(1001, "ICMarkets", "ICMarketsSC-Demo"),
        _MT5(2002, "auto", "Pepperstone-Edge"),
    ]
    manager._mt5_connected_flags = [True, True]
    manager._mt5_was_connected = [True, True]

    snapshot = manager.get_open_positions_snapshot()

    assert snapshot.failed_platforms == set()
    assert snapshot.confirmed_platforms == {"mt5"}
    assert [position.account_key for position in snapshot.positions] == [
        "ICMarkets:1001",
        "pepperstone:2002",
    ]
    assert [position.connector_index for position in snapshot.positions] == [0, 1]
    assert all(position.platform == "mt5" for position in snapshot.positions)


def test_deriv_positions_keep_their_exact_account_identity():
    manager = _manager()
    manager._deriv_connected = True
    manager._deriv_was_connected = True
    manager.deriv = _Deriv()

    snapshot = manager.get_open_positions_snapshot()

    assert snapshot.failed_platforms == set()
    assert snapshot.confirmed_platforms == {"deriv"}
    assert snapshot.positions[0].account_key == "deriv:VRTC12345"
    assert snapshot.positions[0].connector_index == 0
