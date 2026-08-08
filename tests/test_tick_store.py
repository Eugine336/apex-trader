"""Tests for tick.tick_store — ring buffer with Hz coalescing."""

import threading
import time
from datetime import datetime, timezone

import pytest

from tick.models import Tick
from tick.tick_store import TickStore


def _tick(symbol: str = "EURUSD", bid: float = 1.1000, ask: float = 1.1002,
          ts: float = 1700000000.0, source: str = "mt5") -> Tick:
    return Tick(
        symbol=symbol,
        bid=bid,
        ask=ask,
        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
        source=source,
    )


class TestTickStoreBasics:
    def test_put_and_get_latest(self):
        store = TickStore(max_hz=1000.0)
        t = _tick()
        assert store.put(t) is True
        latest = store.get_latest("EURUSD")
        assert latest is not None
        assert latest.bid == 1.1

    def test_get_latest_unknown_symbol(self):
        store = TickStore()
        assert store.get_latest("UNKNOWN") is None

    def test_ring_buffer_capacity(self):
        store = TickStore(max_ticks_per_symbol=5, max_hz=1e9)
        for i in range(10):
            store.put(_tick(bid=1.1 + i * 0.0001, ts=1700000000.0 + i))
        recent = store.get_recent("EURUSD", count=100)
        assert len(recent) == 5
        assert recent[0].bid == pytest.approx(1.1005, abs=1e-6)

    def test_get_recent_empty(self):
        store = TickStore()
        assert store.get_recent("EURUSD") == []

    def test_snapshot(self):
        store = TickStore(max_hz=10000.0)
        store.put(_tick("EURUSD"))
        store.put(_tick("GBPUSD", bid=1.25, ask=1.2502))
        snap = store.snapshot()
        assert "EURUSD" in snap
        assert "GBPUSD" in snap

    def test_symbols(self):
        store = TickStore(max_hz=10000.0)
        store.put(_tick("EURUSD"))
        store.put(_tick("GBPUSD", bid=1.25, ask=1.2502))
        assert sorted(store.symbols()) == ["EURUSD", "GBPUSD"]


class TestTickCoalescing:
    def test_coalescing_drops_fast_ticks(self):
        store = TickStore(max_hz=2.0)
        t1 = _tick(ts=1700000000.0)
        t2 = _tick(bid=1.1001, ts=1700000000.1)
        t3 = _tick(bid=1.1002, ts=1700000000.2)
        assert store.put(t1) is True
        assert store.put(t2) is False
        assert store.put(t3) is False
        latest = store.get_latest("EURUSD")
        assert latest.bid == pytest.approx(1.1002, abs=1e-6)

    def test_coalescing_allows_after_interval(self):
        store = TickStore(max_hz=2.0)
        t1 = _tick(ts=1700000000.0)
        assert store.put(t1) is True
        time.sleep(0.6)
        t2 = _tick(bid=1.1001, ts=1700000001.0)
        assert store.put(t2) is True

    def test_stats_track_coalescing(self):
        store = TickStore(max_hz=1.0)
        store.put(_tick(ts=1700000000.0))
        store.put(_tick(bid=1.1001, ts=1700000000.1))
        stats = store.stats()
        assert stats["ticks_received"] == 2
        assert stats["ticks_stored"] == 1
        assert stats["ticks_coalesced"] == 1


class TestCriticalLevelBypass:
    def test_bypass_near_critical_level(self):
        store = TickStore(max_hz=1.0, critical_distance=0.0005)
        store.register_critical_level("EURUSD", 1.1000)
        t1 = _tick(bid=1.2000, ask=1.2002, ts=1700000000.0)
        store.put(t1)
        t2 = _tick(bid=1.1002, ask=1.1004, ts=1700000000.05)
        result = store.put(t2)
        assert result is True
        stats = store.stats()
        assert stats["critical_bypasses"] >= 1

    def test_no_bypass_when_far_from_level(self):
        store = TickStore(max_hz=1.0, critical_distance=0.0005)
        store.register_critical_level("EURUSD", 1.2000)
        t1 = _tick(bid=1.1000, ask=1.1002, ts=1700000000.0)
        store.put(t1)
        t2 = _tick(bid=1.1001, ask=1.1003, ts=1700000000.05)
        result = store.put(t2)
        assert result is False

    def test_unregister_critical_level(self):
        store = TickStore(max_hz=1.0, critical_distance=0.0005)
        store.register_critical_level("EURUSD", 1.1000)
        store.unregister_critical_level("EURUSD", 1.1000)
        t1 = _tick(bid=1.1000, ts=1700000000.0)
        store.put(t1)
        t2 = _tick(bid=1.1001, ts=1700000000.05)
        assert store.put(t2) is False

    def test_clear_critical_levels(self):
        store = TickStore()
        store.register_critical_level("EURUSD", 1.1)
        store.register_critical_level("GBPUSD", 1.25)
        store.clear_critical_levels("EURUSD")
        store.clear_critical_levels()


class TestTickStoreThreadSafety:
    def test_concurrent_puts(self):
        store = TickStore(max_hz=10000.0)
        errors = []

        def writer(sym, n):
            try:
                for i in range(n):
                    store.put(_tick(sym, bid=1.1 + i * 0.0001, ts=1700000000.0 + i))
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("EURUSD", 100)),
            threading.Thread(target=writer, args=("GBPUSD", 100)),
            threading.Thread(target=writer, args=("EURUSD", 100)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == []
        assert store.get_latest("EURUSD") is not None
        assert store.get_latest("GBPUSD") is not None
