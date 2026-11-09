from types import SimpleNamespace

from scripts.diagnose_mt5_clock import measure_tick_clock


def test_tick_clock_measurement_compares_seconds_and_milliseconds_to_host():
    tick = SimpleNamespace(time=1_800_000_000, time_msc=1_800_000_000_250)
    measured = measure_tick_clock(tick, 1_799_992_800)

    assert measured["tick_time_epoch"] == 1_800_000_000.0
    assert measured["tick_time_msc_epoch"] == 1_800_000_000.25
    assert measured["time_fields_delta_seconds"] == 0.25
    assert measured["host_skew_seconds"] == 7_200.0
    assert measured["host_skew_msc_seconds"] == 7_200.25


def test_tick_clock_measurement_keeps_missing_time_fields_unknown():
    tick = SimpleNamespace(time=0, time_msc=0)
    measured = measure_tick_clock(tick, 1_800_000_000)

    assert measured["tick_time_epoch"] is None
    assert measured["tick_time_msc_epoch"] is None
    assert measured["time_fields_delta_seconds"] is None
    assert measured["host_skew_seconds"] is None
