"""Tests for Phase 2 Feature A — stop-out flip (EntryOrchestrator).

The stop-out flip machinery lives on the orchestrator (evaluate_stopout_flip),
which the event-driven system calls from _on_trade_closed on a stop-loss fill.
These tests exercise the confirmation, the configurable guards (enabled /
cooldown / per-zone whipsaw cap), the emitted opposite-direction decision, and
the per-zone counter reset on a fresh zone touch — all without the full
event-driven system.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import entry.entry_orchestrator as orch_mod
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend
from brain.world_model import WorldModelStore, build_world_model
from entry.entry_orchestrator import EntryOrchestrator
from entry.models import EntryConfig, EntryZone, ZoneType


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_struct_trend(trend) -> StructureAnalysis:
    return StructureAnalysis(
        trend=trend, last_event=StructureEvent.NONE,
        swing_high=2010.0, swing_low=1990.0,
        last_bos_level=None, last_choch_level=None,
        structure_broken=False,
        bullish_swing_points=[], bearish_swing_points=[],
        confidence=0.6,
    )


def _make_orch(monkeypatch, config, tick_mom=0.5, m5_trend=None):
    """Orchestrator wired with a mockable tick_momentum and (optional) M5 trend.

    get_profile is patched to return None so every tuning knob resolves from the
    passed EntryConfig — the tests fully control the flip guards.
    """
    monkeypatch.setattr(orch_mod, "get_profile", lambda _s: None)
    store = WorldModelStore()
    decisions: list = []
    orch = EntryOrchestrator(
        world_model_store=store,
        config=config,
        pip_size_lookup=lambda _s: 0.0001,
        on_entry_decision=lambda d: decisions.append(d),
        get_tick_momentum=lambda s, d, p: tick_mom,
    )
    if m5_trend is not None:
        wm = build_world_model(
            symbol="XAUUSD",
            version=store.next_version(),
            structure={"M5": _make_struct_trend(m5_trend)},
        )
        store.publish(wm)
        orch.on_world_model_update("XAUUSD")
    return orch, decisions


def _flip(orch, **overrides):
    kwargs = dict(
        symbol="XAUUSD",
        closed_direction="LONG",
        closed_entry_price=2000.0,
        closed_stop_loss=1990.0,
        current_price=1990.0,
        conviction=70,
        zone_type="FVG_MIDPOINT",
        timeframe="M5",
    )
    kwargs.update(overrides)
    return orch.evaluate_stopout_flip(
        kwargs.pop("symbol"),
        kwargs.pop("closed_direction"),
        kwargs.pop("closed_entry_price"),
        kwargs.pop("closed_stop_loss"),
        **kwargs,
    )


class TestStopoutFlipConfig:
    def test_entry_config_defaults(self):
        c = EntryConfig()
        assert c.stopout_flip_enabled is True
        assert c.stopout_flip_cooldown_s == 30.0
        assert c.stopout_flip_max_per_zone == 2

    def test_gold_profile_flip_enabled(self):
        from brain.instrument_profile import get_profile
        g = get_profile("XAUUSD")
        assert g.stopout_flip_enabled is True
        assert g.stopout_flip_cooldown_s == 30.0
        assert g.stopout_flip_max_per_zone == 2


class TestStopoutFlipConfirmation:
    def test_confirmed_flip_emits_opposite_decision(self, monkeypatch):
        orch, decisions = _make_orch(monkeypatch, EntryConfig(), tick_mom=0.5)
        result = _flip(orch)
        assert result["confirmed"] is True
        assert result["direction"] == "SHORT"
        assert result["reason"] == "confirmed"
        assert len(decisions) == 1
        d = decisions[0]
        assert d["direction"] == "SHORT"
        assert d["source"] == "stopout_flip"
        # A flipped SHORT must stop ABOVE its entry.
        assert d["stop_loss"] > d["entry_price"]
        assert orch.stats.get("stopout_flips", 0) == 1

    def test_weak_ticks_reject_flip(self, monkeypatch):
        orch, decisions = _make_orch(monkeypatch, EntryConfig(), tick_mom=0.1)
        result = _flip(orch)
        assert result["confirmed"] is False
        assert result["reason"] == "unconfirmed"
        assert decisions == []
        assert orch.stats.get("stopout_flips", 0) == 0

    def test_opposing_m5_blocks_flip(self, monkeypatch):
        # Closed LONG → flip SHORT, but M5 is BULLISH → the flip is refused.
        orch, decisions = _make_orch(
            monkeypatch, EntryConfig(), tick_mom=0.5, m5_trend=Trend.BULLISH,
        )
        result = _flip(orch)
        assert result["confirmed"] is False
        assert decisions == []

    def test_disabled_no_flip(self, monkeypatch):
        orch, decisions = _make_orch(
            monkeypatch, EntryConfig(stopout_flip_enabled=False), tick_mom=0.5,
        )
        result = _flip(orch)
        assert result["confirmed"] is False
        assert result["reason"] == "disabled"
        assert decisions == []


class TestStopoutFlipGuards:
    def test_cooldown_blocks_second_flip(self, monkeypatch):
        orch, decisions = _make_orch(
            monkeypatch, EntryConfig(stopout_flip_cooldown_s=1000.0), tick_mom=0.5,
        )
        first = _flip(orch)
        assert first["confirmed"] is True
        # Second flip immediately after is inside the cooldown window.
        second = _flip(orch)
        assert second["confirmed"] is False
        assert second["reason"] == "cooldown"
        assert len(decisions) == 1

    def test_zone_exhausted_after_max_per_zone(self, monkeypatch):
        orch, decisions = _make_orch(
            monkeypatch,
            EntryConfig(stopout_flip_cooldown_s=0.0, stopout_flip_max_per_zone=1),
            tick_mom=0.5,
        )
        first = _flip(orch)
        assert first["confirmed"] is True
        second = _flip(orch)
        assert second["confirmed"] is False
        assert second["reason"] == "zone_exhausted"
        assert len(decisions) == 1

    def test_zone_touch_resets_flip_counter(self, monkeypatch):
        orch, decisions = _make_orch(
            monkeypatch,
            EntryConfig(stopout_flip_cooldown_s=0.0, stopout_flip_max_per_zone=1),
            tick_mom=0.5,
        )
        assert _flip(orch)["confirmed"] is True
        assert _flip(orch)["reason"] == "zone_exhausted"

        # A fresh touch of the same zone resets its flip budget.
        zone = EntryZone(
            symbol="XAUUSD", direction="LONG", zone_type=ZoneType.FVG_MIDPOINT,
            top=2001.0, bottom=1999.0, midpoint=2000.0, invalidation_level=1990.0,
            conviction=70, created_at=_ts(), expires_at=_ts(), timeframe="M5",
        )
        pending = SimpleNamespace(
            symbol="XAUUSD", direction="LONG", zone=zone,
            touch_price=2000.0, touch_time=_ts(),
        )
        orch._handle_zone_touch(pending)

        third = _flip(orch)
        assert third["confirmed"] is True
        assert len(decisions) == 2
