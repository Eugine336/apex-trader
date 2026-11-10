"""Tests for the event-driven bootstrap system (Phase 9)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from types import SimpleNamespace


from tick import Tick, EventBus, TickStore, CandleCloseDetector, TickRouter
from brain.world_model import WorldModelStore
from execution.intents import Intent, IntentType
from execution.intent_aggregator import IntentAggregator
from execution.action_executor import ActionExecutor


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
        broker_pnl=0.0, broker_lots=lots,
        confluences=[], scale_in_count=0, stake_usd=0.0, multiplier=100,
    )


def _mock_platform_manager(positions=None):
    pm = MagicMock()
    pm.get_all_open_positions.return_value = positions or []
    _price = lambda symbol: SimpleNamespace(
        symbol=symbol,
        bid=1.1000,
        ask=1.1002,
        time=datetime.now(timezone.utc),
    )
    pm.get_price.side_effect = _price
    pm.get_deriv_price.side_effect = _price
    pm.deriv_connected = True
    pm.get_spread.return_value = 1.2
    pm.get_platform_name.return_value = "deriv"
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

    def test_execution_result_callback_receives_broker_outcome(self):
        from event_driven_bootstrap import FlushLoop

        aggregator = IntentAggregator()
        pm = _mock_platform_manager([_make_position()])
        executor = ActionExecutor(broker=pm)
        callbacks = []
        intent = Intent.close(
            symbol="EURUSD", ticket="T1", source="ai_brain",
            reason="Brain close", action_id="action-1",
        )
        aggregator.submit([intent])
        loop = FlushLoop(
            aggregator, executor, pm, interval=0.01,
            on_execution_result_callback=lambda submitted, result: callbacks.append(
                (submitted.action_id, result.success),
            ),
        )
        loop.start()
        time.sleep(0.1)
        loop.stop()

        assert callbacks == [("action-1", True)]

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
        "V75_1S": SimpleNamespace(
            symbol="V75_1S", platform=SimpleNamespace(value="deriv"),
            pip_size=0.01,
        ),
    })
    def test_init_and_classify_symbols(self):
        from event_driven_bootstrap import EventDrivenSystem

        config = MagicMock()
        # The bootstrap subscribes only to configured symbols; MagicMock's
        # implicit empty iterable previously made this test exercise no route.
        config.enabled_pairs = ["EURUSD", "V75_1S"]
        pm = _mock_platform_manager()
        system = EventDrivenSystem(config, pm)

        mt5, deriv = system._classify_symbols()
        assert "EURUSD" in mt5
        assert "V75_1S" in deriv

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
        # Entry orchestration is represented by the CognitiveBrain/Executor
        # pipeline; the removed legacy "entry" stats bucket is not part of the
        # current operational metrics contract.
        assert "position_evals" in stats
        assert "tick_router" in stats
        assert "candle_detector" in stats
        assert "tick_eval" in stats
        assert "zone_edge" in stats

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


# ── Retired legacy entry callback ─────────────────────────────────────

class TestEntryDecisionCallbackRetired:
    def test_direct_entry_callback_is_not_exposed(self):
        """EntryOrchestrator callbacks are retired; origination belongs to the Brain."""
        from event_driven_bootstrap import EventDrivenSystem

        assert not hasattr(EventDrivenSystem, "_on_entry_decision")

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


class TestPendingOrderLifecycle:
    def test_broker_confirmed_pending_fill_is_attributed_to_the_open_position(self, monkeypatch):
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE
        from platforms.mt5 import mt5_connector as mt5_adapter
        monkeypatch.setattr(mt5_adapter, "mt5", SimpleNamespace(DEAL_ENTRY_IN=0, DEAL_ENTRY_INOUT=2))
        from platforms.mt5 import mt5_connector as mt5_adapter
        monkeypatch.setattr(mt5_adapter, "mt5", SimpleNamespace(DEAL_ENTRY_IN=0, DEAL_ENTRY_INOUT=2))

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {
            "ORDER-1": {
                "order_id": "901", "symbol": "EURUSD", "direction": "BUY",
                "order_type": "LIMIT", "lots": 0.1, "entry_price": 99.0,
                "sl": 98.0, "tp": 101.0,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "decision_id": "decision-1", "campaign_id": "campaign-1",
                "opportunity_id": "opportunity-1",
                "decision_feature_snapshot": {"snapshot_complete": True, "snapshot_id": "snap-1"},
            },
        }
        system._entry_context = {}
        system._pm = SimpleNamespace(
            reconcile_pending_order=lambda *args, **kwargs: {
                "status": "FILLED", "order_id": "901", "broker_confirmed": True,
                "record": {"ticket": 901, "position_id": 902, "state": 4},
            },
            get_platform_balance=lambda symbol: 5000.0,
            get_position_entry_order_ids=lambda *args, **kwargs: {"901"},
            get_pending_order_fill_deals=lambda *args, **kwargs: [{
                "order": "901", "position_id": "902", "symbol": "EURUSD",
                "entry": 0, "price": 99.0, "volume": 0.1, "source_verified": True,
            }],
        )
        system._on_order_filled = MagicMock()
        store = MagicMock()
        store.emit.return_value = "event-id"

        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._reconcile_pending_orders([
                SimpleNamespace(
                    order_id="902", symbol="EURUSD", direction="BUY",
                    open_price=99.0, lots=0.1, sl=98.0, tp=101.0,
                ),
            ])

        assert "ORDER-1" not in system._pending_orders
        system._on_order_filled.assert_called_once()
        assert system._entry_context["902"]["decision_id"] == "decision-1"
        event_types = [call.kwargs.get("event_type") for call in store.emit.call_args_list]
        assert DE.ORDER_FILLED in event_types
        assert DE.ORDER_RECONCILED in event_types

    def test_partial_pending_fill_is_attributed_without_removing_resting_order(self, monkeypatch):
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE
        from platforms.mt5 import mt5_connector as mt5_adapter
        monkeypatch.setattr(mt5_adapter, "mt5", SimpleNamespace(DEAL_ENTRY_IN=0, DEAL_ENTRY_INOUT=2))

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {
            "ORDER-PARTIAL": {
                "order_id": "901", "symbol": "EURUSD", "direction": "BUY",
                "order_type": "LIMIT", "lots": 0.1, "entry_price": 99.0,
                "sl": 98.0, "tp": 101.0,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "decision_id": "decision-partial", "campaign_id": "campaign-partial",
                "opportunity_id": "opportunity-partial",
                "decision_feature_snapshot": {"snapshot_complete": True, "snapshot_id": "snap-partial"},
            },
        }
        system._entry_context = {}
        system._pm = SimpleNamespace(
            reconcile_pending_order=lambda *args, **kwargs: {
                "status": "PARTIALLY_FILLED_PENDING", "order_id": "901", "broker_confirmed": True,
                "initial_volume": 0.1, "remaining_volume": 0.06,
                "record": {"ticket": 901, "position_id": "902"},
            },
            get_platform_balance=lambda symbol: 5000.0,
            get_position_entry_order_ids=lambda *args, **kwargs: {"901"},
            get_pending_order_fill_deals=lambda *args, **kwargs: [{
                "order": "901", "position_id": "902", "symbol": "EURUSD",
                "entry": 0, "price": 99.0, "volume": 0.04, "source_verified": True,
            }],
        )
        system._on_order_filled = MagicMock()
        store = MagicMock()

        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._reconcile_pending_orders([
                SimpleNamespace(
                    order_id="902", symbol="EURUSD", direction="BUY",
                    open_price=99.0, lots=0.04, sl=98.0, tp=101.0,
                ),
            ])

        assert "ORDER-PARTIAL" in system._pending_orders
        assert system._pending_orders["ORDER-PARTIAL"]["status"] == "PARTIALLY_FILLED_PENDING"
        assert system._pending_orders["ORDER-PARTIAL"]["attributed"] is True
        system._on_order_filled.assert_called_once()
        event_types = [call.kwargs.get("event_type") for call in store.emit.call_args_list]
        assert DE.ORDER_FILLED in event_types
        assert DE.ORDER_RECONCILED in event_types

    def test_pending_fill_closed_before_position_poll_is_recovered_from_broker_deals(self):
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {
            "ORDER-CLOSED": {
                "order_id": "ORDER-CLOSED", "symbol": "EURUSD", "direction": "BUY",
                "order_type": "LIMIT", "entry_price": 99.0,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "decision_id": "decision-closed", "campaign_id": "campaign-closed",
                "opportunity_id": "opportunity-closed",
                "decision_feature_snapshot": {"snapshot_complete": True},
            },
        }
        system._entry_context = {}
        system._known_open = {}
        system._handle_close_result = MagicMock()
        system._pm = SimpleNamespace(
            reconcile_pending_order=lambda *args, **kwargs: {
                "status": "FILLED", "order_id": "ORDER-CLOSED",
                "broker_confirmed": True,
                "record": {"ticket": "ORDER-CLOSED", "position_id": "POSITION-CLOSED", "connector_index": 0},
            },
            get_pending_order_fill_deals=lambda *args, **kwargs: [{
                "order": "ORDER-CLOSED", "position_id": "POSITION-CLOSED",
                "symbol": "EURUSD", "entry": 0, "price": 99.2,
                "volume": 0.1, "source_verified": True,
            }],
            get_position_entry_order_ids=lambda *args, **kwargs: {"ORDER-CLOSED"},
            get_deal_close_info=lambda *args, **kwargs: SimpleNamespace(
                pnl=12.0, close_price=101.0, commission=-1.0,
                swap=0.0, fee=0.0, exit_reason="take_profit",
            ),
        )
        store = MagicMock()
        store.emit.return_value = "event-id"

        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._reconcile_pending_orders([])

        system._handle_close_result.assert_called_once()
        close_intent = system._handle_close_result.call_args.args[0]
        assert close_intent.position_ticket == "POSITION-CLOSED"
        assert system._entry_context == {}
        assert system._known_open == {}
        assert "ORDER-CLOSED" not in system._pending_orders
        event_types = [call.kwargs.get("event_type") for call in store.emit.call_args_list]
        assert DE.ORDER_FILLED in event_types
        assert DE.ORDER_RECONCILED in event_types

    def test_partial_pending_order_transitions_to_filled_and_is_removed_once(self, monkeypatch):
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE
        from platforms.mt5 import mt5_connector as mt5_adapter
        monkeypatch.setattr(mt5_adapter, "mt5", SimpleNamespace(DEAL_ENTRY_IN=0, DEAL_ENTRY_INOUT=2))

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {
            "ORDER-TRANSITION": {
                "order_id": "ORDER-TRANSITION", "symbol": "EURUSD", "direction": "BUY",
                "order_type": "LIMIT", "lots": 0.1, "entry_price": 99.0,
                "sl": 98.0, "tp": 101.0,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        system._entry_context = {}
        deals_progress = iter([
            [{"order": "ORDER-TRANSITION", "position_id": "POSITION-TRANSITION", "symbol": "EURUSD", "entry": 0, "price": 99.0, "volume": 0.04, "source_verified": True}],
            [
                {"order": "ORDER-TRANSITION", "position_id": "POSITION-TRANSITION", "symbol": "EURUSD", "entry": 0, "price": 99.0, "volume": 0.04, "source_verified": True},
                {"order": "ORDER-TRANSITION", "position_id": "POSITION-TRANSITION", "symbol": "EURUSD", "entry": 0, "price": 99.2, "volume": 0.06, "source_verified": True},
            ],
        ])
        outcomes = iter([
            {
                "status": "PARTIALLY_FILLED_PENDING", "broker_confirmed": True,
                "initial_volume": 0.1, "remaining_volume": 0.06,
                "record": {"ticket": "ORDER-TRANSITION", "position_id": "POSITION-TRANSITION"},
            },
            {
                "status": "FILLED", "broker_confirmed": True,
                "order_id": "ORDER-TRANSITION",
                "record": {"ticket": "ORDER-TRANSITION", "position_id": "POSITION-TRANSITION"},
            },
        ])
        system._pm = SimpleNamespace(
            reconcile_pending_order=lambda *args, **kwargs: next(outcomes),
            get_platform_balance=lambda symbol: 5000.0,
            get_position_entry_order_ids=lambda *args, **kwargs: {"ORDER-TRANSITION"},
            get_pending_order_fill_deals=lambda *args, **kwargs: next(deals_progress),
        )
        system._on_order_filled = MagicMock()
        store = MagicMock()

        position = SimpleNamespace(
            order_id="POSITION-TRANSITION", symbol="EURUSD", direction="BUY",
            open_price=99.0, lots=0.1, sl=98.0, tp=101.0,
        )
        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._reconcile_pending_orders([position])
            assert "ORDER-TRANSITION" in system._pending_orders
            system._reconcile_pending_orders([position])

        assert "ORDER-TRANSITION" not in system._pending_orders
        system._on_order_filled.assert_called_once()
        event_types = [call.kwargs.get("event_type") for call in store.emit.call_args_list]
        assert DE.ORDER_FILLED in event_types
        assert DE.ORDER_RECONCILED in event_types


    def test_confirmed_cancelled_order_is_removed_from_pending_projection(self):
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {
            "ORDER-CANCELLED": {
                "order_id": "ORDER-CANCELLED", "symbol": "EURUSD",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        system._pm = SimpleNamespace(reconcile_pending_order=lambda *args, **kwargs: {
            "status": "CANCELLED", "broker_confirmed": True,
            "record": {"ticket": "ORDER-CANCELLED", "state": 2},
        })
        store = MagicMock()

        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._reconcile_pending_orders([])

        assert "ORDER-CANCELLED" not in system._pending_orders
        assert any(call.kwargs.get("event_type") == DE.ORDER_RECONCILED for call in store.emit.call_args_list)


    def test_confirmed_expired_order_is_removed_from_pending_projection(self):
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {
            "ORDER-EXPIRED": {
                "order_id": "ORDER-EXPIRED", "symbol": "EURUSD",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        system._pm = SimpleNamespace(reconcile_pending_order=lambda *args, **kwargs: {
            "status": "EXPIRED", "broker_confirmed": True,
            "record": {"ticket": "ORDER-EXPIRED", "state": 6},
        })
        store = MagicMock()

        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._reconcile_pending_orders([])

        assert "ORDER-EXPIRED" not in system._pending_orders
        assert any(call.kwargs.get("event_type") == DE.ORDER_RECONCILED for call in store.emit.call_args_list)

    def test_pending_fill_does_not_overwrite_mixed_netting_position_context(self):
        import threading
        from event_driven_bootstrap import EventDrivenSystem

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {
            "ORDER-MIXED": {
                "order_id": "ORDER-MIXED", "symbol": "EURUSD", "direction": "BUY",
                "order_type": "LIMIT", "lots": 0.1, "entry_price": 99.0,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        system._entry_context = {"POSITION-MIXED": {"lots": 0.2, "entry_price": 98.0}}
        deal_reader = MagicMock(return_value=[])
        system._pm = SimpleNamespace(
            reconcile_pending_order=lambda *args, **kwargs: {
                "status": "FILLED", "broker_confirmed": True,
                "record": {"ticket": "ORDER-MIXED", "position_id": "POSITION-MIXED", "connector_index": 0},
            },
            get_position_entry_order_ids=lambda *args, **kwargs: {"ORDER-MIXED", "OTHER-ORDER"},
            get_pending_order_fill_deals=deal_reader,
        )
        system._on_order_filled = MagicMock()
        store = MagicMock()

        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._reconcile_pending_orders([
                SimpleNamespace(
                    order_id="POSITION-MIXED", symbol="EURUSD", direction="BUY",
                    open_price=98.5, lots=0.2, sl=97.0, tp=101.0,
                ),
            ])

        assert "ORDER-MIXED" in system._pending_orders
        assert system._entry_context["POSITION-MIXED"] == {"lots": 0.2, "entry_price": 98.0}
        deal_reader.assert_not_called()
        system._on_order_filled.assert_not_called()

    def test_filled_order_without_position_identity_is_not_attributed_by_order_ticket(self):
        import threading
        from event_driven_bootstrap import EventDrivenSystem

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {
            "ORDER-IDENTITY": {
                "order_id": "ORDER-IDENTITY", "symbol": "EURUSD", "direction": "BUY",
                "order_type": "LIMIT", "lots": 0.1, "entry_price": 99.0,
                "sl": 98.0, "tp": 101.0,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        system._entry_context = {}
        system._pm = SimpleNamespace(
            reconcile_pending_order=lambda *args, **kwargs: {
                "status": "FILLED", "broker_confirmed": True,
                "record": {"ticket": "ORDER-IDENTITY", "position_id": ""},
            },
        )
        system._on_order_filled = MagicMock()
        store = MagicMock()

        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._reconcile_pending_orders([
                SimpleNamespace(
                    order_id="ORDER-IDENTITY", symbol="EURUSD", direction="BUY",
                    open_price=99.0, lots=0.1, sl=98.0, tp=101.0,
                ),
            ])

        assert "ORDER-IDENTITY" in system._pending_orders
        system._on_order_filled.assert_not_called()

    def test_unknown_broker_absence_never_removes_pending_order(self):
        import threading
        from event_driven_bootstrap import EventDrivenSystem

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {
            "ORDER-2": {
                "order_id": "ORDER-2", "symbol": "EURUSD",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        system._pm = SimpleNamespace(
            reconcile_pending_order=lambda *args, **kwargs: {
                "status": "UNKNOWN", "broker_confirmed": False,
                "reason": "not_present_in_live_or_history_book",
            },
        )
        store = MagicMock()
        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._reconcile_pending_orders([])
        assert "ORDER-2" in system._pending_orders
        store.emit.assert_not_called()

    def test_ambiguous_pending_result_is_tracked_by_idempotency_key_without_ticket(self):
        import threading
        from event_driven_bootstrap import EventDrivenSystem

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {}
        result = SimpleNamespace(
            order_id="", success=False,
            timestamp=datetime.now(timezone.utc),
        )
        system._record_pending_order_result(result, {
            "order_id": "", "idempotency_key": "a1b2c3d4e5f6",
            "symbol": "EURUSD", "status": "UNKNOWN",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        })
        assert "a1b2c3d4e5f6" in system._pending_orders
        assert system._pending_orders["a1b2c3d4e5f6"]["order_id"] == ""

    def test_pending_orders_restore_from_durable_event_projection(self):
        import json
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {}
        payload = {
            "order_id": "ORDER-3", "symbol": "EURUSD", "direction": "BUY",
            "order_type": "LIMIT", "entry_price": 99.0,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "decision_id": "decision-3",
        }
        store = MagicMock()
        store.query_events.return_value = [{
            "event_type": DE.ORDER_PENDING,
            "parent_id": "ORDER-3",
            "payload_json": json.dumps(payload),
        }]
        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._restore_pending_orders()
        assert system._pending_orders["ORDER-3"]["decision_id"] == "decision-3"

    def test_restart_recovery_retains_partial_fill_from_cancel_event(self):
        import json
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {}
        pending_payload = {
            "order_id": "ORDER-RESTORE-PARTIAL", "symbol": "EURUSD",
            "direction": "BUY", "order_type": "LIMIT", "lots": 0.1,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        cancelled_payload = {
            "order_id": "ORDER-RESTORE-PARTIAL",
            "pre_cancel_broker_snapshot": {
                "ticket": "ORDER-RESTORE-PARTIAL", "symbol": "EURUSD",
                "volume_initial": 0.1, "volume_current": 0.06,
            },
            "post_cancel_broker_record": {
                "ticket": "ORDER-RESTORE-PARTIAL", "symbol": "EURUSD",
                "state": 2, "volume_initial": 0.1, "volume_current": 0.06,
                "source_verified": True,
            },
        }
        store = MagicMock()
        store.query_events.return_value = [
            {"event_type": DE.ORDER_CANCELLED, "parent_id": "ORDER-RESTORE-PARTIAL",
             "payload_json": json.dumps(cancelled_payload)},
            {"event_type": DE.ORDER_PENDING, "parent_id": "ORDER-RESTORE-PARTIAL",
             "payload_json": json.dumps(pending_payload)},
        ]

        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._restore_pending_orders()

        restored = system._pending_orders["ORDER-RESTORE-PARTIAL"]
        assert restored["status"] == "PARTIALLY_FILLED_TERMINAL"
        assert restored["initial_volume"] == 0.1
        assert restored["last_partial_remaining_volume"] == 0.06


    def test_restart_recovery_removes_cancelled_order_with_no_fill_evidence(self):
        import json
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {}
        pending_payload = {
            "order_id": "ORDER-RESTORE-CANCELLED", "symbol": "EURUSD",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        cancelled_payload = {
            "order_id": "ORDER-RESTORE-CANCELLED",
            "pre_cancel_broker_snapshot": {
                "ticket": "ORDER-RESTORE-CANCELLED", "symbol": "EURUSD",
                "volume_initial": 0.1, "volume_current": 0.1,
            },
            "post_cancel_broker_record": {
                "ticket": "ORDER-RESTORE-CANCELLED", "symbol": "EURUSD",
                "state": 2, "volume_initial": 0.1, "volume_current": 0.1,
                "source_verified": True,
            },
        }
        store = MagicMock()
        store.query_events.return_value = [
            {"event_type": DE.ORDER_CANCELLED, "parent_id": "ORDER-RESTORE-CANCELLED",
             "payload_json": json.dumps(cancelled_payload)},
            {"event_type": DE.ORDER_PENDING, "parent_id": "ORDER-RESTORE-CANCELLED",
             "payload_json": json.dumps(pending_payload)},
        ]

        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._restore_pending_orders()

        assert "ORDER-RESTORE-CANCELLED" not in system._pending_orders

    def test_partial_pending_open_callback_persists_durable_pending_projection(self, monkeypatch):
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {}
        submitted_at = datetime.now(timezone.utc)
        intent = SimpleNamespace(
            symbol="EURUSD", direction="BUY", order_type="LIMIT", lots=0.10,
            entry_price=99.0, new_sl=98.0, new_tp=101.0,
            idempotency_key="a1b2c3d4e5f9", decision_id="decision-pending",
            campaign_id="campaign-pending", opportunity_id="opportunity-pending",
            decision_feature_snapshot={"snapshot_id": "snapshot-pending"},
            timestamp=submitted_at,
        )
        response = SimpleNamespace(
            order_id="ORDER-PARTIAL-CALLBACK",
            order_status="PARTIALLY_FILLED_PENDING",
            timestamp=submitted_at, lots=0.06,
        )
        result = SimpleNamespace(success=True, broker_response=response)

        system._handle_open_result(intent, result)

        meta = system._pending_orders["ORDER-PARTIAL-CALLBACK"]
        assert meta["status"] == "PARTIALLY_FILLED_PENDING"
        assert meta["remaining_lots"] == 0.06


    def test_restart_recovery_does_not_resurrect_attributed_terminal_partial_fill(self):
        import json
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {}
        pending = {
            "order_id": "ORDER-TERMINAL-ATTRIBUTED",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        reconciled = {
            "order_id": "ORDER-TERMINAL-ATTRIBUTED",
            "status": "PARTIALLY_FILLED_TERMINAL",
            "attribution_status": "attributed",
        }
        store = MagicMock()
        store.query_events.return_value = [
            {"event_type": DE.ORDER_RECONCILED, "parent_id": "ORDER-TERMINAL-ATTRIBUTED",
             "payload_json": json.dumps(reconciled)},
            {"event_type": DE.ORDER_PENDING, "parent_id": "ORDER-TERMINAL-ATTRIBUTED",
             "payload_json": json.dumps(pending)},
        ]
        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._restore_pending_orders()
        assert "ORDER-TERMINAL-ATTRIBUTED" not in system._pending_orders


    def test_restart_recovery_retains_unresolved_terminal_partial_fill(self):
        import json
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {}
        pending = {
            "order_id": "ORDER-TERMINAL-UNRESOLVED",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        reconciled = {
            "order_id": "ORDER-TERMINAL-UNRESOLVED",
            "status": "PARTIALLY_FILLED_TERMINAL",
            "attribution_status": "unresolved_closed_fill",
        }
        store = MagicMock()
        store.query_events.return_value = [
            {"event_type": DE.ORDER_RECONCILED, "parent_id": "ORDER-TERMINAL-UNRESOLVED",
             "payload_json": json.dumps(reconciled)},
            {"event_type": DE.ORDER_PENDING, "parent_id": "ORDER-TERMINAL-UNRESOLVED",
             "payload_json": json.dumps(pending)},
        ]
        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._restore_pending_orders()
        assert system._pending_orders["ORDER-TERMINAL-UNRESOLVED"]["attribution_status"] == "unresolved_closed_fill"


    def test_restart_recovery_keeps_cancel_request_unknown_without_post_cancel_history(self):
        import json
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {}
        pending = {
            "order_id": "ORDER-CANCEL-UNKNOWN",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        cancelled = {
            "order_id": "ORDER-CANCEL-UNKNOWN", "status": "UNKNOWN",
            "pre_cancel_broker_snapshot": {
                "volume_initial": 0.1, "volume_current": 0.1,
                "source_verified": True,
            },
            "post_cancel_broker_record": None,
        }
        store = MagicMock()
        store.query_events.return_value = [
            {"event_type": DE.ORDER_CANCELLED, "parent_id": "ORDER-CANCEL-UNKNOWN",
             "payload_json": json.dumps(cancelled)},
            {"event_type": DE.ORDER_PENDING, "parent_id": "ORDER-CANCEL-UNKNOWN",
             "payload_json": json.dumps(pending)},
        ]
        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._restore_pending_orders()
        assert system._pending_orders["ORDER-CANCEL-UNKNOWN"]["status"] == "UNKNOWN"


    def test_restart_recovery_keeps_zero_remainder_cancelled_order_for_deal_attribution(self):
        import json
        import threading
        from event_driven_bootstrap import EventDrivenSystem
        from persistence import domain_events as DE

        system = object.__new__(EventDrivenSystem)
        system._pending_orders_lock = threading.Lock()
        system._pending_orders = {}
        pending = {
            "order_id": "ORDER-CANCEL-ZERO-REMAINDER",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        cancelled = {
            "order_id": "ORDER-CANCEL-ZERO-REMAINDER",
            "post_cancel_broker_record": {
                "state": 2, "volume_initial": 0.1, "volume_current": 0.0,
                "source_verified": True,
            },
        }
        store = MagicMock()
        store.query_events.return_value = [
            {"event_type": DE.ORDER_CANCELLED, "parent_id": "ORDER-CANCEL-ZERO-REMAINDER",
             "payload_json": json.dumps(cancelled)},
            {"event_type": DE.ORDER_PENDING, "parent_id": "ORDER-CANCEL-ZERO-REMAINDER",
             "payload_json": json.dumps(pending)},
        ]
        with patch("event_driven_bootstrap.get_event_store", return_value=store):
            system._restore_pending_orders()
        assert system._pending_orders["ORDER-CANCEL-ZERO-REMAINDER"]["status"] == "UNKNOWN"

