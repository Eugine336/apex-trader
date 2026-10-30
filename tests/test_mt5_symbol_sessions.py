"""Broker-reported MT5 session windows retain explicit timezone/calendar limits."""
from datetime import time
from types import SimpleNamespace

import platforms.mt5.mt5_connector as mt5_module
from platforms.mt5.mt5_connector import MT5Connector


def _session(start, end):
    result = SimpleNamespace()
    setattr(result, "from", start)
    setattr(result, "to", end)
    return result


def test_symbol_sessions_are_read_as_broker_server_time(monkeypatch):
    calls = []

    def session_trade(symbol, day, index):
        calls.append((symbol, day, index))
        if day == 1 and index == 0:
            return _session(time(1, 30), time(22, 0))
        return None

    monkeypatch.setattr(
        mt5_module, "mt5",
        SimpleNamespace(symbol_info_session_trade=session_trade),
    )
    connector = MT5Connector.__new__(MT5Connector)

    sessions = connector._get_symbol_sessions("XAUUSD")

    assert sessions == [{
        "day": "monday",
        "day_index": 1,
        "session_index": 0,
        "start_seconds_server_time": 5400,
        "end_seconds_server_time": 79200,
    }]
    assert calls[0] == ("XAUUSD", 0, 0)
    assert ("XAUUSD", 1, 0) in calls


def test_symbol_sessions_fail_closed_when_api_is_unavailable(monkeypatch):
    monkeypatch.setattr(mt5_module, "mt5", SimpleNamespace())
    connector = MT5Connector.__new__(MT5Connector)

    assert connector._get_symbol_sessions("XAUUSD") == []
