from __future__ import annotations

from types import SimpleNamespace

from platforms.mt5 import mt5_connector as module


class _FakeMT5:
    def __init__(self):
        self.initialize_calls = []

    def initialize(self, *args):
        self.initialize_calls.append(args)
        return True

    @staticmethod
    def account_info():
        return SimpleNamespace(login=123456, balance=1000.0, currency="USD")

    @staticmethod
    def terminal_info():
        return SimpleNamespace(company="JustMarkets")

    @staticmethod
    def version():
        return (500, 1, "test")


def test_connector_pins_initialize_to_configured_terminal(tmp_path, monkeypatch):
    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"test executable marker")
    fake_mt5 = _FakeMT5()
    monkeypatch.setattr(module, "mt5", fake_mt5)
    monkeypatch.setattr(module, "_MT5_AVAILABLE", True)

    connector = module.MT5Connector(terminal_path=str(terminal))
    assert connector.connect() is True
    assert fake_mt5.initialize_calls == [(str(terminal),)]


def test_connector_fails_closed_for_missing_configured_terminal(tmp_path, monkeypatch):
    fake_mt5 = _FakeMT5()
    monkeypatch.setattr(module, "mt5", fake_mt5)
    monkeypatch.setattr(module, "_MT5_AVAILABLE", True)

    connector = module.MT5Connector(
        terminal_path=str(tmp_path / "missing-terminal64.exe"),
    )
    assert connector.connect() is False
    assert fake_mt5.initialize_calls == []


def test_connector_rejects_relative_terminal_path(tmp_path, monkeypatch):
    fake_mt5 = _FakeMT5()
    monkeypatch.setattr(module, "mt5", fake_mt5)
    monkeypatch.setattr(module, "_MT5_AVAILABLE", True)

    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"test executable marker")
    monkeypatch.chdir(tmp_path)
    connector = module.MT5Connector(terminal_path="terminal64.exe")
    assert connector.connect() is False
    assert fake_mt5.initialize_calls == []


def test_connector_keeps_auto_discovery_when_terminal_path_is_blank(monkeypatch):
    fake_mt5 = _FakeMT5()
    monkeypatch.setattr(module, "mt5", fake_mt5)
    monkeypatch.setattr(module, "_MT5_AVAILABLE", True)

    connector = module.MT5Connector()
    assert connector.connect() is True
    assert fake_mt5.initialize_calls == [()]


def test_platform_manager_passes_configured_terminal_path(monkeypatch, tmp_path):
    from config import AppConfig
    from platforms import platform_manager as manager_module

    terminal_path = str(tmp_path / "terminal64.exe")
    captured = []

    class _Connector:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class _Deriv:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setenv("MT5_TERMINAL_PATH", terminal_path)
    monkeypatch.setattr(manager_module, "MT5Connector", _Connector)
    monkeypatch.setattr(manager_module, "DerivConnector", _Deriv)

    manager_module.PlatformManager(AppConfig())
    assert len(captured) == 1
    assert captured[0]["terminal_path"] == terminal_path
