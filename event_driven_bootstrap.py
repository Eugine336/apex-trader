"""APEX TRADER — Event-Driven System Bootstrap (Phase 9).

Wires all Phase 1-7 components into a running event-driven system:

  Analysis Plane:    CandleClose events → brain modules → WorldModel
  Execution Plane:   Ticks → PositionWorkers → IntentAggregator → ActionExecutor → Broker
  Entry Plane:       WorldModel zones → tick detection → M1 confirm → gates → executor

Start:  ``system.start()``
Stop:   ``system.stop()``

Kill switch:  set env ``USE_EVENT_DRIVEN=true`` to activate.
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
from tick import EventBus, Tick, TickStore, CandleCloseDetector, TickRouter
from tick.models import CandleClose
from scanner.candle_close_handler import CandleCloseHandler
from execution.intents import Intent, IntentType
from execution.intent_aggregator import IntentAggregator, AggregatorConfig
from execution.action_executor import ActionExecutor, ExecutorConfig
from execution.risk_gate import GateConfig
from execution.position_worker import PositionWorker, WorkerConfig, ScanContext, MarketContext
from execution.position_snapshot import PositionSnapshot, build_position_snapshot
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

        while self._running:
            for sym in list(self._symbols):
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
        worker_config: Optional[WorkerConfig] = None,
        max_workers: int = 4,
    ) -> None:
        self._pm = platform_manager
        self._tick_store = tick_store
        self._wm_store = world_model_store
        self._aggregator = intent_aggregator
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
        for pos in positions:
            sym = getattr(pos, "symbol", "")
            if sym:
                by_symbol.setdefault(sym, []).append(pos)

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

            snap = build_position_snapshot(pos, current_price=price)
            intents = self._worker.evaluate(snap, now)

            if intents:
                self._aggregator.register_position(
                    ticket=order_id,
                    direction=direction,
                    current_sl=sl,
                    pip_size=pip_size,
                )
                self._aggregator.submit(intents)
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
        interval: float = 0.1,
    ) -> None:
        self._aggregator = aggregator
        self._executor = executor
        self._pm = platform_manager
        self._evaluator = evaluator
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
    ) -> None:
        self._config = config
        self._pm = platform_manager
        self._running = False

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
        self._evaluator = PositionEvaluator(
            platform_manager=self._pm,
            tick_store=self._tick_store,
            world_model_store=self._wm_store,
            intent_aggregator=self._aggregator,
        )

        # ── Entry plane ──────────────────────────────────────────────
        self._entry_orchestrator = EntryOrchestrator(
            world_model_store=self._wm_store,
            config=EntryConfig(),
            pip_size_lookup=self._safe_pip_size,
            on_entry_decision=self._on_entry_decision,
            is_instrument_known=lambda s: s in INSTRUMENT_REGISTRY,
            get_spread_pips=self._get_spread_pips,
            get_m1_dataframe=self._get_m1_dataframe,
        )

        # ── Background loops ─────────────────────────────────────────
        self._flush_loop = FlushLoop(
            self._aggregator, self._executor, self._pm,
            evaluator=self._evaluator,
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

        logger.info("[event-driven] system initialized")

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the event-driven system."""
        if self._running:
            return
        self._running = True

        logger.info("=" * 60)
        logger.info("  APEX TRADER — EVENT-DRIVEN MODE")
        logger.info("=" * 60)

        symbols = list(INSTRUMENT_REGISTRY.keys())
        for sym in symbols:
            self._candle_detector.register(sym)
        logger.info("[event-driven] registered {} symbols for candle detection", len(symbols))

        self._tick_router.start()
        self._mt5_poller.start()
        self._deriv_adapter.start()
        self._flush_loop.start()
        self._tick_eval_loop.start()

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

    def _on_entry_decision(self, decision: dict[str, Any]) -> None:
        """Handle entry decisions from EntryOrchestrator.

        Converts the decision dict into an order execution via PlatformManager.
        Handles MT5 lots-based and Deriv stake-based sizing.
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
            # ── Correlation / exposure check ─────────────────────────
            max_open = self._config.risk.max_open_trades
            max_corr = self._config.risk.max_correlated_trades
            try:
                open_positions = self._pm.get_all_open_positions()
            except Exception:
                open_positions = []

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

            # ── Position sizing ──────────────────────────────────────
            balance = self._pm.get_platform_balance(symbol)
            risk_pct = self._config.risk.risk_per_trade_pct / 100.0
            pip_size = self._safe_pip_size(symbol)
            ctx = build_context_for_symbol(symbol)

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
                context=ctx,
                symbol=symbol,
            )

            # ── Zero-size guard ──────────────────────────────────────
            if size_result.lots <= 0 and size_result.stake_usd <= 0:
                logger.warning(
                    "EVENT-DRIVEN ENTRY SKIPPED | {} — position size is zero (sizing_mode={})",
                    symbol, size_result.sizing_mode,
                )
                return

            # ── Execute order ────────────────────────────────────────
            result = self._pm.execute_entry(
                symbol=symbol,
                direction=direction,
                lots=size_result.lots,
                sl=sl,
                tp=tp1,
                stake_usd=size_result.stake_usd if ctx.uses_stake else None,
                comment=f"ED|{conviction}",
            )

            if result.success:
                logger.info(
                    "EVENT-DRIVEN ORDER PLACED | {} {} {:.2f} lots ticket={}",
                    symbol, direction, result.lots, result.order_id,
                )
            else:
                err = getattr(result, "error", "unknown")
                logger.warning(
                    "EVENT-DRIVEN ORDER FAILED | {} {} | {}", symbol, direction, err,
                )
        except Exception as exc:
            logger.error(
                "EVENT-DRIVEN ORDER ERROR | {} {} | {}", symbol, direction, exc,
            )


# ── Module-level helper ──────────────────────────────────────────────


def is_event_driven_enabled() -> bool:
    """Check if event-driven mode is enabled via environment variable."""
    return os.getenv("USE_EVENT_DRIVEN", "false").lower() in ("true", "1", "yes")
