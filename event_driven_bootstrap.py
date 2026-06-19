"""APEX TRADER — Event-Driven System Bootstrap.

Wires all subsystems into a running event-driven system:

  Analysis Plane:    CandleClose events → brain modules → WorldModel
  Execution Plane:   Ticks → PositionWorkers → IntentAggregator → ActionExecutor → Broker
  Entry Plane:       WorldModel zones → tick detection → M1 confirm → gates → executor

Start:  ``system.start()``
Stop:   ``system.stop()``
"""

from __future__ import annotations

import os
import threading
import time as _time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
from loguru import logger

from config import AppConfig, INSTRUMENT_REGISTRY, get_pip_size, is_always_open, Platform
from brain.world_model import WorldModelStore, build_world_model
from core.system_context import SystemContext
from persistence.event_store import get_event_store
from persistence import domain_events as DE
from tick import EventBus, Tick, TickStore, CandleCloseDetector, TickRouter
from tick.models import CandleClose
from scanner.candle_close_handler import CandleCloseHandler
from execution.intents import Intent, IntentType
from execution.intent_aggregator import IntentAggregator, AggregatorConfig
from execution.action_executor import ActionExecutor, ExecutorConfig
from execution.risk_gate import GateConfig
from execution.position_worker import PositionWorker, WorkerConfig, ScanContext, MarketContext
from execution.position_snapshot import PositionSnapshot, build_position_snapshot
from execution.management_state import ManagementStateStore
from entry import EntryOrchestrator, EntryConfig
from platform_context import build_context_for_symbol
from platforms.platform_manager import PlatformManager
from risk.position_sizer import PositionSizer


# ── Tick source threads ──────────────────────────────────────────────


class MT5TickPoller:
    """Polls MT5 prices and injects Tick objects into the TickRouter."""

    def __init__(
        self,
        platform_manager: PlatformManager,
        tick_router: TickRouter,
        symbols: list[str],
        poll_interval: float = 0.05,
    ) -> None:
        self._pm = platform_manager
        self._router = tick_router
        self._symbols = list(symbols)
        self._interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._error_counts: dict[str, int] = {}
        self._last_error_log: dict[str, float] = {}

    def start(self) -> None:
        if self._running or not self._symbols:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="mt5-tick-poller",
        )
        self._thread.start()
        logger.info("[mt5-poller] started for {} symbols", len(self._symbols))

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        logger.info("[mt5-poller] stopped")

    def _poll_loop(self) -> None:
        error_counts: dict[str, int] = defaultdict(int)
        last_error_log: dict[str, float] = {}
        remove_after = 500
        removed: set[str] = set()

        while self._running:
            for sym in list(self._symbols):
                if not self._running:
                    break
                if sym in removed:
                    continue
                try:
                    td = self._pm.get_price(sym)
                    if td is not None and td.bid > 0:
                        tick = Tick(
                            symbol=sym,
                            bid=td.bid,
                            ask=td.ask,
                            timestamp=datetime.now(timezone.utc),
                            source="mt5",
                        )
                        self._router.on_tick(tick)
                        error_counts[sym] = 0
                    else:
                        error_counts[sym] += 1
                except Exception as exc:
                    error_counts[sym] += 1
                    now = _time.monotonic()
                    if now - last_error_log.get(sym, 0) >= 60.0:
                        last_error_log[sym] = now
                        logger.warning(
                            "[mt5-poller] {} tick error (count={}): {}",
                            sym, error_counts[sym], exc,
                        )

                if error_counts.get(sym, 0) >= remove_after:
                    logger.warning(
                        "[mt5-poller] {} removed from poll — {} consecutive failures (symbol not on broker)",
                        sym, remove_after,
                    )
                    self._symbols.remove(sym)
                    removed.add(sym)
            _time.sleep(self._interval)


class DerivTickAdapter:
    """Hooks into Deriv WebSocket tick stream and injects into TickRouter."""

    def __init__(
        self,
        platform_manager: PlatformManager,
        tick_router: TickRouter,
        symbols: list[str],
        poll_interval: float = 0.1,
    ) -> None:
        self._pm = platform_manager
        self._router = tick_router
        self._symbols = list(symbols)
        self._interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._error_counts: dict[str, int] = {}
        self._last_error_log: dict[str, float] = {}

    def start(self) -> None:
        if self._running or not self._symbols:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="deriv-tick-adapter",
        )
        self._thread.start()
        logger.info("[deriv-adapter] started for {} symbols", len(self._symbols))

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        logger.info("[deriv-adapter] stopped")

    def _poll_loop(self) -> None:
        error_counts: dict[str, int] = defaultdict(int)
        last_error_log: dict[str, float] = {}

        while self._running:
            for sym in self._symbols:
                if not self._running:
                    break
                try:
                    td = self._pm.get_price(sym)
                    if td is not None and td.bid > 0:
                        tick = Tick(
                            symbol=sym,
                            bid=td.bid,
                            ask=td.ask,
                            timestamp=datetime.now(timezone.utc),
                            source="deriv",
                        )
                        self._router.on_tick(tick)
                        error_counts[sym] = 0
                    else:
                        error_counts[sym] += 1
                except Exception as exc:
                    error_counts[sym] += 1
                    now = _time.monotonic()
                    if now - last_error_log.get(sym, 0) >= 60.0:
                        last_error_log[sym] = now
                        logger.warning(
                            "[deriv-adapter] {} tick error (count={}): {}",
                            sym, error_counts[sym], exc,
                        )
            _time.sleep(self._interval)


# ── Management evaluation loop ───────────────────────────────────────


class PositionEvaluator:
    """Evaluates all open positions each tick cycle.

    Groups positions by symbol.  When a tick arrives for a symbol, all
    positions on that symbol are evaluated using the same tick price.
    Intents are submitted to the IntentAggregator.
    """

    def __init__(
        self,
        platform_manager: PlatformManager,
        tick_store: TickStore,
        world_model_store: WorldModelStore,
        intent_aggregator: IntentAggregator,
        mgmt_store: Optional[ManagementStateStore] = None,
        worker_config: Optional[WorkerConfig] = None,
        max_workers: int = 4,
    ) -> None:
        self._pm = platform_manager
        self._tick_store = tick_store
        self._wm_store = world_model_store
        self._aggregator = intent_aggregator
        self._mgmt_store = mgmt_store or ManagementStateStore()
        self._worker = PositionWorker(worker_config or WorkerConfig())
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="pos-eval",
        )
        self._lock = threading.Lock()
        self._running = False
        self._eval_count = 0
        self._suppressed_tickets: dict[str, float] = {}
        self._suppress_duration = 60.0

    def evaluate_all(self) -> None:
        """Snapshot all open positions and evaluate against latest ticks."""
        try:
            positions = self._pm.get_all_open_positions()
        except Exception as exc:
            logger.debug("[pos-eval] failed to get positions: {}", exc)
            return

        if not positions:
            return

        now_mono = _time.monotonic()
        self._suppressed_tickets = {
            t: exp for t, exp in self._suppressed_tickets.items()
            if exp > now_mono
        }

        now = datetime.now(timezone.utc)
        by_symbol: dict[str, list] = {}
        active_tickets: set[str] = set()
        for pos in positions:
            sym = getattr(pos, "symbol", "")
            ticket = str(getattr(pos, "order_id", getattr(pos, "ticket", "")))
            if sym:
                by_symbol.setdefault(sym, []).append(pos)
            if ticket:
                active_tickets.add(ticket)

        self._mgmt_store.cleanup(active_tickets)

        for symbol, pos_list in by_symbol.items():
            tick = self._tick_store.get_latest(symbol)
            if tick is None:
                continue
            price = tick.mid
            for pos in pos_list:
                self._evaluate_position(pos, price, now, now_mono)

        self._eval_count += 1

    def _evaluate_position(
        self, pos, price: float, now: datetime, now_mono: float,
    ) -> None:
        try:
            order_id = str(getattr(pos, "order_id", getattr(pos, "ticket", "")))

            if order_id in self._suppressed_tickets:
                return

            direction = getattr(pos, "direction", "")
            sl = getattr(pos, "sl", 0.0) or 0.0
            pip_size = 0.0001
            try:
                pip_size = get_pip_size(getattr(pos, "symbol", ""))
            except Exception:
                pass

            mgmt = self._mgmt_store.get_or_create(
                order_id,
                original_stop_loss=sl,
                stop_loss=sl,
                tp1=getattr(pos, "tp1", 0.0) or 0.0,
                tp2=getattr(pos, "tp2", 0.0) or 0.0,
                original_tp2=getattr(pos, "tp2", 0.0) or 0.0,
                remaining_size_lots=getattr(pos, "lots", 0.0) or 0.0,
                pip_size=pip_size,
                highest_price_since_entry=getattr(pos, "entry_price", price),
                lowest_price_since_entry=getattr(pos, "entry_price", price),
            )

            if price > mgmt.highest_price_since_entry:
                mgmt.highest_price_since_entry = price
            if price < mgmt.lowest_price_since_entry:
                mgmt.lowest_price_since_entry = price
            mgmt.last_eval_time = now

            snap = build_position_snapshot(pos, tm_trade=mgmt, current_price=price)
            intents = self._worker.evaluate(snap, now)

            if intents:
                self._aggregator.register_position(
                    ticket=order_id,
                    direction=direction,
                    current_sl=sl,
                    pip_size=pip_size,
                )
                self._aggregator.submit(intents)

                for intent in intents:
                    if intent.intent_type == IntentType.CLOSE:
                        self._mgmt_store.remove(order_id)
                    elif intent.intent_type == IntentType.MODIFY_SL and intent.new_sl:
                        mgmt.stop_loss = intent.new_sl
                        if not mgmt.at_breakeven:
                            entry = getattr(pos, "entry_price", 0.0)
                            if direction.upper() in ("BUY", "LONG"):
                                if intent.new_sl >= entry:
                                    mgmt.at_breakeven = True
                            else:
                                if intent.new_sl <= entry:
                                    mgmt.at_breakeven = True
                    elif intent.intent_type == IntentType.PARTIAL_CLOSE:
                        mgmt.partial_closed = True
                        mgmt.tp1_hit = True
        except Exception as exc:
            logger.debug(
                "[pos-eval] error evaluating {}: {}",
                getattr(pos, "symbol", "?"), exc,
            )

    def suppress_ticket(self, ticket: str, duration: float = 60.0) -> None:
        """Suppress evaluation of a ticket for the given duration (seconds)."""
        self._suppressed_tickets[ticket] = _time.monotonic() + duration

    @property
    def eval_count(self) -> int:
        return self._eval_count


# ── Flush loop ───────────────────────────────────────────────────────


class FlushLoop:
    """Periodically drains the IntentAggregator and executes via ActionExecutor."""

    def __init__(
        self,
        aggregator: IntentAggregator,
        executor: ActionExecutor,
        platform_manager: PlatformManager,
        evaluator: Optional[PositionEvaluator] = None,
        on_close_callback: Optional[Any] = None,
        interval: float = 0.1,
    ) -> None:
        self._aggregator = aggregator
        self._executor = executor
        self._pm = platform_manager
        self._evaluator = evaluator
        self._on_close = on_close_callback
        self._interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._flush_count = 0
        self._intents_executed = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="flush-loop",
        )
        self._thread.start()
        logger.info("[flush-loop] started (interval={:.0f}ms)", self._interval * 1000)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        logger.info(
            "[flush-loop] stopped (flushed {} times, {} intents executed)",
            self._flush_count, self._intents_executed,
        )

    def _loop(self) -> None:
        while self._running:
            try:
                intents = self._aggregator.flush()
                if intents:
                    open_positions = self._build_position_map()
                    results = self._executor.execute_batch(
                        intents, open_positions,
                    )
                    for i, r in enumerate(results):
                        if r.success:
                            self._intents_executed += 1
                            intent = intents[i] if i < len(intents) else None
                            if (
                                self._on_close is not None
                                and intent is not None
                                and intent.intent_type == IntentType.CLOSE
                            ):
                                try:
                                    self._on_close(intent, r)
                                except Exception as exc:
                                    logger.debug("[flush-loop] close callback error: {}", exc)
                        elif self._evaluator and not r.success:
                            err_msg = str(getattr(r, "error", "") or "").lower()
                            if "market closed" in err_msg or "market is closed" in err_msg:
                                ticket = intents[i].position_ticket if i < len(intents) else ""
                                if ticket:
                                    self._evaluator.suppress_ticket(ticket, 60.0)
                            elif "no longer open" in err_msg:
                                pass
                self._flush_count += 1
            except Exception as exc:
                logger.warning("[flush-loop] error: {}", exc)
            _time.sleep(self._interval)

    def _build_position_map(self) -> dict[str, dict]:
        """Build the open_positions dict the executor expects."""
        try:
            positions = self._pm.get_all_open_positions()
        except Exception:
            return {}
        result: dict[str, dict] = {}
        for pos in positions:
            ticket = str(getattr(pos, "order_id", getattr(pos, "ticket", "")))
            result[ticket] = {
                "symbol": getattr(pos, "symbol", ""),
                "direction": getattr(pos, "direction", ""),
                "sl": getattr(pos, "sl", 0.0),
                "platform": getattr(pos, "platform", ""),
                "lots": getattr(pos, "lots", 0.0),
                "remaining_lots": getattr(pos, "remaining_lots", getattr(pos, "lots", 0.0)),
            }
        return result


# ── Tick evaluation loop ─────────────────────────────────────────────


class TickEvalLoop:
    """On each tick cycle, evaluates positions and critical levels.

    Runs at a configurable frequency (default 10 Hz) and coordinates:
    1. Position evaluation via PositionEvaluator
    2. Entry detection is handled by EntryOrchestrator subscribing to tick events
    """

    def __init__(
        self,
        evaluator: PositionEvaluator,
        interval: float = 0.1,
    ) -> None:
        self._evaluator = evaluator
        self._interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="tick-eval-loop",
        )
        self._thread.start()
        logger.info("[tick-eval] started (interval={:.0f}ms)", self._interval * 1000)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        logger.info("[tick-eval] stopped ({} evals)", self._evaluator.eval_count)

    def _loop(self) -> None:
        while self._running:
            try:
                self._evaluator.evaluate_all()
            except Exception as exc:
                logger.warning("[tick-eval] error: {}", exc)
            _time.sleep(self._interval)


# ── Main orchestrator ────────────────────────────────────────────────


class EventDrivenSystem:
    """Wires and manages the entire event-driven trading system.

    Usage::

        system = EventDrivenSystem(config, platform_manager)
        system.start()
        # ... system runs until stop() is called
        system.stop()
    """

    def __init__(
        self,
        config: AppConfig,
        platform_manager: PlatformManager,
        ctx: Optional[SystemContext] = None,
    ) -> None:
        self._config = config
        self._pm = platform_manager
        self._running = False
        self._ctx = ctx

        # ── Core infrastructure ──────────────────────────────────────
        self._event_bus = EventBus()
        self._tick_store = TickStore(max_hz=15.0)
        self._candle_detector = CandleCloseDetector(self._event_bus)
        self._tick_router = TickRouter(
            self._tick_store, self._candle_detector, self._event_bus,
        )
        self._wm_store = WorldModelStore()

        # ── Analysis plane ───────────────────────────────────────────
        self._candle_handler = CandleCloseHandler(
            event_bus=self._event_bus,
            world_model_store=self._wm_store,
            candle_fetcher=self._fetch_candles,
        )

        # ── Execution plane ──────────────────────────────────────────
        self._aggregator = IntentAggregator(AggregatorConfig())
        self._executor = ActionExecutor(
            broker=self._pm,  # PlatformManager satisfies BrokerPort
            config=ExecutorConfig(),
        )
        self._mgmt_store = ManagementStateStore(db_path="data/management_state.db")
        self._evaluator = PositionEvaluator(
            platform_manager=self._pm,
            tick_store=self._tick_store,
            world_model_store=self._wm_store,
            intent_aggregator=self._aggregator,
            mgmt_store=self._mgmt_store,
        )

        # ── Entry plane ──────────────────────────────────────────────
        self._entry_orchestrator = EntryOrchestrator(
            world_model_store=self._wm_store,
            config=EntryConfig(),
            pip_size_lookup=self._safe_pip_size,
            on_entry_decision=self._on_entry_decision,
            is_instrument_known=lambda s: s in INSTRUMENT_REGISTRY,
            is_session_active=self._check_session_active,
            is_news_clear=self._check_news_clear,
            get_spread_pips=self._get_spread_pips,
            get_m1_dataframe=self._get_m1_dataframe,
        )

        # ── Background loops ─────────────────────────────────────────
        self._flush_loop = FlushLoop(
            self._aggregator, self._executor, self._pm,
            evaluator=self._evaluator,
            on_close_callback=self._handle_close_result,
        )
        self._tick_eval_loop = TickEvalLoop(self._evaluator)

        # ── Tick source adapters ─────────────────────────────────────
        mt5_symbols, deriv_symbols = self._classify_symbols()
        self._mt5_poller = MT5TickPoller(
            self._pm, self._tick_router, mt5_symbols,
        )
        self._deriv_adapter = DerivTickAdapter(
            self._pm, self._tick_router, deriv_symbols,
        )

        # ── Event wiring ─────────────────────────────────────────────
        self._event_bus.subscribe("tick", self._entry_orchestrator.on_tick)
        self._event_bus.subscribe(
            "candle_close:M1", lambda ev: self._entry_orchestrator.on_m1_close(ev.symbol),
        )
        self._event_bus.subscribe(
            "world_model_update", self._entry_orchestrator.on_world_model_update,
        )
        self._event_bus.subscribe(
            "world_model_update", self._on_world_model_update,
        )

        self._register_tunable_adapters()

        logger.info("[event-driven] system initialized")

    # ── Tunable adapter registration ────────────────────────────────

    def _register_tunable_adapters(self) -> None:
        """Register all tunable adapters with TunerAgent.

        Mirrors TradingLoop._setup_tuner_agent() — each adapter bridges a
        SystemContext subsystem to the TunerAgent's coordinated tune cycle.
        """
        ctx = self._ctx
        if ctx is None or ctx.tuner_agent is None:
            return

        agent = ctx.tuner_agent
        registered = 0

        try:
            from adaptive.tunable_adapters import (
                ScoreOptimizerTunable,
                RegimeLearnerTunable,
                PairLearnerTunable,
                SessionLearnerTunable,
                EVEstimatorTunable,
                GateTunerTunable,
                PlannerCalibratorTunable,
                SignalLedgerTunable,
                PostCloseTrackerTunable,
                VoteCalibratorTunable,
                ModuleGovernorTunable,
                CounterfactualTunable,
                InteractionAnalyzerTunable,
                SignalDiscoveryTunable,
                VirtualSignalManagerTunable,
                CapitalAllocatorTunable,
                ExecutionProfileTunable,
                RegimeDetectorTunable,
                BehaviorDiscoveryTunable,
                ConsumerTunable,
            )
        except ImportError as exc:
            logger.warning("[tuner] failed to import tunable adapters: {}", exc)
            return

        def _trades_provider():
            if ctx.ml_adapter is not None:
                try:
                    return ctx.ml_adapter.get_trade_history()
                except Exception:
                    pass
            return []

        def _prices_provider():
            prices = {}
            for sym in INSTRUMENT_REGISTRY:
                tick = self._tick_store.get_latest(sym)
                if tick is not None:
                    prices[sym] = tick.mid
            return prices

        # ScoreOptimizer
        if ctx.ml_adapter is not None and hasattr(ctx.ml_adapter, "optimizer"):
            try:
                agent.register(ScoreOptimizerTunable(
                    ctx.ml_adapter.optimizer, _trades_provider,
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] ScoreOptimizer registration failed: {}", exc)

        # RegimeLearner
        if ctx.ml_adapter is not None and hasattr(ctx.ml_adapter, "regime_learner"):
            try:
                agent.register(RegimeLearnerTunable(
                    ctx.ml_adapter.regime_learner, _trades_provider,
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] RegimeLearner registration failed: {}", exc)

        # PairLearner
        if ctx.ml_adapter is not None and hasattr(ctx.ml_adapter, "pair_learner"):
            try:
                agent.register(PairLearnerTunable(
                    ctx.ml_adapter.pair_learner, _trades_provider,
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] PairLearner registration failed: {}", exc)

        # SessionLearner
        if ctx.ml_adapter is not None and hasattr(ctx.ml_adapter, "session_learner"):
            try:
                agent.register(SessionLearnerTunable(
                    ctx.ml_adapter.session_learner, _trades_provider,
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] SessionLearner registration failed: {}", exc)

        # EVEstimator
        try:
            from scanner.pair_scanner import PairScanner
            scanner_inst = getattr(self, "_scanner", None)
            ev_estimator = getattr(scanner_inst, "_ev_estimator", None) if scanner_inst else None
            if ev_estimator is not None:
                agent.register(EVEstimatorTunable(ev_estimator))
                registered += 1
        except Exception as exc:
            logger.debug("[tuner] EVEstimator registration failed: {}", exc)

        # GateTuner
        if ctx.gate_tuner is not None:
            try:
                outcomes_provider = (
                    (lambda: ctx.shadow_store.get_outcomes_by_gate())
                    if ctx.shadow_store is not None else (lambda: [])
                )
                agent.register(GateTunerTunable(ctx.gate_tuner, outcomes_provider))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] GateTuner registration failed: {}", exc)

        # PlannerCalibrator
        if ctx.calibrator is not None:
            try:
                def _completed_plans():
                    if ctx.outcome_logger is not None:
                        try:
                            return ctx.outcome_logger.get_completed_trades()
                        except Exception:
                            pass
                    return []

                agent.register(PlannerCalibratorTunable(
                    ctx.calibrator, _completed_plans,
                    planner=ctx.trade_planner,
                    planner_enabled=ctx.trade_planner is not None,
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] PlannerCalibrator registration failed: {}", exc)

        # SignalLedger
        if ctx.signal_ledger is not None:
            try:
                agent.register(SignalLedgerTunable(ctx.signal_ledger, _prices_provider))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] SignalLedger registration failed: {}", exc)

        # PostCloseTracker
        if ctx.post_close_tracker is not None:
            try:
                agent.register(PostCloseTrackerTunable(
                    ctx.post_close_tracker,
                    data_source_provider=lambda: self._pm,
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] PostCloseTracker registration failed: {}", exc)

        # VoteCalibrator
        if ctx.vote_calibrator is not None:
            try:
                vc_cfg = getattr(self._config, "vote_calibrator", None)
                vc_enabled = bool(getattr(vc_cfg, "vote_calibration_enabled", False))
                if vc_enabled:
                    agent.register(VoteCalibratorTunable(ctx.vote_calibrator))
                    registered += 1
            except Exception as exc:
                logger.debug("[tuner] VoteCalibrator registration failed: {}", exc)

        # ModuleGovernor
        if ctx.module_governor is not None:
            try:
                mg_cfg = getattr(self._config, "module_governor", None)
                mg_enabled = bool(getattr(mg_cfg, "module_governor_enabled", False))
                if mg_enabled:
                    agent.register(ModuleGovernorTunable(ctx.module_governor))
                    registered += 1
            except Exception as exc:
                logger.debug("[tuner] ModuleGovernor registration failed: {}", exc)

        # CounterfactualEngine
        if ctx.counterfactual_engine is not None:
            try:
                cf_cfg = getattr(self._config, "counterfactual", None)
                agent.register(CounterfactualTunable(
                    ctx.counterfactual_engine,
                    min_trades=int(getattr(cf_cfg, "min_trades_for_attribution", 50) if cf_cfg else 50),
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] Counterfactual registration failed: {}", exc)

        # InteractionAnalyzer
        if ctx.interaction_analyzer is not None:
            try:
                cf_cfg = getattr(self._config, "counterfactual", None)
                agent.register(InteractionAnalyzerTunable(
                    ctx.interaction_analyzer,
                    min_trades=int(getattr(cf_cfg, "min_trades_for_attribution", 50) if cf_cfg else 50),
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] InteractionAnalyzer registration failed: {}", exc)

        # SignalDiscoveryEngine
        if ctx.signal_discovery is not None:
            try:
                sd_cfg = getattr(self._config, "signal_discovery", None)
                agent.register(SignalDiscoveryTunable(
                    ctx.signal_discovery,
                    min_trades=int(getattr(sd_cfg, "min_trades_for_discovery", 100) if sd_cfg else 100),
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] SignalDiscovery registration failed: {}", exc)

        # VirtualSignalManager
        if ctx.virtual_signal_manager is not None:
            try:
                sd_cfg = getattr(self._config, "signal_discovery", None)
                agent.register(VirtualSignalManagerTunable(
                    ctx.virtual_signal_manager,
                    min_trades=int(getattr(sd_cfg, "retirement_check_interval", 50) if sd_cfg else 50),
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] VirtualSignalManager registration failed: {}", exc)

        # CapitalAllocator
        if ctx.capital_allocator is not None:
            try:
                ca_cfg = getattr(self._config, "capital_allocation", None)
                agent.register(CapitalAllocatorTunable(
                    ctx.capital_allocator,
                    min_trades=int(getattr(ca_cfg, "rebalance_interval_trades", 25) if ca_cfg else 25),
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] CapitalAllocator registration failed: {}", exc)

        # ExecutionProfileManager
        if ctx.execution_profiles is not None:
            try:
                agent.register(ExecutionProfileTunable(ctx.execution_profiles))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] ExecutionProfiles registration failed: {}", exc)

        # RegimeDetector
        if ctx.regime_detector is not None:
            try:
                agent.register(RegimeDetectorTunable(ctx.regime_detector))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] RegimeDetector registration failed: {}", exc)

        # BehaviorDiscoveryEngine
        if ctx.behavior_discovery is not None:
            try:
                bd_cfg = getattr(self._config, "behavior_discovery", None)
                agent.register(BehaviorDiscoveryTunable(
                    ctx.behavior_discovery,
                    min_trades=int(getattr(bd_cfg, "min_trades_to_cluster", 100) if bd_cfg else 100),
                ))
                registered += 1
            except Exception as exc:
                logger.debug("[tuner] BehaviorDiscovery registration failed: {}", exc)

        # Consumer / observer entries — read-only, makes agent see whole system
        _consumer_entries = [
            ("risk_engine", ctx.risk_engine),
            ("position_sizer", None),
            ("orchestrator", ctx.orchestrator),
            ("portfolio_governor", ctx.portfolio_governor),
            ("trade_manager", None),
        ]
        for name, comp in _consumer_entries:
            try:
                provider = None
                if comp is not None and hasattr(comp, "get_current_params"):
                    provider = comp.get_current_params
                agent.register(ConsumerTunable(name, provider))
                registered += 1
            except Exception:
                pass

        # Wire set_tuner_agent on components that support it
        for comp in (ctx.ml_adapter, ctx.gate_tuner, ctx.calibrator,
                     ctx.signal_ledger, ctx.vote_calibrator, ctx.module_governor,
                     ctx.virtual_signal_manager, ctx.capital_allocator,
                     ctx.execution_profiles, ctx.regime_detector):
            if comp is not None and hasattr(comp, "set_tuner_agent"):
                try:
                    comp.set_tuner_agent(agent)
                except Exception:
                    pass

        logger.info(
            "[tuner] registered {} tunable adapters with TunerAgent", registered,
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the event-driven system."""
        if self._running:
            return
        self._running = True

        logger.info("=" * 60)
        logger.info("  APEX TRADER — EVENT-DRIVEN MODE")
        logger.info("=" * 60)

        # ── Startup recovery: crash marker detection ─────────────────
        try:
            from ops.lifecycle import StartupRecovery
            ops_cfg = getattr(self._config, "ops", None)
            if ops_cfg is None:
                from types import SimpleNamespace
                ops_cfg = SimpleNamespace(crash_marker_path="data/.crash_marker")
            recovery = StartupRecovery(ops_cfg)
            result = recovery.run()
            if result.get("unclean_previous_exit"):
                logger.warning(
                    "[event-driven] previous run exited UNCLEAN — "
                    "broker reconciliation recommended",
                )
        except Exception as exc:
            logger.debug("[startup] crash recovery check failed: {}", exc)

        self._recover_open_positions()

        symbols = list(INSTRUMENT_REGISTRY.keys())
        for sym in symbols:
            self._candle_detector.register(sym)
        logger.info("[event-driven] registered {} symbols for candle detection", len(symbols))

        self._tick_router.start()
        self._mt5_poller.start()
        self._deriv_adapter.start()
        self._flush_loop.start()
        self._tick_eval_loop.start()

        # ── Start ProcessWatchdog heartbeat thread ───────────────────
        ctx = self._ctx
        if ctx is not None and ctx.process_watchdog is not None:
            self._watchdog_running = True
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True, name="ed-watchdog",
            )
            self._watchdog_thread.start()
            logger.info("[event-driven] process watchdog started")

        # ── Initialize TradeJournal (async) ──────────────────────────
        if ctx is not None and ctx.trade_journal is not None:
            try:
                import asyncio
                _loop = asyncio.new_event_loop()
                _loop.run_until_complete(ctx.trade_journal.initialize())
                _loop.close()
            except Exception as exc:
                logger.debug("[startup] TradeJournal init failed: {}", exc)

        logger.info("[event-driven] all subsystems started")
        logger.info("-" * 60)
        logger.info("  ANALYSIS:  candle-close → brain modules → WorldModel")
        logger.info("  EXECUTION: ticks → position workers → intent aggregator → executor")
        logger.info("  ENTRY:     zones → tick detection → M1 confirm → gates → executor")
        logger.info("-" * 60)

    def stop(self) -> None:
        """Stop all subsystems gracefully."""
        if not self._running:
            return
        self._running = False
        logger.info("[event-driven] shutting down...")

        self._tick_eval_loop.stop()
        self._flush_loop.stop()
        self._mt5_poller.stop()
        self._deriv_adapter.stop()
        self._tick_router.stop()
        self._candle_handler.shutdown()
        self._event_bus.clear()
        self._mgmt_store.close()

        self._watchdog_running = False
        wt = getattr(self, "_watchdog_thread", None)
        if wt is not None:
            wt.join(timeout=2.0)

        ctx = self._ctx
        if ctx is not None:
            for name in ("signal_ledger", "counterfactual_engine",
                         "interaction_analyzer", "module_governor",
                         "shadow_store", "post_close_tracker",
                         "gate_tuner", "capital_allocator",
                         "execution_profiles", "regime_detector",
                         "behavior_discovery", "signal_discovery",
                         "virtual_module_registry"):
                sub = getattr(ctx, name, None)
                if sub is not None and hasattr(sub, "close"):
                    try:
                        sub.close()
                        logger.debug("[shutdown] {} flushed", name)
                    except Exception as exc:
                        logger.debug("[shutdown] {} close failed: {}", name, exc)

            # Flush event store
            try:
                es = get_event_store()
                es.flush(timeout=3.0)
            except Exception:
                pass

            # Clear crash marker
            try:
                from ops.lifecycle import StartupRecovery
                ops_cfg = getattr(self._config, "ops", None)
                if ops_cfg is not None:
                    StartupRecovery(ops_cfg).clear_crash_marker()
                else:
                    from types import SimpleNamespace
                    StartupRecovery(SimpleNamespace(
                        crash_marker_path="data/.crash_marker",
                    )).clear_crash_marker()
                logger.debug("[shutdown] crash marker cleared")
            except Exception as exc:
                logger.debug("[shutdown] crash marker clear failed: {}", exc)

        logger.info("[event-driven] shutdown complete")

    def run_forever(self) -> None:
        """Block until interrupted (Ctrl-C)."""
        self.start()
        try:
            while self._running:
                _time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def world_model_store(self) -> WorldModelStore:
        return self._wm_store

    @property
    def tick_store(self) -> TickStore:
        return self._tick_store

    @property
    def entry_orchestrator(self) -> EntryOrchestrator:
        return self._entry_orchestrator

    @property
    def executor(self) -> ActionExecutor:
        return self._executor

    @property
    def evaluator(self) -> PositionEvaluator:
        return self._evaluator

    def stats(self) -> dict[str, Any]:
        """Return operational metrics snapshot."""
        return {
            "tick_store": self._tick_store.stats(),
            "candle_handler": self._candle_handler.stats(),
            "executor": {
                "metrics": self._executor.get_metrics().__dict__,
            },
            "entry": self._entry_orchestrator.stats,
            "position_evals": self._evaluator.eval_count,
            "tick_router": {
                "ticks_routed": self._tick_router.ticks_routed,
            },
            "candle_detector": {
                "events_emitted": self._candle_detector.events_emitted,
                "tracked_pairs": self._candle_detector.tracked_pairs,
            },
        }

    # ── Internal helpers ─────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        """Background loop: heartbeat + stall detection + daily maintenance + heat monitoring."""
        _last_heat_check = 0.0
        while getattr(self, "_watchdog_running", False):
            ctx = self._ctx
            if ctx is not None and ctx.process_watchdog is not None:
                try:
                    ctx.process_watchdog.beat()
                    ctx.process_watchdog.record_tick()
                    ctx.process_watchdog.check_stall()
                except Exception:
                    pass

            if ctx is not None and ctx.daily_maintenance is not None:
                try:
                    if ctx.daily_maintenance.should_run():
                        result = ctx.daily_maintenance.run()
                        logger.info("[event-driven] daily maintenance — {}", result)
                        try:
                            es = get_event_store()
                            es.prune()
                        except Exception:
                            pass
                except Exception as exc:
                    logger.debug("[watchdog] daily maintenance check failed: {}", exc)

            # ── Portfolio heat monitoring (every 5s) ─────────────────
            now = _time.time()
            if now - _last_heat_check >= 5.0:
                _last_heat_check = now
                self._check_portfolio_heat()

            # ── PostCloseTracker forward price checks (every 30s) ────
            if ctx is not None and ctx.post_close_tracker is not None:
                try:
                    if getattr(ctx.post_close_tracker, "enabled", False):
                        pending = getattr(ctx.post_close_tracker, "pending_count", 0) or 0
                        if pending > 0:
                            ctx.post_close_tracker.process_pending_checks(self._pm)
                except Exception as exc:
                    logger.debug("[watchdog] PostCloseTracker tick failed: {}", exc)

            # ── Shadow resolution (advance open shadows on latest prices) ──
            self._resolve_shadows()

            _time.sleep(10.0)

    def _check_portfolio_heat(self) -> None:
        """Continuous portfolio heat monitoring — generates intents for open positions."""
        ctx = self._ctx
        if ctx is None or ctx.portfolio_risk_sm is None:
            return
        try:
            from risk.portfolio_risk_state import PortfolioRiskState
            state = ctx.portfolio_risk_sm.state
            if state == PortfolioRiskState.NORMAL:
                return

            positions = self._pm.get_all_open_positions()
            if not positions:
                return

            if state == PortfolioRiskState.EMERGENCY:
                for pos in positions:
                    ticket = str(getattr(pos, "order_id", getattr(pos, "ticket", "")))
                    symbol = getattr(pos, "symbol", "")
                    if ticket:
                        intent = Intent.close(
                            symbol=symbol,
                            ticket=ticket,
                            source="heat_monitor",
                            reason="portfolio_heat_emergency",
                        )
                        self._aggregator.submit(intent)
                logger.warning(
                    "[heat-monitor] EMERGENCY — {} CLOSE intents for all positions",
                    len(positions),
                )
                return

            if state == PortfolioRiskState.REDUCING:
                worst_pos = None
                worst_pnl = float("inf")
                for pos in positions:
                    pnl = getattr(pos, "profit", getattr(pos, "pnl", 0.0)) or 0.0
                    if pnl < worst_pnl:
                        worst_pnl = pnl
                        worst_pos = pos
                if worst_pos is not None:
                    ticket = str(getattr(worst_pos, "order_id", getattr(worst_pos, "ticket", "")))
                    symbol = getattr(worst_pos, "symbol", "")
                    if ticket:
                        intent = Intent.close(
                            symbol=symbol,
                            ticket=ticket,
                            source="heat_monitor",
                            reason="portfolio_heat_reducing_weakest",
                        )
                        self._aggregator.submit(intent)
                        logger.info(
                            "[heat-monitor] REDUCING — closing weakest {} (pnl={:.2f})",
                            symbol, worst_pnl,
                        )
                return

            if state == PortfolioRiskState.DEFENSIVE:
                for pos in positions:
                    ticket = str(getattr(pos, "order_id", getattr(pos, "ticket", "")))
                    symbol = getattr(pos, "symbol", "")
                    entry_price = getattr(pos, "entry_price", 0.0) or 0.0
                    current_sl = getattr(pos, "sl", 0.0) or 0.0
                    direction = getattr(pos, "direction", "LONG")
                    if not ticket or entry_price <= 0:
                        continue
                    is_long = str(direction).upper() in ("BUY", "LONG")
                    already_at_be = (
                        (current_sl >= entry_price if is_long else current_sl <= entry_price)
                        if current_sl > 0 else False
                    )
                    if not already_at_be:
                        pip_size = self._safe_pip_size(symbol)
                        be_price = entry_price + (2 * pip_size) if is_long else entry_price - (2 * pip_size)
                        intent = Intent.modify_sl(
                            symbol=symbol,
                            ticket=ticket,
                            new_sl=be_price,
                            source="heat_monitor",
                            reason="portfolio_heat_defensive_be",
                        )
                        self._aggregator.submit(intent)
                logger.info("[heat-monitor] DEFENSIVE — BE intents submitted")

        except Exception as exc:
            logger.debug("[heat-monitor] check failed: {}", exc)

    def _resolve_shadows(self) -> None:
        """Advance open shadow contracts on latest tick prices."""
        ctx = self._ctx
        if ctx is None or ctx.shadow_store is None:
            return
        try:
            open_shadows = ctx.shadow_store.get_open_contracts()
            if not open_shadows:
                return
            for shadow in open_shadows:
                sym = getattr(shadow, "symbol", "")
                tick = self._tick_store.get_latest(sym)
                if tick is None:
                    continue
                price = tick.mid
                sl = getattr(shadow, "stop_loss", 0.0)
                tp = getattr(shadow, "tp1", 0.0)
                direction = getattr(shadow, "direction", "LONG")
                is_long = str(direction).upper() in ("BUY", "LONG")
                hit_sl = (price <= sl if is_long else price >= sl) if sl > 0 else False
                hit_tp = (price >= tp if is_long else price <= tp) if tp > 0 else False
                if hit_sl or hit_tp:
                    outcome = "WIN" if hit_tp else "LOSS"
                    try:
                        ctx.shadow_store.resolve_contract(
                            getattr(shadow, "contract_id", ""),
                            outcome=outcome,
                            exit_price=price,
                        )
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("[shadow-resolve] failed: {}", exc)

    def _handle_close_result(self, intent: Intent, result: Any) -> None:
        """Called by FlushLoop when a CLOSE intent succeeds.

        Extracts P&L from the broker response and feeds it to the
        risk feedback chain.
        """
        symbol = getattr(intent, "symbol", "") or ""
        direction = getattr(intent, "direction", "")
        ticket = intent.position_ticket

        pnl_dollars = 0.0
        pnl_pips = 0.0
        resp = getattr(result, "broker_response", None)
        if resp is not None:
            pnl_dollars = float(getattr(resp, "pnl", 0.0) or 0.0)

        if symbol and direction:
            try:
                pip_size = get_pip_size(symbol)
                entry = getattr(resp, "entry_price", 0.0) or 0.0
                close_p = getattr(resp, "close_price", 0.0) or 0.0
                if entry > 0 and close_p > 0 and pip_size > 0:
                    if direction.upper() in ("BUY", "LONG"):
                        pnl_pips = (close_p - entry) / pip_size
                    else:
                        pnl_pips = (entry - close_p) / pip_size
            except Exception:
                pass

        self._on_trade_closed(
            symbol=symbol,
            direction=direction,
            pnl_dollars=pnl_dollars,
            pnl_pips=pnl_pips,
            ticket=ticket,
        )

    def _recover_open_positions(self) -> None:
        """Initialize management state for any positions open at startup."""
        try:
            positions = self._pm.get_all_open_positions()
            if not positions:
                return
            logger.info("[event-driven] found {} open positions at startup", len(positions))
            for pos in positions:
                ticket = str(getattr(pos, "order_id", getattr(pos, "ticket", "")))
                if not ticket:
                    continue
                entry_price = getattr(pos, "entry_price", 0.0)
                sl = getattr(pos, "sl", 0.0) or 0.0
                pip_size = 0.0001
                try:
                    pip_size = get_pip_size(getattr(pos, "symbol", ""))
                except Exception:
                    pass
                self._mgmt_store.get_or_create(
                    ticket,
                    original_stop_loss=sl,
                    stop_loss=sl,
                    tp1=getattr(pos, "tp1", 0.0) or 0.0,
                    tp2=getattr(pos, "tp2", 0.0) or 0.0,
                    original_tp2=getattr(pos, "tp2", 0.0) or 0.0,
                    remaining_size_lots=getattr(pos, "lots", 0.0) or 0.0,
                    pip_size=pip_size,
                    highest_price_since_entry=entry_price,
                    lowest_price_since_entry=entry_price,
                )
            logger.info(
                "[event-driven] initialized management state for {} positions",
                len(self._mgmt_store),
            )
        except Exception as exc:
            logger.warning("[event-driven] startup position recovery failed: {}", exc)

    def _classify_symbols(self) -> tuple[list[str], list[str]]:
        """Split registry symbols by platform (MT5 vs Deriv)."""
        mt5: list[str] = []
        deriv: list[str] = []
        for sym, info in INSTRUMENT_REGISTRY.items():
            pf = info.platform.value if hasattr(info.platform, "value") else str(info.platform)
            if pf == "deriv":
                deriv.append(sym)
            elif pf == "both":
                mt5.append(sym)
                deriv.append(sym)
            else:
                mt5.append(sym)
        return mt5, deriv

    def _fetch_candles(
        self, symbol: str, timeframe: str, count: int,
    ) -> Optional[pd.DataFrame]:
        """Candle fetcher for CandleCloseHandler — wraps PlatformManager."""
        try:
            data = self._pm.fetch_market_data(symbol, [timeframe], count)
            return data.get(timeframe)
        except Exception as exc:
            logger.debug("[event-driven] candle fetch failed {}/{}: {}", symbol, timeframe, exc)
            return None

    def _safe_pip_size(self, symbol: str) -> float:
        try:
            return get_pip_size(symbol)
        except Exception:
            return 0.0001

    def _get_spread_pips(self, symbol: str) -> float:
        try:
            return self._pm.get_spread(symbol)
        except Exception:
            return 1.0

    def _get_m1_dataframe(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            data = self._pm.fetch_market_data(symbol, ["M1"], 100)
            return data.get("M1")
        except Exception:
            return None

    def _check_session_active(self, symbol: str) -> bool:
        """SessionEngine callback for EntryOrchestrator gate."""
        ctx = self._ctx
        if ctx is None or ctx.session_engine is None:
            return True
        try:
            status = ctx.session_engine.get_status()
            return status.is_tradeable
        except Exception as exc:
            logger.debug("[session-gate] SessionEngine check failed: {}", exc)
            return True

    def _check_news_clear(self, symbol: str) -> bool:
        """NewsGuard callback for EntryOrchestrator gate."""
        ctx = self._ctx
        if ctx is None or ctx.news_guard is None:
            return True
        try:
            news_status = ctx.news_guard.check([symbol])
            return news_status.is_clear
        except Exception as exc:
            logger.debug("[news-gate] NewsGuard check failed: {}", exc)
            return True

    def _on_world_model_update(self, event: Any) -> None:
        """Feed density tracker when WorldModel updates after a scan."""
        ctx = self._ctx
        if ctx is None:
            return
        if ctx.opportunity_density_tracker is not None:
            try:
                ready_symbols = []
                for sym in INSTRUMENT_REGISTRY:
                    wm = self._wm_store.get(sym)
                    if wm is not None:
                        zones = getattr(wm, "entry_zones", [])
                        if zones:
                            ready_symbols.append(sym)
                ctx.opportunity_density_tracker.record_scan(ready_symbols)
            except Exception as exc:
                logger.debug("[density] density tracker update failed: {}", exc)

    def _on_entry_decision(self, decision: dict[str, Any]) -> None:
        """Handle entry decisions from EntryOrchestrator.

        Checks all risk subsystems before executing an order:
        1. DrawdownGuard — FROZEN mode blocks all entries
        2. PortfolioRiskStateMachine — DEFENSIVE+ blocks new entries
        3. PortfolioGovernor — max positions, daily loss, concentration
        4. AccountRiskManager — per-account daily loss cap / heat
        5. CorrelationEngine — cluster exposure (replaces simple currency count)
        6. PositionSizer — compute lot size / stake
        """
        symbol = decision.get("symbol", "")
        direction = decision.get("direction", "")
        entry_price = decision.get("entry_price", 0.0)
        sl = decision.get("stop_loss", 0.0)
        tp1 = decision.get("tp1", 0.0)
        tp2 = decision.get("tp2", 0.0)
        conviction = decision.get("conviction", 0)

        logger.info(
            "EVENT-DRIVEN ENTRY | {} {} @ {:.5f} SL={:.5f} TP={:.5f} score={}",
            symbol, direction, entry_price, sl, tp1, conviction,
        )

        try:
            ctx = self._ctx
            try:
                open_positions = self._pm.get_all_open_positions()
            except Exception:
                open_positions = []

            balance = self._pm.get_platform_balance(symbol)

            # ── Gate 1: DrawdownGuard ────────────────────────────────
            if ctx is not None and ctx.drawdown_guard is not None:
                try:
                    from brain.drawdown_guard import DrawdownMode
                    dd_status = ctx.drawdown_guard.get_status()
                    if dd_status.mode == DrawdownMode.FROZEN.value:
                        logger.warning(
                            "EVENT-DRIVEN ENTRY BLOCKED | {} — DrawdownGuard FROZEN "
                            "(daily loss limit hit)", symbol,
                        )
                        return
                    if dd_status.mode == DrawdownMode.RECOVERY.value:
                        logger.info(
                            "EVENT-DRIVEN ENTRY NOTE | {} — DrawdownGuard RECOVERY mode", symbol,
                        )
                except Exception as exc:
                    logger.debug("[entry-risk] DrawdownGuard check failed: {}", exc)

            # ── Gate 2: PortfolioRiskStateMachine ────────────────────
            if ctx is not None and ctx.portfolio_risk_sm is not None:
                try:
                    from risk.portfolio_risk_state import PortfolioRiskState
                    sm_state = ctx.portfolio_risk_sm.state
                    if sm_state in (
                        PortfolioRiskState.DEFENSIVE,
                        PortfolioRiskState.REDUCING,
                        PortfolioRiskState.EMERGENCY,
                    ):
                        logger.warning(
                            "EVENT-DRIVEN ENTRY BLOCKED | {} — PortfolioRisk state={} "
                            "(entries frozen)", symbol, sm_state.name,
                        )
                        return
                except Exception as exc:
                    logger.debug("[entry-risk] PortfolioRiskSM check failed: {}", exc)

            # ── Gate 3: PortfolioGovernor ────────────────────────────
            if ctx is not None and ctx.portfolio_governor is not None:
                try:
                    verdict = ctx.portfolio_governor.check(
                        symbol=symbol,
                        direction=direction,
                        open_positions=open_positions,
                        account_balance=balance or 0.0,
                    )
                    if not verdict.allowed:
                        logger.warning(
                            "EVENT-DRIVEN ENTRY BLOCKED | {} — Governor: {}",
                            symbol, verdict.reason,
                        )
                        return
                except Exception as exc:
                    logger.debug("[entry-risk] PortfolioGovernor check failed: {}", exc)

            # ── Gate 4: AccountRiskManager ───────────────────────────
            if ctx is not None and ctx.account_risk is not None:
                try:
                    acct = ctx.account_key(symbol, self._pm)
                    if balance and balance > 0:
                        ctx.account_risk.update_balance(acct, balance)
                    if ctx.account_risk.daily_loss_halted(acct):
                        logger.warning(
                            "EVENT-DRIVEN ENTRY BLOCKED | {} — AccountRisk: account {} "
                            "daily loss cap hit", symbol, acct,
                        )
                        return
                    if ctx.account_risk.heat_blocked(acct):
                        logger.warning(
                            "EVENT-DRIVEN ENTRY BLOCKED | {} — AccountRisk: account {} "
                            "heat blocked", symbol, acct,
                        )
                        return
                except Exception as exc:
                    logger.debug("[entry-risk] AccountRisk check failed: {}", exc)

            # ── Gate 5: Correlation / exposure ───────────────────────
            if ctx is not None and ctx.correlation_engine is not None:
                try:
                    from brain.correlation_engine import OpenTrade
                    corr_trades = []
                    for pos in open_positions:
                        corr_trades.append(OpenTrade(
                            pair=getattr(pos, "symbol", ""),
                            direction=getattr(pos, "direction", "LONG"),
                            risk_pct=0.02,
                        ))
                    approved, reason = ctx.correlation_engine.can_open_trade(
                        pair=symbol,
                        direction=direction,
                        existing_trades=corr_trades,
                    )
                    if not approved:
                        logger.warning(
                            "EVENT-DRIVEN ENTRY BLOCKED | {} — Correlation: {}",
                            symbol, reason,
                        )
                        return
                except Exception as exc:
                    logger.debug("[entry-risk] Correlation check failed: {}", exc)
            else:
                # Fallback: simple currency-count check
                max_open = self._config.risk.max_open_trades
                max_corr = self._config.risk.max_correlated_trades
                if len(open_positions) >= max_open:
                    logger.warning(
                        "EVENT-DRIVEN ENTRY SKIPPED | {} — max open trades {}/{}",
                        symbol, len(open_positions), max_open,
                    )
                    return
                currency_counts: dict[str, int] = defaultdict(int)
                for pos in open_positions:
                    psym = getattr(pos, "symbol", "")
                    for ccy in ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"):
                        if ccy in psym:
                            currency_counts[ccy] += 1
                for ccy in ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"):
                    if ccy in symbol and currency_counts.get(ccy, 0) >= max_corr:
                        logger.warning(
                            "EVENT-DRIVEN ENTRY SKIPPED | {} — {} exposure {}/{} (max correlated)",
                            symbol, ccy, currency_counts[ccy] + 1, max_corr,
                        )
                        return

            # ── Gate 6: DecisionEngine — strategic conviction scoring ─
            de_size_mult = 1.0
            de_conviction = 0.0
            if ctx is not None and ctx.decision_engine is not None and ctx.situation_engine is not None:
                try:
                    from decision.context import EntryContext as DEContext
                    wm = self._wm_store.get(symbol)
                    structure = getattr(wm, "structure", {}) if wm else {}
                    d1_s = structure.get("D1", {})
                    h4_s = structure.get("H4", {})
                    h1_s = structure.get("H1", {})

                    entry_ctx = DEContext(
                        symbol=symbol,
                        direction="LONG" if direction.upper() in ("BUY", "LONG") else "SHORT",
                        scan_score=conviction,
                        entry_type=decision.get("zone_type", ""),
                        entry_price=entry_price,
                        stop_loss=sl,
                        tp1=tp1,
                        tp2=tp2,
                        risk_reward_2=abs(tp2 - entry_price) / max(abs(entry_price - sl), 1e-8) if sl else 0.0,
                        risk_pips=abs(entry_price - sl) / self._safe_pip_size(symbol) if sl else 0.0,
                        d1_trend=d1_s.get("trend", "UNKNOWN"),
                        d1_confidence=d1_s.get("confidence", 0.0),
                        h4_trend=h4_s.get("trend", "UNKNOWN"),
                        h4_confidence=h4_s.get("confidence", 0.0),
                        h1_trend=h1_s.get("trend", "UNKNOWN"),
                        h1_confidence=h1_s.get("confidence", 0.0),
                        m1_aligned_count=decision.get("m1_aligned", 3),
                        m1_event=decision.get("m1_event", ""),
                        open_trade_count=len(open_positions),
                        max_open_trades=self._config.risk.max_open_trades,
                    )
                    sa = ctx.situation_engine.assess_entry(entry_ctx)
                    de_result = ctx.decision_engine.decide_entry(entry_ctx, sa)

                    if not de_result.should_enter:
                        logger.info(
                            "EVENT-DRIVEN ENTRY SKIPPED | {} — DecisionEngine: {}",
                            symbol, de_result.reason[:200],
                        )
                        if ctx.decision_journal is not None:
                            try:
                                ctx.decision_journal.log_entry(entry_ctx, sa, de_result)
                            except Exception:
                                pass
                        self._record_shadow_rejection(
                            symbol, direction, entry_price, sl, tp1,
                            "decision_engine", conviction,
                        )
                        return

                    de_size_mult = de_result.size_multiplier
                    de_conviction = de_result.conviction
                    logger.info(
                        "[DE] {} {} ENTER — conviction={:.2f} size×{:.2f} | {}",
                        symbol, direction, de_conviction, de_size_mult,
                        de_result.reason[:120],
                    )

                    # Gate 6b: RiskGovernor — graded review
                    if ctx.risk_governor is not None:
                        gov_result = ctx.risk_governor.review_entry(de_result, entry_ctx, sa)
                        if not gov_result.should_enter:
                            logger.info(
                                "EVENT-DRIVEN ENTRY VETOED | {} — RiskGovernor: {}",
                                symbol, gov_result.reason[:200],
                            )
                            if ctx.decision_journal is not None:
                                try:
                                    ctx.decision_journal.log_entry(
                                        entry_ctx, sa, gov_result, governor_changed=True,
                                    )
                                except Exception:
                                    pass
                            self._record_shadow_rejection(
                                symbol, direction, entry_price, sl, tp1,
                                "risk_governor", conviction,
                            )
                            return
                        if gov_result is not de_result:
                            de_size_mult = gov_result.size_multiplier
                            risk_mult = getattr(gov_result, "risk_multiplier", 1.0)
                            if risk_mult < 1.0:
                                de_size_mult = round(de_size_mult * risk_mult, 3)
                            logger.info(
                                "[GOVERNOR] {} {} — risk×{:.2f} final_size×{:.2f}",
                                symbol, direction, risk_mult, de_size_mult,
                            )

                    if ctx.decision_journal is not None:
                        try:
                            ctx.decision_journal.log_entry(
                                entry_ctx, sa, gov_result if ctx.risk_governor else de_result,
                                governor_changed=(ctx.risk_governor is not None and gov_result is not de_result),
                            )
                        except Exception:
                            pass

                except Exception as exc:
                    logger.debug("[entry-decision] DecisionEngine check failed: {}", exc)

            # ── ATR-based SL/TP from EntryEngine ─────────────────────
            atr_sl = sl
            atr_tp1 = tp1
            atr_tp2 = tp2
            if ctx is not None and ctx.entry_engine is not None:
                try:
                    pip_size_ee = self._safe_pip_size(symbol)
                    norm_dir = "LONG" if direction.upper() in ("BUY", "LONG") else "SHORT"
                    m5_df = self._fetch_candles(symbol, "M5", 200)
                    h1_df = self._fetch_candles(symbol, "H1", 200)
                    if m5_df is not None and len(m5_df) >= 10:
                        try:
                            atr_sl_calc = ctx.entry_engine.calculate_stop_loss(
                                direction=norm_dir,
                                zone={"entry": entry_price, "top": entry_price, "bottom": entry_price},
                                pip_size=pip_size_ee,
                                buffer_pips=getattr(self._config.risk, "sl_buffer_pips", 2.0),
                                entry_price=entry_price,
                                pair=symbol,
                                m5_df=m5_df,
                            )
                            if atr_sl_calc and atr_sl_calc > 0:
                                if norm_dir == "LONG":
                                    atr_sl = min(sl, atr_sl_calc) if sl > 0 else atr_sl_calc
                                else:
                                    atr_sl = max(sl, atr_sl_calc) if sl > 0 else atr_sl_calc
                        except Exception as exc:
                            logger.debug("[atr-sl] ATR SL calc failed: {}", exc)

                        if h1_df is not None and len(h1_df) >= 5:
                            try:
                                final_sl = atr_sl if atr_sl > 0 else sl
                                atr_tp1_calc, atr_tp2_calc = ctx.entry_engine.calculate_targets(
                                    direction=norm_dir,
                                    entry_price=entry_price,
                                    stop_loss=final_sl,
                                    h1_df=h1_df,
                                    pip_size=pip_size_ee,
                                    tp1_rr=getattr(self._config.risk, "tp1_rr", 1.5),
                                    tp2_rr=getattr(self._config.risk, "tp2_rr", 3.0),
                                    pair=symbol,
                                )
                                if atr_tp1_calc and atr_tp1_calc > 0:
                                    if norm_dir == "LONG":
                                        atr_tp1 = max(tp1, atr_tp1_calc) if tp1 > 0 else atr_tp1_calc
                                        atr_tp2 = max(tp2, atr_tp2_calc) if tp2 > 0 else atr_tp2_calc
                                    else:
                                        atr_tp1 = min(tp1, atr_tp1_calc) if tp1 > 0 else atr_tp1_calc
                                        atr_tp2 = min(tp2, atr_tp2_calc) if tp2 > 0 else atr_tp2_calc
                            except Exception as exc:
                                logger.debug("[atr-tp] ATR target calc failed: {}", exc)

                    sl_changed = abs(atr_sl - sl) > 1e-8 if sl > 0 else False
                    tp_changed = abs(atr_tp1 - tp1) > 1e-8 if tp1 > 0 else False
                    if sl_changed or tp_changed:
                        logger.info(
                            "[ATR] {} SL: {:.5f}→{:.5f} TP1: {:.5f}→{:.5f} (tighter wins)",
                            symbol, sl, atr_sl, tp1, atr_tp1,
                        )
                    sl = atr_sl
                    tp1 = atr_tp1
                    tp2 = atr_tp2
                except Exception as exc:
                    logger.debug("[atr-levels] EntryEngine ATR calc failed: {}", exc)

            # ── Orchestrator round table — graded sizing ─────────────
            orch_mult = 1.0
            if ctx is not None and ctx.orchestrator is not None:
                try:
                    from brain.orchestrator import TradeProposal
                    wm = self._wm_store.get(symbol)
                    structure = getattr(wm, "structure", {}) if wm else {}
                    h4_s = structure.get("H4", {})

                    proposal = TradeProposal(
                        pair=symbol,
                        direction="LONG" if direction.upper() in ("BUY", "LONG") else "SHORT",
                        scan_score=float(conviction),
                        de_conviction=de_conviction if de_conviction > 0 else None,
                        de_margin=None,
                        tf_alignment=h4_s.get("confidence", None),
                    )
                    verdict = ctx.orchestrator.evaluate(proposal)
                    if verdict.vetoed:
                        logger.warning(
                            "EVENT-DRIVEN ENTRY VETOED | {} — Orchestrator: {}",
                            symbol, verdict.veto_reason,
                        )
                        return
                    orch_mult = verdict.size_multiplier
                    if orch_mult < 1.0:
                        logger.info(
                            "[ORCH] {} size×{:.2f} — {}",
                            symbol, orch_mult, verdict.summary()[:120],
                        )
                except Exception as exc:
                    logger.debug("[orchestrator] Orchestrator eval failed: {}", exc)

            # ── Volatility + density sizing adjustments ──────────────
            vol_mult = 1.0
            density_mult = 1.0
            if ctx is not None and ctx.system_volatility_monitor is not None:
                try:
                    vol_mult = ctx.system_volatility_monitor.get_size_multiplier()
                except Exception:
                    pass
            if ctx is not None and ctx.opportunity_density_tracker is not None:
                try:
                    density_mult = ctx.opportunity_density_tracker.get_size_multiplier()
                except Exception:
                    pass

            # ── Execution quality multiplier ─────────────────────────
            exec_mult = 1.0
            if ctx is not None and ctx.execution_monitor is not None:
                try:
                    exec_mult = ctx.execution_monitor.get_size_multiplier(symbol)
                except Exception:
                    pass

            # ── Capital allocation multiplier (L5.5a) ────────────────
            cap_mult = 1.0
            if ctx is not None and ctx.capital_allocator is not None:
                try:
                    from adaptive.capital_allocator import compute_fingerprint
                    wm = self._wm_store.get(symbol)
                    regime_str = ""
                    if ctx.regime_detector is not None:
                        try:
                            rs = ctx.regime_detector.get_regime(symbol)
                            regime_str = getattr(rs, "label", "")
                        except Exception:
                            pass
                    fp = compute_fingerprint(
                        pair=symbol,
                        direction="LONG" if direction.upper() in ("BUY", "LONG") else "SHORT",
                        regime=regime_str,
                        horizon="SWING",
                    )
                    cap_mult = ctx.capital_allocator.get_sizing_multiplier(fp)
                except Exception as exc:
                    logger.debug("[cap-alloc] sizing multiplier failed: {}", exc)

            # ── Execution profile selection (L5.5b) ──────────────────
            exec_profile = None
            if ctx is not None and ctx.execution_profiles is not None:
                try:
                    regime_str = ""
                    if ctx.regime_detector is not None:
                        try:
                            rs = ctx.regime_detector.get_regime(symbol)
                            regime_str = getattr(rs, "label", "")
                        except Exception:
                            pass
                    exec_profile = ctx.execution_profiles.select_profile(
                        horizon="SWING",
                        regime=regime_str,
                        consensus_strength=float(conviction) / 100.0 if conviction else 0.5,
                    )
                except Exception as exc:
                    logger.debug("[exec-prof] profile selection failed: {}", exc)

            # ── Position sizing ──────────────────────────────────────
            risk_pct = self._config.risk.risk_per_trade_pct / 100.0
            if ctx is not None and ctx.drawdown_guard is not None:
                try:
                    risk_map = getattr(ctx.drawdown_guard, "risk_map", None)
                    if risk_map:
                        dd_status = ctx.drawdown_guard.get_status()
                        mode_key = dd_status.mode if isinstance(dd_status.mode, str) else str(dd_status.mode)
                        mapped = risk_map.get(mode_key)
                        if mapped is not None and mapped < risk_pct:
                            risk_pct = mapped
                except Exception:
                    pass
            pip_size = self._safe_pip_size(symbol)
            pctx = build_context_for_symbol(symbol)

            info = INSTRUMENT_REGISTRY.get(symbol)
            pip_value = info.pip_value_per_lot if info else 10.0

            sizer = PositionSizer()
            size_result = sizer.calculate(
                account_balance=balance,
                risk_pct=risk_pct,
                entry_price=entry_price,
                stop_loss=sl,
                pip_size=pip_size,
                pip_value_per_lot=pip_value,
                context=pctx,
                symbol=symbol,
            )

            combined_mult = de_size_mult * orch_mult * vol_mult * density_mult * exec_mult * cap_mult
            combined_mult = max(0.15, min(2.0, combined_mult))
            if abs(combined_mult - 1.0) > 1e-6:
                if size_result.lots > 0:
                    size_result.lots = round(max(0.01, size_result.lots * combined_mult), 2)
                if size_result.stake_usd > 0:
                    size_result.stake_usd = round(max(0.35, size_result.stake_usd * combined_mult), 2)
                logger.info(
                    "[SIZING] {} final×{:.2f} (DE×{:.2f} ORCH×{:.2f} VOL×{:.2f} DEN×{:.2f} EXEC×{:.2f} CAP×{:.2f}) → {:.2f} lots / ${:.2f} stake",
                    symbol, combined_mult, de_size_mult, orch_mult, vol_mult,
                    density_mult, exec_mult, cap_mult, size_result.lots, size_result.stake_usd,
                )

            if size_result.lots <= 0 and size_result.stake_usd <= 0:
                logger.warning(
                    "EVENT-DRIVEN ENTRY SKIPPED | {} — position size is zero (sizing_mode={})",
                    symbol, size_result.sizing_mode,
                )
                return

            # ── Execute order ────────────────────────────────────────
            order_ts = _time.time()

            # Domain event: ORDER_SENT
            try:
                es = get_event_store()
                es.emit(
                    DE.ORDER_SENT, "INFO",
                    symbol=symbol,
                    source_module="event_driven",
                    payload={
                        "symbol": symbol,
                        "direction": direction,
                        "lots": float(size_result.lots),
                        "stake_usd": float(size_result.stake_usd),
                        "entry_price": float(entry_price),
                        "sl": float(sl),
                        "tp": float(tp1),
                        "combined_mult": round(combined_mult, 3),
                    },
                )
            except Exception:
                pass

            result = self._pm.execute_entry(
                symbol=symbol,
                direction=direction,
                lots=size_result.lots,
                sl=sl,
                tp=tp1,
                stake_usd=size_result.stake_usd if pctx.uses_stake else None,
                comment=f"ED|{conviction}|O{orch_mult:.2f}",
            )

            if result.success:
                logger.info(
                    "EVENT-DRIVEN ORDER PLACED | {} {} {:.2f} lots ticket={}",
                    symbol, direction, result.lots, result.order_id,
                )
                self._on_order_filled(symbol, direction, result, balance,
                                      entry_price, order_ts)

                # OutcomeLogger — record the plan at entry time
                if ctx is not None and ctx.outcome_logger is not None:
                    try:
                        plan_data = {
                            "plan_id": str(result.order_id),
                            "symbol": symbol,
                            "direction": direction,
                            "entry_price": entry_price,
                            "sl": sl,
                            "tp1": tp1,
                            "tp2": tp2,
                            "conviction": conviction,
                            "combined_mult": round(combined_mult, 3),
                            "profile": getattr(exec_profile, "name", "default") if exec_profile else "default",
                        }
                        ctx.outcome_logger.log_plan(plan_data, plan_data)
                    except Exception as exc:
                        logger.debug("[post-fill] OutcomeLogger plan failed: {}", exc)
            else:
                err = getattr(result, "error", "unknown")
                logger.warning(
                    "EVENT-DRIVEN ORDER FAILED | {} {} | {}", symbol, direction, err,
                )
        except Exception as exc:
            logger.error(
                "EVENT-DRIVEN ORDER ERROR | {} {} | {}", symbol, direction, exc,
            )

    def _record_shadow_rejection(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl: float,
        tp: float,
        rejection_gate: str,
        conviction: float = 0.0,
    ) -> None:
        """Record a rejected setup for counterfactual shadow tracking."""
        ctx = self._ctx
        if ctx is None:
            return
        if ctx.shadow_store is not None:
            try:
                import uuid
                from persistence.shadow_store import ShadowContract
                contract = ShadowContract(
                    contract_id=str(uuid.uuid4()),
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=sl,
                    tp1=tp,
                    tp2=0.0,
                    pip_size=self._safe_pip_size(symbol),
                    rejecting_gate=rejection_gate,
                    ts_utc_ms=int(_time.time() * 1000),
                    score=int(conviction),
                    source="event_driven",
                )
                ctx.shadow_store.insert_contract(contract)
            except Exception as exc:
                logger.debug("[shadow] rejection record failed: {}", exc)

    # ── Trade close feedback chain ───────────────────────────────────

    def _on_order_filled(
        self,
        symbol: str,
        direction: str,
        result: Any,
        balance: float,
        expected_price: float = 0.0,
        order_ts: float = 0.0,
    ) -> None:
        """Post-fill bookkeeping: risk balance + execution quality + learning attribution."""
        ctx = self._ctx
        if ctx is None:
            return

        order_id = str(getattr(result, "order_id", getattr(result, "ticket", "")) or "")

        try:
            if ctx.account_risk is not None and balance and balance > 0:
                acct = ctx.account_key(symbol, self._pm)
                ctx.account_risk.update_balance(acct, balance)
        except Exception as exc:
            logger.debug("[post-fill] account risk update failed: {}", exc)

        if ctx.execution_monitor is not None and expected_price > 0:
            try:
                fill_price = getattr(result, "fill_price", getattr(result, "entry_price", 0.0)) or 0.0
                if fill_price <= 0:
                    fill_price = expected_price
                now_dt = datetime.now(timezone.utc)
                from datetime import timedelta
                latency_s = _time.time() - order_ts if order_ts > 0 else 0.0
                signal_dt = now_dt - timedelta(seconds=latency_s)
                spread = self._get_spread_pips(symbol) * self._safe_pip_size(symbol)
                ctx.execution_monitor.record_execution(
                    requested_price=expected_price,
                    filled_price=fill_price,
                    signal_timestamp=signal_dt,
                    fill_timestamp=now_dt,
                    spread=spread,
                    pip_size=self._safe_pip_size(symbol),
                    symbol=symbol,
                )
                logger.debug(
                    "[exec-mon] {} fill recorded: expected={:.5f} filled={:.5f} latency={:.1f}s",
                    symbol, expected_price, fill_price, latency_s,
                )
            except Exception as exc:
                logger.debug("[post-fill] ExecutionMonitor record failed: {}", exc)

        # ── Entry-time learning attribution (Phase 4) ────────────────

        # OutcomeFeedback — record entry attribution (which modules drove this)
        if ctx.outcome_feedback is not None and order_id:
            try:
                wm = self._wm_store.get(symbol)
                attribution = {
                    "pair": symbol,
                    "direction": direction,
                    "entry_price": expected_price,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "world_model_version": getattr(wm, "version", 0) if wm else 0,
                }
                ctx.outcome_feedback.record_entry(str(order_id), attribution)
            except Exception as exc:
                logger.debug("[post-fill] OutcomeFeedback entry record failed: {}", exc)

        # CounterfactualEngine — snapshot vote panel at open for leave-one-out replay
        if ctx.counterfactual_engine is not None and order_id:
            try:
                from adaptive.counterfactual import TradeAttribution
                ta = TradeAttribution(
                    trade_id=str(order_id),
                    pair=symbol,
                    direction=direction,
                    timestamp_open=_time.time(),
                )
                ctx.counterfactual_engine.record_open(ta)
            except Exception as exc:
                logger.debug("[post-fill] CounterfactualEngine open record failed: {}", exc)

        # SignalLedger — record trade opened for pair
        if ctx.signal_ledger is not None and order_id:
            try:
                ctx.signal_ledger.record_trade_opened_for_pair(
                    symbol, str(order_id), direction,
                )
            except Exception as exc:
                logger.debug("[post-fill] SignalLedger trade-open failed: {}", exc)

        # ── Domain event: TRADE_OPEN ─────────────────────────────────
        try:
            es = get_event_store()
            fill_price = getattr(result, "fill_price", getattr(result, "entry_price", expected_price)) or expected_price
            es.emit(
                DE.TRADE_OPEN, "INFO",
                symbol=symbol,
                source_module="event_driven",
                payload={
                    "order_id": order_id,
                    "symbol": symbol,
                    "direction": direction,
                    "entry_price": float(fill_price),
                    "lots": float(getattr(result, "lots", 0.0) or 0.0),
                    "sl": float(getattr(result, "sl", 0.0) or 0.0),
                    "tp": float(getattr(result, "tp", 0.0) or 0.0),
                    "balance": float(balance or 0.0),
                },
            )
        except Exception as exc:
            logger.debug("[post-fill] TRADE_OPEN event emit failed: {}", exc)

    def _on_trade_closed(
        self,
        symbol: str,
        direction: str,
        pnl_dollars: float,
        pnl_pips: float,
        ticket: str,
    ) -> None:
        """Feed closed-trade P&L into all risk + learning subsystems.

        Mirrors TradingLoop._record_closed_trade — risk first, then learning.
        """
        ctx = self._ctx
        if ctx is None:
            return

        balance = self._pm.get_platform_balance(symbol)

        # ── RISK LAYER (Phase 1) ────────────────────────────────────

        # DrawdownGuard — register P&L as fraction of balance
        if ctx.drawdown_guard is not None:
            try:
                pnl_pct = pnl_dollars / balance if balance and balance > 0 else 0.0
                ctx.drawdown_guard.register_trade_result(pnl_pct)
            except Exception as exc:
                logger.debug("[close-risk] DrawdownGuard update failed: {}", exc)

        # RiskEngine — update internal balance + PnL tracker
        if ctx.risk_engine is not None:
            try:
                ctx.risk_engine.record_trade_result(
                    pnl_dollars=pnl_dollars,
                    pnl_pips=pnl_pips,
                    pair=symbol,
                    direction=direction,
                )
            except Exception as exc:
                logger.debug("[close-risk] RiskEngine update failed: {}", exc)

        # PortfolioGovernor — fold daily P&L
        if ctx.portfolio_governor is not None:
            try:
                if balance and balance > 0:
                    ctx.portfolio_governor.set_reference_balance(balance)
                ctx.portfolio_governor.update_daily_pnl(pnl_dollars)
            except Exception as exc:
                logger.debug("[close-risk] Governor daily PnL update failed: {}", exc)

        # AccountRiskManager — per-account silo
        if ctx.account_risk is not None:
            try:
                acct = ctx.account_key(symbol, self._pm)
                if balance and balance > 0:
                    ctx.account_risk.update_balance(acct, balance)
                ctx.account_risk.register_realized(acct, pnl_dollars)
            except Exception as exc:
                logger.debug("[close-risk] AccountRisk update failed: {}", exc)

        # ── LEARNING LAYER (Phase 4) ────────────────────────────────

        pnl_r = 0.0
        outcome = "WIN" if pnl_dollars > 0 else "LOSS"
        cause_value = "event_driven_close"

        # OutcomeFeedback — link realised R to entry attribution
        if ctx.outcome_feedback is not None:
            try:
                payload = {
                    "pair": symbol,
                    "direction": direction,
                    "pnl_r": round(pnl_r, 4),
                    "pnl_pips": round(float(pnl_pips), 2),
                    "pnl_dollars": round(float(pnl_dollars), 2),
                    "won": float(pnl_dollars) > 0,
                    "outcome": outcome,
                    "exit_cause": cause_value,
                }
                ctx.outcome_feedback.record_outcome(str(ticket), payload)
            except Exception as exc:
                logger.debug("[close-learn] OutcomeFeedback failed: {}", exc)

        # CounterfactualEngine — complete decision snapshot
        if ctx.counterfactual_engine is not None:
            try:
                ctx.counterfactual_engine.complete(str(ticket), {
                    "pnl_r": round(pnl_r, 4),
                    "won": bool(pnl_dollars > 0),
                    "outcome": outcome,
                    "exit_cause": cause_value,
                })
            except Exception as exc:
                logger.debug("[close-learn] CounterfactualEngine failed: {}", exc)

        # SignalLedger — attach outcome to driving signals
        if ctx.signal_ledger is not None:
            try:
                ctx.signal_ledger.attach_trade_outcome(str(ticket), {
                    "pnl_pips": round(float(pnl_pips), 2),
                    "pnl_dollars": round(float(pnl_dollars), 2),
                    "won": pnl_dollars > 0,
                    "outcome": outcome,
                })
            except Exception as exc:
                logger.debug("[close-learn] SignalLedger attach failed: {}", exc)

        # ML Adapter — register new trade
        if ctx.ml_adapter is not None:
            try:
                ctx.ml_adapter.register_new_trade(exit_cause=cause_value)
            except Exception as exc:
                logger.debug("[close-learn] MLAdapter register failed: {}", exc)

        # TunerAgent — route trade-close tuning
        if ctx.tuner_agent is not None:
            try:
                from adaptive.tunable import TuneContext
                tune_ctx = TuneContext(
                    total_trades=0,
                    trades_since_last_tune=1,
                    seconds_since_last_tune=0.0,
                )
                ctx.tuner_agent.on_trade_close(tune_ctx)
            except Exception as exc:
                logger.debug("[close-learn] TunerAgent failed: {}", exc)

        # PostCloseTracker — schedule forward MFE/MAE checks
        if ctx.post_close_tracker is not None:
            try:
                now_dt = datetime.now(timezone.utc)
                tick = self._tick_store.get_latest(symbol)
                close_price = tick.mid if tick else 0.0
                ctx.post_close_tracker.record_close(
                    trade_id=str(ticket),
                    pair=symbol,
                    direction=direction,
                    entry_price=0.0,
                    exit_price=close_price,
                    exit_cause=cause_value,
                    sl_price=0.0,
                    tp_price=0.0,
                    entry_timestamp=now_dt,
                    exit_timestamp=now_dt,
                    entry_score=0,
                    entry_confluences="",
                )
            except Exception as exc:
                logger.debug("[close-learn] PostCloseTracker failed: {}", exc)

        # ── Domain event: TRADE_CLOSE ────────────────────────────────
        try:
            es = get_event_store()
            es.emit(
                DE.TRADE_CLOSE, "INFO",
                symbol=symbol,
                source_module="event_driven",
                payload={
                    "order_id": str(ticket),
                    "symbol": symbol,
                    "direction": direction,
                    "pnl_dollars": round(float(pnl_dollars), 2),
                    "pnl_pips": round(float(pnl_pips), 2),
                    "outcome": outcome,
                    "exit_reason": cause_value,
                    "balance": float(balance or 0.0),
                },
            )
        except Exception as exc:
            logger.debug("[close-event] TRADE_CLOSE event emit failed: {}", exc)

        # ── TradeJournal — record closed trade for dashboard history ──
        if ctx.trade_journal is not None:
            try:
                import asyncio
                from brain.trade_journal import TradeRecord
                tick = self._tick_store.get_latest(symbol)
                close_price = tick.mid if tick else 0.0
                record = TradeRecord(
                    pair=symbol,
                    direction=direction,
                    entry=0.0,
                    exit=close_price,
                    pnl=round(float(pnl_pips), 2),
                    score=0,
                    confluences="",
                    regime="",
                    session="",
                    spread=0.0,
                    slippage=0.0,
                    entry_type="event_driven",
                    time_to_tp1=0.0,
                    time_to_exit=0.0,
                    outcome=outcome,
                    pnl_dollars=round(float(pnl_dollars), 2),
                    exit_cause=cause_value,
                )
                try:
                    _loop = asyncio.new_event_loop()
                    _loop.run_until_complete(ctx.trade_journal.log_trade(record))
                    _loop.close()
                except Exception as exc_j:
                    logger.debug("[close-journal] async log failed: {}", exc_j)
            except Exception as exc:
                logger.debug("[close-journal] TradeJournal write failed: {}", exc)

        # ── EVOLUTION ENGINES (Phase 6) ──────────────────────────────

        # CapitalAllocator — record outcome for fingerprint-based capital
        if ctx.capital_allocator is not None:
            try:
                from adaptive.capital_allocator import compute_fingerprint
                regime_str = ""
                if ctx.regime_detector is not None:
                    try:
                        rs = ctx.regime_detector.get_regime(symbol)
                        regime_str = getattr(rs, "label", "")
                    except Exception:
                        pass
                fp = compute_fingerprint(
                    pair=symbol,
                    direction=direction,
                    regime=regime_str,
                    horizon="SWING",
                )
                risk_pips_est = abs(pnl_pips) if pnl_pips != 0 else 1.0
                r_multiple = pnl_pips / risk_pips_est if risk_pips_est > 0 else 0.0
                ctx.capital_allocator.record_outcome(fp, r_multiple)
            except Exception as exc:
                logger.debug("[close-evo] CapitalAllocator record failed: {}", exc)

        # ExecutionProfileManager — record outcome for the profile used
        if ctx.execution_profiles is not None:
            try:
                ctx.execution_profiles.record_outcome("standard_swing", pnl_pips / 10.0 if pnl_pips else 0.0)
            except Exception as exc:
                logger.debug("[close-evo] ExecutionProfileManager record failed: {}", exc)

        # BehaviorDiscoveryEngine — record trade features for clustering
        if ctx.behavior_discovery is not None:
            try:
                features = {
                    "pair": symbol,
                    "direction": direction,
                    "pnl_pips": float(pnl_pips),
                    "pnl_dollars": float(pnl_dollars),
                    "exit_cause": cause_value,
                }
                r_est = pnl_pips / 10.0 if pnl_pips else 0.0
                ctx.behavior_discovery.record_trade(features, r_est, trade_id=str(ticket))
            except Exception as exc:
                logger.debug("[close-evo] BehaviorDiscovery record failed: {}", exc)

        # OutcomeLogger — link plan → outcome
        if ctx.outcome_logger is not None:
            try:
                ctx.outcome_logger.log_outcome(str(ticket), {
                    "pnl_pips": round(float(pnl_pips), 2),
                    "pnl_dollars": round(float(pnl_dollars), 2),
                    "outcome": outcome,
                    "exit_cause": cause_value,
                })
            except Exception as exc:
                logger.debug("[close-evo] OutcomeLogger outcome failed: {}", exc)

        # Calibrator — auto-calibrate planner thresholds
        if ctx.calibrator is not None and ctx.outcome_logger is not None:
            try:
                completed = ctx.outcome_logger.completed_count()
                if ctx.calibrator.should_calibrate(completed):
                    trades = ctx.outcome_logger.get_completed_trades()
                    new_config = ctx.calibrator.calibrate(trades)
                    if ctx.trade_planner is not None and new_config is not None:
                        ctx.trade_planner.update_config(new_config)
                        logger.info("[calibrator] planner config auto-calibrated from {} completed trades", completed)
            except Exception as exc:
                logger.debug("[close-evo] Calibrator run failed: {}", exc)

        # ReEntryManager — evaluate re-entry opportunity after close
        if ctx.re_entry_manager is not None:
            try:
                wm = self._wm_store.get(symbol)
                if wm is not None:
                    re_entry_signal = ctx.re_entry_manager.evaluate(
                        symbol=symbol,
                        direction=direction,
                        pnl_pips=pnl_pips,
                        exit_cause=cause_value,
                    )
                    if re_entry_signal and getattr(re_entry_signal, "should_reenter", False):
                        logger.info(
                            "[re-entry] {} {} re-entry signal after {} (score={})",
                            symbol, direction, cause_value,
                            getattr(re_entry_signal, "score", 0),
                        )
            except Exception as exc:
                logger.debug("[close-evo] ReEntryManager eval failed: {}", exc)

        logger.info(
            "EVENT-DRIVEN CLOSE FEEDBACK | {} {} ticket={} pnl=${:.2f} ({:.1f}pip) | risk+learning+evolution",
            direction, symbol, ticket, pnl_dollars, pnl_pips,
        )


def _extract_currencies(symbol: str) -> list[str]:
    """Extract currency legs from a forex symbol (e.g. EURJPY → [EUR, JPY])."""
    sym = str(symbol).upper().strip()
    if len(sym) == 6:
        base, quote = sym[:3], sym[3:]
        if base in _KNOWN_CURRENCIES and quote in _KNOWN_CURRENCIES:
            return [base, quote]
    return []
