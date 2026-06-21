"""APEX TRADER — Event-Driven System Bootstrap.

Wires all subsystems into a running event-driven system:

  Analysis Plane:    CandleClose events → brain modules → WorldModel
  Execution Plane:   Ticks → PositionWorkers → IntentAggregator → ActionExecutor → Broker
  Entry Plane:       WorldModel zones → tick detection → M1 confirm → gates → executor

Start:  ``system.start()``
Stop:   ``system.stop()``
"""

from __future__ import annotations

import json
import os
import threading
import time as _time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
from loguru import logger

from config import AppConfig, INSTRUMENT_REGISTRY, get_pip_size, is_always_open, is_session_gated, Platform
from brain.world_model import WorldModelStore, build_world_model
from compliance import (
    ComplianceAccount,
    ComplianceBook,
    ComplianceCandidate,
)
from core.system_context import SystemContext
from persistence.event_store import get_event_store
from persistence import domain_events as DE
from persistence.position_store import PositionStore, STORE_UNAVAILABLE
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
from ops.lifecycle import ShutdownManager
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


# ── Broker-truth field readers ───────────────────────────────────────
# Open positions returned by the platform layer are broker ``PositionInfo``
# objects (fields: ``pnl``, ``lots``, ``open_price``, ``current_price``,
# ``sl``, ``tp``, ``swap``).  Older call sites read legacy attribute names
# (``profit``, ``entry_price``, ``tp1``/``tp2``) that do not exist on
# ``PositionInfo`` and so silently resolved to defaults.  These helpers
# prefer the broker-reported field and fall back to the legacy name so both
# real broker objects and any legacy/test doubles resolve correctly.


def _broker_pnl(pos) -> float:
    """Broker-reported P&L for an open position (falls back to legacy)."""
    v = getattr(pos, "pnl", None)
    if v is None:
        v = getattr(pos, "profit", None)
    if v is None:
        v = getattr(pos, "broker_pnl", None)
    return float(v) if v is not None else 0.0


def _broker_entry_price(pos, default: float = 0.0) -> float:
    """Broker open price for a position (falls back to legacy ``entry_price``)."""
    v = getattr(pos, "open_price", None)
    if not v:
        v = getattr(pos, "entry_price", None)
    return float(v) if v else float(default)


def _broker_tp(pos) -> float:
    """Broker take-profit for a position (single broker TP == tp1)."""
    v = getattr(pos, "tp", None)
    if not v:
        v = getattr(pos, "tp1", None)
    return float(v) if v else 0.0


# Operations Division — scale-in / partial-close tuning (V13).
# A scale-in is an *add* to an existing winner, so it risks a fraction of a
# fresh entry's per-trade ceiling. The partial-close default banks half the
# position when a verdict carries no explicit ratio.
_SCALE_IN_RISK_FRACTION = 0.5
_DEFAULT_PARTIAL_CLOSE_RATIO = 0.5


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
                    # A symbol confirmed unavailable on the broker will never
                    # recover — drop it immediately instead of spinning to the
                    # 500-failure threshold.
                    if "not available on broker" in str(exc):
                        logger.warning(
                            "[mt5-poller] {} removed from poll — not available on broker",
                            sym,
                        )
                        self._symbols.remove(sym)
                        removed.add(sym)
                        continue

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
        config: Optional[AppConfig] = None,
    ) -> None:
        self._pm = platform_manager
        self._tick_store = tick_store
        self._wm_store = world_model_store
        self._aggregator = intent_aggregator
        self._mgmt_store = mgmt_store or ManagementStateStore()
        self._worker = PositionWorker(worker_config or WorkerConfig())
        self._config = config
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
            pip_size = self._safe_pip_size(symbol)

            mgmt = self._mgmt_store.get_or_create(
                order_id,
                original_stop_loss=sl,
                stop_loss=sl,
                tp1=_broker_tp(pos),
                tp2=getattr(pos, "tp2", 0.0) or 0.0,
                original_tp2=getattr(pos, "tp2", 0.0) or 0.0,
                remaining_size_lots=getattr(pos, "lots", 0.0) or 0.0,
                pip_size=pip_size,
                highest_price_since_entry=_broker_entry_price(pos, price),
                lowest_price_since_entry=_broker_entry_price(pos, price),
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
                        # Do NOT remove management state optimistically — if the
                        # broker close fails (circuit open, market closed, reject)
                        # the position is still live and must stay managed. The
                        # close-success callback (_handle_close_result) removes it.
                        pass
                    elif intent.intent_type == IntentType.MODIFY_SL and intent.new_sl:
                        prev_sl = mgmt.stop_loss
                        prev_at_be = mgmt.at_breakeven
                        mgmt.stop_loss = intent.new_sl
                        new_at_be = mgmt.at_breakeven
                        if not mgmt.at_breakeven:
                            entry = _broker_entry_price(pos)
                            if direction.upper() in ("BUY", "LONG"):
                                if intent.new_sl >= entry:
                                    new_at_be = True
                            else:
                                if intent.new_sl <= entry:
                                    new_at_be = True
                            mgmt.at_breakeven = new_at_be
                        # Snapshot so a failed SL modify rolls back (no phantom
                        # breakeven, SL move retried next cycle).
                        self._record_inflight_manage(
                            order_id, intent.intent_type,
                            {"stop_loss": prev_sl, "at_breakeven": prev_at_be},
                        )
                    elif intent.intent_type == IntentType.PARTIAL_CLOSE:
                        prev_pc = mgmt.partial_closed
                        prev_tp1 = mgmt.tp1_hit
                        mgmt.partial_closed = True
                        mgmt.tp1_hit = True
                        # Snapshot so a rejected TP1 partial rolls back and is
                        # retried instead of being permanently marked taken.
                        self._record_inflight_manage(
                            order_id, intent.intent_type,
                            {"partial_closed": prev_pc, "tp1_hit": prev_tp1},
                        )

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
            pip_size = self._safe_pip_size(symbol)
            if info is not None:
                typical_spread_pips = info.typical_spread_pips if info.typical_spread_pips else None

            if tick is not None and tick.ask and tick.bid and pip_size > 0:
                current_spread_pips = (tick.ask - tick.bid) / pip_size

            m5_df = None
            try:
                data = self._pm.fetch_market_data(symbol, ["M5"], 120)
                if isinstance(data, dict):
                    m5_df = data.get("M5")
            except Exception as exc:
                logger.debug("[pos-eval] {} M5 fetch failed: {}", symbol, exc)

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
                m5_df=m5_df,
            )

            if h1_time is not None:
                self._last_h1_close[symbol] = h1_time

            return ctx
        except Exception:
            return None

    def _safe_pip_size(self, symbol: str) -> float:
        try:
            spec = (
                self._pm.get_symbol_spec(symbol)
                if hasattr(self._pm, "get_symbol_spec")
                else None
            )
            if spec and spec.get("pip_size", 0) > 0:
                return float(spec["pip_size"])
            return float(get_pip_size(symbol))
        except Exception:
            try:
                return float(get_pip_size(symbol))
            except Exception:
                return 0.0001

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
            entry_price = _broker_entry_price(pos)
            norm_dir = "BUY" if direction.upper() in ("BUY", "LONG") else "SELL"
            pip_size = self._safe_pip_size(symbol)
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

            # Live portfolio heat for this position's account — feeds the
            # situation engine / risk governor management review (was hardcoded
            # 0.0, so heat-aware management never engaged).
            de_heat_pct = 0.0
            if ctx.account_risk is not None:
                try:
                    de_heat_pct = float(
                        ctx.account_risk.heat(ctx.account_key(symbol, self._pm))
                    )
                except Exception:
                    de_heat_pct = 0.0

            entry_oq = None
            entry_eq = None
            live_oq = None
            live_eq = None
            oq_decay = None
            eq_decay = None
            try:
                entry_oq = getattr(mgmt, "entry_oq", None)
                if entry_oq is not None:
                    entry_oq = float(entry_oq)
            except Exception:
                entry_oq = None
            try:
                entry_eq = getattr(mgmt, "entry_eq", None)
                if entry_eq is not None:
                    entry_eq = float(entry_eq)
            except Exception:
                entry_eq = None
            try:
                if wm is not None and ctx.opportunity_executor is not None:
                    candidates = (
                        wm.candidates_list()
                        if hasattr(wm, "candidates_list")
                        else list(getattr(wm, "candidates", ()) or [])
                    )
                    want_dir = "LONG" if norm_dir == "BUY" else "SHORT"
                    for cand in candidates:
                        if str(getattr(cand, "direction", "")).upper() != want_dir:
                            continue
                        conf = float(getattr(cand, "confidence", 0.0) or 0.0)
                        coh = float(getattr(cand, "coherence", 0.0) or 0.0)
                        live_oq = max(0.0, min(10.0, conf * 10.0))
                        live_eq = max(0.0, min(10.0, coh * 10.0))
                        break
            except Exception as exc:
                logger.debug("[de-mgmt] live OQ/EQ read failed for {}: {}", symbol, exc)
            if entry_oq is not None and live_oq is not None:
                oq_decay = entry_oq - live_oq
            if entry_eq is not None and live_eq is not None:
                eq_decay = entry_eq - live_eq

            trade_ctx = TradeContext(
                symbol=symbol,
                order_id=order_id,
                direction=norm_dir,
                entry_price=entry_price,
                current_price=price,
                current_sl=sl,
                pnl_pips=pnl_pips,
                pnl_dollars=_broker_pnl(pos),
                hold_minutes=hold_mins,
                at_breakeven=mgmt.at_breakeven,
                tp1_hit=mgmt.tp1_hit,
                trailing=getattr(mgmt, "trailing", False),
                partial_closed=mgmt.partial_closed,
                lots=getattr(pos, "lots", 0.0) or 0.0,
                original_risk_pips=risk_pips,
                scan_score=current_score,
                scan_direction=norm_dir.replace("BUY", "LONG").replace("SELL", "SHORT"),
                live_oq=live_oq,
                live_eq=live_eq,
                entry_oq=entry_oq,
                entry_eq=entry_eq,
                oq_decay=oq_decay,
                eq_decay=eq_decay,
                d1_trend=d1_trend,
                d1_confidence=d1_conf,
                h4_trend=h4_trend,
                h4_confidence=h4_conf,
                h1_trend=h1_trend,
                h1_confidence=h1_conf,
                fast_opposition_streak=fast_opp,
                score_history=list(score_hist[-10:]),
                open_trade_count=len(open_positions),
                portfolio_heat_pct=de_heat_pct,
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
                # V14 — carry the DecisionEngine's own normalised exit cause
                # (e.g. fast_opposition_decay, thesis_decay) as the intent source
                # when it supplied one, so the specific strategic cause reaches
                # the learners; fall back to the generic strategic-close tag.
                de_cause = getattr(de_result, "exit_cause", None) or "decision_engine"
                self._aggregator.submit([Intent.close(
                    symbol=symbol,
                    ticket=order_id,
                    source=de_cause,
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
            elif action_name == Action.MOVE_TO_BREAKEVEN.value:
                # DE explicitly produces MOVE_TO_BREAKEVEN; the executor only
                # understood CLOSE/TIGHTEN_SL/SET_PROTECTIVE_STOP before, so the
                # verdict was journaled then silently dropped. Move SL to entry
                # (use DE's new_sl if it supplied one, else the entry price).
                be_sl = getattr(de_result, "new_sl", None)
                if not be_sl or be_sl <= 0:
                    be_sl = entry_price
                if be_sl and be_sl > 0:
                    pip_s = pip_size
                    self._aggregator.register_position(
                        ticket=order_id, direction=direction,
                        current_sl=sl, pip_size=pip_s,
                    )
                    self._aggregator.submit([Intent.modify_sl(
                        symbol=symbol,
                        ticket=order_id,
                        new_sl=be_sl,
                        source="decision_engine",
                        reason=f"DE breakeven: {getattr(de_result, 'reason', '')[:60]}",
                    )])
            elif action_name == Action.SCALE_IN.value:
                # V13 — route the add-on through the SAME Compliance → Portfolio
                # pipeline as a fresh entry (it was previously a direct
                # Intent.open that bypassed both). Portfolio owns the size.
                self._scale_in_position(
                    pos, price, sl, symbol, direction, de_result,
                    current_score, open_positions,
                )
            elif action_name == Action.PARTIAL_CLOSE.value:
                # V13 — DE/governor can ask to bank part of a position; the
                # verdict was previously journaled then dropped (no handler).
                self._partial_close_position(
                    order_id, symbol, direction, sl, pip_size, de_result,
                )

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

    # ── Scale-in / partial-close handlers (V13) ──────────────────────
    def _scale_in_position(
        self,
        pos,
        price: float,
        sl: float,
        symbol: str,
        direction: str,
        de_result,
        current_score: int,
        open_positions: list,
    ) -> None:
        """Add to a winning position through the full entry pipeline.

        A scale-in is a deliberate same-pair+direction *duplicate*, so it cannot
        simply replay the entry path (Compliance hard-vetoes duplicates). It is
        routed through ``Compliance.permit`` for every OTHER necessary veto
        (market-open, broker, news, spread, daily-loss, heat, drawdown-frozen,
        max-positions, portfolio-risk-state) — the expected ``duplicate`` veto is
        the only failure tolerated — and then sized by ``Portfolio.evaluate`` from
        a reduced base risk so the add-on never re-risks a full position. The
        Portfolio verdict (not the DecisionEngine's advisory ``scale_lots``) is
        the authoritative size.
        """
        ctx = self._ctx
        if ctx is None:
            return
        try:
            tp = float(_broker_tp(pos) or 0.0)
            if tp <= 0.0:
                logger.debug("[de-mgmt] scale-in skipped {} — no take-profit on book", symbol)
                return

            balance = self._pm.get_platform_balance(symbol)
            if not balance or balance <= 0:
                logger.debug("[de-mgmt] scale-in skipped {} — balance unavailable", symbol)
                return
            acct = ctx.account_key(symbol, self._pm)

            # ── Compliance (duplicate-tolerant) ──────────────────────
            if ctx.compliance is None:
                logger.error(
                    "[de-mgmt] scale-in BLOCKED {} — Compliance unavailable (fail-closed)",
                    symbol,
                )
                return
            verdict = ctx.compliance.permit(
                ComplianceCandidate(symbol=symbol, direction=direction),
                ComplianceBook(open_positions=open_positions),
                ComplianceAccount(account_key=acct, balance=balance or 0.0),
            )
            if verdict.rejected:
                blocking = [
                    o for o in verdict.outcomes
                    if not o.passed and o.name != "duplicate"
                ]
                if blocking:
                    logger.info(
                        "[de-mgmt] scale-in BLOCKED {} — Compliance: {}",
                        symbol, "; ".join(o.reason for o in blocking),
                    )
                    return

            # ── Portfolio: size the add-on from a reduced base risk ──
            from portfolio.models import (
                PortfolioAccount as _PFAccount,
                PortfolioCandidate as _PFCandidate,
                SizingFactors as _PFFactors,
            )

            portfolio = ctx.portfolio
            if portfolio is None:
                logger.debug("[de-mgmt] scale-in skipped {} — Portfolio unavailable", symbol)
                return

            risk_pct = _SCALE_IN_RISK_FRACTION * (
                getattr(self._config.risk, "risk_per_trade_pct", 1.0) / 100.0
                if self._config is not None else 0.01
            )
            if ctx.drawdown_guard is not None:
                try:
                    dd_risk = getattr(
                        ctx.drawdown_guard.get_status(), "current_risk_pct", 0.0,
                    ) or 0.0
                    if dd_risk > 0:
                        risk_pct = min(risk_pct, _SCALE_IN_RISK_FRACTION * dd_risk)
                except Exception:
                    pass
            if risk_pct <= 0:
                return

            pip_size = self._safe_pip_size(symbol)
            info = INSTRUMENT_REGISTRY.get(symbol)
            pip_value = info.pip_value_per_lot if info else 10.0
            pctx = build_context_for_symbol(symbol)

            daily_pnl = 0.0
            daily_cap = 0.0
            if ctx.account_risk is not None:
                try:
                    if acct:
                        daily_pnl = float(ctx.account_risk.daily_pnl(acct))
                    daily_cap = float(
                        getattr(ctx.account_risk, "daily_loss_cap_pct", 0.0) or 0.0
                    )
                except Exception:
                    daily_pnl, daily_cap = 0.0, 0.0

            pf_verdict = portfolio.evaluate(
                _PFCandidate(
                    symbol=symbol,
                    direction=direction,
                    entry_price=price,
                    stop_loss=sl,
                    conviction=float(current_score or 0.0),
                    context=pctx,
                    pip_size=pip_size,
                    pip_value_per_lot=pip_value,
                    broker=self._pm.get_platform_name(symbol) if hasattr(self._pm, "get_platform_name") else "",
                ),
                open_positions,
                _PFAccount(
                    balance=balance,
                    account_key=acct,
                    daily_pnl=daily_pnl,
                    daily_loss_cap_pct=daily_cap,
                ),
                _PFFactors(base_risk_pct=risk_pct),
            )
            if not pf_verdict.approved:
                logger.info(
                    "[de-mgmt] scale-in SKIPPED {} — Portfolio: {}",
                    symbol, pf_verdict.reason,
                )
                return

            add_lots = pf_verdict.lots
            add_stake = pf_verdict.stake_usd
            if add_lots <= 0 and add_stake <= 0:
                return

            idem_key = generate_idempotency_key(symbol, direction, add_lots or add_stake)
            comment = build_order_comment("APEX", idem_key, score=current_score)
            self._aggregator.submit([Intent.open(
                symbol=symbol,
                direction=direction,
                lots=add_lots,
                entry_price=price,
                sl=sl,
                tp=tp,
                stake_usd=add_stake if add_stake > 0 else None,
                source="decision_engine_scale_in",
                reason=f"DE scale-in: {getattr(de_result, 'reason', '')[:60]}",
                comment=comment,
                idempotency_key=idem_key,
            )])
            logger.info(
                "[DE-MGMT] {} {} SCALE-IN — {} {:.4g} (risk≈{:.3%})",
                symbol, direction,
                "stake" if add_stake > 0 else "lots",
                add_stake if add_stake > 0 else add_lots,
                pf_verdict.risk_pct,
            )
        except Exception as exc:
            logger.debug("[de-mgmt] scale-in failed for {}: {}", symbol, exc)

    def _partial_close_position(
        self,
        order_id: str,
        symbol: str,
        direction: str,
        sl: float,
        pip_size: float,
        de_result,
    ) -> None:
        """Bank part of an open position on a DE/governor PARTIAL_CLOSE verdict.

        The close fraction comes from the verdict's ``partial_ratio`` when set,
        otherwise a sane default. Routed through the IntentAggregator → executor
        like every other management action (Execution remains the sole gateway).
        """
        try:
            ratio = float(getattr(de_result, "partial_ratio", 0.0) or 0.0)
            if ratio <= 0.0:
                ratio = _DEFAULT_PARTIAL_CLOSE_RATIO
            ratio = max(0.05, min(0.95, ratio))
            self._aggregator.register_position(
                ticket=order_id, direction=direction,
                current_sl=sl, pip_size=pip_size,
            )
            self._aggregator.submit([Intent.partial_close(
                symbol=symbol,
                ticket=order_id,
                fraction=ratio,
                source="decision_engine_partial",
                reason=f"DE partial {ratio:.0%}: {getattr(de_result, 'reason', '')[:60]}",
            )])
            logger.info(
                "[DE-MGMT] {} {} PARTIAL_CLOSE {:.0%} — {}",
                symbol, direction, ratio,
                getattr(de_result, "reason", "")[:80],
            )
        except Exception as exc:
            logger.debug("[de-mgmt] partial-close failed for {}: {}", order_id, exc)

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
                                    pass
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

        # Dedup guard so a closed ticket's realized P&L is booked exactly once
        # across the system-close path (_handle_close_result) and the
        # external-close reconciliation path (_reconcile_external_closes).
        self._closed_tickets: dict[str, float] = {}
        self._closed_tickets_lock = threading.Lock()
        # Last-seen open book for external-close detection:
        # ticket -> {symbol, direction, platform}.
        self._known_open: dict[str, dict] = {}
        self._external_close_attempts: dict[str, int] = {}

        # In-flight management mutations awaiting broker confirmation. The
        # management loop applies an optimistic flag (at_breakeven / partial_-
        # closed / SL move) to prevent re-firing the same action every 100ms,
        # and records the pre-mutation values here keyed by (ticket, type). The
        # execution-result callback commits on success or ROLLS BACK on failure
        # so a rejected modify/partial is retried instead of leaving phantom
        # state (phantom breakeven, an un-taken TP1 partial marked done).
        self._inflight_manage: dict[tuple[str, int], dict[str, Any]] = {}
        self._inflight_manage_lock = threading.Lock()

        # ── Analysis plane ───────────────────────────────────────────
        self._candle_handler = CandleCloseHandler(
            event_bus=self._event_bus,
            world_model_store=self._wm_store,
            candle_fetcher=self._fetch_candles,
            edge_weight=self._zone_edge.zone_weight,
            concept_weight=self._zone_edge.concept_weight,
            vote_calibrator=getattr(ctx, "vote_calibrator", None) if ctx else None,
            module_governor=getattr(ctx, "module_governor", None) if ctx else None,
            win_rate_provider=getattr(ctx, "win_rate_provider", None) if ctx else None,
        )

        # ── Execution plane ──────────────────────────────────────────
        self._aggregator = IntentAggregator(AggregatorConfig())
        self._executor = ActionExecutor(
            broker=self._pm,  # PlatformManager satisfies BrokerPort
            config=ExecutorConfig(),
        )
        # Single execution plane: ALL entries flow through the shared
        # ActionExecutor (RiskGate OPEN validation → CircuitBreaker →
        # broker.execute_entry), serialized on the same executor lock as
        # management actions.  The Execution Division is the sole broker
        # gateway — there is no direct PlatformManager.execute_entry fallback,
        # so no entry can bypass the RiskGate or circuit breaker.
        self._mgmt_store = ManagementStateStore(db_path="data/management_state.db")
        self._evaluator = PositionEvaluator(
            platform_manager=self._pm,
            tick_store=self._tick_store,
            world_model_store=self._wm_store,
            intent_aggregator=self._aggregator,
            mgmt_store=self._mgmt_store,
            worker_config=self._build_worker_config(),
            ctx=ctx,
            config=config,
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
            is_market_open=self._check_market_open,
            is_session_active=self._check_session_active,
            is_news_clear=self._check_news_clear,
            get_spread_pips=self._get_spread_pips,
            get_m1_dataframe=self._get_m1_dataframe,
            on_gate_trace=self._on_gate_trace,
            # Wire the shadow-fed GateTuner into the LIVE entry gate so its
            # learned (bounded) score-bar offset actually modifies live entry
            # decisions — previously the offset only reached the legacy
            # backtest engine and the live EntryGate ignored it.
            gate_tuner=(ctx.gate_tuner if ctx is not None else None),
        )

        # ── Compliance Division runtime binding ──────────────────────
        # The ComplianceDivision is constructed in SystemContext.create()
        # with the subsystem references it owns; bind the broker/platform-
        # dependent callables here (the bootstrap holds the PlatformManager
        # and the broker-truth helpers).  This makes Compliance the single
        # authoritative permit layer consulted in ``_on_entry_decision``.
        if ctx is not None and ctx.compliance is not None:
            try:
                ctx.compliance.bind_runtime(
                    is_market_open=self._check_market_open,
                    is_broker_available=self._is_broker_available,
                    get_spread_pips=self._get_spread_pips,
                    spread_monitor=getattr(ctx.risk_engine, "spread_monitor", None),
                )
            except Exception as exc:
                logger.warning(
                    "[event-driven] ComplianceDivision runtime binding failed: {}",
                    exc,
                )

        # ── Background loops ─────────────────────────────────────────
        # Dedicated bounded pool so the entry path (blocking broker I/O) runs
        # off the tick-poller / EventBus-publishing thread — otherwise each
        # entry stalls tick ingestion for ALL symbols while it executes.
        self._entry_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="entry",
        )
        self._flush_loop = FlushLoop(
            self._aggregator, self._executor, self._pm,
            evaluator=self._evaluator,
            on_close_callback=self._handle_close_result,
            on_manage_callback=self._handle_manage_result,
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
            "candle_close:M1",
            lambda ev: self._entry_pool.submit(
                self._entry_orchestrator.on_m1_close, ev.symbol,
            ),
        )
        self._event_bus.subscribe(
            "world_model_update", self._entry_orchestrator.on_world_model_update,
        )
        self._event_bus.subscribe(
            "world_model_update", self._on_world_model_update,
        )

        self._be_stop_cooldown: dict[str, float] = {}
        self._be_cooldown_seconds = 300.0
        # Re-fire debounce for the ACTIVE consensus entry trigger (Phase 4):
        # last unix-ts a zoneless consensus entry was dispatched per symbol, so
        # a standing thesis is not re-submitted every candle close between fills.
        self._consensus_entry_cooldown: dict[str, float] = {}
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
            open_positions = self._pm.get_all_open_positions()
            pos_by_ticket = {
                str(getattr(p, "order_id", getattr(p, "ticket", ""))): p
                for p in open_positions
            }
            broker_ids = [t for t in pos_by_ticket if t]
            report = run_startup_recovery(get_event_store(), broker_ids)
            # Adopt broker orphans (positions with no entry in the event log,
            # e.g. opened during a crash window) into the log so the persistent
            # lifecycle projection is consistent and the mismatch does not recur
            # on every restart.  Emit a TRADE_OPEN with an audit marker; this is
            # bookkeeping only — it never opens or closes a broker position.
            if report is not None and report.orphan_at_broker:
                es = get_event_store()
                adopted = 0
                for ticket in sorted(report.orphan_at_broker):
                    pos = pos_by_ticket.get(ticket)
                    if pos is None:
                        continue
                    try:
                        es.emit(
                            DE.TRADE_OPEN, "WARNING",
                            symbol=getattr(pos, "symbol", "") or "",
                            source_module="startup_orphan_adoption",
                            payload={
                                "order_id": ticket,
                                "symbol": getattr(pos, "symbol", "") or "",
                                "direction": getattr(pos, "direction", "") or "",
                                "lots": float(getattr(pos, "lots", 0.0) or 0.0),
                                "entry_price": _broker_entry_price(pos),
                                "sl": float(getattr(pos, "sl", 0.0) or 0.0),
                                "tp": _broker_tp(pos),
                                "adopted": True,
                                "reason": "orphan_at_broker_no_log_entry",
                            },
                        )
                        adopted += 1
                    except Exception as exc:
                        logger.debug(
                            "[recovery] orphan adoption emit failed for {}: {}",
                            ticket, exc,
                        )
                if adopted:
                    logger.warning(
                        "[recovery] adopted {} broker orphan position(s) into the "
                        "event log (audit: orphan_at_broker_no_log_entry)", adopted,
                    )
        except Exception as exc:
            logger.debug("[event-driven] event-log reconciliation failed: {}", exc)

        # ── Restore PortfolioGovernor daily tally + loss-cap halt ─────────
        # Must run before the loops so an active daily-loss halt is enforced
        # from the first entry evaluation after a restart.
        self._restore_governor_state()

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
            # Baselines for stall detection — record_tick() only fires when these
            # advance, so a dead loop can no longer mask itself.
            self._wd_last_flush_count = getattr(self._flush_loop, "_flush_count", 0)
            self._wd_last_ticks_routed = getattr(self._tick_router, "ticks_routed", 0)
            # Weekend-awareness: suppress tick-stall CRITICALs during FX closure
            # unless we trade 24/7 synthetics that should always be ticking.
            try:
                setter = getattr(ctx.process_watchdog, "set_market_state_provider", None)
                if callable(setter):
                    _, deriv_symbols = self._classify_symbols()
                    setter(self._is_fx_market_closed, bool(deriv_symbols))
            except Exception as exc:
                logger.debug("[event-driven] watchdog market-state wiring failed: {}", exc)
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True, name="ed-watchdog",
            )
            self._watchdog_thread.start()
            logger.info("[event-driven] process watchdog started")

        # ── Wire SIGTERM/SIGINT/SIGHUP → graceful shutdown ───────────
        # Without this, docker stop / k8s kills the process without flushing
        # stores. register_signal_handlers is a no-op off the main thread.
        try:
            self._shutdown_manager = ShutdownManager(
                self, getattr(self._config, "ops", self._config),
            )
            if self._shutdown_manager.register_signal_handlers():
                logger.info("[event-driven] shutdown signal handlers installed")
        except Exception as exc:
            logger.debug("[event-driven] signal handler registration skipped: {}", exc)

        # ── Initialize TradeJournal (async) ──────────────────────────
        if ctx is not None and ctx.trade_journal is not None:
            try:
                import asyncio
                _loop = asyncio.new_event_loop()
                _loop.run_until_complete(ctx.trade_journal.initialize())
                _loop.close()
            except Exception as exc:
                logger.debug("[startup] TradeJournal init failed: {}", exc)

        # ── Wire the learner trade-history pipeline ──────────────────
        # The adaptive learners (pair/session/regime/score) train on the
        # closed-trade history exposed via ml_adapter.get_trade_history().
        # Back it with the persistent TradeJournal so the learners see the
        # REAL recorded outcomes (previously the call hit a non-existent
        # method, was swallowed, and every learner trained on an empty list).
        # A short TTL cache avoids re-reading the whole journal DB on every
        # tunable adapter within a single tune cycle.
        if (
            ctx is not None
            and ctx.ml_adapter is not None
            and ctx.trade_journal is not None
        ):
            try:
                self._trade_history_cache: list[dict] = []
                self._trade_history_cache_at: float = 0.0
                _journal = ctx.trade_journal

                def _journal_trades() -> list[dict]:
                    now = _time.monotonic()
                    if (
                        self._trade_history_cache
                        and now - self._trade_history_cache_at < 5.0
                    ):
                        return self._trade_history_cache
                    import asyncio
                    loop = asyncio.new_event_loop()
                    try:
                        trades = loop.run_until_complete(
                            _journal.get_all_trades_as_dicts()
                        )
                    finally:
                        loop.close()
                    self._trade_history_cache = list(trades) if trades else []
                    self._trade_history_cache_at = now
                    return self._trade_history_cache

                ctx.ml_adapter.set_trade_history_provider(_journal_trades)
                logger.info(
                    "[event-driven] learner trade-history pipeline wired "
                    "(ml_adapter ← TradeJournal)",
                )
            except Exception as exc:
                logger.warning(
                    "[event-driven] trade-history pipeline wiring failed: {}", exc,
                )

        # Tunable adapters are registered once in __init__ (idempotent by name
        # via TunerAgent.register). The previous duplicate registration here was
        # redundant — the closures (_trades_provider/_prices_provider) resolve
        # their subsystems lazily, so construction-time registration is correct.

        logger.info("[event-driven] all subsystems started")
        logger.info("-" * 60)
        logger.info("  ANALYSIS:  candle-close → brain modules → WorldModel")
        logger.info("  EXECUTION: ticks → position workers → intent aggregator → executor")
        logger.info("  ENTRY:     zones → tick detection → M1 confirm → gates → executor")
        logger.info("-" * 60)

    # ── PortfolioGovernor daily-state persistence ────────────────────────
    # The governor's daily P&L tally + loss-cap halt only lived in memory, so
    # a restart mid-day reset the day's loss budget and lifted any active halt
    # — letting trading resume past the daily loss cap. Persist it to disk and
    # restore on startup so the cap survives crashes/restarts within the day.
    _GOVERNOR_STATE_PATH = "data/governor_state.json"

    def _restore_governor_state(self) -> None:
        ctx = self._ctx
        if ctx is None or ctx.portfolio_governor is None:
            return
        try:
            path = self._GOVERNOR_STATE_PATH
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            # Only restore a same-UTC-day tally; a stale prior-day file must not
            # resurrect yesterday's halt (the day-roll reset owns that).
            saved_day = str(payload.get("utc_day", ""))
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if saved_day and saved_day != today:
                logger.info(
                    "[event-driven] governor state from {} is stale (today {}) "
                    "— starting daily tally fresh", saved_day, today,
                )
                return
            ctx.portfolio_governor.restore_state(payload)
            if payload.get("daily_trading_halted"):
                logger.warning(
                    "[event-driven] restored governor daily-loss HALT from disk "
                    "(daily_pnl={}) — halt persists across restart",
                    payload.get("daily_pnl"),
                )
        except Exception as exc:
            logger.debug("[event-driven] governor state restore failed: {}", exc)

    def _persist_governor_state(self) -> None:
        ctx = self._ctx
        if ctx is None or ctx.portfolio_governor is None:
            return
        try:
            payload = ctx.portfolio_governor.to_state()
            payload["utc_day"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = self._GOVERNOR_STATE_PATH
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except Exception as exc:
            logger.debug("[event-driven] governor state persist failed: {}", exc)

    def stop(self) -> None:
        """Stop all subsystems gracefully."""
        if not self._running:
            return
        self._running = False
        logger.info("[event-driven] shutting down...")

        # Persist the governor daily tally/halt so a restart inside the same
        # UTC day resumes with the correct loss budget.
        self._persist_governor_state()

        self._tick_eval_loop.stop()
        self._flush_loop.stop()
        self._mt5_poller.stop()
        self._deriv_adapter.stop()
        self._tick_router.stop()
        self._candle_handler.shutdown()
        try:
            self._entry_pool.shutdown(wait=False)
        except Exception:
            pass
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

    # Alias used by ShutdownManager.request_shutdown() to suspend the loop on a
    # SIGTERM/SIGINT/SIGHUP so run_forever() falls through to stop().
    @property
    def running(self) -> bool:
        return self._running

    @running.setter
    def running(self, value: bool) -> None:
        self._running = bool(value)

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
                    # Only record a tick when the REAL processing loops have made
                    # progress since the last check. Previously record_tick() was
                    # called unconditionally here, so the watchdog reset its own
                    # stall timer every iteration and check_stall() could never
                    # fire even if the management/flush loops had died.
                    cur_flush = getattr(self._flush_loop, "_flush_count", 0)
                    cur_ticks = getattr(self._tick_router, "ticks_routed", 0)
                    if (
                        cur_flush != self._wd_last_flush_count
                        or cur_ticks != self._wd_last_ticks_routed
                    ):
                        ctx.process_watchdog.record_tick()
                        self._wd_last_flush_count = cur_flush
                        self._wd_last_ticks_routed = cur_ticks
                    ctx.process_watchdog.check_stall()
                except Exception:
                    pass

            # ── Deriv position-store health ───────────────────────────
            # Losing this store leaves multiplier contracts unmanaged. Surface
            # the degraded state loudly so an operator can intervene.
            try:
                deriv = getattr(self._pm, "deriv", None)
                store = getattr(deriv, "_store", None) if deriv is not None else None
                if store is not None and hasattr(store, "is_healthy") and not store.is_healthy():
                    if not getattr(self, "_deriv_store_unhealthy_warned", False):
                        logger.critical(
                            "[event-driven] Deriv position store DEGRADED ({}) — "
                            "multiplier contracts may be unmanaged; manual review required",
                            store.degraded_reason() if hasattr(store, "degraded_reason") else "unknown",
                        )
                        self._deriv_store_unhealthy_warned = True
            except Exception:
                pass

            # ── Broker auto-reconnection ──────────────────────────────
            # Self-heal dropped MT5/Deriv sockets so an unattended system
            # recovers without operator intervention. Non-blocking: only
            # attempts a platform whose backoff window has elapsed.
            try:
                conns = self._pm.check_connections()
                for plat, alive in conns.items():
                    if alive:
                        continue
                    if self._pm.should_attempt_reconnect(plat):
                        ok = self._pm.reconnect_platform(plat)
                        logger.log(
                            "INFO" if ok else "WARNING",
                            "[event-driven] auto-reconnect {} → {}",
                            plat, "recovered" if ok else "failed (will retry)",
                        )
            except Exception as exc:
                logger.debug("[event-driven] auto-reconnect check failed: {}", exc)

            # ── Scanner-blindness watchdog ────────────────────────────
            if ctx is not None and getattr(ctx, "health_watchdog", None) is not None:
                try:
                    blind, reason = ctx.health_watchdog.is_scanner_blind()
                    if blind and not getattr(self, "_scanner_blind_warned", False):
                        logger.critical(
                            "[event-driven] SCANNER BLIND — {} (no fresh "
                            "analysis signals); entries may be starved", reason,
                        )
                        self._scanner_blind_warned = True
                    elif not blind:
                        self._scanner_blind_warned = False
                except Exception:
                    pass

            if ctx is not None and ctx.daily_maintenance is not None:
                try:
                    if ctx.daily_maintenance.should_run():
                        result = ctx.daily_maintenance.run()
                        logger.info("[event-driven] daily maintenance — {}", result)
                        # Day-roll → reset the daily risk silos (portfolio
                        # governor daily P&L/halt + per-account daily loss caps).
                        # These were never reset in the event-driven path, so a
                        # daily-loss halt would persist indefinitely across days.
                        if ctx.portfolio_governor is not None:
                            try:
                                ctx.portfolio_governor.reset_daily()
                            except Exception as exc:
                                logger.debug("[event-driven] governor daily reset failed: {}", exc)
                        if ctx.account_risk is not None:
                            try:
                                ctx.account_risk.reset_daily()
                            except Exception as exc:
                                logger.debug("[event-driven] account-risk daily reset failed: {}", exc)
                        try:
                            es = get_event_store()
                            es.prune()
                        except Exception:
                            pass
                        # Bound shadow-store growth: discard PENDING shadow
                        # contracts older than 48h that will never resolve.
                        try:
                            ss = getattr(ctx, "shadow_store", None)
                            if ss is not None:
                                cutoff_ms = int(
                                    (_time.time() - 48 * 3600) * 1000
                                )
                                discarded = ss.discard_stale_pending(cutoff_ms)
                                if discarded:
                                    logger.info(
                                        "[event-driven] discarded {} stale shadow contract(s)",
                                        discarded,
                                    )
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
        # Drive the portfolio risk state machine + per-account heat gate from
        # live capital-at-risk FIRST, so the canonical heat-response path below
        # acts on a freshly-evaluated state (it only reads ``state``).
        self._apply_portfolio_risk_state(ctx)

        # Canonical heat response (DEFENSIVE/REDUCING/EMERGENCY → intents).
        self._check_portfolio_heat()

        # ── Account risk unrealized P&L tracking ─────────────────────
        if ctx.account_risk is not None:
            try:
                positions = self._pm.get_all_open_positions()
                acct_unrealized: dict[str, float] = defaultdict(float)
                for pos in positions:
                    pnl = _broker_pnl(pos)
                    sym = getattr(pos, "symbol", "")
                    acct = ctx.account_key(sym, self._pm)
                    acct_unrealized[acct] += pnl
                for acct, unrealized in acct_unrealized.items():
                    ctx.account_risk.update_unrealized(acct, unrealized)
            except Exception as exc:
                logger.debug("[heat-mon] account risk unrealized update failed: {}", exc)

        # Persist the governor daily tally/halt every cycle so a hard crash
        # (no clean shutdown) still retains the day's loss budget on restart.
        self._persist_governor_state()

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

    def _register_entry_zone_critical_levels(self) -> None:
        """Register active entry-zone boundaries as tick-store critical levels.

        Boundaries (top/bottom) frame the trigger band the TickEntryDetector
        watches; registering them lets ticks approaching an arming entry bypass
        Hz coalescing. Fully fail-safe — additive registration only.
        """
        try:
            orch = getattr(self, "_entry_orchestrator", None)
            zw = orch.zone_watcher if orch is not None else None
            if zw is None:
                return
            for zsym in zw.all_symbols_with_zones():
                for zone in zw.get_active_zones(zsym):
                    top = float(getattr(zone, "top", 0.0) or 0.0)
                    bottom = float(getattr(zone, "bottom", 0.0) or 0.0)
                    if top > 0:
                        self._tick_store.register_critical_level(zsym, top)
                    if bottom > 0:
                        self._tick_store.register_critical_level(zsym, bottom)
        except Exception as exc:
            logger.debug(
                "[risk-state] entry-zone critical-level resync failed: {}", exc,
            )

    def _apply_portfolio_risk_state(self, ctx: SystemContext) -> None:
        """Compute live capital-at-risk and drive the portfolio risk state
        machine + per-account heat gate.

        Without this, ``PortfolioRiskStateMachine.evaluate`` is never called so
        the SM is frozen at NORMAL and the EMERGENCY/REDUCING/DEFENSIVE response
        handlers in ``_check_portfolio_heat`` can never fire; and
        ``AccountRiskManager.set_heat`` is never called so ``heat_blocked`` is
        always False.  Runs every watchdog cycle.  Fully fail-safe — any failure
        leaves the existing behaviour unchanged.
        """
        if ctx is None:
            return
        try:
            snap = self._pm.get_open_positions_snapshot()
            positions = snap.positions
            failed_platforms = snap.failed_platforms
        except Exception as exc:
            logger.debug("[risk-state] positions fetch failed: {}", exc)
            return
        try:
            from risk.portfolio_risk_state import (
                PortfolioRiskSnapshot,
                PositionRisk,
                compute_position_risk_dollars,
                compute_live_heat_pct,
            )
        except Exception:
            return

        # ── Broker-truth balance reconciliation (B13) ──────────────────
        # Pull the risk engine's running balance back to broker truth every
        # cycle so commission/swap/slippage/manual-trade drift can never
        # accumulate.  Fail-safe — leaves the internal balance untouched on
        # any failure (reconcile_balance ignores None/negative).
        if ctx.risk_engine is not None:
            try:
                broker_balance = self._pm.get_total_balance()
                if broker_balance is not None:
                    ctx.risk_engine.reconcile_balance(broker_balance)
            except Exception as exc:
                logger.debug("[risk-state] balance reconcile failed: {}", exc)

        # ── Broker margin-level stop-out proximity (B14) ───────────────
        # The broker's margin_level is the authoritative stop-out signal.
        # Warn (once, hysteretically) when any account approaches it.
        margin_warned = getattr(self, "_margin_warned", None)
        if margin_warned is None:
            margin_warned = {}
            self._margin_warned = margin_warned
        try:
            for acct, ainfo in self._pm.get_account_summary().items():
                ml = float(getattr(ainfo, "margin_level", 0.0) or 0.0)
                if 0.0 < ml < 150.0:
                    if not margin_warned.get(acct, False):
                        logger.critical(
                            "[risk-state] account {} margin level {:.0f}% — "
                            "approaching broker stop-out", acct, ml,
                        )
                        margin_warned[acct] = True
                elif ml >= 200.0:
                    margin_warned[acct] = False
        except Exception as exc:
            logger.debug("[risk-state] margin-level check failed: {}", exc)

        # ── External-close reconciliation (B16) ────────────────────────
        # Book positions closed at the broker (stop-out / manual / SL/TP)
        # instead of silently dropping their local state.  Broker-confirmed
        # before booking; fully fail-safe.
        try:
            self._reconcile_external_closes(positions, failed_platforms)
        except Exception as exc:
            logger.debug("[risk-state] external-close reconcile failed: {}", exc)

        position_risks: list = []
        acct_risk_dollars: dict[str, float] = defaultdict(float)
        acct_positions: dict[str, list] = defaultdict(list)
        balance_refreshed: set[str] = set()

        # Rebuild the tick-store critical-level set from the live book so ticks
        # approaching an active SL/TP bypass Hz coalescing (they could be
        # silently dropped during volatility spikes otherwise). A full resync
        # each cycle keeps the set current on SL/TP modification and bounded to
        # only live levels — covering register-on-open / update-on-modify /
        # unregister-on-close in one pass.
        try:
            self._tick_store.clear_critical_levels()
        except Exception:
            pass

        # Register active entry-zone boundaries too, so a tick approaching an
        # arming entry trigger isn't coalesced away during a volatility spike
        # (a missed zone touch = a missed entry).
        self._register_entry_zone_critical_levels()

        for pos in positions:
            try:
                symbol = getattr(pos, "symbol", "")
                direction = getattr(pos, "direction", "LONG")
                entry_price = float(getattr(pos, "open_price", 0.0) or 0.0)
                sl = float(getattr(pos, "sl", 0.0) or 0.0)
                tp = float(getattr(pos, "tp", 0.0) or 0.0)
                lots = float(getattr(pos, "lots", 0.0) or 0.0)
                if symbol:
                    try:
                        if sl > 0:
                            self._tick_store.register_critical_level(symbol, sl)
                        if tp > 0:
                            self._tick_store.register_critical_level(symbol, tp)
                    except Exception:
                        pass
                pip_size = self._safe_pip_size(symbol)
                info = INSTRUMENT_REGISTRY.get(symbol)
                pip_value = info.pip_value_per_lot if info else 10.0
                is_long = str(direction).upper() in ("BUY", "LONG")
                at_be = (
                    (sl >= entry_price if is_long else sl <= entry_price)
                    if (sl > 0 and entry_price > 0) else False
                )
                risk_dollars, is_fallback = compute_position_risk_dollars(
                    direction=direction,
                    entry_price=entry_price,
                    sl=sl,
                    lots=lots,
                    pip_size=pip_size,
                    pip_value_per_lot=pip_value,
                    at_breakeven=at_be,
                )
                position_risks.append(PositionRisk(
                    order_id=str(getattr(pos, "order_id", "")),
                    symbol=symbol,
                    direction=direction,
                    risk_dollars=risk_dollars,
                    is_at_breakeven=at_be,
                    is_fallback=is_fallback,
                ))
                if ctx.account_risk is not None:
                    acct = ctx.account_key(symbol, self._pm)
                    acct_risk_dollars[acct] += risk_dollars
                    acct_positions[acct].append(pos)
                    if acct not in balance_refreshed:
                        balance_refreshed.add(acct)
                        try:
                            bal = self._pm.get_platform_balance(symbol)
                            if bal and bal > 0:
                                ctx.account_risk.update_balance(acct, bal)
                        except Exception:
                            pass
            except Exception as exc:
                logger.debug("[risk-state] position risk calc failed: {}", exc)
                continue

        # ── Per-account heat gate (feeds AccountRiskManager.heat_blocked) ──
        if ctx.account_risk is not None:
            try:
                for acct, risk_d in acct_risk_dollars.items():
                    bal = ctx.account_risk.balance(acct)
                    heat_pct = (risk_d / bal * 100.0) if bal > 0 else 0.0
                    ctx.account_risk.set_heat(acct, heat_pct)
            except Exception as exc:
                logger.debug("[risk-state] account heat set failed: {}", exc)

            # ── Daily flatten cap → close intents for breached accounts ──
            try:
                fx_closed = self._is_fx_market_closed()
                for acct, acct_pos in acct_positions.items():
                    if not ctx.account_risk.flatten_breached(acct):
                        continue
                    submitted = 0
                    deferred = 0
                    for pos in acct_pos:
                        ticket = str(getattr(pos, "order_id", "") or "")
                        sym = getattr(pos, "symbol", "")
                        if not ticket:
                            continue
                        # Don't spin closing session-gated instruments while the
                        # market is closed (weekend): the broker rejects with
                        # "Market closed", which would otherwise loop every cycle
                        # and trip the executor circuit breaker.  24/7 synthetic
                        # indices are always closeable and proceed normally.
                        if fx_closed and not is_always_open(sym):
                            deferred += 1
                            continue
                        self._aggregator.submit([Intent.close(
                            symbol=sym,
                            ticket=ticket,
                            source="account_risk",
                            reason="account_flatten_cap_breached",
                        )])
                        submitted += 1
                    if submitted:
                        logger.warning(
                            "[risk-state] account {} flatten cap breached — "
                            "{} CLOSE intents submitted", acct, submitted,
                        )
                    if deferred:
                        logger.warning(
                            "[risk-state] account {} flatten cap breached — "
                            "{} close(s) deferred (market closed, will retry on "
                            "session open)", acct, deferred,
                        )
            except Exception as exc:
                logger.debug("[risk-state] flatten-cap close failed: {}", exc)

        # ── Portfolio risk state machine ──
        if ctx.portfolio_risk_sm is not None:
            try:
                equity = 0.0
                try:
                    equity = float(self._pm.get_total_equity() or 0.0)
                except Exception:
                    equity = 0.0
                if equity <= 0 and ctx.account_risk is not None:
                    equity = ctx.account_risk.total_balance()
                heat_pct = (
                    compute_live_heat_pct(position_risks, equity)
                    if equity > 0 else 0.0
                )
                corr_safe, max_exposure = self._portfolio_correlation_state(positions)
                snapshot = PortfolioRiskSnapshot(
                    live_heat_pct=heat_pct,
                    position_risks=position_risks,
                    correlation_safe=corr_safe,
                    max_currency_exposure=max_exposure,
                )
                # Ladder + de-escalation (NORMAL ↔ DEFENSIVE ↔ REDUCING).
                ctx.portfolio_risk_sm.evaluate(snapshot)

                # Hard emergency triggers force-escalate from any state.
                from risk.portfolio_risk_state import EmergencyTriggerResult
                trig = EmergencyTriggerResult()
                emerg_pct = getattr(
                    ctx.portfolio_risk_sm, "heat_emergency_pct", 4.0,
                )
                if heat_pct >= emerg_pct:
                    trig.extreme_heat = True
                if ctx.drawdown_guard is not None:
                    try:
                        from brain.drawdown_guard import DrawdownMode
                        if ctx.drawdown_guard.get_status().mode == DrawdownMode.FROZEN.value:
                            trig.drawdown_frozen = True
                    except Exception:
                        pass
                if trig.any_fired:
                    ctx.portfolio_risk_sm.escalate_to_emergency(
                        trig, heat_pct, corr_safe,
                    )
            except Exception as exc:
                logger.debug("[risk-state] portfolio SM evaluate failed: {}", exc)

    def _portfolio_correlation_state(self, positions: list) -> tuple[bool, float]:
        """Return (correlation_safe, max_currency_exposure) for the open book.

        Uses the correlation engine when available; defaults to *safe* so a
        missing/failed correlation read never spuriously trips the SM into
        DEFENSIVE (heat remains the primary driver).
        """
        ctx = self._ctx
        if ctx is None or getattr(ctx, "correlation_engine", None) is None:
            return True, 0.0
        try:
            from brain.correlation_engine import OpenTrade
            trades = [
                OpenTrade(
                    pair=getattr(p, "symbol", ""),
                    direction=getattr(p, "direction", "LONG"),
                    risk_pct=0.02,
                )
                for p in positions
                if getattr(p, "symbol", "")
            ]
            exposure_fn = getattr(ctx.correlation_engine, "max_currency_exposure", None)
            if callable(exposure_fn):
                max_exp = float(exposure_fn(trades) or 0.0)
                return (max_exp < 1.0), max_exp
        except Exception as exc:
            logger.debug("[risk-state] correlation read failed: {}", exc)
        return True, 0.0

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
                    pnl = _broker_pnl(pos)
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
                    entry_price = _broker_entry_price(pos)
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

    def _mark_booked(self, ticket: str) -> bool:
        """Claim a ticket for P&L booking exactly once.

        Returns True if this caller is the first to book the ticket, False if
        it was already booked (caller should skip to avoid a double count).
        Thread-safe; bounded.
        """
        if not ticket:
            return True
        key = str(ticket)
        now = _time.monotonic()
        with self._closed_tickets_lock:
            if len(self._closed_tickets) > 256:
                cutoff = now - 3600.0
                self._closed_tickets = {
                    t: ts for t, ts in self._closed_tickets.items() if ts > cutoff
                }
            if key in self._closed_tickets:
                return False
            self._closed_tickets[key] = now
            return True

    def _book_external_close(self, ticket: str, meta: dict) -> bool:
        """Book a position closed outside the system using broker truth.

        Returns True when the close is confirmed by the broker's deal history
        and booked (or was already booked by the system-close path); False when
        the close cannot be confirmed yet (transient fetch failure, reconnect,
        or a platform without deal history) so the caller retries next cycle.
        Requiring broker confirmation before booking prevents a transient empty
        position fetch from false-booking live positions as closed.
        """
        platform = (meta.get("platform") or "") if isinstance(meta, dict) else ""
        deal_info = None
        if platform:
            try:
                deal_info = self._pm.get_deal_close_info(str(ticket), platform)
            except Exception as exc:
                logger.warning(
                    "[external-close] deal info fetch failed {}: {}", ticket, exc,
                )
                return False
        if deal_info is None:
            return False  # cannot confirm — retry later

        if not self._mark_booked(str(ticket)):
            return True  # already booked by the system-close path

        symbol = meta.get("symbol", "") if isinstance(meta, dict) else ""
        direction = meta.get("direction", "") if isinstance(meta, dict) else ""
        pnl_dollars = float(getattr(deal_info, "pnl", 0.0) or 0.0)
        broker_commission = float(getattr(deal_info, "commission", 0.0) or 0.0)
        broker_swap = float(getattr(deal_info, "swap", 0.0) or 0.0)
        broker_fee = float(getattr(deal_info, "fee", 0.0) or 0.0)
        close_price = float(getattr(deal_info, "close_price", 0.0) or 0.0)
        exit_reason = getattr(deal_info, "exit_reason", None) or "EXTERNAL_CLOSE"
        raw_broker_reason = getattr(deal_info, "raw_reason_code", None)

        pnl_pips = 0.0
        try:
            pip_size = self._safe_pip_size(symbol)
            entry = float(
                (self._entry_context.get(ticket) or {}).get("entry_price", 0.0)
                or 0.0
            )
            if entry > 0 and close_price > 0 and pip_size > 0:
                if str(direction).upper() in ("BUY", "LONG"):
                    pnl_pips = (close_price - entry) / pip_size
                else:
                    pnl_pips = (entry - close_price) / pip_size
        except Exception:
            pass

        logger.warning(
            "[external-close] {} {} closed at broker (reason={}, pnl=${:.2f}) "
            "— booking realized outcome", symbol, ticket, exit_reason, pnl_dollars,
        )
        self._on_trade_closed(
            symbol=symbol,
            direction=direction,
            pnl_dollars=pnl_dollars,
            pnl_pips=pnl_pips,
            ticket=ticket,
            close_price=close_price,
            exit_reason=exit_reason,
            exit_reason_source="broker",
            raw_broker_reason=raw_broker_reason,
            commission=broker_commission,
            swap=broker_swap,
            fee=broker_fee,
        )
        try:
            self._mgmt_store.remove(str(ticket))
        except Exception:
            pass
        return True

    def _reconcile_external_closes(
        self, positions: list, failed_platforms: Optional[set] = None
    ) -> None:
        """Detect positions closed at the broker (SL/TP/stop-out/manual) and
        book their realized outcome instead of silently dropping local state.

        Broker-confirmed before booking, so a transient empty/partial position
        fetch never false-books live positions.  Unconfirmed disappearances are
        retried; after a bounded number of cycles they are dropped with a
        CRITICAL log so a stuck ticket cannot leak forever.

        ``failed_platforms`` (from the broker snapshot) lists platforms whose
        position fetch errored this cycle.  A ticket on a failed platform is
        *unknown*, not absent — it is held in tracking and never booked as
        closed, so a transient connector failure can never false-book a live
        position on the other (healthy) platform.
        """
        failed = failed_platforms or set()
        current: dict[str, dict] = {}
        for pos in positions:
            ticket = str(
                getattr(pos, "order_id", getattr(pos, "ticket", "")) or ""
            )
            if not ticket:
                continue
            current[ticket] = {
                "symbol": getattr(pos, "symbol", ""),
                "direction": getattr(pos, "direction", ""),
                "platform": getattr(pos, "platform", ""),
            }

        vanished = [t for t in self._known_open if t not in current]
        still_pending: dict[str, dict] = {}
        for ticket in vanished:
            meta = self._known_open.get(ticket, {})
            # A ticket whose owning platform failed to report this cycle is
            # unknown — never book it as closed.  Hold it in tracking and reset
            # the debounce counter so the next clean cycle starts fresh.
            if failed and (meta.get("platform") or "") in failed:
                still_pending[ticket] = meta
                self._external_close_attempts.pop(ticket, None)
                continue
            attempts = self._external_close_attempts.get(ticket, 0) + 1
            self._external_close_attempts[ticket] = attempts
            # Debounce: require the ticket to be absent for at least two
            # consecutive cycles before attempting to book, so a single
            # transient empty/partial position fetch (e.g. mid-reconnect) can
            # never false-book a still-open position as closed.
            if attempts < 2:
                still_pending[ticket] = meta
                continue
            if self._book_external_close(ticket, meta):
                self._external_close_attempts.pop(ticket, None)
                continue
            if attempts >= 12:
                logger.critical(
                    "[external-close] {} ({}) vanished but the broker close "
                    "could not be confirmed after {} cycles — dropping "
                    "tracking; P&L NOT booked, manual reconciliation required",
                    ticket, meta.get("symbol", "?"), attempts,
                )
                self._external_close_attempts.pop(ticket, None)
            else:
                still_pending[ticket] = meta

        self._known_open = {**current, **still_pending}

    def _handle_close_result(self, intent: Intent, result: Any) -> None:
        """Called by FlushLoop when a CLOSE intent succeeds.

        Extracts P&L from the broker response and feeds it to the
        risk feedback chain.
        """
        symbol = getattr(intent, "symbol", "") or ""
        direction = getattr(intent, "direction", "")
        ticket = intent.position_ticket

        # CLOSE intents historically carried no direction, so every learner fed
        # off this path received pnl_pips=0 and an R-multiple of 0 — silently
        # corrupting all adaptive learning. Recover the direction from the
        # last-known open book (snapshotted each reconcile cycle) when the
        # intent itself does not carry it.
        if not direction:
            meta = self._known_open.get(ticket, {}) if ticket else {}
            direction = (meta.get("direction") if isinstance(meta, dict) else "") or ""
            if not symbol and isinstance(meta, dict):
                symbol = meta.get("symbol", "") or ""

        pnl_dollars = 0.0
        pnl_pips = 0.0
        close_price = 0.0
        exit_reason: Optional[str] = None
        exit_reason_source = "event_driven"
        raw_broker_reason: Optional[int] = None
        broker_commission = 0.0
        broker_swap = 0.0
        broker_fee = 0.0

        resp = getattr(result, "broker_response", None)
        platform = getattr(resp, "platform", "") if resp is not None else ""
        if resp is not None:
            pnl_dollars = float(getattr(resp, "pnl", 0.0) or 0.0)
            close_price = float(getattr(resp, "close_price", 0.0) or 0.0)

        # Broker truth: realized P&L (includes commission + swap + fee), the
        # actual fill price and the broker's exit reason from deal history.
        # Fail-safe — falls back to the CloseResult / local values on any
        # failure (e.g. Deriv, timeout, ticket not yet in history).  Never
        # blocks the close-feedback path.
        deal_info = None
        if ticket and platform:
            try:
                deal_info = self._pm.get_deal_close_info(str(ticket), platform)
            except Exception as exc:
                logger.warning(
                    "[close] broker deal info fetch failed {}: {}", ticket, exc,
                )
        if deal_info is not None:
            di_pnl = getattr(deal_info, "pnl", None)
            if di_pnl is not None:
                pnl_dollars = float(di_pnl)
            broker_commission = float(getattr(deal_info, "commission", 0.0) or 0.0)
            broker_swap = float(getattr(deal_info, "swap", 0.0) or 0.0)
            broker_fee = float(getattr(deal_info, "fee", 0.0) or 0.0)
            di_close = getattr(deal_info, "close_price", None)
            if di_close:
                close_price = float(di_close)
            di_reason = getattr(deal_info, "exit_reason", None)
            if di_reason:
                exit_reason = di_reason
                exit_reason_source = "broker"
                raw_broker_reason = getattr(deal_info, "raw_reason_code", None)

        if symbol and direction:
            try:
                pip_size = self._safe_pip_size(symbol)
                entry = float(
                    (self._entry_context.get(ticket) or {}).get("entry_price", 0.0)
                    or 0.0
                )
                if entry > 0 and close_price > 0 and pip_size > 0:
                    if direction.upper() in ("BUY", "LONG"):
                        pnl_pips = (close_price - entry) / pip_size
                    else:
                        pnl_pips = (entry - close_price) / pip_size
            except Exception:
                pass

        # When the broker deal history did not supply an exit reason, fall back
        # to the close intent's own structured source (e.g. "stop_loss",
        # "conviction_collapse", "tp2_target", "structure_trailing") so the
        # management *cause* reaches the learners instead of being lost. This is
        # what lets _on_trade_closed normalise to a typed ExitCause (V14).
        if not exit_reason:
            intent_cause = (
                getattr(intent, "source", "") or getattr(intent, "reason", "")
            )
            if intent_cause:
                exit_reason = intent_cause
                exit_reason_source = "management"

        # Book exactly once — the external-close reconciler may race this path.
        if not self._mark_booked(str(ticket)):
            return

        self._on_trade_closed(
            symbol=symbol,
            direction=direction,
            pnl_dollars=pnl_dollars,
            pnl_pips=pnl_pips,
            ticket=ticket,
            close_price=close_price,
            exit_reason=exit_reason,
            exit_reason_source=exit_reason_source,
            raw_broker_reason=raw_broker_reason,
            commission=broker_commission,
            swap=broker_swap,
            fee=broker_fee,
        )

        # Now that the broker-confirmed close is booked, drop management state.
        # (Removal moved here from the optimistic management-loop path so a
        # FAILED close keeps the position managed and retried.)
        try:
            self._mgmt_store.remove(str(ticket))
        except Exception:
            pass
        self._clear_inflight_manage_ticket(str(ticket))

    def _record_inflight_manage(
        self, ticket: str, intent_type: IntentType, prev: dict[str, Any],
    ) -> None:
        """Record pre-mutation management values for rollback on exec failure."""
        try:
            with self._inflight_manage_lock:
                self._inflight_manage[(str(ticket), int(intent_type))] = prev
        except Exception:
            pass

    def _clear_inflight_manage_ticket(self, ticket: str) -> None:
        """Drop all in-flight rollback snapshots for a (now closed) ticket."""
        try:
            with self._inflight_manage_lock:
                for key in [k for k in self._inflight_manage if k[0] == str(ticket)]:
                    self._inflight_manage.pop(key, None)
        except Exception:
            pass

    def _handle_manage_result(self, intent: Intent, result: Any) -> None:
        """Commit or roll back an optimistic management mutation.

        Called by FlushLoop for every executed MODIFY_SL / MODIFY_TP /
        PARTIAL_CLOSE intent (success and failure). On success the optimistic
        state stands and the snapshot is dropped. On a real failure (anything
        other than an expected market-closed skip) the pre-mutation values are
        restored so the action re-fires next cycle instead of leaving phantom
        state.
        """
        try:
            ticket = str(getattr(intent, "position_ticket", "") or "")
            itype = getattr(intent, "intent_type", None)
            if not ticket or itype is None:
                return
            key = (ticket, int(itype))
            with self._inflight_manage_lock:
                prev = self._inflight_manage.pop(key, None)
            success = bool(getattr(result, "success", False))
            if success or prev is None:
                return
            err = str(getattr(result, "error", "") or "").lower()
            if "market_closed" in err or "market closed" in err or "market is closed" in err:
                # Expected: market shut. Keep the optimistic guard so we do not
                # hammer a closed market; it will reconcile when the market opens
                # (a fresh evaluation re-derives the correct state).
                return
            mgmt = self._mgmt_store.get(ticket)
            if mgmt is None:
                return
            for field_name, value in prev.items():
                try:
                    setattr(mgmt, field_name, value)
                except Exception:
                    pass
            try:
                self._mgmt_store.persist(mgmt, force=True)
            except Exception:
                pass
            logger.warning(
                "[manage] {} {} failed — rolled back optimistic state ({})",
                itype.name, ticket, getattr(result, "error", ""),
            )
        except Exception as exc:
            logger.debug("[manage] result handling failed: {}", exc)

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
                entry_price = _broker_entry_price(pos)
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
                    tp1=_broker_tp(pos),
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

    def _symbol_spec(self, symbol: str) -> dict:
        """Broker symbol spec (cached), or ``{}`` when unavailable.

        Specs rarely change during a session, so the first successful lookup
        per symbol is cached.  Fully fail-safe — returns ``{}`` on any error so
        callers fall back to config-derived values.
        """
        cache = getattr(self, "_symbol_spec_cache", None)
        if cache is None:
            cache = {}
            self._symbol_spec_cache = cache
        if symbol in cache:
            return cache[symbol]
        spec: dict = {}
        try:
            connector = self._pm.get_connector(symbol)
            spec_fn = getattr(connector, "get_symbol_spec", None)
            if callable(spec_fn):
                got = spec_fn(symbol)
                if isinstance(got, dict):
                    spec = got
        except Exception as exc:
            logger.debug("[symbol-spec] lookup failed for {}: {}", symbol, exc)
        # Only cache non-empty specs so a transient failure is retried later.
        if spec:
            cache[symbol] = spec
        return spec

    def _broker_pip_value(
        self, symbol: str, pip_size: float, fallback: float,
    ) -> float:
        """Broker-truth money-per-pip-per-lot from the symbol spec.

        Derived as ``tick_value * (pip_size / tick_size)``.  Returns
        ``fallback`` (config value) on any gap so sizing/P&L never break.
        """
        try:
            spec = self._symbol_spec(symbol)
            tick_value = spec.get("trade_tick_value")
            tick_size = spec.get("trade_tick_size")
            if (
                tick_value and tick_size and tick_size > 0
                and pip_size and pip_size > 0
            ):
                pv = float(tick_value) * (float(pip_size) / float(tick_size))
                if pv > 0:
                    return pv
        except Exception as exc:
            logger.debug("[symbol-spec] pip-value derive failed {}: {}", symbol, exc)
        return fallback

    def _snap_to_broker_volume(self, symbol: str, lots: float) -> float:
        """Snap a lot size to the broker's volume_min/max/step.

        Prevents broker rejection from an unaligned volume.  No-op (returns
        ``lots`` unchanged) when the spec is unavailable or ``lots <= 0``.
        """
        if lots <= 0:
            return lots
        try:
            spec = self._symbol_spec(symbol)
            if not spec:
                return lots
            vmin = spec.get("volume_min")
            vmax = spec.get("volume_max")
            vstep = spec.get("volume_step")
            snapped = lots
            if vstep and float(vstep) > 0:
                step = float(vstep)
                snapped = round(round(snapped / step) * step, 8)
            if vmin and snapped < float(vmin):
                snapped = float(vmin)
            if vmax and float(vmax) > 0 and snapped > float(vmax):
                snapped = float(vmax)
            if snapped > 0:
                return snapped
        except Exception as exc:
            logger.debug("[symbol-spec] volume snap failed {}: {}", symbol, exc)
        return lots

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
        """SessionEngine callback for EntryOrchestrator gate.

        24/7 instruments (Deriv synthetic indices) never close — they bypass
        the FX session / weekend gate entirely.  For session-gated (FX) symbols
        we additionally block new entries near the Friday close and during the
        gap-prone first minutes of the Sunday FX reopen.
        """
        ctx = self._ctx
        if ctx is None or ctx.session_engine is None:
            return True
        # 24/7 synthetics — never gate on session or weekend status.
        try:
            if is_always_open(symbol):
                return True
        except Exception:
            pass
        try:
            session_engine = ctx.session_engine
            status = session_engine.get_status()
            if not status.is_tradeable:
                return False
            # Weekend-edge buffers only apply to session-gated (FX) instruments.
            if is_session_gated(symbol):
                risk_cfg = getattr(self._config, "risk", None)
                close_buffer = int(getattr(risk_cfg, "weekend_close_buffer_minutes", 15))
                mins_to_close = session_engine.minutes_to_fx_close()
                if mins_to_close < close_buffer:
                    logger.info(
                        "[session-gate] {} — blocking entry, {}min to FX weekend close",
                        symbol, mins_to_close,
                    )
                    return False
                # Sunday-open gap window: block the first N minutes after the
                # Sunday 22:00 UTC FX reopen (wide spreads / gap risk).
                sunday_buffer = int(getattr(risk_cfg, "sunday_open_buffer_minutes", 30))
                now = datetime.now(timezone.utc)
                if now.weekday() == 6 and now.hour == 22 and now.minute < sunday_buffer:
                    logger.info(
                        "[session-gate] {} — blocking entry, Sunday-open gap window",
                        symbol,
                    )
                    return False
            return status.is_tradeable
        except Exception as exc:
            logger.debug("[session-gate] SessionEngine check failed: {}", exc)
            return True

    def _is_fx_market_closed(self) -> bool:
        """True when the FX market is closed (weekend) per the SessionEngine.

        Used to suppress the process watchdog's tick-stall alert during the
        weekend, when FX ticks legitimately stop.
        """
        ctx = self._ctx
        if ctx is None or ctx.session_engine is None:
            return False
        try:
            return ctx.session_engine.get_status().current_session == "WEEKEND"
        except Exception:
            return False

    def _build_worker_config(self) -> WorkerConfig:
        """Build the PositionWorker config, mapping AppConfig weekend-protection
        settings onto the worker so live config changes take effect.

        Defensive: only primitive values are consumed (tests construct the
        system with a MagicMock config, which would otherwise inject mocks).
        """
        cfg = WorkerConfig()
        risk_cfg = getattr(self._config, "risk", None)
        if risk_cfg is None:
            return cfg
        enabled = getattr(risk_cfg, "weekend_protection_enabled", None)
        if isinstance(enabled, bool):
            cfg.weekend_protection_enabled = enabled
        mode = getattr(risk_cfg, "weekend_protection_mode", None)
        if isinstance(mode, str):
            cfg.weekend_protection_mode = mode
        buffer = getattr(risk_cfg, "weekend_close_buffer_minutes", None)
        if isinstance(buffer, int) and not isinstance(buffer, bool):
            cfg.weekend_close_buffer_minutes = buffer
        friday_hour = getattr(risk_cfg, "friday_close_hour_utc", None)
        if isinstance(friday_hour, int) and not isinstance(friday_hour, bool):
            cfg.friday_close_hour_utc = friday_hour
        return cfg

    def _check_market_open(self, symbol: str) -> bool:
        """Broker-truth market-open gate for the EntryOrchestrator.

        24/7 instruments (Deriv synthetics) are always open.  For everything
        else we consult the broker's symbol ``trade_mode`` (MT5): CLOSEONLY (3)
        or DISABLED (0) means the market is closed and new entries are blocked.
        When the broker spec is unavailable (tests / dev) we pass through — the
        SessionEngine gate still applies.
        """
        try:
            if is_always_open(symbol):
                return True
        except Exception:
            pass
        try:
            connector = self._pm.get_connector(symbol)
        except Exception:
            return True
        spec_fn = getattr(connector, "get_symbol_spec", None)
        if not callable(spec_fn):
            return True
        try:
            spec = spec_fn(symbol)
        except Exception as exc:
            logger.debug("[market-open] symbol spec lookup failed for {}: {}", symbol, exc)
            return True
        mode = spec.get("trade_mode") if isinstance(spec, dict) else None
        if mode is None:
            return True
        # 0 = DISABLED, 3 = CLOSEONLY — market not open for new positions.
        if mode in (0, 3):
            logger.info("[market-open] {} closed — broker trade_mode={}", symbol, mode)
            return False
        return True

    def _is_broker_available(self, symbol: str) -> bool:
        """Broker-health permit signal for the ComplianceDivision.

        True when the connector routing ``symbol`` reports a live connection.
        When the connector or its state can't be determined we return True
        (the downstream ActionExecutor circuit breaker is the authoritative
        execution guard — Compliance should not false-block on an unknown).
        """
        try:
            connector = self._pm.get_connector(symbol)
        except Exception:
            return True
        is_conn = getattr(connector, "is_connected", None)
        if not callable(is_conn):
            return True
        try:
            return bool(is_conn())
        except Exception as exc:
            logger.debug("[broker-health] {} is_connected check failed: {}", symbol, exc)
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
        # A WorldModel update means the analysis/scan pipeline is alive — feed
        # the health watchdog so is_scanner_blind() reflects real liveness.
        if getattr(ctx, "health_watchdog", None) is not None:
            try:
                ctx.health_watchdog.record_scan_success()
            except Exception:
                pass
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

        # ── Consensus Division: ACTIVE market-driven entry trigger ────
        # The big flip — when enabled, a sufficiently convicted consensus
        # thesis initiates an entry on its own, with no structural zone
        # required. Best-effort and fully guarded so it can never disrupt the
        # analysis/feed path above; a no-op unless the operator turns it on.
        try:
            sym = getattr(event, "symbol", "")
            if sym:
                self._evaluate_consensus_entry(sym)
        except Exception as exc:
            logger.debug("[consensus-trigger] evaluation failed: {}", exc)

    def _evaluate_consensus_entry(self, symbol: str) -> None:
        """Consensus Division — ACTIVE, market-driven entry trigger (Phase 4).

        Independent of any structural zone: when the full analyst panel forms a
        thesis whose conviction clears ``ConsensusConfig.conviction_threshold``,
        the Consensus Division itself initiates an entry candidate. Direction
        comes from the weighted vote (never from a zone bias), SL/TP are
        ATR-derived, and the candidate flows through the SAME
        ``_on_entry_decision`` pipeline (Compliance → Portfolio → Execution) as
        every other entry. Gated behind ``active_trigger_enabled`` so it is
        behaviour-neutral until the operator enables it.

        There is deliberately no frequency governor — only a re-fire debounce
        (``trigger_cooldown_seconds``); the Learning Division tightens
        conviction organically through outcomes, and Compliance owns the
        authoritative duplicate / risk vetoes.
        """
        cfg = getattr(self._config, "consensus", None)
        if cfg is None or not getattr(cfg, "active_trigger_enabled", False):
            return

        try:
            wm = self._wm_store.get(symbol)
        except Exception:
            wm = None
        if wm is None:
            return
        votes = list(getattr(wm, "votes", ()) or [])
        if not votes:
            return

        from brain.directional_consensus import form_thesis

        try:
            thesis = form_thesis(
                votes,
                min_net_score=cfg.min_net_score,
                min_agreement=cfg.min_agreement,
                high_authority_modules=list(cfg.high_authority_modules),
                high_authority_oppose_confidence=cfg.high_authority_oppose_confidence,
                min_contributors=cfg.min_contributors,
                conviction_threshold=cfg.conviction_threshold,
                net_scale=cfg.net_scale,
            )
        except Exception as exc:
            logger.debug("[consensus-trigger] {} thesis failed: {}", symbol, exc)
            return

        # Always log the thesis (visibility) — even when it does not trigger.
        logger.debug("[consensus-trigger] {} {}", symbol, thesis.summary)
        if not thesis.trigger:
            return

        direction = thesis.direction  # "LONG" | "SHORT"

        # Re-fire debounce: avoid resubmitting the same standing thesis every
        # candle. Compliance still enforces the authoritative duplicate veto.
        now = _time.time()
        if now < self._consensus_entry_cooldown.get(symbol, 0.0):
            return

        # Cheap pre-check: skip if we already hold this pair in this direction.
        try:
            long_aliases = {"LONG", "BUY"}
            short_aliases = {"SHORT", "SELL"}
            want = long_aliases if direction == "LONG" else short_aliases
            for p in (self._pm.get_all_open_positions() or []):
                if isinstance(p, dict):
                    psym = str(p.get("symbol", ""))
                    pdir = str(p.get("direction", "") or p.get("type", ""))
                else:
                    psym = str(getattr(p, "symbol", ""))
                    pdir = str(getattr(p, "direction", "") or getattr(p, "type", ""))
                if psym == symbol and pdir.upper() in want:
                    return
        except Exception:
            pass

        # Reference price + ATR for a zoneless SL/TP. The order fills at market;
        # these define risk geometry only.
        m5 = self._fetch_candles(symbol, "M5", max(60, int(cfg.atr_period) + 20))
        if m5 is None or len(m5) < max(15, int(cfg.atr_period) + 1):
            return
        try:
            entry_price = float(m5["close"].iloc[-1])
        except Exception:
            return
        if entry_price <= 0:
            return

        try:
            from brain.volatility_stop import latest_atr
            atr = latest_atr(m5, int(cfg.atr_period))
        except Exception as exc:
            logger.debug("[consensus-trigger] {} ATR failed: {}", symbol, exc)
            return
        if not atr or atr <= 0:
            return

        sl_dist = float(atr) * float(cfg.atr_sl_mult)
        if sl_dist <= 0:
            return
        if direction == "LONG":
            sl = entry_price - sl_dist
            tp1 = entry_price + sl_dist * float(cfg.atr_tp1_rr)
            tp2 = entry_price + sl_dist * float(cfg.atr_tp2_rr)
        else:
            sl = entry_price + sl_dist
            tp1 = entry_price - sl_dist * float(cfg.atr_tp1_rr)
            tp2 = entry_price - sl_dist * float(cfg.atr_tp2_rr)

        score = int(round(max(0.0, min(1.0, thesis.conviction)) * 100))
        try:
            risk_pips = abs(entry_price - sl) / self._safe_pip_size(symbol)
        except Exception:
            risk_pips = 0.0
        try:
            spread_pips = self._get_spread_pips(symbol)
        except Exception:
            spread_pips = 0.0

        decision = {
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": round(sl, 8),
            "tp1": round(tp1, 8),
            "tp2": round(tp2, 8),
            "conviction": score,
            "zone_type": "",
            "timeframe": "M5",
            "source": "consensus",
            "consensus_conviction": thesis.conviction,
            "risk_pips": risk_pips,
            "spread_pips": spread_pips,
        }

        logger.info(
            "[consensus-trigger] {} {} thesis conviction={:.2f} → ENTRY @ {:.5f} "
            "SL={:.5f} TP={:.5f} (zoneless, market-driven)",
            symbol, direction, thesis.conviction, entry_price, sl, tp1,
        )
        self._consensus_entry_cooldown[symbol] = now + float(
            getattr(cfg, "trigger_cooldown_seconds", 300.0)
        )
        try:
            self._on_entry_decision(decision)
        except Exception:
            logger.exception("[consensus-trigger] {} entry dispatch failed", symbol)
        """Handle entry decisions from EntryOrchestrator.

        Permit / sizing pipeline before an order is placed:
        0. Operational guards — BE-stop cooldown, system-paused.
        1. ComplianceDivision — the single authoritative permit layer
           (market-open, broker-available, news, spread, duplicate, daily-loss
           [per-account silo, one source], heat, DrawdownGuard FROZEN,
           max-positions, PortfolioRisk DEFENSIVE+).  Fail-CLOSED.
        2. PortfolioGovernor — portfolio CONCENTRATION only (currency / sector
           / correlated), via ``check_exposure_only`` (Portfolio Division).
        3. CorrelationEngine — cluster exposure (Portfolio Division).
        4. RiskEngine EV veto — edge/profitability judgement (fails open).
        5. RL authority, DecisionEngine, RiskGovernor — conviction + graded review.
        6. PositionSizer — compute lot size / stake.
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

            # ── Compliance Division: single authoritative permit ─────
            # Department 3 — the ONE pure permit layer.  Consolidates the
            # necessary vetoes (market-open, broker-available, news, spread,
            # duplicate, daily-loss [single source: per-account silo],
            # per-account heat, DrawdownGuard FROZEN, max-positions,
            # PortfolioRisk DEFENSIVE+) into a single fail-CLOSED call that
            # collects ALL rejection reasons.  Replaces the previously
            # scattered inline Gates 1/2/4 + the duplicate/spread sub-checks
            # of the old fail-OPEN Gate 5c.
            acct = ""
            try:
                if ctx is not None:
                    acct = ctx.account_key(symbol, self._pm)
                    if (
                        ctx.account_risk is not None
                        and balance and balance > 0
                    ):
                        ctx.account_risk.update_balance(acct, balance)
            except Exception as exc:
                logger.debug("[compliance] account-key/balance refresh failed: {}", exc)
                acct = ""

            if ctx is not None:
                if ctx.compliance is None:
                    # ctx exists but the permit layer failed to construct —
                    # never trade without Compliance (fail-closed).
                    logger.error(
                        "EVENT-DRIVEN ENTRY BLOCKED | {} — Compliance Division "
                        "unavailable (failing closed)", symbol,
                    )
                    return
                verdict = ctx.compliance.permit(
                    ComplianceCandidate(symbol=symbol, direction=direction),
                    ComplianceBook(open_positions=open_positions),
                    ComplianceAccount(account_key=acct, balance=balance or 0.0),
                )
                if verdict.rejected:
                    logger.warning(
                        "EVENT-DRIVEN ENTRY BLOCKED | {} — Compliance: {}",
                        symbol, "; ".join(verdict.reasons),
                    )
                    return

            # ── Gate 3: PortfolioGovernor — concentration only ───────
            # Daily-loss + max-positions are now owned by the Compliance
            # Division above (V7 — one daily-loss source).  The Governor here
            # contributes ONLY its portfolio concentration analysis (currency
            # / sector / correlated exposure), which the Portfolio Division
            # will absorb in a later phase.
            if ctx is not None and ctx.portfolio_governor is not None:
                try:
                    verdict = ctx.portfolio_governor.check_exposure_only(
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
                    logger.warning(
                        "EVENT-DRIVEN ENTRY BLOCKED | {} — PortfolioGovernor check "
                        "errored, failing closed: {}", symbol, exc,
                    )
                    return

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
                    logger.warning(
                        "EVENT-DRIVEN ENTRY BLOCKED | {} — Correlation check "
                        "errored, failing closed: {}", symbol, exc,
                    )
                    return
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

            # ── Gate 5d: RiskEngine EV veto ──────────────────────────
            # Duplicate-pair and adaptive-spread vetoes moved to the
            # Compliance Division above (necessary permits, fail-closed).
            # The expected-value veto remains here — it is an edge/profitability
            # judgement (Portfolio/Learning territory), not a hard permit, so it
            # deliberately fails OPEN (an insufficient-history EV never vetoes).
            if ctx is not None and ctx.risk_engine is not None:
                try:
                    ev_estimator = getattr(ctx.risk_engine, "ev_estimator", None)
                    trade_history = (
                        ctx.ml_adapter.get_trade_history()
                        if ctx.ml_adapter is not None else []
                    )
                    if ev_estimator is not None and trade_history:
                        _regime = ""
                        _session = ""
                        try:
                            if ctx.regime_detector is not None:
                                _rs = ctx.regime_detector.get_regime(symbol)
                                _regime = getattr(_rs, "regime", "") or ""
                        except Exception:
                            _regime = ""
                        try:
                            if ctx.session_engine is not None:
                                _session = getattr(
                                    ctx.session_engine.get_status(), "name", "",
                                ) or ""
                        except Exception:
                            _session = ""
                        ev_est = ev_estimator.estimate(
                            symbol, _regime, _session, trade_history,
                        )
                        ev_threshold = self._config.risk.ev_threshold
                        if (
                            ev_est.expected_value < ev_threshold
                            and ev_est.confidence in ("high", "medium")
                        ):
                            logger.warning(
                                "EVENT-DRIVEN ENTRY BLOCKED | {} — negative EV "
                                "{:+.4f} ({} conf, n={}) < threshold {}",
                                symbol, ev_est.expected_value,
                                ev_est.confidence, ev_est.sample_size,
                                ev_threshold,
                            )
                            return
                except Exception as exc:
                    logger.debug(
                        "[entry-risk] EV/duplicate veto skipped (non-fatal): {}",
                        exc,
                    )

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

                    # Live graded-risk inputs — the RiskGovernor's graded entry
                    # path measures portfolio heat + spread; previously these
                    # arrived as 0.0 so the dimensions never engaged.
                    de_heat_pct = 0.0
                    if ctx.account_risk is not None:
                        try:
                            de_heat_pct = float(
                                ctx.account_risk.heat(ctx.account_key(symbol, self._pm))
                            )
                        except Exception:
                            de_heat_pct = 0.0
                    de_cur_spread = 0.0
                    de_typ_spread = 0.0
                    try:
                        de_cur_spread = float(self._get_spread_pips(symbol) or 0.0)
                        _sinfo = INSTRUMENT_REGISTRY.get(symbol)
                        de_typ_spread = float(
                            getattr(_sinfo, "typical_spread_pips", 0.0) or 0.0
                        ) if _sinfo else 0.0
                    except Exception:
                        pass

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
                        portfolio_heat_pct=de_heat_pct,
                        current_spread=de_cur_spread,
                        typical_spread=de_typ_spread,
                        # Full directional-consensus panel synthesized by the
                        # analysis plane — passed uncompressed so the decision
                        # engine reasons over which modules agree/dissent.
                        consensus_votes=(
                            list(getattr(wm, "votes", ()) or [])
                            if wm is not None else []
                        ),
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
                        day_of_week=datetime.now(timezone.utc).weekday(),
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

                    want_dir = "LONG" if direction.upper() in ("BUY", "LONG") else "SHORT"

                    # Select the ranked opportunity matching the entry direction
                    # (highest EV) so the orchestrator's ranker_ev sizing
                    # dimension is fed by the calibrated candidate EV instead of
                    # staying neutral.  Gated by ``orchestrator.use_ranker_ev``
                    # (default off) so sizing is unchanged until the operator
                    # opts in; best-effort selection leaves the fields None
                    # (neutral) on any miss.
                    ranker_ev = None
                    ranker_coherence = None
                    ranker_confidence = None
                    candidate_count = 0
                    _use_ranker_ev = False
                    try:
                        _orch_cfg = getattr(self._config, "orchestrator", None)
                        _use_ranker_ev = bool(
                            getattr(_orch_cfg, "use_ranker_ev", False)
                        )
                    except Exception:
                        _use_ranker_ev = False
                    if _use_ranker_ev:
                        try:
                            cands = (
                                wm.candidates_list()
                                if wm is not None and hasattr(wm, "candidates_list")
                                else list(getattr(wm, "candidates", ()) or [])
                            )
                            matching = [
                                c for c in cands
                                if str(getattr(c, "direction", "")).upper() == want_dir
                            ]
                            candidate_count = len(cands)
                            if matching:
                                best = max(
                                    matching,
                                    key=lambda c: float(getattr(c, "expected_value", 0.0) or 0.0),
                                )
                                ranker_ev = float(getattr(best, "expected_value", 0.0) or 0.0)
                                ranker_coherence = float(getattr(best, "coherence", 0.0) or 0.0)
                                ranker_confidence = float(getattr(best, "confidence", 0.0) or 0.0)
                        except Exception as exc:
                            logger.debug(
                                "[orch] {} candidate EV select failed: {}", symbol, exc,
                            )

                    proposal = TradeProposal(
                        pair=symbol,
                        direction=want_dir,
                        scan_score=float(conviction),
                        de_conviction=de_conviction if de_conviction > 0 else None,
                        de_margin=None,
                        tf_alignment=h4_alignment,
                        ranker_ev=ranker_ev,
                        ranker_coherence=ranker_coherence,
                        ranker_confidence=ranker_confidence,
                        candidate_count=candidate_count,
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

            # ── Adaptive optimizer: losing-pattern block + size adjust ──
            # Applies learned pair/session/regime edge to the live entry: a
            # statistically-confident losing pattern is blocked; otherwise the
            # learned size multiplier is folded into combined_mult below.
            adapt_mult = 1.0
            if ctx is not None and getattr(ctx, "ml_adapter", None) is not None:
                try:
                    _regime = "UNKNOWN"
                    wm = self._wm_store.get(symbol)
                    if wm is not None:
                        try:
                            rbtf = wm.regime_by_tf()
                            _regime = str(
                                rbtf.get("H1")
                                or rbtf.get("H4")
                                or next(iter(rbtf.values()), "UNKNOWN")
                            )
                        except Exception:
                            _regime = "UNKNOWN"
                    _session = "UNKNOWN"
                    if ctx.session_engine is not None:
                        try:
                            _session = getattr(
                                ctx.session_engine.get_status(), "name", "UNKNOWN",
                            )
                        except Exception:
                            _session = "UNKNOWN"
                    _zone_type = decision.get("zone_type", "")
                    block_enabled = getattr(
                        self._config.risk, "losing_pattern_block_enabled", True,
                    )
                    if block_enabled:
                        is_loser, loser_reason = ctx.ml_adapter.is_losing_pattern(
                            symbol, _regime, _session, _zone_type,
                        )
                        # Learning recommends the block; Governance authorises it
                        # (auto-approved until Phase 7 → identical behaviour).
                        if is_loser and self._recommendation_approved(
                            "AVOID_PATTERN",
                            {
                                "pair": symbol,
                                "regime": _regime,
                                "session": _session,
                                "entry_type": _zone_type,
                                "reason": loser_reason,
                            },
                            source="optimizer.losing_pattern",
                            confidence=0.8,
                        ):
                            logger.warning(
                                "EVENT-DRIVEN ENTRY BLOCKED | {} — losing pattern: {}",
                                symbol, loser_reason,
                            )
                            self._record_shadow_rejection(
                                symbol, direction, entry_price, sl, tp1,
                                "losing_pattern", conviction,
                            )
                            return
                    adj = ctx.ml_adapter.get_trade_adjustments(symbol, _regime, _session)
                    # Honor the optimizer's AVOID veto. When should_trade is
                    # False (regime/pair/session flagged AVOID) the size
                    # multiplier is left at its 1.0 default — reading it alone
                    # silently traded at full size through the veto. The veto is
                    # a Learning recommendation; Governance authorises it
                    # (auto-approved until Phase 7 → identical behaviour).
                    if not getattr(adj, "should_trade", True) and \
                            self._recommendation_approved(
                                "AVOID_PATTERN",
                                {
                                    "pair": symbol,
                                    "regime": _regime,
                                    "session": _session,
                                    "reason": getattr(adj, "reason", ""),
                                },
                                source="optimizer.avoid_veto",
                                confidence=float(getattr(adj, "confidence", 0.0) or 0.0),
                            ):
                        logger.warning(
                            "EVENT-DRIVEN ENTRY BLOCKED | {} — optimizer AVOID: {}",
                            symbol, getattr(adj, "reason", "")[:80],
                        )
                        self._record_shadow_rejection(
                            symbol, direction, entry_price, sl, tp1,
                            "optimizer_avoid", conviction,
                        )
                        return
                    # Position-size multiplier is a SIZE_ADJUST recommendation —
                    # applied on approval, neutral (1.0) if Governance rejects.
                    _opt_mult = float(
                        getattr(adj, "position_size_multiplier", 1.0) or 1.0
                    )
                    if _opt_mult != 1.0 and not self._recommendation_approved(
                        "SIZE_ADJUST",
                        {"multiplier": _opt_mult, "pair": symbol},
                        source="optimizer.size",
                        confidence=float(getattr(adj, "confidence", 0.0) or 0.0),
                    ):
                        _opt_mult = 1.0
                    adapt_mult = _opt_mult
                except Exception as exc:
                    logger.debug("[adaptive] optimizer adjust failed: {}", exc)
                    adapt_mult = 1.0

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
                    # Capital-allocation sizing is a SIZE_ADJUST recommendation —
                    # applied on approval, neutral (1.0) if Governance rejects.
                    if cap_mult != 1.0 and not self._recommendation_approved(
                        "SIZE_ADJUST",
                        {"multiplier": float(cap_mult), "fingerprint": str(fp),
                         "pair": symbol},
                        source="capital_allocator",
                    ):
                        cap_mult = 1.0
                except Exception as exc:
                    logger.warning("[cap-alloc] sizing multiplier failed: {}", exc)

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
                    # Execution-profile selection is a PROFILE_CHANGE recommendation
                    # — applied on approval, dropped if Governance rejects.
                    if exec_profile is not None and not self._recommendation_approved(
                        "PROFILE_CHANGE",
                        {
                            "profile": getattr(exec_profile, "name", str(exec_profile)),
                            "regime": regime_str,
                            "pair": symbol,
                        },
                        source="execution_profiles",
                    ):
                        exec_profile = None
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
            # Prefer broker-truth money-per-pip from the symbol spec; falls
            # back to the config value when the spec is unavailable.
            pip_value = self._broker_pip_value(symbol, pip_size, pip_value)

            # The Portfolio Division reuses the RiskEngine's shared PositionSizer
            # (so the per-trade risk ceiling stays authoritative). The same
            # instance backs the per-instrument volatility factor below.
            shared_sizer = (
                getattr(ctx.risk_engine, "position_sizer", None)
                if ctx is not None and ctx.risk_engine is not None
                else None
            )
            if shared_sizer is None:
                shared_sizer = PositionSizer()

            # ── Per-instrument volatility sizing (current vs average ATR) ──
            # Complements the system-wide vol_mult: scales THIS instrument's
            # size by its own ATR regime. Passed to Portfolio as one factor so
            # the existing [0.15, 1.0] clamp still bounds the final size.
            inst_vol_mult = 1.0
            try:
                vdf = self._fetch_candles(symbol, "M5", 60)
                if vdf is not None and len(vdf) >= 20:
                    tr = (vdf["high"] - vdf["low"]).abs()
                    cur_atr = float(tr.tail(14).mean())
                    avg_atr = float(tr.tail(50).mean())
                    inst_vol_mult = shared_sizer.adjust_for_volatility(
                        1.0, cur_atr, avg_atr,
                    )
            except Exception:
                inst_vol_mult = 1.0

            # ── Portfolio Division: cohesive sizing + exposure + budget ──
            # Replaces the inline fresh-sizer + ad-hoc multiplier chain. The
            # division folds every factor transparently, enforces the remaining
            # daily-loss budget (the protection the dead RiskEngine.assess chain
            # owned), and reports book exposure.
            from portfolio.division import PortfolioDivision as _PortfolioDivision
            from portfolio.models import (
                PortfolioAccount as _PFAccount,
                PortfolioCandidate as _PFCandidate,
                SizingFactors as _PFFactors,
            )
            from risk.position_sizer import SizeResult as _SizeResult

            portfolio = ctx.portfolio if ctx is not None else None
            if portfolio is None:
                portfolio = _PortfolioDivision(
                    position_sizer=shared_sizer,
                    correlation_engine=(
                        ctx.correlation_engine if ctx is not None else None
                    ),
                )

            _acct_key = ctx.account_key(symbol, self._pm) if ctx is not None else ""
            _daily_pnl = 0.0
            _daily_cap = 0.0
            if ctx is not None and ctx.account_risk is not None:
                try:
                    if _acct_key:
                        _daily_pnl = float(ctx.account_risk.daily_pnl(_acct_key))
                    _daily_cap = float(
                        getattr(ctx.account_risk, "daily_loss_cap_pct", 0.0) or 0.0
                    )
                except Exception:
                    _daily_pnl, _daily_cap = 0.0, 0.0

            pf_factors = _PFFactors(
                base_risk_pct=risk_pct,
                de_size_mult=de_size_mult,
                orch_mult=orch_mult,
                vol_mult=vol_mult,
                inst_vol_mult=inst_vol_mult,
                density_mult=density_mult,
                exec_mult=exec_mult,
                cap_mult=cap_mult,
                adapt_mult=adapt_mult,
            )
            pf_verdict = portfolio.evaluate(
                _PFCandidate(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=sl,
                    conviction=float(conviction or 0.0),
                    context=pctx,
                    pip_size=pip_size,
                    pip_value_per_lot=pip_value,
                ),
                open_positions,
                _PFAccount(
                    balance=balance or 0.0,
                    account_key=_acct_key,
                    daily_pnl=_daily_pnl,
                    daily_loss_cap_pct=_daily_cap,
                ),
                pf_factors,
            )

            if not pf_verdict.approved:
                logger.warning(
                    "EVENT-DRIVEN ENTRY SKIPPED | {} — Portfolio: {}",
                    symbol, pf_verdict.reason,
                )
                return

            combined_mult = pf_verdict.combined_mult
            # Adapt the Portfolio verdict back into a SizeResult so the existing
            # downstream (zero-check, broker-volume snap, intent build, audit)
            # stays unchanged.
            size_result = _SizeResult(
                lots=pf_verdict.lots,
                stake_usd=pf_verdict.stake_usd,
                risk_amount=round((balance or 0.0) * pf_verdict.risk_pct, 2),
                risk_pips=0.0,
                pip_value=pip_value,
                max_loss=pf_verdict.max_loss,
                margin_estimate=0.0,
                sizing_mode=pf_verdict.sizing_mode,
            )

            if size_result.lots <= 0 and size_result.stake_usd <= 0:
                logger.warning(
                    "EVENT-DRIVEN ENTRY SKIPPED | {} — position size is zero (sizing_mode={})",
                    symbol, size_result.sizing_mode,
                )
                return

            # Snap MT5 lots to the broker's volume_min/max/step so an
            # unaligned size is never rejected (broker-truth constraints).
            if size_result.lots > 0:
                size_result.lots = self._snap_to_broker_volume(
                    symbol, size_result.lots,
                )

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

            # Single execution plane: the entry is routed through the shared
            # ActionExecutor — the sole broker gateway (RiskGate OPEN
            # validation → CircuitBreaker → broker.execute_entry).  There is
            # no direct PlatformManager fallback: if the Intent.open factory
            # is unavailable the entry is aborted *before* any broker call so
            # a flagged entry can never bypass the RiskGate or circuit breaker.
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
                logger.error(
                    "EVENT-DRIVEN ORDER ABORTED | {} {} | Intent.open "
                    "unavailable, refusing to bypass execution plane | {}",
                    symbol, direction, exc,
                )
                return

            exec_result = self._executor.execute(open_intent, {})
            result = (
                exec_result.broker_response
                if exec_result.broker_response is not None
                else exec_result
            )

            if result.success:
                logger.info(
                    "EVENT-DRIVEN ORDER PLACED | {} {} {:.2f} lots ticket={}",
                    symbol, direction, result.lots, result.order_id,
                )
                try:
                    from ops.logging_config import audit_trade
                    audit_trade(
                        "order_placed",
                        event="open",
                        symbol=symbol,
                        direction=direction,
                        lots=float(getattr(result, "lots", 0.0) or 0.0),
                        stake_usd=float(size_result.stake_usd or 0.0),
                        entry_price=float(entry_price or 0.0),
                        sl=float(sl or 0.0),
                        tp=float(tp1 or 0.0),
                        ticket=str(getattr(result, "order_id", "")),
                        conviction=float(conviction or 0.0),
                    )
                except Exception:
                    pass
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
                    # Broker fill price is the truth for entry attribution and
                    # for the close path's pnl_pips / risk_pips; fall back to
                    # the planned entry price only if the broker omits it.
                    fill_price = float(getattr(result, "fill_price", 0.0) or 0.0)
                    if fill_price <= 0:
                        fill_price = float(entry_price or 0.0)
                    self._entry_context[result.order_id] = {
                        "zone_type": decision.get("zone_type", ""),
                        "regime": regime,
                        "concepts": sorted(set(concept_names)),
                        "entry_price": fill_price,
                        "sl": float(sl or 0.0),
                        "tp": float(tp1 or 0.0),
                        "risk_pips": (
                            abs(fill_price - float(sl)) / self._safe_pip_size(symbol)
                            if fill_price and sl and self._safe_pip_size(symbol) > 0
                            else 0.0
                        ),
                        # Record the execution profile actually selected at entry
                        # so the close path attributes the outcome to the right
                        # profile (was hard-coded to "standard_swing").
                        "exec_profile": (
                            getattr(exec_profile, "name", "") or "standard_swing"
                        ),
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

    def _recommendation_approved(
        self,
        rec_type: str,
        payload: dict,
        *,
        source: str,
        confidence: float = 0.0,
        evidence: Optional[dict] = None,
    ) -> bool:
        """Submit a Learning recommendation through the gateway, return approval.

        This is the Department ⑦→⑧ boundary: Learning recommends, Governance
        authorises. When no gateway is wired (or governance is not required) the
        recommendation is auto-approved, so the caller applies the learner's
        change exactly as before — behaviour-neutral. Never raises.
        """
        ctx = self._ctx
        gateway = getattr(ctx, "recommendation_gateway", None) if ctx else None
        if gateway is None:
            return True
        try:
            from adaptive.recommendations import LearningRecommendation
            rec = LearningRecommendation(
                source=source,
                recommendation_type=rec_type,
                payload=dict(payload or {}),
                confidence=float(confidence),
                evidence=dict(evidence or {}),
            )
            return bool(gateway.submit(rec).approved)
        except Exception as exc:  # noqa: BLE001 — authorisation overlay must never block trading
            logger.debug("[recommendations] submit failed ({}): {}", rec_type, exc)
            return True

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

        # Broker fill price (truth) for all entry-time learning attribution;
        # fall back to the requested price only when the broker omits it.
        entry_fill_price = float(getattr(result, "fill_price", 0.0) or 0.0)
        if entry_fill_price <= 0:
            entry_fill_price = float(expected_price or 0.0)

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
                    "entry_price": entry_fill_price,
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
        close_price: float = 0.0,
        exit_reason: Optional[str] = None,
        exit_reason_source: str = "event_driven",
        raw_broker_reason: Optional[int] = None,
        commission: float = 0.0,
        swap: float = 0.0,
        fee: float = 0.0,
    ) -> None:
        """Feed closed-trade P&L into all risk + learning subsystems.

        Mirrors TradingLoop._record_closed_trade — risk first, then learning.
        """
        # ── LEARNED ANALYSIS EDGE (data-driven combiner) ────────────
        # Attribute the realized outcome to the zone/concept keys captured at
        # entry so future conviction reflects what actually pays off.  Runs
        # before the ctx guard (no ctx dependency) and is fully best-effort.
        info: dict[str, Any] = {}
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

        # Audit trail — realized close (trade audit) + the post-close daily
        # risk picture (risk audit). Best-effort; never blocks the close path.
        try:
            from ops.logging_config import audit_trade, audit_risk
            audit_trade(
                "position_closed",
                event="close",
                symbol=symbol,
                direction=direction,
                pnl_dollars=round(float(pnl_dollars), 2),
                pnl_pips=round(float(pnl_pips), 2),
                ticket=str(ticket),
            )
            if ctx.account_risk is not None:
                acct = ctx.account_key(symbol, self._pm)
                audit_risk(
                    "account_daily_pnl",
                    account=acct,
                    daily_pnl=round(ctx.account_risk.daily_pnl(acct), 2),
                    daily_pnl_pct=round(ctx.account_risk.daily_pnl_pct(acct), 2),
                    halted=ctx.account_risk.daily_loss_halted(acct),
                )
        except Exception:
            pass

        # ── LEARNING LAYER (Phase 4) ────────────────────────────────

        # Realized R from the entry's original stop distance (captured at fill).
        # Previously hardcoded to 0.0, which fed every learner a flat reward.
        _risk_pips = float(info.get("risk_pips", 0.0) or 0.0)
        pnl_r = (pnl_pips / _risk_pips) if _risk_pips > 0 else 0.0
        outcome = "WIN" if pnl_dollars > 0 else "LOSS"
        # V14 — normalise the exit reason to a typed ExitCause value so every
        # learner receives a consistent categorical exit feature instead of an
        # inconsistent free-text string. The structured management intent source
        # (carried through by _handle_close_result) is often already a canonical
        # ExitCause value — match it exactly first; otherwise fall back to the
        # best-effort text classifier for broker deal-history strings.
        from management.exit_cause import ExitCause
        _raw_cause = exit_reason or "event_driven_close"
        try:
            cause_value = ExitCause(_raw_cause).value
        except (ValueError, TypeError):
            cause_value = ExitCause.from_reason(_raw_cause).value

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
                exit_price = close_price or (tick.mid if tick else 0.0)
                ctx.post_close_tracker.record_close(
                    trade_id=str(ticket),
                    pair=symbol,
                    direction=direction,
                    entry_price=float(info.get("entry_price", 0.0) or 0.0),
                    exit_price=exit_price,
                    exit_cause=cause_value,
                    sl_price=float(info.get("sl", 0.0) or 0.0),
                    tp_price=float(info.get("tp", 0.0) or 0.0),
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
                    # Attribution for the reconciliation feed: when the broker's
                    # deal history supplied the close reason, source is "broker"
                    # and ``raw_broker_reason`` carries the broker reason code.
                    # Otherwise the ED system determined the close internally
                    # (source "event_driven", no raw broker reason).  Either way
                    # invariant I2 (every exit_reason has a source) holds.
                    "exit_reason_source": exit_reason_source,
                    "raw_broker_reason": raw_broker_reason,
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
                exit_price = close_price or (tick.mid if tick else 0.0)
                record = TradeRecord(
                    pair=symbol,
                    direction=direction,
                    entry=float(info.get("entry_price", 0.0) or 0.0),
                    exit=exit_price,
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
                # Use the entry's real stop distance for R, not abs(pnl_pips)
                # (which collapsed every outcome to ±1).
                risk_pips_est = _risk_pips if _risk_pips > 0 else (
                    abs(pnl_pips) if pnl_pips != 0 else 1.0
                )
                r_multiple = pnl_pips / risk_pips_est if risk_pips_est > 0 else 0.0
                ctx.capital_allocator.record_outcome(fp, r_multiple)
            except Exception as exc:
                logger.warning("[close-evo] CapitalAllocator record failed: {}", exc)

        # ExecutionProfileManager — record outcome for the profile used
        if ctx.execution_profiles is not None:
            try:
                _prof_name = str(info.get("exec_profile", "") or "standard_swing")
                ctx.execution_profiles.record_outcome(_prof_name, pnl_pips / 10.0 if pnl_pips else 0.0)
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
                entry_tf = str(info.get("entry_timeframe", "M5") or "M5")
                m5_df = self._fetch_candles(symbol, "M5", 50)
                m1_df = self._fetch_candles(symbol, "M1", 50)
                if m5_df is not None:
                    from types import SimpleNamespace
                    # Feed the ReEntryManager the REAL closed-trade context (not a
                    # bare pair/direction stub): the actual ticket, the entry
                    # price/risk, the captured thesis (zone/regime/concepts) and
                    # the trade's own timeframe — so the re-entry evaluation and
                    # its audit reflect the trade that just closed, and the
                    # cooldown is anchored to the real close time.
                    closed = SimpleNamespace(
                        pair=symbol,
                        direction=direction,
                        re_entry_eligible=True,
                        close_time=datetime.now(timezone.utc),
                        entry_timeframe=entry_tf,
                        candles_since_entry=0,
                        trade_id=str(ticket),
                        entry_price=float(info.get("entry_price", 0.0) or 0.0),
                        risk_pips=float(info.get("risk_pips", 0.0) or 0.0),
                        regime=info.get("regime"),
                        zone_type=info.get("zone_type"),
                        concepts=info.get("concepts"),
                    )
                    opp = ctx.re_entry_manager.check_re_entry(closed, m5_df, m1_df)
                    if opp is not None and getattr(opp, "eligible", False):
                        logger.info(
                            "[re-entry] {} {} opportunity: {} (zone={}, thesis: regime={} zone_type={})",
                            symbol, direction,
                            getattr(opp, "reason", ""),
                            getattr(opp, "new_entry_zone", None),
                            info.get("regime"), info.get("zone_type"),
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
