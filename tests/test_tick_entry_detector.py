"""Tests for entry.tick_entry_detector — TickEntryDetector."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from brain.fvg_detector import FairValueGap, FVGStatus
from brain.world_model import WorldModelStore, build_world_model
from entry.models import EntryConfig, EntryState, EntryZone, ZoneType
from entry.tick_entry_detector import TickEntryDetector
from entry.zone_watcher import ZoneWatcher


def _ts() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FakeTick:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime


def _make_zone(
    symbol="EURUSD", direction="LONG", top=1.0850, bottom=1.0840,
    midpoint=1.0845, conviction=85, has_sweep=False,
) -> EntryZone:
    now = datetime.now(timezone.utc)
    return EntryZone(
        symbol=symbol,
        direction=direction,
        zone_type=ZoneType.FVG_MIDPOINT,
        top=top,
        bottom=bottom,
        midpoint=midpoint,
        invalidation_level=bottom - 0.0005 if direction == "LONG" else top + 0.0005,
        conviction=conviction,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        timeframe="M5",
        has_sweep=has_sweep,
    )


def _setup_detector(zones=None, config=None):
    store = WorldModelStore()
    watcher = ZoneWatcher(store, config=config)
    detector = TickEntryDetector(
        zone_watcher=watcher,
        config=config,
        pip_size_lookup=lambda _: 0.0001,
    )
    if zones:
        with watcher._lock:
            for z in zones:
                watcher._zones.setdefault(z.symbol, []).append(z)
    return detector, watcher


class TestTickEntryDetectorBasic:
    def test_no_zones_returns_none(self):
        detector, _ = _setup_detector()
        tick = FakeTick("EURUSD", 1.0845, 1.0846, datetime.now(timezone.utc))
        assert detector.on_tick(tick) is None

    def test_price_inside_zone_triggers(self):
        zone = _make_zone()
        detector, _ = _setup_detector(zones=[zone])
        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        result = detector.on_tick(tick)
        assert result is not None
        assert result.state == EntryState.ZONE_TOUCHED
        assert result.symbol == "EURUSD"
        assert result.direction == "LONG"

    def test_price_within_proximity_triggers(self):
        zone = _make_zone()
        cfg = EntryConfig(zone_proximity_pips=5.0)
        detector, _ = _setup_detector(zones=[zone], config=cfg)
        tick = FakeTick("EURUSD", 1.0852, 1.0853, datetime.now(timezone.utc))
        result = detector.on_tick(tick)
        assert result is not None

    def test_price_far_away_no_trigger(self):
        zone = _make_zone()
        cfg = EntryConfig(zone_proximity_pips=2.0)
        detector, _ = _setup_detector(zones=[zone], config=cfg)
        tick = FakeTick("EURUSD", 1.0880, 1.0881, datetime.now(timezone.utc))
        result = detector.on_tick(tick)
        assert result is None


class TestTickEntryDetectorShort:
    def test_short_zone_uses_bid(self):
        zone = _make_zone(direction="SHORT", top=1.0860, bottom=1.0850)
        detector, _ = _setup_detector(zones=[zone])
        tick = FakeTick("EURUSD", 1.0855, 1.0856, datetime.now(timezone.utc))
        result = detector.on_tick(tick)
        assert result is not None
        assert result.direction == "SHORT"


class TestTickEntryDetectorInvalidation:
    def test_invalidation_cancels_zone(self):
        zone = _make_zone()
        detector, _ = _setup_detector(zones=[zone])
        tick = FakeTick("EURUSD", 1.0830, 1.0831, datetime.now(timezone.utc))
        result = detector.on_tick(tick)
        assert result is None
        tick2 = FakeTick("EURUSD", 1.0845, 1.0846, datetime.now(timezone.utc))
        result2 = detector.on_tick(tick2)
        assert result2 is None


class TestTickEntryDetectorDuplicate:
    def test_same_zone_not_triggered_twice(self):
        zone = _make_zone()
        detector, _ = _setup_detector(zones=[zone])
        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        result1 = detector.on_tick(tick)
        assert result1 is not None
        result2 = detector.on_tick(tick)
        assert result2 is None

    def test_max_concurrent_pending_respected(self):
        zones = [
            _make_zone(symbol=f"PAIR{i}", top=1.085, bottom=1.084)
            for i in range(10)
        ]
        cfg = EntryConfig(max_concurrent_pending=3)
        detector, _ = _setup_detector(zones=zones, config=cfg)
        hits = 0
        for i in range(10):
            tick = FakeTick(f"PAIR{i}", 1.0845, 1.0846, datetime.now(timezone.utc))
            if detector.on_tick(tick) is not None:
                hits += 1
        assert hits == 3


class TestTickEntryDetectorSpread:
    def test_wide_spread_rejects(self):
        zone = _make_zone()
        cfg = EntryConfig(max_spread_multiplier=1.0)
        detector, _ = _setup_detector(zones=[zone], config=cfg)
        tick = FakeTick("EURUSD", 1.0840, 1.0860, datetime.now(timezone.utc))
        result = detector.on_tick(tick)
        assert result is None


class TestTickEntryDetectorCallbacks:
    def test_on_zone_touch_callback_fires(self):
        called = []
        zone = _make_zone()
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        with watcher._lock:
            watcher._zones["EURUSD"] = [zone]
        detector = TickEntryDetector(
            zone_watcher=watcher,
            on_zone_touch=lambda p: called.append(p),
            pip_size_lookup=lambda _: 0.0001,
        )
        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        detector.on_tick(tick)
        assert len(called) == 1

    def test_cancel_pending_removes_entry(self):
        zone = _make_zone()
        detector, _ = _setup_detector(zones=[zone])
        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        detector.on_tick(tick)
        assert "EURUSD" in detector.pending_entries
        detector.cancel_pending("EURUSD")
        assert "EURUSD" not in detector.pending_entries

    def test_reset_clears_everything(self):
        zone = _make_zone()
        detector, _ = _setup_detector(zones=[zone])
        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        detector.on_tick(tick)
        detector.reset()
        assert len(detector.pending_entries) == 0
