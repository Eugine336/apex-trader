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
    monkeypatch.setattr(clock_module, "_query_ntp", lambda host: ntp[host])
    monkeypatch.setattr(
        clock_module, "_query_https_time",
        lambda url: clock_module.NTPClockSample(
            1000.05, 10.05, 0.9, f"https:{url.split('/')[2]}",
        ),
    )

    samples = clock_module.query_ntp_sources()

    assert len(samples) == 4
    assert sum(sample.server.startswith("ntp:") for sample in samples) == 2
    assert sum(sample.server.startswith("https:") for sample in samples) == 2


def test_automatic_clock_estimator_preserves_uncertainty_ceiling():
    from platforms.mt5.clock import NTPClockSample, estimate_mt5_server_clock

    samples = (
        NTPClockSample(1_800_000_000.0, 50.0, 1.5, "ntp:a"),
        NTPClockSample(1_800_000_000.0, 50.0, 1.5, "ntp:b"),
    )
    assert estimate_mt5_server_clock(
        "JustMarkets-Demo", 1_800_007_200.0, 50.0, ntp_samples=samples,
        tick_precision_seconds=0.001, tick_transport_uncertainty_seconds=1.0,
    ) is None


def test_automatic_clock_estimator_does_not_count_two_protocols_from_one_provider():
    from platforms.mt5.clock import NTPClockSample, estimate_mt5_server_clock

    samples = (
        NTPClockSample(1_800_000_000.0, 50.0, 0.2, "ntp:time.cloudflare.com"),
        NTPClockSample(1_800_000_000.0, 50.0, 0.2, "https:www.cloudflare.com"),
    )
    assert estimate_mt5_server_clock(
        "JustMarkets-Demo", 1_800_007_200.0, 50.0, ntp_samples=samples,
    ) is None


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


def test_duplicate_quote_polls_do_not_reset_advancing_tick_clock_baseline(monkeypatch):
    """Repeated latest-quote reads must not erase the last advancing tick."""
    from types import SimpleNamespace
    import platforms.mt5.mt5_connector as connector_module
    from platforms.mt5.clock import NTPClockSample
    from platforms.mt5.mt5_connector import MT5Connector

    class _MT5:
        def account_info(self):
            return SimpleNamespace(server="JustMarkets-Demo")

    # The poller reads the same latest quote repeatedly, then receives a new
    # quote 3 seconds later only 100 ms after the previous poll.
    times = iter((10.0, 10.1, 10.2, 13.0, 13.1))
    monkeypatch.setattr(connector_module._time, "monotonic", lambda: next(times))
    monkeypatch.setattr(connector_module, "mt5", _MT5())
    monkeypatch.setattr(connector_module, "query_ntp_sources", lambda: (
        NTPClockSample(1003.1, 13.1, 0.55, "ntp:a"),
        NTPClockSample(1003.2, 13.1, 0.55, "ntp:b"),
    ))
    connector = MT5Connector.__new__(MT5Connector)
    first = SimpleNamespace(time=8200, time_msc=8_200_000)
    advanced = SimpleNamespace(time=8203, time_msc=8_203_000)

    assert connector._read_server_clock_state(tick=first, symbol="XAUUSD") is None
    assert connector._read_server_clock_state(tick=first, symbol="XAUUSD") is None
    assert connector._read_server_clock_state(tick=first, symbol="XAUUSD") is None
    assert connector._read_server_clock_state(tick=first, symbol="XAUUSD") is None
    state = connector._read_server_clock_state(tick=advanced, symbol="XAUUSD")

    assert state is not None
    assert state.server == "JustMarkets-Demo"
    assert abs(state.offset_seconds - 7199.9) < 0.2



def test_clock_holdover_grows_uncertainty_without_moving_verified_anchor():
    from platforms.mt5.clock import MT5ServerClockState

    state = MT5ServerClockState(
        server="JustMarkets-Demo",
        offset_seconds=7200.0,
        observed_server_epoch=1_800_007_200,
        observed_gmt_epoch=1_800_000_000,
        file_age_seconds=0.0,
        observed_monotonic=50.0,
        uncertainty_seconds=0.8,
        sources=("ntp:one", "ntp:two"),
    )

    holdover = state.as_holdover(12.0)

    assert holdover.synchronization_status == "holdover"
    assert holdover.holdover_age_seconds == 12.0
    assert holdover.uncertainty_seconds == 0.812
    assert holdover.offset_seconds == state.offset_seconds
    assert holdover.observed_server_epoch == state.observed_server_epoch
    assert holdover.observed_gmt_epoch == state.observed_gmt_epoch
    assert holdover.observed_monotonic == state.observed_monotonic
    import pytest
    assert holdover.server_now_epoch() == pytest.approx(
        state.server_now_epoch(), rel=0.0, abs=1e-5,
    )


def test_clock_holdover_rejects_negative_or_non_finite_age():
    import math
    import pytest
    from platforms.mt5.clock import MT5ServerClockState

    state = MT5ServerClockState(
        server="JustMarkets-Demo",
        offset_seconds=7200.0,
        observed_server_epoch=1_800_007_200,
        observed_gmt_epoch=1_800_000_000,
        file_age_seconds=0.0,
        observed_monotonic=50.0,
        uncertainty_seconds=0.8,
        sources=("ntp:one", "ntp:two"),
    )

    for age in (-0.1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="holdover age"):
            state.as_holdover(age)


def test_connector_returns_growing_holdover_only_after_identity_revalidation(monkeypatch):
    from types import SimpleNamespace
    import platforms.mt5.mt5_connector as connector_module
    from platforms.mt5.clock import MT5ServerClockState
    from platforms.mt5.mt5_connector import MT5Connector

    state = MT5ServerClockState(
        server="JustMarkets-Demo",
        offset_seconds=7200.0,
        observed_server_epoch=1_800_007_200,
        observed_gmt_epoch=1_800_000_000,
        file_age_seconds=0.0,
        observed_monotonic=10.0,
        uncertainty_seconds=0.8,
        sources=("ntp:one", "ntp:two"),
    )
    connector = MT5Connector.__new__(MT5Connector)
    connector._server_clock_state_cache = state
    connector._server_clock_state_cache_at = 10.0
    connector._server_clock_retry_after = 20.0
    connector._clock_tick_observations = {}
    monkeypatch.setattr(connector_module._time, "monotonic", lambda: 15.0)
    monkeypatch.setattr(
        connector_module, "mt5",
        SimpleNamespace(account_info=lambda: SimpleNamespace(server="JustMarkets-Demo")),
    )

    holdover = connector._synchronize_server_clock_state()

    assert holdover is not None
    assert holdover.synchronization_status == "holdover"
    assert holdover.holdover_age_seconds == 5.0
    assert holdover.uncertainty_seconds == 0.805


def test_connector_does_not_use_holdover_when_broker_identity_is_unavailable(monkeypatch):
    from types import SimpleNamespace
    import platforms.mt5.mt5_connector as connector_module
    from platforms.mt5.clock import MT5ServerClockState
    from platforms.mt5.mt5_connector import MT5Connector

    state = MT5ServerClockState(
        server="JustMarkets-Demo",
        offset_seconds=7200.0,
        observed_server_epoch=1_800_007_200,
        observed_gmt_epoch=1_800_000_000,
        file_age_seconds=0.0,
        observed_monotonic=10.0,
        uncertainty_seconds=0.8,
        sources=("ntp:one", "ntp:two"),
    )
    connector = MT5Connector.__new__(MT5Connector)
    connector._server_clock_state_cache = state
    connector._server_clock_state_cache_at = 10.0
    connector._server_clock_retry_after = 20.0
    monkeypatch.setattr(connector_module._time, "monotonic", lambda: 15.0)
    monkeypatch.setattr(
        connector_module, "mt5",
        SimpleNamespace(account_info=lambda: None),
    )

    assert connector._synchronize_server_clock_state() is None

def test_connector_rejects_delayed_tick_burst_as_clock_witness(monkeypatch):
    from types import SimpleNamespace
    import platforms.mt5.mt5_connector as connector_module
    from platforms.mt5.clock import NTPClockSample
    from platforms.mt5.mt5_connector import MT5Connector

    class _MT5:
        def account_info(self):
            return SimpleNamespace(server="JustMarkets-Demo")

    times = iter((10.0, 20.0, 30.0))
    monkeypatch.setattr(connector_module._time, "monotonic", lambda: next(times))
    monkeypatch.setattr(connector_module, "mt5", _MT5())
    monkeypatch.setattr(connector_module, "query_ntp_sources", lambda: (
        NTPClockSample(1011.0, 30.0, 0.55, "ntp:a"),
        NTPClockSample(1011.1, 30.0, 0.55, "ntp:b"),
    ))
    connector = MT5Connector.__new__(MT5Connector)
    first = SimpleNamespace(time=8200, time_msc=8_200_000)
    delayed = SimpleNamespace(time=8201, time_msc=8_201_000)
    current = SimpleNamespace(time=8211, time_msc=8_211_000)

    assert connector._read_server_clock_state(tick=first, symbol="XAUUSD") is None
    # The source advanced only one second over ten seconds of monotonic time.
    # It is newer, but cannot safely anchor the broker clock.
    assert connector._read_server_clock_state(tick=delayed, symbol="XAUUSD") is None
    state = connector._read_server_clock_state(tick=current, symbol="XAUUSD")

    assert state is not None
    assert abs(state.offset_seconds - 7200.0) < 0.2

def test_connector_utc_lower_bound_subtracts_offset_and_uncertainty(monkeypatch):
    from types import SimpleNamespace
    from platforms.mt5.mt5_connector import MT5Connector

    connector = MT5Connector.__new__(MT5Connector)
    connector._read_server_clock_state = lambda: SimpleNamespace(
        server_now_epoch=lambda: 1_800_007_205.0,
        offset_seconds=7200.0,
        uncertainty_seconds=0.8,
    )

    assert connector.get_utc_time_lower_bound() == 1_800_000_004.2


def test_connector_utc_lower_bound_uses_independent_authority_when_broker_clock_is_unknown(monkeypatch):
    import platforms.mt5.clock as clock_module
    from platforms.mt5.mt5_connector import MT5Connector

    connector = MT5Connector.__new__(MT5Connector)
    connector._read_server_clock_state = lambda: None
    monkeypatch.setattr(clock_module, "independent_utc_lower_bound", lambda: 1_800_000_000.0)

    assert connector.get_utc_time_lower_bound() == 1_800_000_000.0

def test_independent_utc_estimator_requires_two_corroborating_authorities():
    from platforms.mt5.clock import NTPClockSample, estimate_utc_clock

    samples = (
        NTPClockSample(1_800_000_000.0, 99.0, 0.5, "ntp:time.cloudflare.com"),
        NTPClockSample(1_800_000_000.1, 99.0, 0.5, "ntp:time.google.com"),
    )
    state = estimate_utc_clock(samples, observed_monotonic=100.0)
    assert state is not None
    assert abs(state.observed_utc_epoch - 1_800_000_001.05) < 0.01
    assert state.lower_bound(100.0) < state.observed_utc_epoch
    assert estimate_utc_clock(samples[:1], observed_monotonic=100.0) is None

    conflicting = (
        samples[0],
        NTPClockSample(1_800_000_010.0, 99.0, 0.5, "ntp:time.google.com"),
    )
    assert estimate_utc_clock(conflicting, observed_monotonic=100.0) is None


def test_independent_utc_lower_bound_caches_monotonic_consensus(monkeypatch):
    import platforms.mt5.clock as clock_module
    from platforms.mt5.clock import (
        NTPClockSample,
        clear_independent_utc_clock_cache,
        independent_utc_lower_bound,
    )

    clear_independent_utc_clock_cache()
    times = iter((100.0, 101.0, 102.0))
    calls = []
    monkeypatch.setattr(clock_module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        clock_module, "query_ntp_sources",
        lambda: calls.append("query") or (
            NTPClockSample(1_800_000_000.0, 100.0, 0.5, "ntp:time.cloudflare.com"),
            NTPClockSample(1_800_000_000.1, 100.0, 0.5, "ntp:time.google.com"),
        ),
    )

    first = independent_utc_lower_bound()
    second = independent_utc_lower_bound()
    assert first is not None and second is not None
    assert second > first
    assert calls == ["query"]
    clear_independent_utc_clock_cache()

def test_clock_state_without_monotonic_anchor_never_uses_host_wall_time():
    import pytest
    from platforms.mt5.clock import MT5ServerClockState

    state = MT5ServerClockState(
        server="JustMarkets-Demo",
        offset_seconds=7200.0,
        observed_server_epoch=1_800_007_200,
        observed_gmt_epoch=1_800_000_000,
        file_age_seconds=0.0,
    )
    with pytest.raises(ValueError, match="host wall time is not an authority"):
        state.server_now_epoch()
    assert state.server_now_epoch(now_epoch=1_800_000_010.0) == 1_800_007_210.0


def test_cached_clock_invalidates_and_recovers_after_forward_and_backward_dst_steps(monkeypatch):
    from types import SimpleNamespace
    import platforms.mt5.mt5_connector as connector_module
    from platforms.mt5.clock import MT5ServerClockState, NTPClockSample
    from platforms.mt5.mt5_connector import MT5Connector

    class _MT5:
        def account_info(self):
            return SimpleNamespace(server="JustMarkets-Demo")

    for jump_seconds, expected_offset in ((3600, 10800.0), (-3600, 3600.0)):
        times = iter((10.1, 10.2))
        monkeypatch.setattr(
            connector_module._time, "monotonic", lambda: next(times),
        )
        monkeypatch.setattr(connector_module, "mt5", _MT5())
        monkeypatch.setattr(connector_module, "query_ntp_sources", lambda: (
            NTPClockSample(1000.2, 10.2, 0.55, "ntp:a"),
            NTPClockSample(1000.3, 10.2, 0.55, "ntp:b"),
        ))
        connector = MT5Connector.__new__(MT5Connector)
        cached = MT5ServerClockState(
            server="JustMarkets-Demo",
            offset_seconds=7200.0,
            observed_server_epoch=8200.0,
            observed_gmt_epoch=1000.0,
            file_age_seconds=0.0,
            observed_monotonic=10.0,
            uncertainty_seconds=0.8,
            sources=("ntp:a", "ntp:b"),
        )
        connector._server_clock_state_cache = cached
        connector._server_clock_state_cache_at = 10.0
        connector._server_clock_retry_after = 20.0
        connector._clock_tick_observations = {
            ("JustMarkets-Demo", "XAUUSD"): (8200.0, 10.0),
        }

        jumped_epoch = 8200.0 + jump_seconds
        jumped = SimpleNamespace(
            time=int(jumped_epoch),
            time_msc=int(jumped_epoch * 1000),
        )
        assert connector._read_server_clock_state(
            tick=jumped, symbol="XAUUSD",
        ) is None
        assert connector._server_clock_state_cache is None

        recovered_epoch = jumped_epoch + 0.1
        recovered = SimpleNamespace(
            time=int(recovered_epoch),
            time_msc=int(recovered_epoch * 1000),
        )
        state = connector._read_server_clock_state(
            tick=recovered, symbol="XAUUSD",
        )

        assert state is not None
        assert abs(state.offset_seconds - expected_offset) < 0.2
