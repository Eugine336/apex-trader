from __future__ import annotations

import os

from platforms.mt5.clock import CLOCK_FILENAME, load_mt5_server_clock


def _write_clock(tmp_path, *, server="JustMarkets-Demo", offset=7200,
                 server_epoch=1_800_007_200, gmt_epoch=1_800_000_000):
    path = tmp_path / "MQL5" / "Files" / CLOCK_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(
        "schema_version=1\n"
        f"server={server}\n"
        f"offset_seconds={offset}\n"
        f"observed_server_epoch={server_epoch}\n"
        f"observed_gmt_epoch={gmt_epoch}\n",
        encoding="utf-8",
    )
    os.utime(path, (1_800_000_000, 1_800_000_000))
    return path


def test_load_fresh_server_clock_and_normalize_server_epoch(tmp_path):
    _write_clock(tmp_path)
    state = load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_005,
    )

    assert state is not None
    assert state.offset_seconds == 7200
    assert state.to_utc_epoch(1_800_007_210) == 1_800_000_010
    assert state.server_now_epoch(1_800_000_005) == 1_800_007_205
    assert state.age_seconds(1_800_007_200, 1_800_000_005) == 5


def test_clock_reader_rejects_stale_publisher_file(tmp_path):
    _write_clock(tmp_path)
    state = load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_100,
        max_age_seconds=20,
    )
    assert state is None


def test_clock_reader_rejects_different_broker_server(tmp_path):
    _write_clock(tmp_path)
    assert load_mt5_server_clock(
        tmp_path, "JustMarkets-Live", now_epoch=1_800_000_005,
    ) is None


def test_clock_reader_rejects_inconsistent_offset_payload(tmp_path):
    _write_clock(tmp_path, offset=3600)
    assert load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_005,
    ) is None


def test_clock_reader_rejects_missing_or_implausible_clock(tmp_path):
    assert load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_005,
    ) is None
    _write_clock(tmp_path, offset=99 * 3600)
    assert load_mt5_server_clock(
        tmp_path, "JustMarkets-Demo", now_epoch=1_800_000_005,
    ) is None


def test_connector_caches_clock_terminal_ipc_for_one_second(monkeypatch):
    from types import SimpleNamespace

    import platforms.mt5.mt5_connector as connector_module
    from platforms.mt5.mt5_connector import MT5Connector

    class _Terminal:
        data_path = "C:/mt5-data"

    class _Account:
        server = "JustMarkets-Demo"

    class _MT5:
        terminal_calls = 0
        account_calls = 0

        def terminal_info(self):
            self.terminal_calls += 1
            return _Terminal()

        def account_info(self):
            self.account_calls += 1
            return _Account()

    fake = _MT5()
    state = SimpleNamespace(file_age_seconds=0.0, offset_seconds=7200)
    monkeypatch.setattr(connector_module, "mt5", fake)
    monkeypatch.setattr(connector_module, "load_mt5_server_clock", lambda *a, **k: state)
    connector = MT5Connector.__new__(MT5Connector)

    assert connector._read_server_clock_state() is state
    assert connector._read_server_clock_state() is state
    assert fake.terminal_calls == 1
    assert fake.account_calls == 1


def test_mql5_clock_publisher_is_read_only():
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "mt5_clock_publisher.mq5"
    ).read_text(encoding="utf-8")
    assert "TimeTradeServer()" in script
    assert "TimeGMT()" in script
    assert "FileOpen(" in script
    for forbidden in ("OrderSend(", "OrderSendAsync(", "OrderModify(", "OrderDelete("):
        assert forbidden not in script
