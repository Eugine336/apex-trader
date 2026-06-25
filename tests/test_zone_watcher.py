"""Tests for entry.zone_watcher — ZoneWatcher."""

from datetime import datetime, timedelta, timezone

import pytest

from brain.fvg_detector import FairValueGap, FVGStatus
from brain.order_block import OrderBlock, OBStatus
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend
from brain.world_model import WorldModelStore, build_world_model
from entry.models import EntryConfig, ZoneType
from entry.zone_watcher import ZoneWatcher, extract_entry_zones

import pandas as pd


def _ts(offset_minutes: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


def _make_fvg(
    kind="BULLISH", top=1.0850, bottom=1.0840, midpoint=1.0845,
    status=FVGStatus.OPEN, timeframe="M5",
) -> FairValueGap:
    return FairValueGap(
        kind=kind, top=top, bottom=bottom, midpoint=midpoint,
        size_pips=10.0, strength="STRONG", status=status,
        candle_index=50, timestamp=_ts(), timeframe=timeframe,
    )


def _make_ob(
    kind="BULLISH", top=1.0855, bottom=1.0835, midpoint=1.0845,
    status=OBStatus.FRESH, timeframe="H1",
) -> OrderBlock:
    return OrderBlock(
        kind=kind, top=top, bottom=bottom, midpoint=midpoint,
        origin_index=30, strength="STRONG", status=status,
        impulse_size=25.0, timestamp=_ts(), timeframe=timeframe,
        breaker=False,
    )


def _make_structure(trend=Trend.BULLISH) -> StructureAnalysis:
    return StructureAnalysis(
        trend=trend, last_event=StructureEvent.BOS_BULLISH,
        swing_high=1.0900, swing_low=1.0800,
        last_bos_level=1.0870, last_choch_level=None,
        structure_broken=False,
        bullish_swing_points=[], bearish_swing_points=[],
        confidence=0.8,
    )


def _publish_model(store, symbol, fvgs=None, order_blocks=None, structure=None, bias=None):
    wm = build_world_model(
        symbol=symbol,
        version=store.next_version(),
        fvgs=fvgs,
        order_blocks=order_blocks,
        structure=structure,
        bias=bias,
    )
    store.publish(wm)


class TestZoneWatcherBasic:
    def test_no_zones_without_model(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        assert watcher.get_active_zones("EURUSD") == []

    def test_fvg_creates_zone(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        _publish_model(store, "EURUSD", fvgs={"M5": [_make_fvg()]})
        watcher.on_world_model_update("EURUSD")
        zones = watcher.get_active_zones("EURUSD")
        assert len(zones) == 1
        assert zones[0].zone_type == ZoneType.FVG_MIDPOINT
        assert zones[0].direction == "LONG"

    def test_ob_creates_zone(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        _publish_model(store, "GBPUSD", order_blocks={"H1": [_make_ob()]})
        watcher.on_world_model_update("GBPUSD")
        zones = watcher.get_active_zones("GBPUSD")
        assert len(zones) == 1
        assert zones[0].zone_type == ZoneType.OB_MIDPOINT

    def test_fvg_ob_overlap_creates_overlap_zone(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        fvg = _make_fvg(top=1.0850, bottom=1.0840)
        ob = _make_ob(top=1.0855, bottom=1.0835)
        _publish_model(
            store, "EURUSD",
            fvgs={"M5": [fvg]},
            order_blocks={"H1": [ob]},
        )
        watcher.on_world_model_update("EURUSD")
        zones = watcher.get_active_zones("EURUSD")
        overlap = [z for z in zones if z.zone_type == ZoneType.FVG_OB_OVERLAP]
        assert len(overlap) == 1
        assert overlap[0].top == min(fvg.top, ob.top)
        assert overlap[0].bottom == max(fvg.bottom, ob.bottom)

    def test_bearish_fvg_creates_short_zone(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        fvg = _make_fvg(kind="BEARISH")
        _publish_model(
            store, "EURUSD",
            fvgs={"M5": [fvg]},
            structure={"H4": _make_structure(Trend.BEARISH)},
        )
        watcher.on_world_model_update("EURUSD")
        zones = watcher.get_active_zones("EURUSD")
        shorts = [z for z in zones if z.direction == "SHORT"]
        assert len(shorts) >= 1


class TestZoneWatcherFiltering:
    def test_filled_fvg_ignored(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        fvg = _make_fvg(status=FVGStatus.FILLED)
        _publish_model(store, "EURUSD", fvgs={"M5": [fvg]})
        watcher.on_world_model_update("EURUSD")
        assert watcher.get_active_zones("EURUSD") == []

    def test_broken_ob_ignored(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        ob = _make_ob(status=OBStatus.BROKEN)
        _publish_model(store, "EURUSD", order_blocks={"H1": [ob]})
        watcher.on_world_model_update("EURUSD")
        assert watcher.get_active_zones("EURUSD") == []

    def test_expired_zone_filtered_on_read(self):
        store = WorldModelStore()
        cfg = EntryConfig(zone_expiry_seconds=0.001)
        watcher = ZoneWatcher(store, config=cfg)
        _publish_model(store, "EURUSD", fvgs={"M5": [_make_fvg()]})
        watcher.on_world_model_update("EURUSD")
        import time
        time.sleep(0.01)
        assert watcher.get_active_zones("EURUSD") == []

    def test_stale_version_skipped(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        _publish_model(store, "EURUSD", fvgs={"M5": [_make_fvg()]})
        watcher.on_world_model_update("EURUSD")
        assert len(watcher.get_active_zones("EURUSD")) == 1
        watcher.on_world_model_update("EURUSD")
        assert len(watcher.get_active_zones("EURUSD")) == 1


class TestZoneWatcherBias:
    def test_bullish_bias_filters_short_zones(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        fvg = _make_fvg(kind="BEARISH")
        struct = _make_structure(Trend.BULLISH)
        _publish_model(
            store, "EURUSD",
            fvgs={"M5": [fvg]},
            structure={"H4": struct},
        )
        watcher.on_world_model_update("EURUSD")
        zones = watcher.get_active_zones("EURUSD")
        shorts = [z for z in zones if z.direction == "SHORT"]
        assert len(shorts) == 0

    def test_no_bias_allows_both_directions(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        bull_fvg = _make_fvg(kind="BULLISH", top=1.085, bottom=1.084)
        bear_fvg = _make_fvg(kind="BEARISH", top=1.090, bottom=1.089)
        struct = _make_structure(Trend.RANGING)
        _publish_model(
            store, "EURUSD",
            fvgs={"M5": [bull_fvg, bear_fvg]},
            structure={"H4": struct},
        )
        watcher.on_world_model_update("EURUSD")
        zones = watcher.get_active_zones("EURUSD")
        dirs = {z.direction for z in zones}
        assert "LONG" in dirs
        assert "SHORT" in dirs

    def test_clear_removes_zones(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        _publish_model(store, "EURUSD", fvgs={"M5": [_make_fvg()]})
        watcher.on_world_model_update("EURUSD")
        assert len(watcher.get_active_zones("EURUSD")) >= 1
        watcher.clear("EURUSD")
        assert watcher.get_active_zones("EURUSD") == []

    def test_all_symbols_with_zones(self):
        store = WorldModelStore()
        watcher = ZoneWatcher(store)
        _publish_model(store, "EURUSD", fvgs={"M5": [_make_fvg()]})
        _publish_model(store, "GBPUSD", fvgs={"M5": [_make_fvg()]})
        watcher.on_world_model_update("EURUSD")
        watcher.on_world_model_update("GBPUSD")
        syms = watcher.all_symbols_with_zones()
        assert "EURUSD" in syms
        assert "GBPUSD" in syms


class TestCounterTrendConvictionPenalty:
    """Bug #4 — counter-trend zones get a conviction penalty so geometry-only
    counter-trend setups fall below the score gate."""

    def _model(self, store, fvgs=None, order_blocks=None, structure=None):
        return build_world_model(
            symbol="EURUSD",
            version=store.next_version(),
            fvgs=fvgs,
            order_blocks=order_blocks,
            structure=structure,
        )

    def test_counter_trend_overlap_penalised(self):
        store = WorldModelStore()
        # Bearish FVG+OB overlap under BULLISH HTF bias → counter-trend SHORT.
        fvg = _make_fvg(kind="BEARISH", top=1.0850, bottom=1.0840)
        ob = _make_ob(kind="BEARISH", top=1.0855, bottom=1.0835)
        model = self._model(
            store,
            fvgs={"M5": [fvg]},
            order_blocks={"H1": [ob]},
            structure={"H4": _make_structure(Trend.BULLISH)},
        )
        zones = extract_entry_zones(model, EntryConfig(ev_gate_enabled=False))
        overlap = [z for z in zones if z.zone_type == ZoneType.FVG_OB_OVERLAP]
        assert len(overlap) == 1
        assert overlap[0].is_counter_trend is True
        # Legacy gate: 100 * 0.70 = 70 → below the 85 score gate. (Under the
        # default EV gate the haircut is softened to 0.85 → covered in
        # tests/test_ev_gate.py.)
        assert overlap[0].conviction == 70

    def test_with_trend_overlap_unpenalised(self):
        store = WorldModelStore()
        # Bullish FVG+OB overlap under BULLISH HTF bias → with-trend LONG.
        fvg = _make_fvg(kind="BULLISH", top=1.0850, bottom=1.0840)
        ob = _make_ob(kind="BULLISH", top=1.0855, bottom=1.0835)
        model = self._model(
            store,
            fvgs={"M5": [fvg]},
            order_blocks={"H1": [ob]},
            structure={"H4": _make_structure(Trend.BULLISH)},
        )
        zones = extract_entry_zones(model, EntryConfig())
        overlap = [z for z in zones if z.zone_type == ZoneType.FVG_OB_OVERLAP]
        assert len(overlap) == 1
        assert overlap[0].is_counter_trend is False
        assert overlap[0].conviction == 100

    def test_penalty_multiplier_configurable(self):
        store = WorldModelStore()
        fvg = _make_fvg(kind="BEARISH", top=1.0850, bottom=1.0840)
        ob = _make_ob(kind="BEARISH", top=1.0855, bottom=1.0835)
        model = self._model(
            store,
            fvgs={"M5": [fvg]},
            order_blocks={"H1": [ob]},
            structure={"H4": _make_structure(Trend.BULLISH)},
        )
        cfg = EntryConfig(counter_trend_conviction_mult=0.5, ev_gate_enabled=False)
        zones = extract_entry_zones(model, cfg)
        overlap = [z for z in zones if z.zone_type == ZoneType.FVG_OB_OVERLAP]
        assert overlap[0].conviction == 50
