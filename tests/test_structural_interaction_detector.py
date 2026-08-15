"""Tests for tick.structural_interaction_detector (Violation #2).

The detector watches the live tick stream against the CONFIRMED WorldModel's
already-computed structural levels and flags inter-candle interactions (level
touch / sweep / break, FVG fill), recording them as short-lived non-directional
Evidence and waking the Brain — without re-running the brain modules.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from brain.world_model import WorldModelStore, build_world_model
from tick.event_bus import EventBus
from tick.models import Tick
from tick.structural_interaction_detector import (
    BREAK,
    FVG_FILL,
    SWEEP,
    TOUCH,
    StructuralInteractionDetector,
    _extract_levels,
)


# ── Fixtures / helpers ──────────────────────────────────────────────────────


class _FakeLoop:
    """Captures maybe_reason_on_change calls."""

    def __init__(self):
        self.calls = []

    def maybe_reason_on_change(self, symbol, change_magnitude=None, **kw):
        self.calls.append((symbol, change_magnitude))
        return True


def _tick(symbol, mid, epoch, spread=0.0002):
    return Tick(
        symbol=symbol,
        bid=mid - spread / 2.0,
        ask=mid + spread / 2.0,
        timestamp=datetime.fromtimestamp(epoch, tz=timezone.utc),
        source="test",
    )


def _fvg(top, bottom, status="OPEN", kind="BULLISH"):
    return SimpleNamespace(
        kind=kind, top=top, bottom=bottom, midpoint=(top + bottom) / 2.0, status=status,
    )


def _ob(top, bottom, status="FRESH", kind="BULLISH"):
    return SimpleNamespace(
        kind=kind, top=top, bottom=bottom, midpoint=(top + bottom) / 2.0, status=status,
    )


def _liq(price, kind="SELL_SIDE", swept=False):
    return SimpleNamespace(price=price, kind=kind, swept=swept)


def _liqmap(buy=None, sell=None):
    return SimpleNamespace(
        buy_side_liquidity=buy or [], sell_side_liquidity=sell or [],
        nearest_buy_liq=None, nearest_sell_liq=None,
        dominant_liquidity_side="BALANCED", current_price=0.0,
    )


def _struct(swing_high=0.0, swing_low=0.0, bos=0.0, choch=0.0):
    return SimpleNamespace(
        swing_high=swing_high, swing_low=swing_low,
        last_bos_level=bos, last_choch_level=choch,
    )


def _wm(symbol="EURUSD", **kw):
    return build_world_model(symbol=symbol, version=1, **kw)


def _detector(store, bus, loop=None, **kw):
    params = dict(
        world_model_store=store, event_bus=bus, cognition_loop=loop,
        pip_size_provider=lambda s: 0.0001, tolerance_pips=2.0,
        sweep_window_seconds=5.0, emit_cooldown_seconds=0.0,
    )
    params.update(kw)
    return StructuralInteractionDetector(**params)


def _feed(bus, mids, *, symbol="EURUSD", start=1_000_000.0, step=1.0):
    """Publish a sequence of ticks (1s apart by default). Returns the next epoch
    (last tick + step), suitable to pass as ``evidence_for(now=...)``."""
    t = start
    for mid in mids:
        bus.publish("tick", _tick(symbol, mid, t))
        t += step
    return t


# ── Level extraction ────────────────────────────────────────────────────────


def test_extract_levels_selects_active_only():
    wm = _wm(
        fvgs={"M5": [_fvg(1.106, 1.105), _fvg(1.120, 1.119, status="FILLED")]},
        order_blocks={"H1": [_ob(1.090, 1.088), _ob(1.070, 1.069, status="BROKEN")]},
        liquidity={"M15": _liqmap(
            sell=[_liq(1.100), _liq(1.095, swept=True)],
            buy=[_liq(1.115, kind="BUY_SIDE")],
        )},
        structure={"H1": _struct(swing_high=1.130, swing_low=1.080)},
    )
    levels = _extract_levels(wm, 48)
    kinds = {(lv.kind, lv.side) for lv in levels}
    # Active FVG + OB kept; FILLED FVG and BROKEN OB dropped.
    assert ("fvg", "bullish") in kinds
    assert sum(1 for lv in levels if lv.kind == "fvg") == 1
    assert sum(1 for lv in levels if lv.kind == "order_block") == 1
    # Un-swept liquidity kept, swept dropped.
    liq = [lv for lv in levels if lv.kind == "liquidity"]
    assert len(liq) == 2 and all(not (abs(lv.ref - 1.095) < 1e-9) for lv in liq)
    # Both structure levels surfaced as points.
    struct = {lv.side for lv in levels if lv.kind == "structure"}
    assert struct == {"swing_high", "swing_low"}


def test_extract_levels_capped():
    fvgs = [_fvg(1.100 + 0.001 * i + 0.0005, 1.100 + 0.001 * i) for i in range(50)]
    wm = _wm(fvgs={"M5": fvgs})
    assert len(_extract_levels(wm, 10)) == 10


# ── Interaction detection ───────────────────────────────────────────────────


def test_sweep_detected_on_pierce_and_reclaim():
    store = WorldModelStore()
    store.publish(_wm(liquidity={"M15": _liqmap(sell=[_liq(1.1000, "SELL_SIDE")])}))
    bus, loop = EventBus(), _FakeLoop()
    det = _detector(store, bus, loop)
    det.start()
    # approach, pierce below, reclaim above the pool
    t = _feed(bus, (1.1020, 1.1005, 1.09960, 1.10030))
    evs = det.evidence_for("EURUSD", now=t)
    assert [e.measurements["interaction"] for e in evs] == [SWEEP]
    assert evs[0].measurements["level_kind"] == "liquidity"
    assert evs[0].domain.value == "liquidity"
    assert loop.calls and loop.calls[-1] == ("EURUSD", None)  # bare-nudge wake


def test_break_detected_when_cross_holds_through():
    store = WorldModelStore()
    store.publish(_wm(structure={"H1": _struct(swing_high=1.1100)}))
    bus, loop = EventBus(), _FakeLoop()
    det = _detector(store, bus, loop, sweep_window_seconds=5.0)
    det.start()
    bus.publish("tick", _tick("EURUSD", 1.1080, 1_000_000.0))  # prime below
    bus.publish("tick", _tick("EURUSD", 1.1105, 1_000_001.0))  # cross above
    bus.publish("tick", _tick("EURUSD", 1.1106, 1_000_007.0))  # hold past window
    evs = det.evidence_for("EURUSD", now=1_000_008.0)
    assert any(e.measurements["interaction"] == BREAK for e in evs)


def test_fvg_fill_detected_on_zone_entry():
    store = WorldModelStore()
    store.publish(_wm(fvgs={"M5": [_fvg(1.1060, 1.1050)]}))
    bus, loop = EventBus(), _FakeLoop()
    det = _detector(store, bus, loop)
    det.start()
    bus.publish("tick", _tick("EURUSD", 1.1030, 1_000_000.0))  # prime outside (below)
    bus.publish("tick", _tick("EURUSD", 1.1055, 1_000_001.0))  # enter the gap
    evs = det.evidence_for("EURUSD", now=1_000_002.0)
    assert [e.measurements["interaction"] for e in evs] == [FVG_FILL]
    assert evs[0].measurements["zone_low"] == 1.105 and evs[0].measurements["zone_high"] == 1.106


def test_touch_detected_within_tolerance():
    store = WorldModelStore()
    store.publish(_wm(structure={"H1": _struct(swing_low=1.0950)}))
    bus = EventBus()
    det = _detector(store, bus)
    det.start()
    bus.publish("tick", _tick("EURUSD", 1.0980, 1_000_000.0))   # prime away
    bus.publish("tick", _tick("EURUSD", 1.09501, 1_000_001.0))  # within 2-pip tol
    evs = det.evidence_for("EURUSD", now=1_000_002.0)
    assert [e.measurements["interaction"] for e in evs] == [TOUCH]


def test_evidence_is_non_directional_and_expires():
    store = WorldModelStore()
    store.publish(_wm(liquidity={"M15": _liqmap(sell=[_liq(1.1000)])}))
    bus = EventBus()
    det = _detector(store, bus, evidence_ttl_seconds=30.0)
    det.start()
    t = _feed(bus, (1.1020, 1.0996, 1.1003))
    evs = det.evidence_for("EURUSD", now=t)
    assert evs and all(e.polarity == 0.0 for e in evs)          # never a lean
    # No directional measurement keys leak in.
    banned = {"direction", "bias", "lean", "signal", "score", "dominant"}
    assert all(not (banned & set(e.measurements)) for e in evs)
    # Ages out past the TTL.
    assert det.evidence_for("EURUSD", now=t + 120.0) == []


def test_cooldown_debounces_repeated_touches():
    store = WorldModelStore()
    store.publish(_wm(structure={"H1": _struct(swing_low=1.0950)}))
    bus = EventBus()
    det = _detector(store, bus, emit_cooldown_seconds=60.0)
    det.start()
    bus.publish("tick", _tick("EURUSD", 1.0980, 1_000_000.0))   # prime away
    bus.publish("tick", _tick("EURUSD", 1.09501, 1_000_001.0))  # touch (emit)
    bus.publish("tick", _tick("EURUSD", 1.0980, 1_000_002.0))   # leave
    bus.publish("tick", _tick("EURUSD", 1.09501, 1_000_003.0))  # touch again (cooled)
    assert det.stats()["by_type"].get(TOUCH) == 1


# ── Cache refresh / wiring ──────────────────────────────────────────────────


def test_world_model_update_refreshes_cached_levels():
    store = WorldModelStore()
    bus = EventBus()
    det = _detector(store, bus)
    det.start()
    # No model yet: a tick primes nothing.
    bus.publish("tick", _tick("EURUSD", 1.1020, 1_000_000.0))
    assert det.stats()["by_type"] == {}
    # Publish a model + fire the candle-close refresh event, then sweep.
    store.publish(_wm(liquidity={"M15": _liqmap(sell=[_liq(1.1000)])}))
    bus.publish("world_model_update", "EURUSD")
    _feed(bus, (1.1020, 1.0996, 1.1003), start=1_000_001.0)
    assert det.stats()["by_type"].get(SWEEP) == 1


def test_disabled_detector_is_noop():
    store = WorldModelStore()
    store.publish(_wm(liquidity={"M15": _liqmap(sell=[_liq(1.1000)])}))
    bus, loop = EventBus(), _FakeLoop()
    det = _detector(store, bus, loop, enabled=False)
    det.start()  # no subscription
    for mid in (1.1020, 1.0996, 1.1003):
        bus.publish("tick", _tick("EURUSD", mid, 1_000_000.0))
    assert det.evidence_for("EURUSD") == [] and loop.calls == []


def test_tick_is_fault_safe_when_store_raises():
    class _BoomStore:
        def get(self, symbol):
            raise RuntimeError("store down")
    bus = EventBus()
    det = _detector(_BoomStore(), bus)
    det.start()
    # Must not raise despite the store faulting on the lazy refresh.
    bus.publish("tick", _tick("EURUSD", 1.1000, 1_000_000.0))
    assert det.evidence_for("EURUSD") == []


def test_garbage_tick_ignored():
    store = WorldModelStore()
    store.publish(_wm(liquidity={"M15": _liqmap(sell=[_liq(1.1000)])}))
    bus = EventBus()
    det = _detector(store, bus)
    det.start()
    bus.publish("tick", SimpleNamespace(symbol="", mid=0.0))  # no symbol / price
    bus.publish("tick", None)
    assert det.evidence_for("EURUSD") == []


def test_shutdown_unsubscribes():
    store = WorldModelStore()
    store.publish(_wm(liquidity={"M15": _liqmap(sell=[_liq(1.1000)])}))
    bus = EventBus()
    det = _detector(store, bus)
    det.start()
    assert bus.subscriber_count("tick") == 1
    det.shutdown()
    assert bus.subscriber_count("tick") == 0
    assert bus.subscriber_count("world_model_update") == 0
