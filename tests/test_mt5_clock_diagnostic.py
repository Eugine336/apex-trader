from types import SimpleNamespace

import pytest

from platforms.mt5.clock import NTPClockSample
from scripts.diagnose_mt5_clock import measure_tick_clock, wait_for_advancing_tick


def test_tick_clock_measurement_uses_network_utc_and_preserves_raw_fields():
    tick = SimpleNamespace(time=1_800_007_200, time_msc=1_800_007_200_250)
    samples = (
        NTPClockSample(1_800_000_000.0, 50.0, 0.55, "ntp:a"),
        NTPClockSample(1_800_000_000.1, 50.1, 0.55, "ntp:b"),
    )

    measured = measure_tick_clock(tick, "JustMarkets-Demo", 50.2, samples)

    assert measured["tick_time_epoch"] == 1_800_007_200.0
    assert measured["tick_time_msc_epoch"] == 1_800_007_200.25
    assert measured["time_fields_delta_seconds"] == 0.25
    assert measured["server"] == "JustMarkets-Demo"
    assert abs(measured["offset_seconds"] - 7200.05) < 0.1
    assert measured["uncertainty_seconds"] <= 2.5
    assert "ntp:a" in measured["time_sources"]


def test_tick_clock_measurement_keeps_missing_time_fields_unknown():
    tick = SimpleNamespace(time=0, time_msc=0)
    measured = measure_tick_clock(tick, "JustMarkets-Demo", 50.0, ())

    assert measured["tick_time_epoch"] is None
    assert measured["tick_time_msc_epoch"] is None
    assert measured["time_fields_delta_seconds"] is None
    assert measured["offset_seconds"] is None


def test_wait_for_advancing_tick_tolerates_repeated_cached_observations():
    ticks = iter([
        SimpleNamespace(time=100, time_msc=100_000),
        SimpleNamespace(time=100, time_msc=100_000),
        SimpleNamespace(time=101, time_msc=101_000),
    ])
    clock = {"now": 10.0}

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += seconds

    result = wait_for_advancing_tick(
        lambda _symbol: next(ticks), "XAUUSD.ecn", 2.0, 0.25,
        monotonic=monotonic, sleep=sleep,
    )

    assert result is not None
    first, first_mono, latest, observed_mono = result
    assert first.time_msc == 100_000
    assert latest.time_msc == 101_000
    assert first_mono == 10.0
    assert observed_mono == 10.5


def test_wait_for_advancing_tick_returns_none_when_feed_does_not_advance():
    tick = SimpleNamespace(time=100, time_msc=100_000)
    clock = {"now": 10.0}

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += seconds

    result = wait_for_advancing_tick(
        lambda _symbol: tick, "XAUUSD.ecn", 0.6, 0.2,
        monotonic=monotonic, sleep=sleep,
    )

    assert result is None
    assert clock["now"] == pytest.approx(10.6)


@pytest.mark.parametrize("timeout,interval", [(0, 0.25), (61, 0.25), (1, 0), (1, 6)])
def test_wait_for_advancing_tick_rejects_invalid_wait_configuration(timeout, interval):
    with pytest.raises(ValueError):
        wait_for_advancing_tick(lambda _symbol: None, "XAUUSD.ecn", timeout, interval)
