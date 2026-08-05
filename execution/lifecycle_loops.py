"""APEX TRADER — Execution lifecycle loops (extracted from the bootstrap).

Phase K (Constitution Part XI — modular design): the flush loop (drains the
IntentAggregator and executes via the ActionExecutor) and the tick-eval loop
(runs the PositionEvaluator each cycle) were defined inline in the 11k-line
``event_driven_bootstrap.py``. Lifting them into a named module is part of the
behaviour-preserving decomposition. The bootstrap re-imports these names, so
every construction site (and the existing
``from event_driven_bootstrap import FlushLoop`` / ``TickEvalLoop`` test import
paths) resolves identically — behaviour unchanged.
"""

from __future__ import annotations

import threading
import time as _time
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from execution.intents import Intent, IntentType
from persistence import domain_events as DE
from persistence.event_store import get_event_store

if TYPE_CHECKING:  # type hints only — not needed (and not imported) at runtime
    from execution.action_executor import ActionExecutor
    from execution.intent_aggregator import IntentAggregator
    from execution.management_scheduler import ManagementScheduler
    from event_driven_bootstrap import PositionEvaluator


class FlushLoop:
    """Periodically drains the IntentAggregator and executes via ActionExecutor."""

    def __init__(
        self,
        aggregator: IntentAggregator,
        executor: ActionExecutor,
        platform_manager: PlatformManager,
        evaluator: Optional[PositionEvaluator] = None,
        on_close_callback: Optional[Any] = None,
        on_manage_callback: Optional[Any] = None,
        interval: float = 0.1,
    ) -> None:
        self._aggregator = aggregator
        self._executor = executor
        self._pm = platform_manager
        self._evaluator = evaluator
        self._on_close = on_close_callback
        self._on_manage = on_manage_callback
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
            intents: list[Intent] = []
            try:
                intents = self._aggregator.flush()
                if intents:
                    open_positions = self._build_position_map()
                    results = self._executor.execute_batch(
                        intents, open_positions,
                    )
                    for i, r in enumerate(results):
                        intent = intents[i] if i < len(intents) else None
                        if r.success:
                            self._intents_executed += 1
                            if intent is not None and intent.intent_type == IntentType.CLOSE:
                                # Stop the evaluator from re-emitting another
                                # CLOSE for this ticket while the local position
                                # book still shows it open (it refreshes on the
                                # next reconcile cycle).  Without this the same
                                # position is closed once at the broker and then
                                # a second CLOSE races in ~3s later and is
                                # rejected as "no longer open".
                                if self._evaluator is not None:
                                    tkt = intent.position_ticket
                                    if tkt:
                                        self._evaluator.suppress_ticket(tkt, 60.0)
                                if self._on_close is not None:
                                    try:
                                        self._on_close(intent, r)
                                    except Exception as exc:
                                        logger.debug("[flush-loop] close callback error: {}", exc)
                            elif intent is not None:
                                # SL/TP modify or partial close — commit the
                                # optimistic management state (callback) and
                                # surface the during-trade action on the dashboard.
                                if self._on_manage is not None:
                                    try:
                                        self._on_manage(intent, r)
                                    except Exception as exc:
                                        logger.debug("[flush-loop] manage callback error: {}", exc)
                                try:
                                    self._emit_modify_event(intent)
                                except Exception as exc:
                                    logger.debug("[flush-loop] modify event error: {}", exc)
                        else:
                            # Failed execution: roll back any optimistic
                            # management mutation so the action retries rather
                            # than leaving phantom state (breakeven/partial).
                            if (
                                intent is not None
                                and intent.intent_type != IntentType.OPEN
                                and intent.intent_type != IntentType.CLOSE
                                and self._on_manage is not None
                            ):
                                try:
                                    self._on_manage(intent, r)
                                except Exception as exc:
                                    logger.debug("[flush-loop] manage rollback error: {}", exc)
                            if self._evaluator:
                                err_msg = str(getattr(r, "error", "") or "").lower()
                                if "market closed" in err_msg or "market is closed" in err_msg or "market_closed" in err_msg:
                                    ticket = intent.position_ticket if intent is not None else ""
                                    if ticket:
                                        self._evaluator.suppress_ticket(ticket, 60.0)
                                elif "no longer open" in err_msg:
                                    # The position is already gone at the broker
                                    # but still in the local book — suppress so
                                    # the evaluator stops re-emitting closes for
                                    # it until the book refreshes next reconcile.
                                    ticket = intent.position_ticket if intent is not None else ""
                                    if ticket:
                                        self._evaluator.suppress_ticket(ticket, 60.0)
                self._flush_count += 1
            except Exception as exc:
                # The aggregator was already drained by flush(); if execution
                # raised, re-queue the intents so risk-reducing actions
                # (emergency close, breakeven moves) are retried next cycle
                # instead of being silently lost.
                if intents:
                    try:
                        self._aggregator.submit(intents)
                    except Exception as resubmit_exc:
                        logger.error(
                            "[flush-loop] re-queue failed, {} intents lost: {}",
                            len(intents), resubmit_exc,
                        )
                logger.warning(
                    "[flush-loop] error ({} intents re-queued): {}",
                    len(intents), exc,
                )
            _time.sleep(self._interval)

    def _emit_modify_event(self, intent: Intent) -> None:
        """Emit TRADE_MODIFIED for an executed SL/TP modify or partial close so
        the action surfaces on the dashboard activity feed (best-effort)."""
        store = get_event_store()
        if store is None:
            return
        store.emit(
            event_type=DE.TRADE_MODIFIED,
            severity="INFO",
            symbol=intent.symbol,
            parent_id=intent.position_ticket or None,
            source_module=intent.source or "execution",
            payload={
                "action": intent.intent_type.name,
                "order_id": intent.position_ticket,
                "symbol": intent.symbol,
                "new_sl": intent.new_sl,
                "new_tp": intent.new_tp,
                "close_fraction": intent.close_fraction,
                "source": intent.source,
                "reason": (intent.reason or "")[:200],
            },
        )

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
        scheduler: Optional[ManagementScheduler] = None,
    ) -> None:
        self._evaluator = evaluator
        self._interval = interval
        self._scheduler = scheduler
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._durations_ms: deque = deque(maxlen=500)
        self._slow_ticks: deque = deque(maxlen=20)
        self._lock = threading.Lock()
        self._slow_threshold_ms = 250.0

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
        self._evaluator.shutdown()
        logger.info("[tick-eval] stopped ({} evals)", self._evaluator.eval_count)

    def _loop(self) -> None:
        while self._running:
            t0 = _time.perf_counter()
            try:
                self._evaluator.evaluate_all(scheduler=self._scheduler)
            except Exception as exc:
                logger.warning("[tick-eval] error: {}", exc)
            finally:
                elapsed_ms = (_time.perf_counter() - t0) * 1000.0
                with self._lock:
                    self._durations_ms.append(elapsed_ms)
                    if elapsed_ms >= self._slow_threshold_ms:
                        self._slow_ticks.append({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "duration_ms": round(elapsed_ms, 2),
                        })
            _time.sleep(self._interval)

    def get_profile(self) -> dict[str, Any]:
        """Latency profile of the position-evaluation cycle (milliseconds)."""
        with self._lock:
            durations = list(self._durations_ms)
            slow = list(self._slow_ticks)
        if not durations:
            return {
                "samples": 0, "last_ms": 0.0, "avg_ms": 0.0,
                "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0, "slow_ticks": [],
            }
        ordered = sorted(durations)
        n = len(ordered)
        return {
            "samples": n,
            "last_ms": round(durations[-1], 3),
            "avg_ms": round(sum(ordered) / n, 3),
            "p50_ms": round(ordered[int(n * 0.50)], 3),
            "p95_ms": round(ordered[min(n - 1, int(n * 0.95))], 3),
            "max_ms": round(ordered[-1], 3),
            "slow_ticks": slow,
        }
