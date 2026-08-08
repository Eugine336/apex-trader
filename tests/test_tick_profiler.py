"""Tests for the tick-cycle profiler (P4 — ops.tick_profiler).

Stdlib-only target, so these tests avoid the numpy/pandas/loguru stubs the rest
of the suite installs — they import the profiler module directly.
"""

import time

import pytest

from ops.tick_profiler import TickProfiler


def test_measure_records_timing():
    p = TickProfiler(enabled=True, window_size=100)
    with p.measure("scanner"):
        time.sleep(0.005)
    report = p.get_profile_report()
    comps = {c["component"]: c for c in report["components"]}
    assert "scanner" in comps
    assert comps["scanner"]["samples"] == 1
    assert comps["scanner"]["avg_ms"] > 0.0


def test_component_stats_avg_p50_p95_max():
    p = TickProfiler(enabled=True, window_size=100)
    # Feed deterministic durations by recording directly.
    for ms in (1.0, 2.0, 3.0, 4.0, 100.0):
        p._record("comp", ms)
    report = p.get_profile_report()
    c = report["components"][0]
    assert c["component"] == "comp"
    assert c["samples"] == 5
    assert c["max_ms"] == 100.0
    assert c["avg_ms"] == pytest.approx(22.0, abs=0.01)
    # p95 nearest-rank on 5 samples lands on the largest.
    assert c["p95_ms"] == 100.0
    assert c["p50_ms"] == 3.0


def test_slow_tick_detection():
    p = TickProfiler(enabled=True, slow_tick_threshold_ms=50.0)
    p.record_tick(10.0)   # fast
    p.record_tick(120.0)  # slow
    p.record_tick(40.0)   # fast
    tick = p.get_profile_report()["tick"]
    assert tick["tick_count"] == 3
    assert tick["slow_tick_count"] == 1


def test_slow_tick_captures_top_components():
    p = TickProfiler(enabled=True, slow_tick_threshold_ms=5.0)
    p._record("heavy", 80.0)
    p._record("light", 2.0)
    p.record_tick(90.0)
    dash = p.get_dashboard_data()
    assert len(dash["slow_ticks"]) == 1
    top = dash["slow_ticks"][0]["components"]
    assert top[0]["component"] == "heavy"


def test_dashboard_data_shape():
    p = TickProfiler(enabled=True)
    with p.measure("x"):
        pass
    p.record_tick(3.0)
    dash = p.get_dashboard_data()
    for key in ("enabled", "tick", "components", "slow_ticks", "recommendations"):
        assert key in dash
    assert dash["enabled"] is True


def test_disabled_is_noop():
    p = TickProfiler(enabled=False)
    with p.measure("scanner"):
        time.sleep(0.001)
    p.record_tick(999.0)
    report = p.get_profile_report()
    assert report["enabled"] is False
    assert report["components"] == []
    assert report["tick"]["samples"] == 0
    # dashboard + recommendations stay empty / safe.
    assert p.get_optimization_recommendations() == []


def test_window_size_bounds_memory():
    p = TickProfiler(enabled=True, window_size=10)
    for _ in range(50):
        p._record("c", 1.0)
        p.record_tick(1.0)
    report = p.get_profile_report()
    c = report["components"][0]
    # Only the last `window_size` durations are retained …
    assert c["samples"] == 10
    # … but the cumulative call count survives eviction.
    assert c["calls"] == 50
    assert report["tick"]["samples"] == 10
    assert report["tick"]["tick_count"] == 50


def test_recommendations_flag_dominant_component():
    p = TickProfiler(enabled=True, slow_tick_threshold_ms=1000.0)
    # 'dominant' is ~60ms of a ~70ms tick → > 50% share, above the 5ms floor.
    for _ in range(5):
        p._record("dominant", 60.0)
        p._record("minor", 10.0)
        p.record_tick(70.0)
    recs = p.get_optimization_recommendations()
    assert any(r["component"] == "dominant" for r in recs)
    dom = next(r for r in recs if r["component"] == "dominant")
    assert dom["severity"] == "high"
    assert dom["share_of_tick"] >= 0.5


def test_small_component_not_recommended():
    p = TickProfiler(enabled=True)
    for _ in range(5):
        p._record("tiny", 0.5)  # below the 5ms absolute floor
        p.record_tick(0.5)
    recs = p.get_optimization_recommendations()
    assert all(r["component"] != "tiny" for r in recs)


def test_measure_records_even_on_exception():
    p = TickProfiler(enabled=True)
    with pytest.raises(ValueError):
        with p.measure("boom"):
            raise ValueError("x")
    comps = {c["component"]: c for c in p.get_profile_report()["components"]}
    assert "boom" in comps  # finally-block recorded the timing


def test_concurrent_record_is_safe():
    import threading

    p = TickProfiler(enabled=True, window_size=10000)

    def worker():
        for _ in range(200):
            p._record("shared", 1.0)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    comps = {c["component"]: c for c in p.get_profile_report()["components"]}
    assert comps["shared"]["calls"] == 8 * 200


def test_reset_clears_stats():
    p = TickProfiler(enabled=True)
    p._record("c", 5.0)
    p.record_tick(5.0)
    p.reset()
    report = p.get_profile_report()
    assert report["components"] == []
    assert report["tick"]["tick_count"] == 0
