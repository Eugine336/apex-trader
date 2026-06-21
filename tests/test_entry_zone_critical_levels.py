"""Tests for entry-zone critical-level registration (P1 dormant activation).

``TickStore`` Hz-coalescing can silently drop ticks during a volatility spike.
Active entry-zone boundaries must be registered as critical levels so a tick
arriving inside the arming band bypasses coalescing — otherwise the zone touch
(and the entry it would trigger) is lost. This exercises
``EventDrivenSystem._register_entry_zone_critical_levels`` without standing up
the full system.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from event_driven_bootstrap import EventDrivenSystem
from tick.models import Tick
from tick.tick_store import TickStore


def _fast_tick(symbol: str, price: float) -> Tick:
    return Tick(
        symbol=symbol,
        bid=price - 0.0001,
        ask=price + 0.0001,
        timestamp=datetime.now(timezone.utc),
        source="test",
    )


def _system_with_zones(store, zones_by_symbol):
    zw = SimpleNamespace(
        all_symbols_with_zones=lambda: list(zones_by_symbol.keys()),
        get_active_zones=lambda sym: zones_by_symbol.get(sym, []),
    )
    orch = SimpleNamespace(zone_watcher=zw)
    sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
    sys_obj._entry_orchestrator = orch
    sys_obj._tick_store = store
    return sys_obj


def test_zone_boundaries_registered_as_critical_levels():
    store = TickStore(max_hz=15.0, critical_distance=0.0005)
    zones = {"EURUSD": [SimpleNamespace(top=1.2050, bottom=1.2000)]}
    _system_with_zones(store, zones)._register_entry_zone_critical_levels()

    # First tick on an idle symbol always stores.
    assert store.put(_fast_tick("EURUSD", 1.2000)) is True
    # A second tick within the coalescing window but FAR from any zone is
    # coalesced away.
    assert store.put(_fast_tick("EURUSD", 1.5000)) is False
    # A tick near the registered zone bottom bypasses coalescing.
    assert store.put(_fast_tick("EURUSD", 1.2000)) is True
    # A tick near the registered zone top also bypasses.
    assert store.put(_fast_tick("EURUSD", 1.2050)) is True


def test_no_orchestrator_is_noop():
    store = TickStore(max_hz=15.0)
    sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
    sys_obj._entry_orchestrator = None
    sys_obj._tick_store = store
    # Must not raise; nothing registered.
    sys_obj._register_entry_zone_critical_levels()
    assert store.put(_fast_tick("EURUSD", 1.2000)) is True
    assert store.put(_fast_tick("EURUSD", 1.2000)) is False


def test_zero_boundaries_skipped():
    store = TickStore(max_hz=15.0, critical_distance=0.0005)
    zones = {"GBPUSD": [SimpleNamespace(top=0.0, bottom=0.0)]}
    _system_with_zones(store, zones)._register_entry_zone_critical_levels()
    assert store.put(_fast_tick("GBPUSD", 1.3000)) is True
    # No valid level registered → still coalesced.
    assert store.put(_fast_tick("GBPUSD", 1.3000)) is False
