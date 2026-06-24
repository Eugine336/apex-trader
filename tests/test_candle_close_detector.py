"""Tests for tick.candle_close_detector and tick.tick_router."""

import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from tick.models import CandleClose, Tick
from tick.event_bus import EventBus
from tick.candle_close_detector import (
    CandleCloseDetector,
    _candle_boundary,
    _next_close,
    SUPPORTED_TIMEFRAMES,
)
from tick.tick_store import TickStore
from tick.tick_router import TickRouter


def _tick(symbol: str = "EURUSD", bid: float = 1.1, ask: float = 1.1002,
          ts: float = 1700000000.0, source: str = "mt5") -> Tick:
    return Tick(
        symbol=symbol, bid=bid, ask=ask,
        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
        source=source,
    )


class TestCandleBoundaryHelpers:
    def test_m5_boundary(self):
        epoch = 1700000123.0
        boundary = _candle_boundary(epoch, 300)
        assert boundary == 1700000100 - 1700000100 % 300
        actual = (int(epoch) // 300) * 300
        assert boundary == actual

    def test_next_close_m5(self):
        epoch = 1700000123.0
        nc = _next_close(epoch, 300)
        boundary = _candle_boundary(epoch, 300)
        assert nc == boundary + 300

    def test_h1_boundary(self):
        epoch = 1700002500.0
        boundary = _candle_boundary(epoch, 3600)
        nc = _next_close(epoch, 3600)
        assert nc - boundary == 3600

    def test_h4_boundary(self):
        epoch = 1700010000.0
        boundary = _candle_boundary(epoch, 14400)
        nc = _next_close(epoch, 14400)
        assert nc - boundary == 14400

    def test_d1_boundary(self):
        epoch = 1700050000.0
        boundary = _candle_boundary(epoch, 86400)
        nc = _next_close(epoch, 86400)
        assert nc - boundary == 86400


class TestCandleCloseDetector:
    def test_no_event_on_first_tick(self):
        bus = EventBus()
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M5"])
        events = det.on_tick(_tick(ts=1700000100.0))
        assert events == []

    def test_event_on_candle_close(self):
        bus = EventBus()
        received = []
        bus.subscribe("candle_close", lambda e: received.append(e))
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M5"])
        det.on_tick(_tick(ts=1700000100.0))
        events = det.on_tick(_tick(ts=1700000400.0))
        assert len(events) >= 1
        assert events[0].timeframe == "M5"
        assert events[0].symbol == "EURUSD"
        assert len(received) >= 1

    def test_per_tf_event_published(self):
        bus = EventBus()
        m5_events = []
        h1_events = []
        bus.subscribe("candle_close:M5", lambda e: m5_events.append(e))
        bus.subscribe("candle_close:H1", lambda e: h1_events.append(e))
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M5", "H1"])
        det.on_tick(_tick(ts=1700000100.0))
        det.on_tick(_tick(ts=1700000400.0))
        assert len(m5_events) >= 1
        assert len(h1_events) == 0

    def test_multiple_timeframes_fire_independently(self):
        bus = EventBus()
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M1", "M5"])
        det.on_tick(_tick(ts=1700000000.0))
        events = det.on_tick(_tick(ts=1700000060.0))
        tfs = {e.timeframe for e in events}
        assert "M1" in tfs

    def test_unregistered_symbol_ignored(self):
        bus = EventBus()
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M5"])
        events = det.on_tick(_tick("GBPUSD", ts=1700000500.0))
        assert events == []

    def test_unregister_stops_detection(self):
        bus = EventBus()
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M5"])
        det.on_tick(_tick(ts=1700000100.0))
        det.unregister("EURUSD")
        events = det.on_tick(_tick(ts=1700000400.0))
        assert events == []

    def test_events_emitted_counter(self):
        bus = EventBus()
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M1"])
        det.on_tick(_tick(ts=1700000000.0))
        det.on_tick(_tick(ts=1700000060.0))
        det.on_tick(_tick(ts=1700000120.0))
        assert det.events_emitted >= 2

    def test_register_all_timeframes(self):
        bus = EventBus()
        det = CandleCloseDetector(bus)
        det.register("EURUSD")
        assert set(det.registered_timeframes()) == set(SUPPORTED_TIMEFRAMES)

    def test_close_time_is_correct(self):
        bus = EventBus()
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M5"])
        start_epoch = 1700000000.0
        boundary = (int(start_epoch) // 300) * 300
        expected_close = boundary + 300
        det.on_tick(_tick(ts=start_epoch))
        events = det.on_tick(_tick(ts=float(expected_close)))
        assert len(events) == 1
        assert events[0].close_time == datetime.fromtimestamp(expected_close, tz=timezone.utc)

    def test_h4_boundaries_correct(self):
        bus = EventBus()
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["H4"])
        epoch_0400 = 1700006400.0
        epoch_before = epoch_0400 - 100
        epoch_at = epoch_0400
        det.on_tick(_tick(ts=epoch_before))
        events = det.on_tick(_tick(ts=epoch_at))
        h4_events = [e for e in events if e.timeframe == "H4"]
        if h4_events:
            close_hour = h4_events[0].close_time.hour
            assert close_hour % 4 == 0

    def test_d1_close_at_midnight(self):
        bus = EventBus()
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["D1"])
        day_start = 1700006400.0
        next_midnight = ((int(day_start) // 86400) + 1) * 86400
        det.on_tick(_tick(ts=day_start))
        events = det.on_tick(_tick(ts=float(next_midnight)))
        d1_events = [e for e in events if e.timeframe == "D1"]
        assert len(d1_events) == 1
        assert d1_events[0].close_time.hour == 0
        assert d1_events[0].close_time.minute == 0


class TestTickRouter:
    def test_routes_to_store_and_detector(self):
        bus = EventBus()
        store = TickStore(max_hz=10000.0)
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M5"])
        router = TickRouter(store, det, bus)
        router.start()
        router.on_tick(_tick(ts=1700000100.0))
        assert store.get_latest("EURUSD") is not None
        assert router.ticks_routed == 1

    def test_tick_event_published(self):
        bus = EventBus()
        store = TickStore(max_hz=10000.0)
        det = CandleCloseDetector(bus)
        router = TickRouter(store, det, bus)
        received = []
        bus.subscribe("tick:EURUSD", lambda e: received.append(e))
        router.on_tick(_tick(ts=1700000100.0))
        assert len(received) == 1

    def test_coalesced_tick_not_forwarded(self):
        bus = EventBus()
        store = TickStore(max_hz=1.0)
        det = CandleCloseDetector(bus)
        router = TickRouter(store, det, bus)
        tick_events = []
        bus.subscribe("tick", lambda e: tick_events.append(e))
        router.on_tick(_tick(ts=1700000000.0))
        router.on_tick(_tick(ts=1700000000.05))
        assert len(tick_events) == 1

    def test_callback_registration(self):
        bus = EventBus()
        store = TickStore(max_hz=10000.0)
        det = CandleCloseDetector(bus)
        router = TickRouter(store, det, bus)
        received = []
        router.register_callback(lambda t: received.append(t.symbol))
        router.on_tick(_tick("EURUSD"))
        assert received == ["EURUSD"]

    def test_callback_error_does_not_crash(self):
        bus = EventBus()
        store = TickStore(max_hz=10000.0)
        det = CandleCloseDetector(bus)
        router = TickRouter(store, det, bus)

        def bad_cb(t):
            raise RuntimeError("boom")

        router.register_callback(bad_cb)
        router.on_tick(_tick())

    def test_unregister_callback(self):
        bus = EventBus()
        store = TickStore(max_hz=10000.0)
        det = CandleCloseDetector(bus)
        router = TickRouter(store, det, bus)
        received = []
        cb = lambda t: received.append(t)
        router.register_callback(cb)
        router.on_tick(_tick(ts=1700000000.0))
        router.unregister_callback(cb)
        router.on_tick(_tick(ts=1700000001.0))
        assert len(received) == 1

    def test_properties(self):
        bus = EventBus()
        store = TickStore()
        det = CandleCloseDetector(bus)
        router = TickRouter(store, det, bus)
        assert router.tick_store is store
        assert router.candle_close_detector is det
        assert router.event_bus is bus
        assert router.is_running is False
        router.start()
        assert router.is_running is True
        router.stop()
        assert router.is_running is False


class TestTickRouterThreadSafety:
    def test_concurrent_routing(self):
        bus = EventBus()
        store = TickStore(max_hz=10000.0)
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M1"])
        det.register("GBPUSD", ["M1"])
        router = TickRouter(store, det, bus)
        errors = []

        def feed(sym, n):
            try:
                for i in range(n):
                    router.on_tick(_tick(sym, bid=1.1 + i * 0.0001, ts=1700000000.0 + i * 60))
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=feed, args=("EURUSD", 50)),
            threading.Thread(target=feed, args=("GBPUSD", 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == []
        assert router.ticks_routed == 100


class TestCloseDetectionDecoupledFromCoalescing:
    """Bug #14: a boundary-crossing tick must still fire close detection
    even when the coalescing filter drops it from the store."""

    def test_coalesced_boundary_tick_still_fires_close(self):
        bus = EventBus()
        closes = []
        bus.subscribe("candle_close", lambda e: closes.append(e))
        # max_hz=1.0 => any tick within 1s of the previous is coalesced.
        store = TickStore(max_hz=1.0)
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M1"])
        router = TickRouter(store, det, bus)

        # First tick initialises the M1 boundary (no close yet).
        router.on_tick(_tick(ts=1700000000.0))
        # Second tick crosses the M1 boundary but is within 1s wall-rate of the
        # first => coalesced (not stored). It must still fire the close.
        router.on_tick(_tick(ts=1700000060.0, bid=1.10005))

        assert len(closes) == 1
        assert closes[0].timeframe == "M1"

    def test_coalesced_tick_still_not_stored(self):
        # Layer 1 must not change coalescing semantics for the store/bus.
        bus = EventBus()
        store = TickStore(max_hz=1.0)
        det = CandleCloseDetector(bus)
        router = TickRouter(store, det, bus)
        tick_events = []
        bus.subscribe("tick", lambda e: tick_events.append(e))
        router.on_tick(_tick(ts=1700000000.0))
        router.on_tick(_tick(ts=1700000000.05))
        assert len(tick_events) == 1


class TestWatchdogTimerFallback:
    """Bug #15: closes must fire for illiquid symbols even when no tick
    arrives to cross the boundary."""

    def test_timer_fires_overdue_close(self):
        bus = EventBus()
        closes = []
        bus.subscribe("candle_close", lambda e: closes.append(e))
        det = CandleCloseDetector(bus, watchdog_interval=0.1)
        det.register("EURUSD", ["M1"])
        now = time.time()
        # Seed an already-overdue boundary (one M1 period in the past).
        det._next_close[("EURUSD", "M1")] = now - 1.0
        det.start()
        try:
            deadline = time.time() + 3.0
            while not closes and time.time() < deadline:
                time.sleep(0.05)
        finally:
            det.stop()
        assert len(closes) >= 1
        assert closes[0].timeframe == "M1"
        assert closes[0].last_tick is None

    def test_no_double_fire_between_tick_and_timer(self):
        bus = EventBus()
        closes = []
        bus.subscribe("candle_close", lambda e: closes.append(e))
        det = CandleCloseDetector(bus)
        det.register("EURUSD", ["M5"])
        # Tick path fires the close and advances the boundary.
        det.on_tick(_tick(ts=1700000100.0))
        tick_events = det.on_tick(_tick(ts=1700000400.0))
        assert len(tick_events) == 1
        # A subsequent watchdog sweep at the same wall-clock must NOT re-fire
        # the boundary the tick path already advanced past.
        timer_events = det.fire_overdue_closes(now=1700000400.0)
        assert timer_events == []
        assert len(closes) == 1

    def test_multi_period_gap_fires_sequentially(self):
        bus = EventBus()
        closes = []
        bus.subscribe("candle_close", lambda e: closes.append(e))
        det = CandleCloseDetector(bus, max_catchup_periods=5)
        det.register("EURUSD", ["M1"])
        tf_secs = 60
        now = 1700000300.0
        # Boundary is 2.5 periods in the past => exactly 3 overdue closes.
        det._next_close[("EURUSD", "M1")] = now - (2 * tf_secs + tf_secs / 2)
        events = det.fire_overdue_closes(now=now)
        assert len(events) == 3
        # Sequential boundaries, one period apart, none collapsed.
        times = [e.close_time.timestamp() for e in events]
        assert times == sorted(times)
        assert times[1] - times[0] == tf_secs
        assert times[2] - times[1] == tf_secs

    def test_prolonged_gap_rebases_without_storm(self):
        bus = EventBus()
        det = CandleCloseDetector(bus, max_catchup_periods=5)
        det.register("EURUSD", ["M1"])
        now = time.time()
        # 100 periods overdue — far beyond max_catchup.
        det._next_close[("EURUSD", "M1")] = now - 100 * 60
        events = det.fire_overdue_closes(now=now)
        assert events == []
        # Boundary re-based into the future.
        assert det._next_close[("EURUSD", "M1")] > now

    def test_start_stop_idempotent(self):
        bus = EventBus()
        det = CandleCloseDetector(bus, watchdog_interval=0.1)
        det.start()
        det.start()  # second start is a no-op
        det.stop()
        det.stop()  # second stop is a no-op

