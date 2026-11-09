from types import SimpleNamespace

from platforms.mt5.clock import NTPClockSample
from scripts.diagnose_mt5_clock import measure_tick_clock


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
