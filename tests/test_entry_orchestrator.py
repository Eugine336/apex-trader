"""Tests for entry.entry_orchestrator — EntryOrchestrator."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from brain.fvg_detector import FairValueGap, FVGStatus
from brain.order_block import OrderBlock, OBStatus
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend
from brain.world_model import WorldModelStore, build_world_model
from entry.entry_orchestrator import EntryOrchestrator
from entry.models import EntryConfig


def _ts() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FakeTick:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime


def _make_fvg(kind="BULLISH", top=1.0850, bottom=1.0840) -> FairValueGap:
    return FairValueGap(
        kind=kind, top=top, bottom=bottom, midpoint=(top + bottom) / 2,
        size_pips=10.0, strength="STRONG", status=FVGStatus.OPEN,
        candle_index=50, timestamp=_ts(), timeframe="M5",
    )


def _make_ob(kind="BULLISH", top=1.0855, bottom=1.0835) -> OrderBlock:
    return OrderBlock(
        kind=kind, top=top, bottom=bottom, midpoint=(top + bottom) / 2,
        origin_index=30, strength="STRONG", status=OBStatus.FRESH,
        impulse_size=25.0, timestamp=_ts(), timeframe="H1", breaker=False,
    )


def _make_struct(trend=Trend.BULLISH) -> StructureAnalysis:
    return StructureAnalysis(
        trend=trend, last_event=StructureEvent.BOS_BULLISH,
        swing_high=1.0900, swing_low=1.0800,
        last_bos_level=1.0870, last_choch_level=None,
        structure_broken=False,
        bullish_swing_points=[], bearish_swing_points=[],
        confidence=0.8,
    )


def _bullish_m1(n=30, base=1.0835) -> pd.DataFrame:
    """M1 data that starts below the zone and rises into it."""
    step = 0.00005
    data = {
        "open": [base + i * step for i in range(n)],
        "high": [base + i * step + 0.0003 for i in range(n)],
        "low": [base + i * step - 0.0001 for i in range(n)],
        "close": [base + (i + 0.5) * step for i in range(n)],
        "tick_volume": [100 + i * 10 for i in range(n)],
    }
    return pd.DataFrame(data)


def _setup_orchestrator(
    fvgs=None, obs=None, structure=None,
    m1_df=None, config=None,
):
    """Build an EntryOrchestrator with a pre-populated WorldModelStore."""
    store = WorldModelStore()
    decisions = []

    orch = EntryOrchestrator(
        world_model_store=store,
        config=config or EntryConfig(min_entry_score=50),
        pip_size_lookup=lambda _: 0.0001,
        on_entry_decision=lambda d: decisions.append(d),
        get_m1_dataframe=lambda _: m1_df,
    )

    wm_fvgs = {}
    if fvgs:
        wm_fvgs = {"M5": fvgs}
    wm_obs = {}
    if obs:
        wm_obs = {"H1": obs}
    wm_struct = {}
    if structure:
        wm_struct = {"H4": structure}

    wm = build_world_model(
        symbol="EURUSD",
        version=store.next_version(),
        fvgs=wm_fvgs or None,
        order_blocks=wm_obs or None,
        structure=wm_struct or None,
    )
    store.publish(wm)
    orch.on_world_model_update("EURUSD")

    return orch, decisions, store


class TestEntryOrchestratorFullFlow:
    def test_happy_path_fvg_entry(self):
        """WorldModel → zone → tick touch → M1 confirm → gate pass → entry emitted."""
        m1_df = _bullish_m1()
        orch, decisions, _ = _setup_orchestrator(
            fvgs=[_make_fvg()],
            m1_df=m1_df,
        )

        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        orch.on_tick(tick)

        assert orch.stats["zone_touches"] == 1

        orch.on_m1_close("EURUSD")

        assert len(decisions) == 1
        d = decisions[0]
        assert d["symbol"] == "EURUSD"
        assert d["direction"] == "LONG"
        assert d["entry_price"] > 0
        assert d["stop_loss"] > 0
        assert d["tp1"] > d["entry_price"]

    def test_happy_path_ob_entry(self):
        m1_df = _bullish_m1()
        orch, decisions, _ = _setup_orchestrator(
            obs=[_make_ob()],
            m1_df=m1_df,
        )

        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        orch.on_tick(tick)
        orch.on_m1_close("EURUSD")

        assert len(decisions) == 1


class TestEntryOrchestratorRejection:
    def test_no_zones_no_trigger(self):
        orch, decisions, _ = _setup_orchestrator()
        tick = FakeTick("EURUSD", 1.0845, 1.0846, datetime.now(timezone.utc))
        orch.on_tick(tick)
        assert orch.stats["zone_touches"] == 0
        assert len(decisions) == 0

    def test_m1_not_confirming_no_entry(self):
        """Zone touched but M1 close not called → no entry."""
        m1_df = _bullish_m1()
        orch, decisions, _ = _setup_orchestrator(
            fvgs=[_make_fvg()],
            m1_df=m1_df,
        )
        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        orch.on_tick(tick)
        assert len(decisions) == 0

    def test_gate_failure_blocks_entry(self):
        """Drawdown block → gate fails → no entry emitted."""
        m1_df = _bullish_m1()
        store = WorldModelStore()
        decisions = []

        orch = EntryOrchestrator(
            world_model_store=store,
            config=EntryConfig(min_entry_score=50),
            pip_size_lookup=lambda _: 0.0001,
            on_entry_decision=lambda d: decisions.append(d),
            is_drawdown_ok=lambda: False,
            get_m1_dataframe=lambda _: m1_df,
        )

        wm = build_world_model(
            symbol="EURUSD",
            version=store.next_version(),
            fvgs={"M5": [_make_fvg()]},
        )
        store.publish(wm)
        orch.on_world_model_update("EURUSD")

        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        orch.on_tick(tick)
        orch.on_m1_close("EURUSD")

        assert len(decisions) == 0
        assert orch.stats["gate_failures"] >= 1


class TestEntryOrchestratorMultiSymbol:
    def test_independent_symbols(self):
        store = WorldModelStore()
        decisions = []
        m1_df = _bullish_m1()

        orch = EntryOrchestrator(
            world_model_store=store,
            config=EntryConfig(min_entry_score=50, max_concurrent_pending=10),
            pip_size_lookup=lambda _: 0.0001,
            on_entry_decision=lambda d: decisions.append(d),
            get_m1_dataframe=lambda _: m1_df,
        )

        for sym in ("EURUSD", "GBPUSD"):
            wm = build_world_model(
                symbol=sym,
                version=store.next_version(),
                fvgs={"M5": [_make_fvg()]},
            )
            store.publish(wm)
            orch.on_world_model_update(sym)

        for sym in ("EURUSD", "GBPUSD"):
            tick = FakeTick(sym, 1.0844, 1.0845, datetime.now(timezone.utc))
            orch.on_tick(tick)

        for sym in ("EURUSD", "GBPUSD"):
            orch.on_m1_close(sym)

        assert len(decisions) == 2
        syms = {d["symbol"] for d in decisions}
        assert syms == {"EURUSD", "GBPUSD"}


class TestEntryOrchestratorStats:
    def test_stats_tracking(self):
        m1_df = _bullish_m1()
        orch, decisions, _ = _setup_orchestrator(
            fvgs=[_make_fvg()],
            m1_df=m1_df,
        )
        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        orch.on_tick(tick)
        orch.on_m1_close("EURUSD")

        stats = orch.stats
        assert stats["zones_extracted"] >= 1
        assert stats["zone_touches"] == 1
        assert stats["entries_emitted"] == 1


class TestEntryOrchestratorReset:
    def test_reset_clears_state(self):
        m1_df = _bullish_m1()
        orch, _, _ = _setup_orchestrator(
            fvgs=[_make_fvg()],
            m1_df=m1_df,
        )
        tick = FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc))
        orch.on_tick(tick)
        orch.reset()
        assert orch.zone_watcher.get_active_zones("EURUSD") == []
        assert len(orch.tick_detector.pending_entries) == 0
