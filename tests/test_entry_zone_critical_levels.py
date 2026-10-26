"""Tests for the active broker-side SL/TP tick critical-level refresh.

The old EntryOrchestrator/zone-boundary registration is retired with the legacy
directional entry stack. The live execution plane must still bypass tick
coalescing near broker-confirmed stop-loss and take-profit levels.
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


def _system_with_store(store):
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._tick_store = store
    return system


def test_live_sl_and_tp_levels_bypass_tick_coalescing():
    store = TickStore(max_hz=15.0, critical_distance=0.0005)
    system = _system_with_store(store)
    positions = [
        SimpleNamespace(symbol="EURUSD", sl=1.2000, tp=1.2050),
    ]
    system._refresh_tick_store_critical_levels(positions)

    # An ordinary distant tick is stored once, then coalesced.
    assert store.put(_fast_tick("EURUSD", 1.5000)) is True
    assert store.put(_fast_tick("EURUSD", 1.5000)) is False

    # Broker-side SL and TP are critical levels and bypass coalescing.
    assert store.put(_fast_tick("EURUSD", 1.2000)) is True
    assert store.put(_fast_tick("EURUSD", 1.2050)) is True


def test_refresh_clears_levels_after_position_leaves_live_book():
    store = TickStore(max_hz=15.0, critical_distance=0.0005)
    system = _system_with_store(store)
    system._refresh_tick_store_critical_levels([
        SimpleNamespace(symbol="EURUSD", sl=1.2000, tp=1.2050),
    ])
    assert store.put(_fast_tick("EURUSD", 1.2000)) is True
    # The live-book refresh removes a closed position's old levels.
    system._refresh_tick_store_critical_levels([])
    assert store.put(_fast_tick("EURUSD", 1.2000)) is False


def test_zero_or_missing_levels_are_not_registered():
    store = TickStore(max_hz=15.0, critical_distance=0.0005)
    system = _system_with_store(store)
    system._refresh_tick_store_critical_levels([
        SimpleNamespace(symbol="GBPUSD", sl=0.0, tp=0.0),
    ])
    assert store.put(_fast_tick("GBPUSD", 1.3000)) is True
    assert store.put(_fast_tick("GBPUSD", 1.3000)) is False


def test_missing_tick_store_is_a_safe_noop():
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    positions = [SimpleNamespace(symbol="EURUSD", sl=1.2, tp=1.3)]
    assert system._refresh_tick_store_critical_levels(positions) == positions
