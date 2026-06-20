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
from collections import defaultdict, deque
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
from execution.management_scheduler import ManagementScheduler
from entry import EntryOrchestrator, EntryConfig
from platform_context import build_context_for_symbol
from platforms.platform_manager import PlatformManager
from platforms.order_idempotency import build_order_comment, generate_idempotency_key
from risk.position_sizer import PositionSizer
from adaptive.zone_edge_tracker import ZoneEdgeTracker


def _struct_trend_conf(struct_by_tf: dict, tf: str) -> tuple[str, float]:
    """Read ``(trend, confidence)`` for a timeframe from a WorldModel's
    ``structure_by_tf()`` mapping of ``StructureAnalysis`` objects.

    Returns ``("UNKNOWN", 0.0)`` when the timeframe is absent.  Centralises
    the correct way to read the WorldModel's structure layer so consumers
    never treat it as a dict-of-dicts.
    """
    sa = struct_by_tf.get(tf)
    if sa is None:
        return "UNKNOWN", 0.0
    trend = sa.trend.value if hasattr(sa.trend, "value") else str(sa.trend)
    return trend, float(getattr(sa, "confidence", 0.0) or 0.0)


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
        ctx: Optional[SystemContext] = None,
    ) -> None:
        self._pm = platform_manager
        self._tick_store = tick_store
        self._wm_store = world_model_store
        self._aggregator = intent_aggregator
        self._mgmt_store = mgmt_store or ManagementStateStore()
        self._worker = PositionWorker(worker_config or WorkerConfig())
        # Parallelize the per-tick position scan across symbols.  Each pool
        # task evaluates ALL positions of one symbol sequentially, so every
        # per-position / per-order_id evaluator field is written by exactly one
        # thread, and the shared stores it touches (ManagementStateStore,
        # IntentAggregator, DecisionJournal) are internally locked.  Set
        # APEX_ED_POSEVAL_PARALLEL=0 to fall back to a serial scan.
        self._parallel = os.environ.get(
            "APEX_ED_POSEVAL_PARALLEL", "1"
        ).strip().lower() in ("1", "true", "yes", "on")
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="pos-eval",
        )
        self._lock = threading.Lock()
        self._running = False
        self._eval_count = 0
        self._suppressed_tickets: dict[str, float] = {}
        self._suppress_duration = 60.0
        self._ctx = ctx
        self._last_de_eval: dict[str, float] = {}
        self._de_interval = 10.0
        self._fast_opposition: dict[str, int] = {}
        self._last_h1_close: dict[str, datetime] = {}

    def evaluate_all(
        self, scheduler: Optional[ManagementScheduler] = None,
    ) -> None:
        """Snapshot all open positions and evaluate against latest ticks.

        When *scheduler* is provided (event-reactive mode), only the symbols it
        reports as due this cycle are evaluated — active symbols shortly after a
        tick, idle symbols on the safety-net cadence.  When None, every open
        position is evaluated (the legacy fixed-rate sweep).
        """
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

        # Event-reactive gating: restrict this cycle to the symbols the
        # scheduler reports as due.  cleanup() above still ran over ALL live
        # tickets, so state for not-yet-due symbols is preserved.
        if scheduler is not None:
            due = set(scheduler.due(list(by_symbol.keys())))
            scheduler.forget(by_symbol.keys())
            by_symbol = {s: pl for s, pl in by_symbol.items() if s in due}

        if self._parallel and len(by_symbol) > 1:
            # Fan out one task per symbol; each task evaluates that symbol's
            # positions sequentially.  Wait for all before returning so the
            # cycle's timing/eval_count stay accurate.
            futures = [
                self._pool.submit(
                    self._evaluate_symbol, symbol, pos_list, now, now_mono,
                )
                for symbol, pos_list in by_symbol.items()
            ]
            for fut in futures:
                try:
                    fut.result()
                except Exception as exc:
                    logger.debug("[pos-eval] symbol task error: {}", exc)
        else:
            for symbol, pos_list in by_symbol.items():
                self._evaluate_symbol(symbol, pos_list, now, now_mono)

        self._eval_count += 1

    def _evaluate_symbol(
        self, symbol: str, pos_list: list, now: datetime, now_mono: float,
    ) -> None:
        """Evaluate all positions of one symbol against its latest tick.

        Runs on a single thread per symbol (serial within the symbol), so
        per-order_id evaluator state is never touched concurrently.
        """
        try:
            tick = self._tick_store.get_latest(symbol)
        except Exception as exc:
            logger.debug("[pos-eval] {} get_latest failed: {}", symbol, exc)
            return
        if tick is None:
            return
        price = tick.mid
        for pos in pos_list:
            self._evaluate_position(pos, price, now, now_mono)

    def _evaluate_position(
        self, pos, price: float, now: datetime, now_mono: float,
    ) -> None:
        try:
            order_id = str(getattr(pos, "order_id", getattr(pos, "ticket", "")))

            if order_id in self._suppressed_tickets:
                return

            symbol = getattr(pos, "symbol", "")
            direction = getattr(pos, "direction", "")
            sl = getattr(pos, "sl", 0.0) or 0.0
            pip_size = 0.0001
            try:
                pip_size = get_pip_size(symbol)
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

            scan_ctx = self._build_scan_context(symbol, direction)
            market_ctx = self._build_market_context(symbol, now)

            if scan_ctx is not None:
                current_score = scan_ctx.score
                mgmt.score_history.append(current_score)
                if len(mgmt.score_history) > 20:
                    mgmt.score_history = mgmt.score_history[-20:]

            snap = build_position_snapshot(
                pos, tm_trade=mgmt, current_price=price,
                score_history=tuple(mgmt.score_history),
            )
            intents = self._worker.evaluate(snap, now, scan=scan_ctx, market=market_ctx)

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

            self._run_decision_engine_management(
                pos, price, now, now_mono, mgmt, order_id, snap,
            )

            # Persist the in-place mutations from this cycle (trailing SL,
            # breakeven / partial / tp1 flags, price extremes) so they survive
            # a crash — otherwise recovery reloads stale flags and could, e.g.,
            # re-fire a partial close. Dirty-gated, so it is a no-op write when
            # nothing material changed. Skipped if a CLOSE already removed the
            # state this cycle (avoids re-inserting a closed position).
            if self._mgmt_store.get(order_id) is not None:
                self._mgmt_store.persist(mgmt)
        except Exception as exc:
            logger.debug(
                "[pos-eval] error evaluating {}: {}",
                getattr(pos, "symbol", "?"), exc,
            )

    def _build_scan_context(
        self, symbol: str, position_direction: str,
    ) -> Optional[ScanContext]:
        """Build ScanContext from WorldModel data. Returns None if unavailable."""
        try:
            wm = self._wm_store.get(symbol)
            if wm is None:
                return None

            score = 0
            scan_direction = ""
            opposing_boost = 0

            bias_dict = wm.bias_dict()
            if bias_dict:
                scan_direction = str(bias_dict.get("direction", "")).upper()
                score = int(bias_dict.get("score", 0))
                opposing_boost = int(bias_dict.get("opposing_boost", 0))

            if not scan_direction or score == 0:
                structs = wm.structure_by_tf()
                for tf in ("H1", "H4", "M5"):
                    sa = structs.get(tf)
                    if sa is not None:
                        trend = getattr(sa, "trend", "")
                        if trend:
                            scan_direction = "LONG" if "BULL" in str(trend).upper() else (
                                "SHORT" if "BEAR" in str(trend).upper() else ""
                            )
                        s = int(round(float(getattr(sa, "confidence", 0.0) or 0.0) * 100))
                        if s:
                            score = int(s)
                        break

            if score == 0:
                return None

            return ScanContext(
                direction=scan_direction,
                score=score,
                opposing_score_boost=opposing_boost,
            )
        except Exception:
            return None

    def _build_market_context(
        self, symbol: str, now: datetime,
    ) -> Optional[MarketContext]:
        """Build MarketContext from TickStore + INSTRUMENT_REGISTRY + WorldModel."""
        try:
            tick = self._tick_store.get_latest(symbol)
            current_spread_pips: Optional[float] = None
            typical_spread_pips: Optional[float] = None

            info = INSTRUMENT_REGISTRY.get(symbol)
            pip_size = 0.0001
            if info is not None:
                pip_size = info.pip_size if hasattr(info, "pip_size") else 0.0001
                typical_spread_pips = info.typical_spread_pips if info.typical_spread_pips else None

            if tick is not None and tick.ask and tick.bid and pip_size > 0:
                current_spread_pips = (tick.ask - tick.bid) / pip_size

            h1_open = h1_close = h1_high = h1_low = h1_time = None
            last_h1 = self._last_h1_close.get(symbol)
            wm = self._wm_store.get(symbol)
            if wm is not None:
                structs = wm.structure_by_tf()
                h1_sa = structs.get("H1")
                if h1_sa is not None:
                    h1_open = getattr(h1_sa, "last_candle_open", None)
                    h1_close = getattr(h1_sa, "last_candle_close", None)
                    h1_high = getattr(h1_sa, "last_candle_high", None)
                    h1_low = getattr(h1_sa, "last_candle_low", None)
                    h1_time = getattr(h1_sa, "last_candle_time", None)

            ctx = MarketContext(
                typical_spread=typical_spread_pips,
                current_spread=current_spread_pips,
                h1_last_closed_open=h1_open,
                h1_last_closed_close=h1_close,
                h1_last_closed_high=h1_high,
                h1_last_closed_low=h1_low,
                h1_last_closed_time=h1_time,
                last_seen_h1_close=last_h1,
            )

            if h1_time is not None:
                self._last_h1_close[symbol] = h1_time

            return ctx
        except Exception:
            return None

    def suppress_ticket(self, ticket: str, duration: float = 60.0) -> None:
        """Suppress evaluation of a ticket for the given duration (seconds)."""
        self._suppressed_tickets[ticket] = _time.monotonic() + duration

    def shutdown(self) -> None:
        """Stop the per-symbol evaluation worker pool (called on system stop)."""
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass

    def _run_decision_engine_management(
        self, pos, price: float, now: datetime, now_mono: float,
        mgmt, order_id: str, snap: PositionSnapshot,
    ) -> None:
        ctx = self._ctx
        if ctx is None or ctx.decision_engine is None or ctx.situation_engine is None:
            return
        if now_mono - self._last_de_eval.get(order_id, 0.0) < self._de_interval:
            return
        self._last_de_eval[order_id] = now_mono
        try:
            from decision.context import TradeContext
            from decision.actions import Action
            symbol = getattr(pos, "symbol", "")
            direction = getattr(pos, "direction", "")
            entry_price = getattr(pos, "entry_price", 0.0)
            norm_dir = "BUY" if direction.upper() in ("BUY", "LONG") else "SELL"
            pip_size = 0.0001
            try:
                pip_size = get_pip_size(symbol)
            except Exception:
                pass
            sl = getattr(pos, "sl", 0.0) or mgmt.stop_loss or 0.0
            if norm_dir == "BUY":
                pnl_pips = (price - entry_price) / pip_size if pip_size > 0 else 0.0
            else:
                pnl_pips = (entry_price - price) / pip_size if pip_size > 0 else 0.0
            risk_pips = abs(entry_price - sl) / pip_size if sl and pip_size > 0 else 0.0
            open_positions = []
            try:
                open_positions = self._pm.get_all_open_positions()
            except Exception:
                pass
            wm = self._wm_store.get(symbol)
            structure = wm.structure_by_tf() if wm is not None else {}
            d1_trend, d1_conf = _struct_trend_conf(structure, "D1")
            h4_trend, h4_conf = _struct_trend_conf(structure, "H4")
            h1_trend, h1_conf = _struct_trend_conf(structure, "H1")
            score_hist = getattr(mgmt, "score_history", []) or []
            current_score = 0
            if wm is not None:
                want_dir = norm_dir.replace("BUY", "LONG").replace("SELL", "SHORT")
                for z in wm.entry_zones:
                    if getattr(z, "direction", "").upper() == want_dir:
                        current_score = max(current_score, getattr(z, "conviction", 0))
            fast_opp = self._fast_opposition.get(order_id, 0)
            _m1_aligned = 0
            _m1_trend = "UNKNOWN"
            session_name = "UNKNOWN"
            session_tradeable = True
            if ctx.session_engine is not None:
                try:
                    ss = ctx.session_engine.get_status()
                    session_name = getattr(ss, "name", "UNKNOWN")
                    session_tradeable = getattr(ss, "is_tradeable", True)
                except Exception:
                    pass
            news_mins = 999.0
            if ctx.news_guard is not None:
                try:
                    ns = ctx.news_guard.check([symbol])
                    news_mins = getattr(ns, "minutes_to_next", 999.0)
                except Exception:
                    pass
            hold_mins = 0.0
            entry_time = getattr(pos, "open_time", None) or getattr(pos, "entry_time", None)
            if entry_time is not None:
                try:
                    if hasattr(entry_time, "timestamp"):
                        hold_mins = (now - entry_time).total_seconds() / 60.0
                except Exception:
                    pass

            trade_ctx = TradeContext(
                symbol=symbol,
                order_id=order_id,
                direction=norm_dir,
                entry_price=entry_price,
                current_price=price,
                current_sl=sl,
                pnl_pips=pnl_pips,
                pnl_dollars=getattr(pos, "profit", 0.0) or 0.0,
                hold_minutes=hold_mins,
                at_breakeven=mgmt.at_breakeven,
                tp1_hit=mgmt.tp1_hit,
                trailing=getattr(mgmt, "trailing", False),
                partial_closed=mgmt.partial_closed,
                lots=getattr(pos, "lots", 0.0) or 0.0,
                original_risk_pips=risk_pips,
                scan_score=current_score,
                scan_direction=norm_dir.replace("BUY", "LONG").replace("SELL", "SHORT"),
                d1_trend=d1_trend,
                d1_confidence=d1_conf,
                h4_trend=h4_trend,
                h4_confidence=h4_conf,
                h1_trend=h1_trend,
                h1_confidence=h1_conf,
                fast_opposition_streak=fast_opp,
                score_history=list(score_hist[-10:]),
                open_trade_count=len(open_positions),
                portfolio_heat_pct=0.0,
                session_name=session_name,
                session_tradeable=session_tradeable,
                minutes_to_high_impact_news=news_mins,
            )
            sa = ctx.situation_engine.assess_open_trade(trade_ctx)
            de_result = ctx.decision_engine.decide_management(trade_ctx, sa)

            if ctx.risk_governor is not None:
                try:
                    de_result = ctx.risk_governor.review(de_result, trade_ctx, sa)
                except Exception:
                    pass

            if ctx.decision_journal is not None:
                try:
                    ctx.decision_journal.log(trade_ctx, sa, de_result)
                except Exception:
                    pass

            # Emit POSITION_HEALTH so the dashboard's live-management panel is
            # fed by the event-driven system (mirrors
            # TradingLoop._record_position_health).  One event per DE eval
            # cycle, synthesized from the situation read + decision verdict.
            try:
                store = get_event_store()
                if store is not None:
                    de_action = getattr(de_result, "action", None)
                    action_str = (
                        de_action.value if hasattr(de_action, "value")
                        else str(de_action or "")
                    )
                    tc_risk = float(getattr(trade_ctx, "original_risk_pips", 0.0) or 0.0)
                    tc_pnl_pips = float(getattr(trade_ctx, "pnl_pips", 0.0) or 0.0)
                    profit_r = round(tc_pnl_pips / tc_risk, 4) if tc_risk > 0 else 0.0
                    store.emit(
                        event_type=DE.POSITION_HEALTH,
                        severity="INFO",
                        symbol=symbol,
                        parent_id=order_id or None,
                        source_module="decision.engine",
                        payload={
                            "order_id": order_id,
                            "pair": symbol,
                            "direction": norm_dir.replace("BUY", "LONG").replace("SELL", "SHORT"),
                            "horizon": "",
                            "health_score": round(float(getattr(de_result, "confidence", 0.0) or 0.0), 4),
                            "action": action_str,
                            "profit_r": profit_r,
                            "pnl_dollars": round(float(getattr(trade_ctx, "pnl_dollars", 0.0) or 0.0), 4),
                            "hold_minutes": round(float(getattr(trade_ctx, "hold_minutes", 0.0) or 0.0), 2),
                            "dimension_scores": {
                                "tf_alignment": round(float(getattr(sa, "tf_alignment", 0.0) or 0.0), 4),
                                "momentum": round(float(getattr(sa, "momentum", 0.0) or 0.0), 4),
                                "structure_integrity": round(float(getattr(sa, "structure_integrity", 0.0) or 0.0), 4),
                            },
                            "reason": str(getattr(de_result, "reason", "") or "")[:200],
                        },
                    )
            except Exception as exc:
                logger.debug("[de-mgmt] POSITION_HEALTH persist failed: {}", exc)

            action = getattr(de_result, "action", None)
            if action is None:
                return
            action_name = action.value if hasattr(action, "value") else str(action)

            if action_name == Action.CLOSE.value:
                pip_s = pip_size
                self._aggregator.register_position(
                    ticket=order_id, direction=direction,
                    current_sl=sl, pip_size=pip_s,
                )
                self._aggregator.submit([Intent.close(
                    symbol=symbol,
                    ticket=order_id,
                    source="decision_engine",
                    reason=f"DE: {getattr(de_result, 'reason', '')[:100]}",
                )])
                logger.info(
                    "[DE-MGMT] {} {} CLOSE — {} (conf={:.2f})",
                    symbol, direction, getattr(de_result, "reason", "")[:80],
                    getattr(de_result, "confidence", 0.0),
                )
            elif action_name == Action.TIGHTEN_SL.value:
                new_sl = getattr(de_result, "new_sl", None)
                if new_sl and new_sl > 0:
                    pip_s = pip_size
                    self._aggregator.register_position(
                        ticket=order_id, direction=direction,
                        current_sl=sl, pip_size=pip_s,
                    )
                    self._aggregator.submit([Intent.modify_sl(
                        symbol=symbol,
                        ticket=order_id,
                        new_sl=new_sl,
                        source="decision_engine",
                        reason=f"DE: {getattr(de_result, 'reason', '')[:80]}",
                    )])
            elif action_name == Action.SET_PROTECTIVE_STOP.value:
                new_sl = getattr(de_result, "new_sl", None)
                if new_sl and new_sl > 0:
                    pip_s = pip_size
                    self._aggregator.register_position(
                        ticket=order_id, direction=direction,
                        current_sl=sl, pip_size=pip_s,
                    )
                    self._aggregator.submit([Intent.modify_sl(
                        symbol=symbol,
                        ticket=order_id,
                        new_sl=new_sl,
                        source="decision_engine",
                        reason=f"DE protective: {getattr(de_result, 'reason', '')[:60]}",
                    )])

            tf_align = getattr(sa, "tf_alignment", 0.0)
            momentum = getattr(sa, "momentum", 0.0)
            is_long = norm_dir == "BUY"
            if (is_long and (tf_align < -0.2 or momentum < -0.3)) or \
               (not is_long and (tf_align > 0.2 or momentum > 0.3)):
                self._fast_opposition[order_id] = fast_opp + 1
            else:
                self._fast_opposition[order_id] = 0

        except Exception as exc:
            logger.debug("[de-mgmt] DecisionEngine management failed for {}: {}", order_id, exc)

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
                        intent = intents[i] if i < len(intents) else None
                        if r.success:
                            self._intents_executed += 1
                            if intent is not None and intent.intent_type == IntentType.CLOSE:
                                if self._on_close is not None:
                                    try:
                                        self._on_close(intent, r)
                                    except Exception as exc:
                                        logger.debug("[flush-loop] close callback error: {}", exc)
                            elif intent is not None:
                                # SL/TP modify or partial close — surface the
                                # during-trade action on the dashboard.
                                try:
                                    self._emit_modify_event(intent)
                                except Exception as exc:
                                    logger.debug("[flush-loop] modify event error: {}", exc)
                        elif self._evaluator and not r.success:
                            err_msg = str(getattr(r, "error", "") or "").lower()
                            if "market closed" in err_msg or "market is closed" in err_msg:
                                ticket = intent.position_ticket if intent is not None else ""
                                if ticket:
                                    self._evaluator.suppress_ticket(ticket, 60.0)
                            elif "no longer open" in err_msg:
                                pass
                self._flush_count += 1
            except Exception as exc:
                logger.warning("[flush-loop] error: {}", exc)
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

        # ── Learned analysis edge ────────────────────────────────────
        # Bounded, default-neutral multipliers that make the analysis
        # combination data-driven: zone conviction and per-concept bias are
        # scaled by realized trade outcomes recorded on the close path.
        self._zone_edge = ZoneEdgeTracker()
        # Per-ticket entry context (zone_type, regime, concepts) captured at
        # fill time so the close path can attribute the outcome to the right
        # learned keys.
        self._entry_context: dict[Any, dict[str, Any]] = {}

        # ── Analysis plane ───────────────────────────────────────────
        self._candle_handler = CandleCloseHandler(
            event_bus=self._event_bus,
            world_model_store=self._wm_store,
            candle_fetcher=self._fetch_candles,
            edge_weight=self._zone_edge.zone_weight,
            concept_weight=self._zone_edge.concept_weight,
        )

        # ── Execution plane ──────────────────────────────────────────
        self._aggregator = IntentAggregator(AggregatorConfig())
        self._executor = ActionExecutor(
            broker=self._pm,  # PlatformManager satisfies BrokerPort
            config=ExecutorConfig(),
        )
        # Phase 1 (single execution plane): entries flow through the shared
        # ActionExecutor (RiskGate OPEN validation → CircuitBreaker →
        # broker.execute_entry), serialized on the same executor lock as
        # management actions, instead of calling PlatformManager.execute_entry
        # directly on the tick thread.  Live by default; set
        # APEX_ED_ENTRY_VIA_EXECUTOR=0 as a kill switch to fall back to the
        # direct broker path.
        self._entry_via_executor = os.environ.get(
            "APEX_ED_ENTRY_VIA_EXECUTOR", "1"
        ).strip().lower() in ("1", "true", "yes", "on")
        self._mgmt_store = ManagementStateStore(db_path="data/management_state.db")
        self._evaluator = PositionEvaluator(
            platform_manager=self._pm,
            tick_store=self._tick_store,
            world_model_store=self._wm_store,
            intent_aggregator=self._aggregator,
            mgmt_store=self._mgmt_store,
            ctx=ctx,
        )
        # Phase 3 (event-reactive management): the tick-eval loop evaluates
        # only the symbols a ManagementScheduler reports as due (active symbols
        # shortly after a tick, idle symbols on the safety-net cadence) instead
        # of re-scanning every position on the fixed 10 Hz timer.  Live by
        # default; set APEX_ED_EVENT_REACTIVE_MGMT=0 as a kill switch to fall
        # back to the full timer sweep.
        self._event_reactive_mgmt = os.environ.get(
            "APEX_ED_EVENT_REACTIVE_MGMT", "1"
        ).strip().lower() in ("1", "true", "yes", "on")
        self._mgmt_scheduler = (
            ManagementScheduler() if self._event_reactive_mgmt else None
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
            on_gate_trace=self._on_gate_trace,
        )

        # ── Background loops ─────────────────────────────────────────
        self._flush_loop = FlushLoop(
            self._aggregator, self._executor, self._pm,
            evaluator=self._evaluator,
            on_close_callback=self._handle_close_result,
        )
        self._tick_eval_loop = TickEvalLoop(
            self._evaluator, scheduler=self._mgmt_scheduler,
        )

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
        if self._mgmt_scheduler is not None:
            # Event-reactive management: mark a symbol active on each tick so
            # the tick-eval loop evaluates it on the next cycle.  Cheap + thread
            # -safe; heavy evaluation stays on the loop, not the tick thread.
            self._event_bus.subscribe(
                "tick",
                lambda tick: self._mgmt_scheduler.note_event(
                    getattr(tick, "symbol", ""),
                ),
            )
        self._event_bus.subscribe(
            "candle_close:M1", lambda ev: self._entry_orchestrator.on_m1_close(ev.symbol),
        )
        self._event_bus.subscribe(
            "world_model_update", self._entry_orchestrator.on_world_model_update,
        )
        self._event_bus.subscribe(
            "world_model_update", self._on_world_model_update,
        )

        self._be_stop_cooldown: dict[str, float] = {}
        self._be_cooldown_seconds = 300.0
        self._paused = False
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

        # ── Event-log reconciliation: surface crash-window discrepancies ──
        # Read-only: fold the replayable event log into the order_ids it
        # believes are open and compare to the broker's live positions.  Logs
        # any mismatch (missed closes / orphan positions) so an unclean restart
        # is visible; never opens or closes anything.  Best-effort.
        try:
            from persistence.recovery import run_startup_recovery
            broker_ids = [
                str(getattr(p, "order_id", getattr(p, "ticket", "")))
                for p in self._pm.get_all_open_positions()
            ]
            run_startup_recovery(get_event_store(), broker_ids)
        except Exception as exc:
            logger.debug("[event-driven] event-log reconciliation failed: {}", exc)

        symbols = list(INSTRUMENT_REGISTRY.keys())
        for sym in symbols:
            self._candle_detector.register(sym)
        logger.info("[event-driven] registered {} symbols for candle detection", len(symbols))

        # ── Warmup scan: backfill WorldModels before live loops start ────
        # Without this the tick-driven analysis is blind until live candles
        # close — HTF (H1/H4/D1) structure/bias is empty and tick-starved
        # symbols are never analysed.  Best-effort; on failure we simply fall
        # back to the live path.  Runs before the loops so it can't race a live
        # candle-close for the same symbol.
        if getattr(self._config, "ed_warmup_on_start", True):
            try:
                self._candle_handler.warmup(symbols)
            except Exception as exc:
                logger.warning(
                    "[event-driven] warmup scan failed (continuing live): {}", exc,
                )

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

        # ── Register tunable adapters with TunerAgent ────────────────
        self._register_tunable_adapters()

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
            "tick_eval": self._tick_eval_loop.get_profile(),
            "tick_router": {
                "ticks_routed": self._tick_router.ticks_routed,
            },
            "candle_detector": {
                "events_emitted": self._candle_detector.events_emitted,
                "tracked_pairs": self._candle_detector.tracked_pairs,
            },
            "zone_edge": self._zone_edge.snapshot(),
        }

    def get_tick_profile(self) -> dict[str, Any]:
        """Per-component tick-latency profile for the dashboard ops panel.

        Shaped like the legacy ``TradingLoop.get_tick_profile`` so the
        operations page renders it identically: the position-eval cycle as
        the primary ``tick`` block plus per-component throughput rows.
        """
        prof = self._tick_eval_loop.get_profile()
        slow = prof.pop("slow_ticks", [])
        components = [
            {"name": "position_eval", "p50_ms": prof.get("p50_ms", 0.0),
             "p95_ms": prof.get("p95_ms", 0.0), "max_ms": prof.get("max_ms", 0.0),
             "samples": prof.get("samples", 0)},
            {"name": "ticks_routed", "count": self._tick_router.ticks_routed},
            {"name": "candle_events", "count": self._candle_detector.events_emitted},
        ]
        recommendations: list[str] = []
        if prof.get("p95_ms", 0.0) >= self._tick_eval_loop._slow_threshold_ms:
            recommendations.append(
                "position-eval p95 latency is high — consider raising the "
                "tick-eval interval or reducing per-position work",
            )
        return {
            "enabled": prof.get("samples", 0) > 0,
            "tick": prof,
            "components": components,
            "slow_ticks": slow,
            "recommendations": recommendations,
        }

    # ── Internal helpers ─────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        """Background loop: heartbeat + stall detection + daily maintenance + heat monitoring."""
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

            # ── Portfolio heat monitoring — active position reduction ──
            if ctx is not None:
                self._run_portfolio_heat_check(ctx)

            # ── Periodic learning tasks ──────────────────────────────
            if ctx is not None:
                self._run_periodic_learning(ctx)

            _time.sleep(10.0)

    def _run_portfolio_heat_check(self, ctx: SystemContext) -> None:
        # Canonical heat response (DEFENSIVE/REDUCING/EMERGENCY → intents).
        self._check_portfolio_heat()

        # ── Account risk unrealized P&L tracking ─────────────────────
        if ctx.account_risk is not None:
            try:
                positions = self._pm.get_all_open_positions()
                acct_unrealized: dict[str, float] = defaultdict(float)
                for pos in positions:
                    pnl = getattr(pos, "profit", 0.0) or 0.0
                    sym = getattr(pos, "symbol", "")
                    acct = ctx.account_key(sym, self._pm)
                    acct_unrealized[acct] += pnl
                for acct, unrealized in acct_unrealized.items():
                    ctx.account_risk.update_unrealized(acct, unrealized)
            except Exception as exc:
                logger.debug("[heat-mon] account risk unrealized update failed: {}", exc)

    def _run_periodic_learning(self, ctx: SystemContext) -> None:
        if ctx.post_close_tracker is not None:
            try:
                tick_fn = getattr(ctx.post_close_tracker, "tick", None)
                if tick_fn is not None:
                    tick_fn()
                proc_fn = getattr(ctx.post_close_tracker, "process_pending", None)
                if proc_fn is not None:
                    proc_fn()
            except Exception as exc:
                logger.debug("[periodic] PostCloseTracker tick failed: {}", exc)

        # Shadow contract resolution — live tick-driven (see _resolve_shadows).
        self._resolve_shadows()

    def _check_portfolio_heat(self) -> None:
        """Continuous portfolio heat monitoring — generates intents for open positions.

        Canonical heat-response path used by the watchdog:
          EMERGENCY → CLOSE every position
          REDUCING  → CLOSE the weakest losing position
          DEFENSIVE → MODIFY_SL to breakeven (only when price has cleared it)

        Intents are always submitted to the IntentAggregator as a list (its
        ``submit`` contract); the FlushLoop drains and executes them.
        """
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
                        self._aggregator.submit([Intent.close(
                            symbol=symbol,
                            ticket=ticket,
                            source="heat_monitor",
                            reason="portfolio_heat_emergency",
                        )])
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
                if worst_pos is not None and worst_pnl < 0:
                    ticket = str(getattr(worst_pos, "order_id", getattr(worst_pos, "ticket", "")))
                    symbol = getattr(worst_pos, "symbol", "")
                    if ticket:
                        self._aggregator.submit([Intent.close(
                            symbol=symbol,
                            ticket=ticket,
                            source="heat_monitor",
                            reason="portfolio_heat_reducing_weakest",
                        )])
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
                    if already_at_be:
                        continue
                    pip_size = self._safe_pip_size(symbol)
                    be_price = entry_price + (2 * pip_size) if is_long else entry_price - (2 * pip_size)
                    # Only move to BE once price has cleared the BE level, else the
                    # SL would land on the wrong side of market (instant stop-out).
                    tick = self._tick_store.get_latest(symbol)
                    price = tick.mid if tick is not None else 0.0
                    if price <= 0:
                        continue
                    can_be = (price > be_price) if is_long else (price < be_price)
                    if not can_be:
                        continue
                    self._aggregator.register_position(
                        ticket=ticket, direction=direction,
                        current_sl=current_sl, pip_size=pip_size,
                    )
                    self._aggregator.submit([Intent.modify_sl(
                        symbol=symbol,
                        ticket=ticket,
                        new_sl=be_price,
                        source="heat_monitor",
                        reason="portfolio_heat_defensive_be",
                    )])
                logger.info("[heat-monitor] DEFENSIVE — BE intents submitted")

        except Exception as exc:
            logger.debug("[heat-monitor] check failed: {}", exc)

    def _resolve_shadows(self) -> None:
        """Resolve pending shadow contracts against the latest tick price.

        Live counterpart to the CSV-replay ``run_resolver``: when a PENDING
        shadow contract's SL or TP1 is touched by the current tick, it is
        resolved WIN/LOSS via ``ShadowStore.resolve_contract`` with a proper
        ``ShadowResolution`` payload.
        """
        ctx = self._ctx
        if ctx is None or ctx.shadow_store is None:
            return
        try:
            from persistence.shadow_store import ShadowResolution, _now_ms
            pending = ctx.shadow_store.get_pending(limit=200)
            if not pending:
                return
            for shadow in pending:
                sym = getattr(shadow, "symbol", "")
                tick = self._tick_store.get_latest(sym)
                if tick is None:
                    continue
                price = tick.mid
                sl = getattr(shadow, "stop_loss", 0.0) or 0.0
                tp = getattr(shadow, "tp1", 0.0) or 0.0
                entry = getattr(shadow, "entry_price", 0.0) or 0.0
                direction = getattr(shadow, "direction", "LONG")
                is_long = str(direction).upper() in ("BUY", "LONG")
                hit_sl = (price <= sl if is_long else price >= sl) if sl > 0 else False
                hit_tp = (price >= tp if is_long else price <= tp) if tp > 0 else False
                if not (hit_sl or hit_tp):
                    continue
                outcome = "WIN" if hit_tp else "LOSS"
                exit_price = tp if hit_tp else sl
                r_multiple = 0.0
                if entry > 0 and sl > 0:
                    risk = abs(entry - sl)
                    if risk > 0:
                        r_multiple = (
                            (exit_price - entry) / risk if is_long
                            else (entry - exit_price) / risk
                        )
                try:
                    resolution = ShadowResolution(
                        outcome=outcome,
                        r_multiple=round(float(r_multiple), 4),
                        exit_reason="TP1" if hit_tp else "SL",
                        exit_price=float(exit_price),
                        resolution_ts=_now_ms(),
                        resolution_granularity="LIVE_TICK",
                        bars_replayed=0,
                        resolver_meta={"resolver": "live_tick"},
                    )
                    ctx.shadow_store.resolve_contract(
                        getattr(shadow, "contract_id", ""), resolution,
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

    def _rl_augment(self, symbol: str, direction: str, base_score: float):
        """Run RL ``augment_score`` for an entry candidate.

        Builds the multi-timeframe observation from M5/M15/H1/H4 frames and
        feeds it to the active RL bridge. Returns the ``AugmentedScore`` or
        ``None``. Only does work when the bridge is enabled, so the candle
        fetches never run on a dormant (default) install.
        """
        ctx = self._ctx
        rl = getattr(ctx, "rl_bridge", None) if ctx is not None else None
        if rl is None or not getattr(rl, "enabled", False):
            return None
        try:
            from rl.multi_tf_obs_builder import MultiTFObservationBuilder
            from rl.contracts import build_symbol_vocab
        except Exception:
            return None
        builders = getattr(self, "_rl_mtf_builders", None)
        if builders is None:
            builders = self._rl_mtf_builders = {}
        universe = getattr(self, "_rl_symbol_universe", None)
        if universe is None:
            try:
                universe = self._rl_symbol_universe = build_symbol_vocab()
            except Exception:
                universe = self._rl_symbol_universe = None
        frames: dict[str, pd.DataFrame] = {}
        for tf in ("M5", "M15", "H1", "H4"):
            df = self._fetch_candles(symbol, tf, 80)
            if df is None or len(df) == 0:
                return None
            frames[tf] = df
        if symbol not in builders:
            builders[symbol] = MultiTFObservationBuilder()
        mtf = builders[symbol].build_from_frames(
            frames=frames, instrument=symbol, universe=universe,
        )
        if mtf is None:
            return None
        obs, ctx_vec, sym_id = mtf
        m5 = frames["M5"]
        close_now = float(m5["close"].iloc[-1])
        tr = (m5["high"] - m5["low"]).abs().tail(14)
        atr_now = float(tr.mean()) if len(tr) > 0 else 0.0
        d = str(direction).upper()
        base_dir = 1 if d in ("BUY", "LONG") else (-1 if d in ("SELL", "SHORT") else 0)
        return rl.augment_score(
            pair=symbol,
            base_score=float(base_score),
            obs=obs,
            close=close_now,
            atr=atr_now,
            pip_size=self._safe_pip_size(symbol),
            context_vec=ctx_vec,
            symbol_id=sym_id,
            base_direction=base_dir,
        )

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

    def _on_gate_trace(
        self,
        symbol: str,
        direction: str,
        results: Any,
        passed: bool,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist a DECISION_TRACE for the entry-gate verdict chain.

        Mirrors the legacy TradingLoop trace recorder, but scoped to the
        event-driven entry plane.  A fresh recorder per call keeps this
        thread-safe across concurrently-confirming symbols; best-effort so a
        tracing failure never blocks an entry.
        """
        try:
            from brain.decision_trace import (
                DecisionTraceRecorder,
                STAGE_RANKER,
                STAGE_RISK_STACK,
            )
            recorder = DecisionTraceRecorder(getattr(self._config, "decision_trace", None))
            if not recorder.enabled:
                return
            recorder.begin(symbol)
            evidence = dict(meta or {})
            evidence["direction"] = direction
            conviction = evidence.get("conviction", 0)
            zone_type = evidence.get("zone_type", "zone")
            timeframe = evidence.get("timeframe", "")

            # Stage 1 — ranker: the zone conviction is the entry plane's
            # ranking signal (also satisfies trace completeness).
            recorder.stamp(
                stage=STAGE_RANKER,
                owner="entry.zone_watcher",
                verdict="PASS",
                justification=f"entry zone {zone_type} conviction {conviction} ({timeframe})",
                evidence=evidence,
                confidence=1.0,
            )

            # Stage 2 — risk stack: the entry-gate validation result.
            failed = [
                str(getattr(g, "gate_name", "gate"))
                for g in (results or []) if not getattr(g, "passed", False)
            ]
            gate_just = (
                "all entry gates passed" if passed
                else "entry gates failed: " + ", ".join(failed[:6])
            )
            recorder.stamp(
                stage=STAGE_RISK_STACK,
                owner="entry.gate",
                verdict="PASS" if passed else "REJECT",
                justification=gate_just,
                evidence=evidence,
                confidence=1.0,
                blocking=not passed,
            )

            if passed:
                recorder.finalize_success()
            else:
                recorder.finalize_rejection(reason=gate_just, stage=STAGE_RISK_STACK)
        except Exception as exc:
            logger.debug("[decision-trace] entry gate trace failed for {}: {}", symbol, exc)

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

        # ── Feed signal_ledger with brain module directional reads ────
        if ctx.signal_ledger is not None:
            try:
                sym = getattr(event, "symbol", "")
                if sym:
                    wm = self._wm_store.get(sym)
                    if wm is not None:
                        zones = getattr(wm, "entry_zones", [])
                        direction = ""
                        score = 0
                        if zones:
                            best = max(zones, key=lambda z: getattr(z, "conviction", 0))
                            direction = getattr(best, "direction", "")
                            score = getattr(best, "conviction", 0)
                        if direction and score > 0:
                            ctx.signal_ledger.record_signal(
                                pair=sym,
                                direction=direction,
                                score=score,
                                source="world_model",
                            )
            except Exception as exc:
                logger.debug("[signal-ledger] feed failed: {}", exc)

        # ── Feed spread_monitor with current spreads ─────────────────
        if ctx.risk_engine is not None:
            try:
                spread_mon = getattr(ctx.risk_engine, "spread_monitor", None)
                if spread_mon is not None:
                    sym = getattr(event, "symbol", "")
                    if sym:
                        spread = self._get_spread_pips(sym)
                        if spread > 0:
                            spread_mon.record_spread(sym, spread)
            except Exception as exc:
                logger.debug("[spread-feed] failed: {}", exc)

        # ── Feed RL bridge: price stream + periodic authority eval ────
        # Gated on the bridge being active (trained checkpoint loaded), so
        # there is zero overhead when RL is dormant (the default).
        rl = getattr(ctx, "rl_bridge", None)
        if rl is not None and getattr(rl, "enabled", False):
            try:
                sym = getattr(event, "symbol", "")
                if sym:
                    df = self._fetch_candles(sym, "M5", 20)
                    if df is not None and len(df) > 0:
                        close_val = float(df["close"].iloc[-1])
                        high_val = float(df["high"].iloc[-1])
                        low_val = float(df["low"].iloc[-1])
                        tr = (df["high"] - df["low"]).abs().tail(14)
                        atr_val = float(tr.mean()) if len(tr) > 0 else 0.0
                        rl.update_price(sym, high_val, low_val, close_val, atr_val, None)
                # Throttle authority evaluation to roughly once per N updates.
                n = getattr(self, "_rl_eval_counter", 0) + 1
                self._rl_eval_counter = n
                if n % 50 == 0:
                    rl.evaluate_authority()
            except Exception as exc:
                logger.debug("[rl] price/authority feed failed: {}", exc)

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

            # ── Gate 0: BE-stop cooldown ─────────────────────────────
            cooldown_expiry = self._be_stop_cooldown.get(symbol, 0.0)
            if cooldown_expiry > _time.monotonic():
                remaining = cooldown_expiry - _time.monotonic()
                logger.info(
                    "EVENT-DRIVEN ENTRY SKIPPED | {} — BE-stop cooldown ({:.0f}s remaining)",
                    symbol, remaining,
                )
                return

            # ── Gate 0b: Paused ──────────────────────────────────────
            if self._paused:
                logger.info("EVENT-DRIVEN ENTRY SKIPPED | {} — system paused", symbol)
                return

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
                        open_trades=corr_trades,
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

            # ── Gate 5b: RL authority — veto + score augmentation ────
            rl = getattr(ctx, "rl_bridge", None) if ctx is not None else None
            if rl is not None and getattr(rl, "enabled", False):
                try:
                    aug = self._rl_augment(symbol, direction, conviction)
                    if aug is not None:
                        hw = getattr(ctx, "health_watchdog", None)
                        if hw is not None:
                            try:
                                hw.record_rl_signal_success()
                            except Exception:
                                pass
                        if aug.vetoed:
                            logger.warning(
                                "EVENT-DRIVEN ENTRY BLOCKED | {} — RL veto "
                                "(conf={:.2f} stage={})",
                                symbol, aug.rl_confidence, aug.authority_stage,
                            )
                            return
                        if aug.rl_delta != 0.0:
                            conviction = int(aug.final_score)
                            logger.info(
                                "EVENT-DRIVEN ENTRY | {} — RL Δ{:+.1f} → score={} "
                                "(action={} conf={:.2f})",
                                symbol, aug.rl_delta, conviction,
                                aug.rl_action, aug.rl_confidence,
                            )
                except Exception as exc:
                    hw = getattr(ctx, "health_watchdog", None) if ctx is not None else None
                    if hw is not None:
                        try:
                            hw.record_rl_signal_failure()
                        except Exception:
                            pass
                    logger.debug("[entry-risk] RL augmentation failed: {}", exc)

            # ── Gate 6: DecisionEngine — strategic conviction scoring ─
            de_size_mult = 1.0
            de_conviction = 0.0
            if ctx is not None and ctx.decision_engine is not None and ctx.situation_engine is not None:
                try:
                    from decision.context import EntryContext as DEContext
                    wm = self._wm_store.get(symbol)
                    structure = wm.structure_by_tf() if wm is not None else {}
                    d1_trend, d1_conf = _struct_trend_conf(structure, "D1")
                    h4_trend, h4_conf = _struct_trend_conf(structure, "H4")
                    h1_trend, h1_conf = _struct_trend_conf(structure, "H1")

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
                        d1_trend=d1_trend,
                        d1_confidence=d1_conf,
                        h4_trend=h4_trend,
                        h4_confidence=h4_conf,
                        h1_trend=h1_trend,
                        h1_confidence=h1_conf,
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

            # ── Gate 7: TradePlanner — advisory skip/wait/enter ──────
            if ctx is not None and ctx.trade_planner is not None:
                try:
                    from planning.models import TradePlanContext
                    plan_ctx = TradePlanContext(
                        symbol=symbol,
                        direction="LONG" if direction.upper() in ("BUY", "LONG") else "SHORT",
                        current_price=entry_price,
                        zone_entry_price=entry_price,
                        proposed_sl_price=sl,
                        proposed_tp1_price=tp1,
                        proposed_tp2_price=tp2,
                        scanner_score=float(conviction),
                        de_confidence=de_conviction if de_conviction > 0 else float(conviction) / 100.0,
                    )
                    plan = ctx.trade_planner.plan_trade(plan_ctx)
                    if plan is not None and hasattr(plan, "action"):
                        plan_action = getattr(plan, "action", "ENTER")
                        if plan_action in ("SKIP", "WAIT"):
                            logger.info(
                                "EVENT-DRIVEN ENTRY SKIPPED | {} — TradePlanner: {} ({})",
                                symbol, plan_action, getattr(plan, "reason", "")[:80],
                            )
                            self._record_shadow_rejection(
                                symbol, direction, entry_price, sl, tp1,
                                "trade_planner", conviction,
                            )
                            return
                except Exception as exc:
                    logger.debug("[entry-planner] TradePlanner check failed: {}", exc)

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
                                entry_zone={"entry": entry_price, "top": entry_price, "bottom": entry_price},
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
                    structure = wm.structure_by_tf() if wm is not None else {}
                    h4_sa = structure.get("H4")
                    h4_alignment = (
                        float(getattr(h4_sa, "confidence", 0.0) or 0.0)
                        if h4_sa is not None else None
                    )

                    proposal = TradeProposal(
                        pair=symbol,
                        direction="LONG" if direction.upper() in ("BUY", "LONG") else "SHORT",
                        scan_score=float(conviction),
                        de_conviction=de_conviction if de_conviction > 0 else None,
                        de_margin=None,
                        tf_alignment=h4_alignment,
                    )
                    verdict = ctx.orchestrator.evaluate(proposal)
                    # Emit ORCHESTRATOR_PROPOSAL so the dashboard's orchestrator
                    # panel is fed by the event-driven system (not just the
                    # legacy TradingLoop).  Covers both applied and vetoed cases.
                    try:
                        store = get_event_store()
                        if store is not None:
                            payload = verdict.to_dict() if hasattr(verdict, "to_dict") else {}
                            payload["applied"] = not bool(getattr(verdict, "vetoed", False))
                            if hasattr(proposal, "to_dict"):
                                payload["proposal"] = proposal.to_dict()
                            store.emit(
                                event_type=DE.ORCHESTRATOR_PROPOSAL,
                                severity="INFO",
                                symbol=symbol,
                                source_module="brain.orchestrator",
                                payload=payload,
                            )
                    except Exception as exc:
                        logger.debug("[orchestrator] proposal persist failed: {}", exc)
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
                            regime_str = getattr(rs, "regime", "")
                        except Exception:
                            pass
                    fp = compute_fingerprint(
                        horizon="SWING",
                        extra=regime_str,
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
                            regime_str = getattr(rs, "regime", "")
                        except Exception:
                            pass
                    exec_profile = ctx.execution_profiles.select_profile(
                        horizon="SWING",
                        regime=regime_str,
                        consensus_strength=float(conviction) / 100.0 if conviction else 0.5,
                    )
                except Exception as exc:
                    logger.debug("[exec-prof] profile selection failed: {}", exc)

            # ── Apply execution profile to SL/TP if available ────────
            if exec_profile is not None:
                try:
                    _prof_sl_mult = getattr(exec_profile, "sl_atr_multiplier", 0.0)
                    prof_tp_rr = getattr(exec_profile, "tp_rr_ratio", 0.0)
                    prof_tp2_rr = getattr(exec_profile, "tp2_rr_ratio", 0.0)
                    risk_dist = abs(entry_price - sl) if sl > 0 else 0.0
                    if prof_tp_rr > 0 and risk_dist > 0:
                        norm_dir = "LONG" if direction.upper() in ("BUY", "LONG") else "SHORT"
                        if norm_dir == "LONG":
                            prof_tp1 = entry_price + risk_dist * prof_tp_rr
                            prof_tp2 = entry_price + risk_dist * prof_tp2_rr if prof_tp2_rr > 0 else tp2
                            tp1 = min(tp1, prof_tp1) if tp1 > 0 else prof_tp1
                            tp2 = min(tp2, prof_tp2) if tp2 > 0 else prof_tp2
                        else:
                            prof_tp1 = entry_price - risk_dist * prof_tp_rr
                            prof_tp2 = entry_price - risk_dist * prof_tp2_rr if prof_tp2_rr > 0 else tp2
                            tp1 = max(tp1, prof_tp1) if tp1 > 0 else prof_tp1
                            tp2 = max(tp2, prof_tp2) if tp2 > 0 else prof_tp2
                except Exception as exc:
                    logger.debug("[exec-prof] profile application failed: {}", exc)

            # ── Position sizing ──────────────────────────────────────
            risk_pct = self._config.risk.risk_per_trade_pct / 100.0
            if ctx is not None and ctx.drawdown_guard is not None:
                try:
                    dd_status = ctx.drawdown_guard.get_status()
                    # current_risk_pct already encodes the per-mode reduction
                    # (NORMAL/CAUTION/RECOVERY/FROZEN). Use it as a cap so a
                    # drawdown never lets risk exceed the guard's recommendation.
                    dd_risk = getattr(dd_status, "current_risk_pct", 0.0) or 0.0
                    if dd_risk > 0 and dd_risk < risk_pct:
                        risk_pct = dd_risk
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

            idem_key = generate_idempotency_key(
                symbol, direction, float(size_result.lots),
            )
            _comment = build_order_comment("APEX", idem_key, score=conviction)
            _stake = size_result.stake_usd if pctx.uses_stake else None

            # Phase 1 (single execution plane): route the entry through the
            # shared ActionExecutor when enabled, otherwise use the direct
            # broker call.  The executor path is built defensively — if the
            # Intent.open factory is unavailable the entry falls back to the
            # direct path *before* any broker call, so a flagged-on entry can
            # never be dropped or double-sent.
            open_intent = None
            if self._entry_via_executor:
                try:
                    open_intent = Intent.open(
                        symbol=symbol,
                        direction=direction,
                        lots=float(size_result.lots),
                        entry_price=float(entry_price),
                        sl=float(sl),
                        tp=float(tp1),
                        stake_usd=_stake,
                        comment=_comment,
                        idempotency_key=idem_key,
                        source="event_driven",
                        reason="entry_decision",
                    )
                except Exception as exc:
                    logger.warning(
                        "[entry-via-executor] Intent.open unavailable, using "
                        "direct path | {} {} | {}", symbol, direction, exc,
                    )
                    open_intent = None

            if open_intent is not None:
                exec_result = self._executor.execute(open_intent, {})
                result = (
                    exec_result.broker_response
                    if exec_result.broker_response is not None
                    else exec_result
                )
            else:
                result = self._pm.execute_entry(
                    symbol=symbol,
                    direction=direction,
                    lots=size_result.lots,
                    sl=sl,
                    tp=tp1,
                    stake_usd=_stake,
                    comment=_comment,
                    idempotency_key=idem_key,
                )

            if result.success:
                logger.info(
                    "EVENT-DRIVEN ORDER PLACED | {} {} {:.2f} lots ticket={}",
                    symbol, direction, result.lots, result.order_id,
                )
                self._on_order_filled(symbol, direction, result, balance,
                                      entry_price, order_ts)

                # Capture the entry's learned-edge context keyed by ticket so
                # the close path can attribute the realized outcome to the
                # right zone/concept keys.  Best-effort — never blocks a trade.
                try:
                    timeframe = decision.get("timeframe", "")
                    regime = None
                    concept_names: list[str] = []
                    wm = self._wm_store.get(symbol)
                    if wm is not None:
                        regime = wm.regime_by_tf().get(timeframe)
                        for sigs in wm.concepts_by_tf().values():
                            for sig in sigs or []:
                                if getattr(sig, "is_directional", False):
                                    concept_names.append(sig.name)
                    self._entry_context[result.order_id] = {
                        "zone_type": decision.get("zone_type", ""),
                        "regime": regime,
                        "concepts": sorted(set(concept_names)),
                    }
                except Exception as exc:
                    logger.debug("[entry-ctx] capture failed: {}", exc)

                # OutcomeLogger — record the plan at entry time
                if ctx is not None and ctx.outcome_logger is not None:
                    try:
                        from planning.models import TradePlan, TradePlanContext
                        norm_dir = "LONG" if direction.upper() in ("BUY", "LONG") else "SHORT"
                        plan_obj = TradePlan(
                            action="ENTER",
                            direction=norm_dir,
                            entry_price=entry_price,
                            sl_price=sl,
                            tp1_price=tp1,
                            tp2_price=tp2,
                            confidence=float(conviction) / 100.0,
                            plan_id=str(result.order_id),
                        )
                        plan_context = TradePlanContext(
                            symbol=symbol,
                            direction=norm_dir,
                            current_price=entry_price,
                            zone_entry_price=entry_price,
                            proposed_sl_price=sl,
                            proposed_tp1_price=tp1,
                            proposed_tp2_price=tp2,
                            scanner_score=float(conviction),
                        )
                        ctx.outcome_logger.log_plan(plan_obj, plan_context)
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

        # RL — count the live trade so authority progression can advance
        if getattr(ctx, "rl_bridge", None) is not None:
            try:
                ctx.rl_bridge.record_live_trade()
            except Exception as exc:
                logger.debug("[post-fill] RL record_live_trade failed: {}", exc)

        # ── Domain event: ORDER_FILLED ───────────────────────────────
        try:
            es = get_event_store()
            fill_price = getattr(result, "fill_price", getattr(result, "entry_price", expected_price)) or expected_price
            es.emit(
                DE.ORDER_FILLED, "INFO",
                symbol=symbol,
                parent_id=order_id or None,
                source_module="event_driven",
                payload={
                    "order_id": order_id,
                    "symbol": symbol,
                    "direction": direction,
                    "requested_price": float(expected_price or 0.0),
                    "fill_price": float(fill_price),
                    "lots": float(getattr(result, "lots", 0.0) or 0.0),
                },
            )
        except Exception as exc:
            logger.debug("[post-fill] ORDER_FILLED event emit failed: {}", exc)

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
        # ── LEARNED ANALYSIS EDGE (data-driven combiner) ────────────
        # Attribute the realized outcome to the zone/concept keys captured at
        # entry so future conviction reflects what actually pays off.  Runs
        # before the ctx guard (no ctx dependency) and is fully best-effort.
        try:
            info = self._entry_context.pop(ticket, None) or {}
            # A flat-dollar close that still gained pips (e.g. commission ate
            # the dollar P&L) counts as a win for edge attribution.
            won = (pnl_dollars or 0.0) > 0.0 or (
                (pnl_dollars or 0.0) == 0.0 and (pnl_pips or 0.0) > 0.0
            )
            self._zone_edge.record_trade(
                symbol,
                direction,
                won,
                zone_type=info.get("zone_type"),
                regime=info.get("regime"),
                concepts=info.get("concepts"),
            )
        except Exception as exc:
            logger.debug("[close-learn] zone-edge record failed: {}", exc)

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
                    # Attribution for the reconciliation feed: the ED system
                    # determined this close internally (no separate broker
                    # deal-history reason), so source is "event_driven" and
                    # there is no raw broker reason to diverge from.  This also
                    # satisfies invariant I2 (every exit_reason has a source).
                    "exit_reason_source": "event_driven",
                    "raw_broker_reason": None,
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
                        regime_str = getattr(rs, "regime", "")
                    except Exception:
                        pass
                fp = compute_fingerprint(
                    horizon="SWING",
                    extra=regime_str,
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

        logger.info(
            "EVENT-DRIVEN CLOSE FEEDBACK | {} {} ticket={} pnl=${:.2f} ({:.1f}pip) | risk+learning+evolution",
            direction, symbol, ticket, pnl_dollars, pnl_pips,
        )

        # ── BE-stop cooldown — prevent chop re-entry ────────────────
        is_breakeven_exit = abs(pnl_dollars) < 0.01 and abs(pnl_pips) < 2.0
        if is_breakeven_exit:
            self._be_stop_cooldown[symbol] = _time.monotonic() + self._be_cooldown_seconds
            logger.info("[be-cooldown] {} cooldown for {:.0f}s (breakeven exit)", symbol, self._be_cooldown_seconds)

        # ── Re-entry evaluation ──────────────────────────────────────
        # ReEntryManager only re-arms trades stopped at breakeven whose
        # structural setup is still valid (see management/re_entry.py).
        if ctx is not None and ctx.re_entry_manager is not None and is_breakeven_exit:
            try:
                m5_df = self._fetch_candles(symbol, "M5", 50)
                if m5_df is not None:
                    from types import SimpleNamespace
                    closed = SimpleNamespace(
                        pair=symbol,
                        direction=direction,
                        re_entry_eligible=True,
                        candles_since_entry=0,
                    )
                    opp = ctx.re_entry_manager.check_re_entry(closed, m5_df)
                    if opp is not None and getattr(opp, "eligible", False):
                        logger.info(
                            "[re-entry] {} {} opportunity: {} (zone={})",
                            symbol, direction,
                            getattr(opp, "reason", ""),
                            getattr(opp, "new_entry_zone", None),
                        )
                        self._arm_re_entry_zone(symbol, direction, opp)
            except Exception as exc:
                logger.debug("[re-entry] evaluation failed: {}", exc)

    def _arm_re_entry_zone(self, symbol: str, direction: str, opp: Any) -> None:
        """Re-arm the event-driven entry path after a confirmed re-entry.

        ReEntryManager has confirmed the structural setup is still valid after a
        breakeven stop.  Rather than placing an order directly (which would bypass
        the entry gates), we clear the BE-stop cooldown so the normal
        WorldModel → zone → tick-detection → M1-confirm → gate flow can re-arm the
        entry on the next analysis-plane update.
        """
        try:
            self._be_stop_cooldown.pop(symbol, None)
            logger.info(
                "[re-entry] {} {} re-armed — BE cooldown cleared, entry path live",
                symbol, direction,
            )
            try:
                es = get_event_store()
                es.emit(
                    DE.RE_ENTRY_ARMED, "INFO",
                    symbol=symbol,
                    source_module="re_entry",
                    payload={
                        "symbol": symbol,
                        "direction": direction,
                        "reason": getattr(opp, "reason", ""),
                        "zone": getattr(opp, "new_entry_zone", ""),
                        "kind": "re_entry_rearm",
                    },
                )
            except Exception:
                pass
        except Exception as exc:
            logger.debug("[re-entry] arm failed: {}", exc)
