"""Tests for the event-driven bootstrap system (Phase 9)."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock
from types import SimpleNamespace

import pytest

from tick import Tick, EventBus, TickStore, CandleCloseDetector, TickRouter
from brain.world_model import WorldModelStore
from execution.intents import Intent, IntentType
from execution.intent_aggregator import IntentAggregator
from execution.action_executor import ActionExecutor, ExecutorConfig
from execution.position_snapshot import PositionSnapshot
from execution.position_worker import PositionWorker, WorkerConfig


# ── Helpers ──────────────────────────────────────────────────────────


def _make_tick(symbol: str = "EURUSD", bid: float = 1.1000, ask: float = 1.1002) -> Tick:
    return Tick(
        symbol=symbol, bid=bid, ask=ask,
        timestamp=datetime.now(timezone.utc), source="test",
    )


def _make_position(
    symbol: str = "EURUSD",
    order_id: str = "T1",
    direction: str = "BUY",
    sl: float = 1.0950,
    tp1: float = 1.1050,
    lots: float = 0.1,
) -> SimpleNamespace:
    return SimpleNamespace(
        order_id=order_id, platform="mt5", symbol=symbol,
        direction=direction, entry_price=1.1000, lots=lots,
        remaining_lots=lots, open_time=datetime.now(timezone.utc),
        score=85, sl=sl, tp1=tp1, tp2=1.1100,
        at_breakeven=False, trailing=False, tp1_hit=False,
        re_entry_eligible=False, broker_pnl=0.0, broker_lots=lots,
        confluences=[], scale_in_count=0, stake_usd=0.0, multiplier=100,
    )


def _mock_platform_manager(positions=None):
    pm = MagicMock()
    pm.get_all_open_positions.return_value = positions or []
    pm.get_price.return_value = SimpleNamespace(bid=1.1000, ask=1.1002)
    pm.get_spread.return_value = 1.2
    pm.fetch_market_data.return_value = {}
    pm.modify_trade.return_value = True
    pm.close_trade.return_value = SimpleNamespace(success=True, error=None)
    pm.any_connected = True
    return pm


# ── Test: MT5TickPoller ──────────────────────────────────────────────


class TestMT5TickPoller:
    def test_start_stop(self):
        from event_driven_bootstrap import MT5TickPoller

        bus = EventBus()
        store = TickStore()
        detector = CandleCloseDetector(bus)
        router = TickRouter(store, detector, bus)
        pm = _mock_platform_manager()

        poller = MT5TickPoller(pm, router, ["EURUSD"], poll_interval=0.01)
        poller.start()
        assert poller._running
        time.sleep(0.05)
        poller.stop()
        assert not poller._running

    def test_routes_ticks(self):
        from event_driven_bootstrap import MT5TickPoller

        bus = EventBus()
        store = TickStore()
        detector = CandleCloseDetector(bus)
        router = TickRouter(store, detector, bus)
        pm = _mock_platform_manager()

        poller = MT5TickPoller(pm, router, ["EURUSD"], poll_interval=0.01)
        poller.start()
        time.sleep(0.1)
        poller.stop()

        latest = store.get_latest("EURUSD")
        assert latest is not None
        assert latest.source == "mt5"
        assert latest.bid == 1.1000

    def test_empty_symbols_no_start(self):
        from event_driven_bootstrap import MT5TickPoller

        bus = EventBus()
        store = TickStore()
        detector = CandleCloseDetector(bus)
        router = TickRouter(store, detector, bus)
        pm = _mock_platform_manager()

        poller = MT5TickPoller(pm, router, [], poll_interval=0.01)
        poller.start()
        assert not poller._running


# ── Test: DerivTickAdapter ───────────────────────────────────────────


class TestDerivTickAdapter:
    def test_start_stop(self):
        from event_driven_bootstrap import DerivTickAdapter

        bus = EventBus()
        store = TickStore()
        detector = CandleCloseDetector(bus)
        router = TickRouter(store, detector, bus)
        pm = _mock_platform_manager()

        adapter = DerivTickAdapter(pm, router, ["V75"], poll_interval=0.01)
        adapter.start()
        assert adapter._running
        time.sleep(0.05)
        adapter.stop()
        assert not adapter._running

    def test_routes_ticks_as_deriv_source(self):
        from event_driven_bootstrap import DerivTickAdapter

        bus = EventBus()
        store = TickStore()
        detector = CandleCloseDetector(bus)
        router = TickRouter(store, detector, bus)
        pm = _mock_platform_manager()

        adapter = DerivTickAdapter(pm, router, ["V75"], poll_interval=0.01)
        adapter.start()
        time.sleep(0.1)
        adapter.stop()

        latest = store.get_latest("V75")
        assert latest is not None
        assert latest.source == "deriv"


# ── Test: PositionEvaluator ──────────────────────────────────────────


class TestPositionEvaluator:
    def test_evaluate_empty_positions(self):
        from event_driven_bootstrap import PositionEvaluator

        pm = _mock_platform_manager([])
        store = TickStore()
        wm_store = WorldModelStore()
        aggregator = IntentAggregator()

        evaluator = PositionEvaluator(pm, store, wm_store, aggregator)
        evaluator.evaluate_all()
        assert evaluator.eval_count == 1
        intents = aggregator.flush()
        assert len(intents) == 0

    def test_evaluate_with_sl_hit(self):
        from event_driven_bootstrap import PositionEvaluator

        pos = _make_position(sl=1.1010, direction="BUY")
        pm = _mock_platform_manager([pos])
        store = TickStore()
        store.put(_make_tick("EURUSD", bid=1.0940, ask=1.0942))
        wm_store = WorldModelStore()
        aggregator = IntentAggregator()

        evaluator = PositionEvaluator(pm, store, wm_store, aggregator)
        evaluator.evaluate_all()

        intents = aggregator.flush()
        assert len(intents) >= 1
        close_intents = [i for i in intents if i.intent_type == IntentType.CLOSE]
        assert len(close_intents) >= 1


# ── Test: FlushLoop ──────────────────────────────────────────────────


class TestFlushLoop:
    def test_start_stop(self):
        from event_driven_bootstrap import FlushLoop

        aggregator = IntentAggregator()
        pm = _mock_platform_manager()
        executor = ActionExecutor(broker=pm)

        loop = FlushLoop(aggregator, executor, pm, interval=0.01)
        loop.start()
        assert loop._running
        time.sleep(0.05)
        loop.stop()
        assert not loop._running
        assert loop._flush_count > 0

    def test_executes_intents(self):
        from event_driven_bootstrap import FlushLoop

        aggregator = IntentAggregator()
        pm = _mock_platform_manager([_make_position()])
        executor = ActionExecutor(broker=pm)

        intent = Intent.close(
            symbol="EURUSD", ticket="T1", source="test", reason="test close",
        )
        aggregator.submit([intent])

        loop = FlushLoop(aggregator, executor, pm, interval=0.01)
        loop.start()
        time.sleep(0.1)
        loop.stop()

        assert loop._intents_executed >= 1


# ── Test: TickEvalLoop ───────────────────────────────────────────────


class TestTickEvalLoop:
    def test_start_stop(self):
        from event_driven_bootstrap import TickEvalLoop, PositionEvaluator

        pm = _mock_platform_manager()
        store = TickStore()
        wm_store = WorldModelStore()
        aggregator = IntentAggregator()
        evaluator = PositionEvaluator(pm, store, wm_store, aggregator)

        loop = TickEvalLoop(evaluator, interval=0.01)
        loop.start()
        assert loop._running
        time.sleep(0.05)
        loop.stop()
        assert not loop._running
        assert evaluator.eval_count > 0


# ── Test: EventDrivenSystem ──────────────────────────────────────────


class TestEventDrivenSystem:
    @patch("event_driven_bootstrap.INSTRUMENT_REGISTRY", {
        "EURUSD": SimpleNamespace(
            symbol="EURUSD", platform=SimpleNamespace(value="mt5"),
            pip_size=0.0001,
        ),
        "V75": SimpleNamespace(
            symbol="V75", platform=SimpleNamespace(value="deriv"),
            pip_size=0.01,
        ),
    })
    def test_init_and_classify_symbols(self):
        from event_driven_bootstrap import EventDrivenSystem

        config = MagicMock()
        pm = _mock_platform_manager()
        system = EventDrivenSystem(config, pm)

        mt5, deriv = system._classify_symbols()
        assert "EURUSD" in mt5
        assert "V75" in deriv

    @patch("event_driven_bootstrap.INSTRUMENT_REGISTRY", {
        "EURUSD": SimpleNamespace(
            symbol="EURUSD", platform=SimpleNamespace(value="mt5"),
            pip_size=0.0001,
        ),
    })
    def test_start_stop(self):
        from event_driven_bootstrap import EventDrivenSystem

        config = MagicMock()
        pm = _mock_platform_manager()
        system = EventDrivenSystem(config, pm)

        system.start()
        assert system.is_running
        time.sleep(0.1)

        system.stop()
        assert not system.is_running

    @patch("event_driven_bootstrap.INSTRUMENT_REGISTRY", {
        "EURUSD": SimpleNamespace(
            symbol="EURUSD", platform=SimpleNamespace(value="mt5"),
            pip_size=0.0001,
        ),
    })
    def test_stats(self):
        from event_driven_bootstrap import EventDrivenSystem

        config = MagicMock()
        pm = _mock_platform_manager()
        system = EventDrivenSystem(config, pm)

        system.start()
        time.sleep(0.1)
        stats = system.stats()
        system.stop()

        assert "tick_store" in stats
        assert "candle_handler" in stats
        assert "executor" in stats
        assert "entry" in stats
        assert "position_evals" in stats
        assert "tick_router" in stats
        assert "candle_detector" in stats

    @patch("event_driven_bootstrap.INSTRUMENT_REGISTRY", {
        "EURUSD": SimpleNamespace(
            symbol="EURUSD", platform=SimpleNamespace(value="mt5"),
            pip_size=0.0001,
        ),
    })
    def test_double_start_noop(self):
        from event_driven_bootstrap import EventDrivenSystem

        config = MagicMock()
        pm = _mock_platform_manager()
        system = EventDrivenSystem(config, pm)

        system.start()
        system.start()
        assert system.is_running
        system.stop()

    @patch("event_driven_bootstrap.INSTRUMENT_REGISTRY", {
        "EURUSD": SimpleNamespace(
            symbol="EURUSD", platform=SimpleNamespace(value="mt5"),
            pip_size=0.0001,
        ),
    })
    def test_double_stop_noop(self):
        from event_driven_bootstrap import EventDrivenSystem

        config = MagicMock()
        pm = _mock_platform_manager()
        system = EventDrivenSystem(config, pm)

        system.start()
        system.stop()
        system.stop()
        assert not system.is_running


# ── Test: BrokerPort protocol compatibility ──────────────────────────


class TestBrokerPortCompatibility:
    def test_platform_manager_satisfies_broker_port(self):
        """PlatformManager has the methods BrokerPort requires."""
        from execution.action_executor import BrokerPort

        pm = _mock_platform_manager()
        assert hasattr(pm, "modify_trade")
        assert hasattr(pm, "close_trade")
        assert callable(pm.modify_trade)
        assert callable(pm.close_trade)

    def test_executor_accepts_mock_pm(self):
        pm = _mock_platform_manager()
        executor = ActionExecutor(broker=pm)

        intent = Intent.close(
            symbol="EURUSD", ticket="T1", source="test", reason="test",
        )
        result = executor.execute(
            intent, {"T1": {"symbol": "EURUSD", "direction": "BUY", "sl": 1.0950, "platform": "mt5"}},
        )
        assert result.success


# ── Test: Entry integration via on_entry_decision ────────────────────


class TestEntryDecisionCallback:
    @patch("event_driven_bootstrap.INSTRUMENT_REGISTRY", {
        "EURUSD": SimpleNamespace(
            symbol="EURUSD", platform=SimpleNamespace(value="mt5"),
            pip_size=0.0001, pip_value_per_lot=10.0,
        ),
    })
    @patch("event_driven_bootstrap.build_context_for_symbol")
    @patch("event_driven_bootstrap.PositionSizer")
    def test_entry_decision_callback_places_order(self, mock_sizer_cls, mock_ctx, caplog):
        from event_driven_bootstrap import EventDrivenSystem

        mock_ctx_obj = MagicMock()
        mock_ctx_obj.uses_stake = False
        mock_ctx.return_value = mock_ctx_obj

        mock_sizer = MagicMock()
        mock_result = SimpleNamespace(lots=0.02, stake_usd=0.0, sizing_mode="lots")
        mock_sizer.calculate.return_value = mock_result
        mock_sizer_cls.return_value = mock_sizer

        config = MagicMock()
        config.risk.max_open_trades = 5
        config.risk.max_correlated_trades = 2
        config.risk.risk_per_trade_pct = 0.75
        pm = _mock_platform_manager()
        pm.get_platform_balance.return_value = 10000.0
        pm.execute_entry.return_value = SimpleNamespace(
            success=True, order_id="T123", lots=0.02, error=None,
        )

        system = EventDrivenSystem(config, pm)
        decision = {
            "symbol": "EURUSD",
            "direction": "LONG",
            "entry_price": 1.1000,
            "stop_loss": 1.0950,
            "tp1": 1.1075,
            "conviction": 88,
        }
        system._on_entry_decision(decision)
        pm.execute_entry.assert_called_once()

    @patch("event_driven_bootstrap.INSTRUMENT_REGISTRY", {
        "EURUSD": SimpleNamespace(
            symbol="EURUSD", platform=SimpleNamespace(value="mt5"),
            pip_size=0.0001, pip_value_per_lot=10.0,
        ),
    })
    @patch("event_driven_bootstrap.build_context_for_symbol")
    @patch("event_driven_bootstrap.PositionSizer")
    def test_entry_decision_skips_zero_lots(self, mock_sizer_cls, mock_ctx, caplog):
        from event_driven_bootstrap import EventDrivenSystem

        mock_ctx_obj = MagicMock()
        mock_ctx_obj.uses_stake = False
        mock_ctx.return_value = mock_ctx_obj

        mock_sizer = MagicMock()
        mock_result = SimpleNamespace(lots=0.0, stake_usd=0.0, sizing_mode="skip_inflated")
        mock_sizer.calculate.return_value = mock_result
        mock_sizer_cls.return_value = mock_sizer

        config = MagicMock()
        config.risk.max_open_trades = 5
        config.risk.max_correlated_trades = 2
        config.risk.risk_per_trade_pct = 0.75
        pm = _mock_platform_manager()
        pm.get_platform_balance.return_value = 10000.0

        system = EventDrivenSystem(config, pm)
        decision = {
            "symbol": "EURUSD",
            "direction": "LONG",
            "entry_price": 1.1000,
            "stop_loss": 1.0950,
            "tp1": 1.1075,
            "conviction": 88,
        }
        system._on_entry_decision(decision)
        pm.execute_entry.assert_not_called()


# ── Tier 1–3 data-path helper functions ──────────────────────────────


class TestDataPathHelpers:
    def test_struct_swings_reads_levels(self):
        from event_driven_bootstrap import _struct_swings
        structure = {
            "H4": SimpleNamespace(swing_high=1.18, swing_low=1.06),
        }
        assert _struct_swings(structure, "H4") == (1.18, 1.06)

    def test_struct_swings_missing_tf_is_none(self):
        from event_driven_bootstrap import _struct_swings
        assert _struct_swings({}, "D1") == (None, None)

    def test_micro_confirmation_aligned_bos_is_market(self):
        from event_driven_bootstrap import _micro_confirmation_from_event
        assert _micro_confirmation_from_event("BOS_BULLISH", "LONG") == (
            "choch_bos", "MARKET",
        )
        assert _micro_confirmation_from_event("CHOCH_BEARISH", "SELL") == (
            "choch_bos", "MARKET",
        )

    def test_micro_confirmation_opposing_or_none_stays_pending(self):
        from event_driven_bootstrap import _micro_confirmation_from_event
        assert _micro_confirmation_from_event("BOS_BEARISH", "LONG") == ("", "PENDING")
        assert _micro_confirmation_from_event("NONE", "SHORT") == ("", "PENDING")
