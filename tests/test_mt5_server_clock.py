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


def test_automatic_clock_estimator_uses_independent_ntp_and_monotonic_anchor():
    from platforms.mt5.clock import NTPClockSample, estimate_mt5_server_clock

    samples = (
        NTPClockSample(utc_epoch=1_800_000_001.0, monotonic_epoch=50.0,
                       uncertainty_seconds=0.55, server="ntp:a"),
        NTPClockSample(utc_epoch=1_800_000_001.1, monotonic_epoch=50.1,
                       uncertainty_seconds=0.55, server="ntp:b"),
    )
    state = estimate_mt5_server_clock(
        "JustMarkets-Demo", 1_800_007_201.2, 50.2, ntp_samples=samples,
    )

    assert state is not None
    assert abs(state.offset_seconds - 7200.0) < 0.2
    assert state.uncertainty_seconds <= 2.5
    assert state.sources == ("ntp:a", "ntp:b")


def test_automatic_clock_estimator_rejects_disagreeing_time_sources():
    from platforms.mt5.clock import NTPClockSample, estimate_mt5_server_clock

    samples = (
        NTPClockSample(utc_epoch=1_800_000_000.0, monotonic_epoch=50.0,
                       uncertainty_seconds=0.55, server="ntp:a"),
        NTPClockSample(utc_epoch=1_800_000_010.0, monotonic_epoch=50.0,
                       uncertainty_seconds=0.55, server="ntp:b"),
    )
    assert estimate_mt5_server_clock(
        "JustMarkets-Demo", 1_800_007_200.0, 50.0, ntp_samples=samples,
    ) is None


def test_automatic_clock_estimator_rejects_one_outlier_when_two_sources_agree():
    from platforms.mt5.clock import NTPClockSample, estimate_mt5_server_clock

    samples = (
        NTPClockSample(1_800_000_000.0, 50.0, 0.55, "ntp:a"),
        NTPClockSample(1_800_000_000.1, 50.1, 0.55, "ntp:b"),
        NTPClockSample(1_800_000_015.0, 50.0, 0.55, "ntp:outlier"),
    )
    state = estimate_mt5_server_clock(
        "JustMarkets-Demo", 1_800_007_200.2, 50.2, ntp_samples=samples,
    )

    assert state is not None
    assert abs(state.offset_seconds - 7200.0) < 0.2
    assert state.sources == ("ntp:a", "ntp:b")


def test_ntp_collection_adds_https_witnesses_when_only_two_ntp_sources_answer(monkeypatch):
    import platforms.mt5.clock as clock_module

    ntp = {
        "time.cloudflare.com": clock_module.NTPClockSample(1000.0, 10.0, 0.6, "ntp:a"),
        "time.google.com": clock_module.NTPClockSample(1000.1, 10.1, 0.6, "ntp:b"),
        "pool.ntp.org": None,
    }
    https = clock_module.NTPClockSample(1000.05, 10.05, 0.9, "https:clock")
    monkeypatch.setattr(clock_module, "_query_ntp", lambda host: ntp[host])
    monkeypatch.setattr(clock_module, "_query_https_time", lambda url: https)

    samples = clock_module.query_ntp_sources()

    assert len(samples) == 4
    assert sum(sample.server.startswith("ntp:") for sample in samples) == 2
    assert sum(sample.server.startswith("https:") for sample in samples) == 2


def test_automatic_clock_estimator_requires_two_independent_sources():
    from platforms.mt5.clock import NTPClockSample, estimate_mt5_server_clock

    sample = NTPClockSample(
        utc_epoch=1_800_000_000.0, monotonic_epoch=50.0,
        uncertainty_seconds=0.55, server="ntp:a",
    )
    assert estimate_mt5_server_clock(
        "JustMarkets-Demo", 1_800_007_200.0, 50.0, ntp_samples=(sample,),
    ) is None


def test_automatic_clock_uses_monotonic_time_after_synchronization(monkeypatch):
    from platforms.mt5.clock import NTPClockSample, estimate_mt5_server_clock
    import platforms.mt5.clock as clock_module

    samples = (
        NTPClockSample(1_800_000_000.0, 50.0, 0.55, "ntp:a"),
        NTPClockSample(1_800_000_000.1, 50.1, 0.55, "ntp:b"),
    )
    state = estimate_mt5_server_clock(
        "JustMarkets-Demo", 1_800_007_200.2, 50.2, ntp_samples=samples,
    )
    assert state is not None
    monkeypatch.setattr(clock_module.time, "monotonic", lambda: 55.2)
    assert abs(state.server_now_epoch() - 1_800_007_205.2) < 0.01


def test_connector_automatically_synchronizes_after_advancing_tick(monkeypatch):
    from types import SimpleNamespace
    import platforms.mt5.mt5_connector as connector_module
    from platforms.mt5.clock import NTPClockSample
    from platforms.mt5.mt5_connector import MT5Connector

    class _MT5:
        def account_info(self):
            return SimpleNamespace(server="JustMarkets-Demo")

    times = iter((10.0, 11.1))
    monkeypatch.setattr(connector_module._time, "monotonic", lambda: next(times))
    monkeypatch.setattr(connector_module, "mt5", _MT5())
    monkeypatch.setattr(connector_module, "query_ntp_sources", lambda: (
        NTPClockSample(1001.0, 11.1, 0.55, "ntp:a"),
        NTPClockSample(1001.1, 11.1, 0.55, "ntp:b"),
    ))
    connector = MT5Connector.__new__(MT5Connector)
    first = SimpleNamespace(time=8200, time_msc=8_200_000)
    second = SimpleNamespace(time=8201, time_msc=8_201_000)

    assert connector._read_server_clock_state(tick=first, symbol="XAUUSD") is None
    state = connector._read_server_clock_state(tick=second, symbol="XAUUSD")

    assert state is not None
    assert state.server == "JustMarkets-Demo"
    assert abs(state.offset_seconds - 7199.9) < 0.2


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
