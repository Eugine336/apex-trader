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
from typing import Any, Callable, Optional

import pandas as pd
from loguru import logger

from config import AppConfig, INSTRUMENT_REGISTRY, get_pip_size, is_always_open, is_session_gated
from brain.symbol_mapper import resolve_to_internal
from brain.world_model import WorldModelStore
from brain.compression_detector import CompressionDetector
from brain.session_context import SessionContext
from brain.news_planner import NewsPlanner
from brain.currency_strength import CurrencyStrengthMeter, CURRENCY_PAIRS
from compliance import (
    ComplianceAccount,
    ComplianceBook,
    ComplianceCandidate,
)
from core.system_context import SystemContext
from persistence.event_store import get_event_store
from persistence import domain_events as DE
from tick import EventBus, Tick, TickStore, CandleCloseDetector, TickRouter
from scanner.candle_close_handler import CandleCloseHandler
from execution.intents import Intent, IntentType
from execution.intent_aggregator import IntentAggregator, AggregatorConfig
from execution.action_executor import ActionExecutor, ExecutorConfig
from execution.position_worker import (
    PositionWorker, WorkerConfig, ScanContext, MarketContext,
)
from execution.position_snapshot import PositionSnapshot, build_position_snapshot
from execution.management_state import ManagementStateStore
from execution.management_scheduler import ManagementScheduler
from entry import EntryConfig
from entry.flip_sequence_tracker import FlipSequenceTracker
from platform_context import build_context_for_symbol
from platforms.platform_manager import PlatformManager
from platforms.order_idempotency import build_order_comment, generate_idempotency_key
from risk.position_sizer import PositionSizer
from ops.lifecycle import ShutdownManager
from adaptive.zone_edge_tracker import ZoneEdgeTracker


# ── Structure & M1 micro-context readers ─────────────────────────────
# Phase K (Part XI — modular design): these WorldModel-structure / M1
# micro-structure readers were lifted into :mod:`brain.structure_context`. They
# are re-imported here so every call site below (and the existing
# ``from event_driven_bootstrap import _struct_swings`` /
# ``_micro_confirmation_from_event`` paths used by the tests) resolves
# identically — behaviour unchanged.
from brain.structure_context import (  # noqa: E402
    _micro_confirmation_from_event,
    _struct_event,
    _struct_swings,
    _struct_trend_conf,
)


# ── Broker-truth field readers ───────────────────────────────────────
# Phase K (Part XI — modular design): these pure, shared readers were lifted
# into :mod:`execution.broker_fields`. They are re-imported here so every call
# site below (and the existing ``from event_driven_bootstrap import
# _broker_pip_value_from_spec`` path) resolves identically — behaviour unchanged.
from execution.broker_fields import (  # noqa: E402
    _broker_entry_price,
    _broker_pip_value_from_spec,
    _broker_pnl,
    _broker_tp,
)


# Operations Division — scale-in / partial-close tuning (V13).
# A scale-in is an *add* to an existing winner, so it risks a fraction of a
# fresh entry's per-trade ceiling. The partial-close default banks half the
# position when a verdict carries no explicit ratio.
_SCALE_IN_RISK_FRACTION = 0.5
_DEFAULT_PARTIAL_CLOSE_RATIO = 0.5

# A tick older than this strongly implies the session is CLOSED (weekend /
# holiday), not merely quiet. Used by the market-open gate as the session
# signal ``trade_mode`` cannot give (a broker leaves trade_mode "full" through a
# weekend). Deliberately far larger than the trading staleness limit (~120s) so
# a quiet-but-OPEN market is never mis-flagged closed. Env override:
# APEX_MARKET_CLOSED_TICK_AGE_SECONDS.
try:
    _MARKET_CLOSED_TICK_AGE_SECONDS = float(
        os.getenv("APEX_MARKET_CLOSED_TICK_AGE_SECONDS", "900") or "900"
    )
except (TypeError, ValueError):
    _MARKET_CLOSED_TICK_AGE_SECONDS = 900.0

# How long (seconds, monotonic) a worker-path SL move stays "pending broker
# confirmation" after it is optimistically applied. While pending, the synthetic
# stop-hit check is suppressed so a broker-rejected SL cannot trigger a phantom
# stop-loss CLOSE before the modify result lands.
#
# Bug #20: this guard is HANDSHAKE-based — the modify-result callback
# (_handle_manage_result) is what normally clears it, after rolling back the
# optimistic SL on failure. This timeout is only a SAFETY NET for the case where
# the result callback never arrives (e.g. the aggregator deduped the intent
# away); when it elapses the eval thread rolls back the optimistic state BEFORE
# resuming the synthetic stop-hit check. A modify round-trip is normally well
# under 1s, but the executor retries transient failures with exponential backoff
# (which the earlier 5s value could undershoot, dropping the guard mid-flight and
# exposing the unconfirmed SL to a phantom stop). 30s comfortably covers the full
# retry budget so the handshake — not the timer — is the normal clear path.
_SL_MODIFY_PENDING_GRACE_S = 30.0


# -- Tick source threads (extracted) --
# Phase K: extracted to tick.tick_sources (behaviour-preserving).
from tick.tick_sources import DerivTickAdapter, MT5TickPoller  # noqa: E402


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
        developing_world_model_store: Optional[WorldModelStore] = None,
    ) -> None:
        self._pm = platform_manager
        self._tick_store = tick_store
        self._wm_store = world_model_store
        # Phase 5 — developing (forming-bar) WorldModel store. Optional and
        # ADVISORY-ONLY: the management path reads it as an early-warning
        # secondary signal that can only TIGHTEN protection on an open
        # position, never loosen a stop, close, or open one. None (the default,
        # and how every existing unit test constructs the evaluator) makes the
        # advisory a no-op, so behaviour is identical to before Phase 5.
        self._developing_wm_store = developing_world_model_store
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
        # Evidence-based exit (Session 28) — per-ticket monotonic timestamp of
        # the last ThesisEngine health check, so the check is throttled to
        # ``thesis.evidence_exit_check_interval`` rather than firing every
        # tick-eval cycle.
        self._last_evidence_exit_check: dict[str, float] = {}
        # Atomic reversal (Session 29) — pending opposite-direction entries armed
        # when a thesis_flip is judged reversal-viable, keyed by the exit leg's
        # ticket. The owning EventDrivenSystem aliases its OWN dict onto this
        # field after construction so the arm site (here, management path) and
        # the dispatch site (_handle_close_result, once the close confirms) share
        # ONE map. Defaulted here so the evaluator is safe standalone / in tests.
        self._pending_reversals: dict[str, dict[str, Any]] = {}
        self._fast_opposition: dict[str, int] = {}
        self._last_h1_close: dict[str, datetime] = {}
        # Per-ticket candidate provenance (Session 4 multi-opportunity). The
        # owning EventDrivenSystem aliases its own dict onto this field after
        # construction so the fill-time recorder, the close-time cleanup, the
        # dashboard read, and this evaluator's candidate-scoped management read
        # all share one map. Defaulted here so the evaluator is also safe to use
        # standalone (and in unit tests) without that aliasing step.
        self._candidate_positions: dict[str, "CandidatePosition"] = {}
        # In-flight optimistic-management rollback snapshots, keyed by
        # (ticket, intent_type). The worker-path optimistic SL / partial writes
        # record the pre-mutation values here so a rejected modify/partial rolls
        # back instead of leaving phantom state. The owning EventDrivenSystem
        # aliases its OWN dict + lock onto these fields after construction so the
        # record (here) and the execution-result rollback (_handle_manage_result)
        # operate on ONE shared store — without that aliasing the record landed
        # in a dict the result callback never read. Defaulted here so the
        # evaluator is also safe to use standalone (and in unit tests).
        self._inflight_manage: dict[tuple[str, int], dict[str, Any]] = {}
        self._inflight_manage_lock = threading.Lock()
        # Symbols whose pip_size lookup fell back to the 0.0001 default. Used to
        # warn once per symbol (avoid log floods) when risk sizing is derived
        # from an unreliable pip_size.
        self._pip_size_fallback_symbols: set[str] = set()

    def _record_inflight_manage(
        self, ticket: str, intent_type: IntentType, prev: dict[str, Any],
    ) -> None:
        """Record pre-mutation management values for rollback on exec failure.

        Mirrors EventDrivenSystem._record_inflight_manage but lives on the
        evaluator (which performs the optimistic mutation). Both write into the
        SAME dict once the owner aliases it, so the FlushLoop result callback can
        find and roll back the snapshot. Previously this method existed only on
        EventDrivenSystem, so calling it from here raised AttributeError — which
        the broad per-position try/except swallowed, leaving the optimistic SL
        write committed but its pending-confirmation guard (set on the line
        after the failing call) never armed.
        """
        try:
            with self._inflight_manage_lock:
                self._inflight_manage[(str(ticket), int(intent_type))] = prev
        except Exception as exc:
            # A failure here means the optimistic mutation has no rollback
            # snapshot — surface it (this is the exact condition that previously
            # silently disabled the phantom-SL guard) instead of passing.
            logger.warning(
                "[pos-eval] failed to record inflight rollback for {} {}: {}",
                ticket, intent_type, exc,
            )

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

            # Live mark-to-market P&L in pips.  The management state's
            # ``pnl_pips`` was never refreshed, so every PositionWorker check
            # that gates on P&L (invalidation's ``pnl_pips <= 0``, breakeven,
            # profit protection, stall flat-threshold) saw a permanent 0.0 —
            # making "low score with negative P&L" fire on freshly opened
            # positions.  Feed the broker-truth entry price + latest tick so
            # those checks evaluate against reality.
            entry_px = _broker_entry_price(pos, price)
            if pip_size > 0 and entry_px > 0:
                if direction.upper() in ("BUY", "LONG"):
                    mgmt.pnl_pips = (price - entry_px) / pip_size
                else:
                    mgmt.pnl_pips = (entry_px - price) / pip_size

            scan_ctx = self._build_scan_context(symbol, direction)
            market_ctx = self._build_market_context(symbol, now)

            if scan_ctx is not None:
                current_score = scan_ctx.score
                mgmt.score_history.append(current_score)
                if len(mgmt.score_history) > 20:
                    mgmt.score_history = mgmt.score_history[-20:]

            # Resolve the per-cycle "SL modify in flight" flag from the monotonic
            # deadline set when a worker-path SL move was last submitted (and
            # auto-cleared by _handle_manage_result on the broker result). While
            # pending, the snapshot tells the worker to skip the synthetic
            # stop-hit check so an unconfirmed / rejected SL cannot fire a
            # phantom stop-loss CLOSE.
            mgmt.sl_pending_confirmation = mgmt.sl_modify_pending_until > now_mono

            # If a prior optimistic SL move's pending-confirmation window has
            # elapsed with no broker result, the aggregator dropped/deduped the
            # intent (so _handle_manage_result never fired to commit or roll it
            # back). The optimistic at_breakeven / stop_loss were never confirmed
            # and its inflight rollback snapshot still lingers — restore the
            # pre-mutation values so a phantom breakeven flag or unconfirmed SL
            # cannot persist. A live, still-pending move is left untouched.
            if mgmt.sl_modify_pending_until and not mgmt.sl_pending_confirmation:
                _sl_key = (str(order_id), int(IntentType.MODIFY_SL))
                with self._inflight_manage_lock:
                    _stale = self._inflight_manage.pop(_sl_key, None)
                if _stale is not None:
                    for _field, _value in _stale.items():
                        try:
                            setattr(mgmt, _field, _value)
                        except Exception:
                            pass
                    logger.warning(
                        "[pos-eval] {} optimistic SL move never confirmed "
                        "(dropped by aggregator) — rolled back optimistic state",
                        order_id,
                    )
                mgmt.sl_modify_pending_until = 0.0

            snap = build_position_snapshot(
                pos, tm_trade=mgmt, current_price=price,
                score_history=tuple(mgmt.score_history),
            )
            intents = self._worker.evaluate(snap, now, scan=scan_ctx, market=market_ctx)

            # Collapse multiple MODIFY_SL intents for this ticket to the single
            # tightest one BEFORE any optimistic mutation, mirroring the
            # IntentAggregator's tightest-SL resolution (LONG → highest new_sl,
            # SHORT → lowest). The worker can emit several SL moves per cycle
            # (profit-protection + dynamic-tighten + trail); without collapsing,
            # each overwrites mgmt.stop_loss AND the rollback baseline, so a later
            # rollback would restore an unconfirmed level instead of the original.
            if intents:
                _sl_intents = [
                    i for i in intents if i.intent_type == IntentType.MODIFY_SL
                ]
                if len(_sl_intents) > 1:
                    if direction.upper() in ("BUY", "LONG"):
                        _best_sl = max(
                            _sl_intents,
                            key=lambda i: i.new_sl if i.new_sl is not None else 0.0,
                        )
                    else:
                        _best_sl = min(
                            _sl_intents,
                            key=lambda i: i.new_sl if i.new_sl is not None else float("inf"),
                        )
                    intents = [
                        i for i in intents
                        if i.intent_type != IntentType.MODIFY_SL or i is _best_sl
                    ]

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
                        # Mark the optimistic SL as pending broker confirmation
                        # so the synthetic stop-hit check is suppressed until the
                        # modify result lands. Without this a broker rejection
                        # ("Invalid stops") leaves the unconfirmed SL in the
                        # snapshot long enough to fire a phantom stop-loss CLOSE.
                        mgmt.sl_modify_pending_until = now_mono + _SL_MODIFY_PENDING_GRACE_S
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
            return float(get_pip_size(resolve_to_internal(symbol)))
        except Exception:
            try:
                return float(get_pip_size(resolve_to_internal(symbol)))
            except Exception as exc:
                self._note_pip_size_fallback(symbol, exc)
                return 0.0001

    def _note_pip_size_fallback(self, symbol: str, exc: Exception) -> None:
        """Record + warn (once per symbol) that pip_size fell back to 0.0001.

        The 0.0001 fallback is correct only for FX majors.  For indices
        (0.01), metals (0.01) or synthetics (0.1–1.0) it is wrong by
        100×–10,000×, so any risk_pips derived from it is unreliable.  We
        surface that loudly — once per symbol to avoid log floods — so a
        silently mis-sized instrument is diagnosable instead of invisible.
        """
        fb = self._pip_size_fallback_symbols
        if symbol not in fb:
            fb.add(symbol)
            logger.warning(
                "[pip-size] {} fallback to 0.0001 — risk sizing may be "
                "inaccurate: {}", symbol, exc,
            )

    def _effective_pip_value(
        self, symbol: str, pip_size: Optional[float] = None,
    ) -> float:
        """Centralised money-per-pip-per-lot: registry fallback → broker truth.

        Mirrors ``EventDrivenSystem._effective_pip_value`` for the scale-in
        sizing path, sourcing the broker spec from the shared PlatformManager.
        Both routes derive the override through the one shared
        :func:`_broker_pip_value_from_spec`, so the scale-in add can never size
        on a different pip value from the heat monitor or the entry sizer.
        """
        info = INSTRUMENT_REGISTRY.get(symbol)
        fallback = info.pip_value_per_lot if info else 10.0
        if pip_size is None:
            pip_size = self._safe_pip_size(symbol)
        spec: dict = {}
        try:
            spec = self._pm.get_symbol_spec(symbol) or {}
        except Exception as exc:
            logger.debug(
                "[symbol-spec] scale-in pip-value spec lookup failed {}: {}",
                symbol, exc,
            )
        return _broker_pip_value_from_spec(spec, pip_size, fallback)

    def suppress_ticket(self, ticket: str, duration: float = 60.0) -> None:
        """Suppress evaluation of a ticket for the given duration (seconds)."""
        self._suppressed_tickets[ticket] = _time.monotonic() + duration

    def _compute_tick_momentum(
        self, symbol: str, norm_dir: str, pip_size: float,
    ) -> float:
        """Live sub-candle momentum from the recent tick stream → [-1, +1].

        Candle-derived momentum only refreshes on M1 close, so between closes the
        DecisionEngine is blind to price moving against an open position. This
        reads the recent stored ticks and measures the *directional efficiency*
        of the move — net displacement ÷ total path length, in [-1, +1]: a clean
        one-directional move scores near ±1, choppy/flat action near 0. The
        result is signed for the trade (rising price favours a long, falling a
        short) so the engine can react to a rapid adverse move immediately.

        Returns 0.0 (no pressure) when there are too few ticks, the path is flat,
        or the net move is below ~0.5 pip of micro-noise — so it never invents a
        pulse out of a stationary price.
        """
        try:
            ticks = self._tick_store.get_recent(symbol, count=20)
        except Exception:
            return 0.0
        if not ticks or len(ticks) < 5:
            return 0.0
        mids: list[float] = []
        for t in ticks:
            bid = float(getattr(t, "bid", 0.0) or 0.0)
            ask = float(getattr(t, "ask", 0.0) or 0.0)
            if bid > 0.0 and ask > 0.0:
                mids.append((bid + ask) / 2.0)
            elif bid > 0.0:
                mids.append(bid)
            elif ask > 0.0:
                mids.append(ask)
        if len(mids) < 5:
            return 0.0
        net = mids[-1] - mids[0]
        path = sum(abs(mids[i] - mids[i - 1]) for i in range(1, len(mids)))
        if path <= 0.0:
            return 0.0
        # Ignore sub-noise drift so a near-flat price never reads as momentum.
        if pip_size > 0.0 and abs(net) / pip_size < 0.5:
            return 0.0
        efficiency = max(-1.0, min(1.0, net / path))
        signed = efficiency if norm_dir == "BUY" else -efficiency
        return max(-1.0, min(1.0, signed))

    def _compute_tick_move_pips(
        self, symbol: str, norm_dir: str, pip_size: float,
    ) -> float:
        """Signed net displacement of the recent tick stream, in pips.

        Reads the SAME recent tick window as :meth:`_compute_tick_momentum` and
        returns the net (first→last) mid-price move converted to pips, signed
        for ``norm_dir`` (positive when price moved in the trade's favour). The
        hardened FlipConfirmer's ATR-normalised magnitude check divides this by
        the M5 ATR so a flip must ride a move of real size, not a single wick or
        a spread spike. Returns 0.0 when there are too few ticks or ``pip_size``
        is non-positive so a missing feed degrades the check to a graceful skip.
        """
        try:
            ticks = self._tick_store.get_recent(symbol, count=20)
        except Exception:
            return 0.0
        if not ticks or len(ticks) < 5 or pip_size <= 0.0:
            return 0.0
        mids: list[float] = []
        for t in ticks:
            bid = float(getattr(t, "bid", 0.0) or 0.0)
            ask = float(getattr(t, "ask", 0.0) or 0.0)
            if bid > 0.0 and ask > 0.0:
                mids.append((bid + ask) / 2.0)
            elif bid > 0.0:
                mids.append(bid)
            elif ask > 0.0:
                mids.append(ask)
        if len(mids) < 5:
            return 0.0
        net_pips = (mids[-1] - mids[0]) / pip_size
        return net_pips if norm_dir == "BUY" else -net_pips

    def _stamp_management_trace(
        self, symbol: str, order_id: str, trade_ctx, sa, de_result,
    ) -> None:
        """Record a STAGE_MANAGEMENT DecisionTrace for one management eval.

        Surfaces the live-position management verdict (the action chosen + the
        situation dimensions that drove it) onto the decision-trace panel. Uses a
        fresh per-eval recorder (thread-safe across concurrent positions) and the
        management completeness exemption (no ranker stage for an open trade).
        """
        from brain.decision_trace import DecisionTraceRecorder, STAGE_MANAGEMENT

        recorder = DecisionTraceRecorder(getattr(self._config, "decision_trace", None))
        if not recorder.enabled:
            return
        action = getattr(de_result, "action", None)
        action_str = action.value if hasattr(action, "value") else str(action or "HOLD")
        reason = str(getattr(de_result, "reason", "") or "")
        justification = reason[:240] if len(reason) >= 8 else f"management verdict {action_str}"
        evidence = {
            "order_id": str(order_id or ""),
            "action": action_str,
            "confidence": round(float(getattr(de_result, "confidence", 0.0) or 0.0), 4),
            "tf_alignment": round(float(getattr(sa, "tf_alignment", 0.0) or 0.0), 4),
            "momentum": round(float(getattr(sa, "momentum", 0.0) or 0.0), 4),
            "tick_momentum": round(float(getattr(sa, "tick_momentum", 0.0) or 0.0), 4),
            "structure_integrity": round(float(getattr(sa, "structure_integrity", 0.0) or 0.0), 4),
            "price_vs_structure": round(float(getattr(sa, "price_vs_structure", 0.0) or 0.0), 4),
            "profit_state": round(float(getattr(sa, "profit_state", 0.0) or 0.0), 4),
        }
        recorder.begin(symbol, setup_id=str(order_id or ""))
        recorder.stamp(
            stage=STAGE_MANAGEMENT,
            owner="decision.engine",
            verdict=action_str,
            justification=justification,
            evidence=evidence,
            confidence=float(getattr(de_result, "confidence", 0.0) or 0.0),
        )
        recorder.finalize_success()

    def shutdown(self) -> None:
        """Stop the per-symbol evaluation worker pool (called on system stop)."""
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass

    def _maybe_arm_reversal(
        self,
        order_id: str,
        symbol: str,
        direction: str,
        detail: dict,
    ) -> Optional[str]:
        """Decide whether a ``thesis_flip`` exit should become an atomic reversal.

        Called at the flip decision (before the exit-leg CLOSE is submitted).
        Consults the anti-ping-pong :class:`ReversalManager` with the competing
        thesis's effective edge over Flat (from the flip ``detail``). When a
        reversal is permitted, records a pending reversal keyed by the exit
        ticket and returns the reversal direction (``"LONG"``/``"SHORT"``).
        Returns ``None``
        (fall back to a plain evidence exit) when reversal is disabled, the
        manager rejects it (cooldown / per-session cap / escalating threshold),
        or anything errors — a reversal is never armed on a fault.

        Note: the reversal is *counted* (cooldown + session tally) only when it
        actually dispatches, not here, so an armed-but-never-executed reversal
        does not consume the budget.
        """
        ctx = self._ctx
        rm = getattr(ctx, "reversal_manager", None) if ctx is not None else None
        if rm is None or not getattr(rm, "enabled", False):
            return None
        try:
            want = "LONG" if str(direction or "").upper() in ("LONG", "BUY") else "SHORT"
            to_direction = "SHORT" if want == "LONG" else "LONG"
            try:
                competing_ev = float((detail or {}).get("opp_over_flat", 0.0) or 0.0)
            except (TypeError, ValueError):
                competing_ev = 0.0
            verdict = rm.evaluate(symbol, competing_ev)
            if not verdict.reverse:
                logger.info(
                    "[reversal] {} {} flip NOT reversing (plain evidence exit) — {}",
                    symbol, want, verdict.reason,
                )
                return None
            plan = {
                "symbol": symbol,
                "from_direction": want,
                "to_direction": to_direction,
                "competing_ev": competing_ev,
                "required_threshold": float(verdict.required_threshold),
                "reversals_so_far": int(verdict.reversals_so_far),
                "detail": dict(detail or {}),
                "decided_at": _time.time(),
            }
            self._pending_reversals[str(order_id)] = plan
            logger.info(
                "[reversal] {} ARMED {}→{} on close of ticket {} — {}",
                symbol, want, to_direction, order_id, verdict.reason,
            )
            return to_direction
        except Exception as exc:  # noqa: BLE001 — never fail into a reversal
            logger.warning(
                "[reversal] arm failed for {} {} — plain evidence exit: {}",
                symbol, direction, exc,
            )
            return None

    def _run_decision_engine_management(
        self, pos, price: float, now: datetime, now_mono: float,
        mgmt, order_id: str, snap: PositionSnapshot,
    ) -> None:
        """Legacy DecisionEngine management — RETIRED.

        The single Cognitive Brain is the sole market manager (Constitution
        Part VI/X). The deterministic protectors (PositionWorker trailing/
        breakeven/TP + portfolio heat) remain as the Part X safety floor.
        """
        return

    # ── Phase 5: developing-structure management advisory ────────────
    def _apply_developing_advisory(
        self,
        de_result,
        symbol: str,
        norm_dir: str,
        entry_price: float,
        price: float,
        current_sl: float,
        pnl_pips: float,
        trade_ctx,
    ):
        """Escalate HOLD/OBSERVE to a tightening protective stop on a developing
        reversal warning.

        The developing (forming-bar) WorldModel updates between candle closes,
        so it can flag a reversal against an open position long before the
        confirmed structure refreshes on the next candle close. This advisory
        reads that early-warning signal and, when the confirmed DecisionEngine
        verdict is only HOLD/OBSERVE, escalates to ``SET_PROTECTIVE_STOP``.

        Safety contract (the developing layer is advisory, never authoritative):

        * **No store → no-op.** When ``_developing_wm_store`` is None (every
          existing unit test, and any deployment with developing analysis off)
          this returns ``de_result`` unchanged.
        * **Never overrides a confirmed CLOSE / protective action.** Only acts
          when the verdict is HOLD or OBSERVE.
        * **Never opens, never loosens.** The only mutation is a protective stop
          at breakeven, and only when that strictly TIGHTENS the existing stop
          (moves it toward price / reduces risk). If it cannot tighten safely it
          annotates evidence and leaves the verdict untouched.
        """
        store = self._developing_wm_store
        if store is None:
            return de_result
        cfg = getattr(self._config, "developing_analysis", None) if self._config else None
        if cfg is not None and not getattr(cfg, "management_advisory_enabled", True):
            return de_result
        try:
            from decision.actions import Action

            action = getattr(de_result, "action", None)
            action_name = action.value if hasattr(action, "value") else str(action or "")
            # Only nudge a passive verdict — never override CLOSE or an already
            # protective/active action (TIGHTEN_SL, SET_PROTECTIVE_STOP,
            # MOVE_TO_BREAKEVEN, PARTIAL_CLOSE, SCALE_IN).
            if action_name not in (Action.HOLD.value, Action.OBSERVE.value):
                return de_result

            dev_wm = store.get(symbol)
            if dev_wm is None:
                return de_result
            dev_struct = dev_wm.structure_by_tf()
            if not dev_struct:
                return de_result

            min_conf = float(
                getattr(cfg, "management_advisory_min_confidence", 0.6)
                if cfg is not None else 0.6
            )
            opposed_tfs = self._developing_reversal_tfs(
                dev_struct, norm_dir, min_conf,
            )
            if not opposed_tfs:
                return de_result

            # Reversal warning present — try to tighten to a protective stop.
            protective_sl = self._tightening_breakeven_sl(
                norm_dir, entry_price, price, current_sl,
            )
            warn = "developing reversal on " + ",".join(opposed_tfs)
            if protective_sl is None:
                # Cannot tighten safely (e.g. not yet in profit) — leave the
                # verdict alone, just record the early warning for visibility.
                try:
                    de_result.evidence = list(getattr(de_result, "evidence", []) or [])
                    de_result.evidence.append(f"developing advisory: {warn} (no safe tighten)")
                except Exception:
                    pass
                return de_result

            de_result.action = Action.SET_PROTECTIVE_STOP
            de_result.new_sl = protective_sl
            try:
                de_result.evidence = list(getattr(de_result, "evidence", []) or [])
                de_result.evidence.append(f"developing advisory: {warn}")
            except Exception:
                pass
            prior_reason = str(getattr(de_result, "reason", "") or "")
            de_result.reason = (f"developing advisory protective stop ({warn})"
                                 + (f" | {prior_reason}" if prior_reason else ""))
            logger.info(
                "[dev-advisory] {} {} HOLD→SET_PROTECTIVE_STOP @ {:.5f} — {}",
                symbol, norm_dir, protective_sl, warn,
            )
            return de_result
        except Exception as exc:
            logger.debug("[dev-advisory] {} advisory skipped: {}", symbol, exc)
            return de_result

    @staticmethod
    def _developing_reversal_tfs(
        dev_struct: dict, norm_dir: str, min_conf: float,
    ) -> list[str]:
        """Tactical timeframes (H1/M5) whose developing structure opposes the
        position — by an opposing trend at/above ``min_conf`` OR a reversal
        event (CHoCH/BOS) against the trade. Returns the list of TFs warning.
        """
        # A BUY is opposed by bearish structure; a SELL by bullish structure.
        opp_trend = "BEARISH" if norm_dir == "BUY" else "BULLISH"
        opp_event_tag = "BEARISH" if norm_dir == "BUY" else "BULLISH"
        warning: list[str] = []
        for tf in ("H1", "M5"):
            trend, conf = _struct_trend_conf(dev_struct, tf)
            event = _struct_event(dev_struct, tf)
            trend_opposes = trend == opp_trend and conf >= min_conf
            event_opposes = (
                event in ("CHOCH_" + opp_event_tag, "BOS_" + opp_event_tag)
            )
            if trend_opposes or event_opposes:
                warning.append(tf)
        return warning

    @staticmethod
    def _tightening_breakeven_sl(
        norm_dir: str, entry_price: float, price: float, current_sl: float,
    ) -> Optional[float]:
        """A breakeven (entry-price) protective stop, but ONLY when it strictly
        tightens the current stop and sits on the valid side of price.

        Returns None when a breakeven move would loosen the stop or is invalid
        (e.g. position not yet in profit), so the advisory never increases risk.
        """
        if entry_price <= 0:
            return None
        be = entry_price
        cur = current_sl or 0.0
        if norm_dir == "BUY":
            # Valid long stop sits below price; tighten means raise the stop.
            if be >= price:
                return None
            if cur > 0 and be <= cur:
                return None  # would loosen (move stop down) or no change
            return be
        else:  # SELL
            # Valid short stop sits above price; tighten means lower the stop.
            if be <= price:
                return None
            if cur > 0 and be >= cur:
                return None  # would loosen (move stop up) or no change
            return be

    # ── Scale-in / partial-close handlers (V13) ──────────────────────
    def _cognition_management_allows(self, symbol: str, direction: str, action: str) -> bool:
        """AI Cognitive Brain management gate (Phase F) — fail-safe.

        Only exposure-ADDING management actions (scale-in / re-entry) are gated;
        de-risking always proceeds. Fail-open in the soft modes; fail-closed
        under ``authoritative`` (no reasoning ⇒ no new exposure).
        """
        ctx = self._ctx
        gate = getattr(ctx, "management_gate", None) if ctx is not None else None
        if gate is None:
            return True
        try:
            return bool(gate.evaluate(symbol, direction, action).allow)
        except Exception as exc:  # noqa: BLE001
            authoritative = str(getattr(gate, "mode", "")) == "authoritative"
            logger.warning(
                "[management-gate] {} errored — {} {} (fail-{}): {}",
                symbol, "blocking" if authoritative else "allowing", action,
                "closed" if authoritative else "open", exc,
            )
            return not authoritative

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
                except Exception as exc:
                    logger.warning(
                        "[scale-in] {} drawdown-guard risk read failed — "
                        "scale-in risk fraction left unclamped: {}", symbol, exc,
                    )
            if risk_pct <= 0:
                return

            pip_size = self._safe_pip_size(symbol)
            # Broker-truth money-per-pip (registry fallback → live broker spec),
            # centralised so the scale-in add is sized on the same pip value the
            # heat monitor and the entry sizer use.
            pip_value = self._effective_pip_value(symbol, pip_size)
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
                except Exception as exc:
                    daily_pnl, daily_cap = 0.0, 0.0
                    logger.warning(
                        "[scale-in] {} daily P&L/cap read failed — daily-loss "
                        "budget guard defaulting to 0.0/0.0: {}", symbol, exc,
                    )

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
            # Guard against repeated banking. The DE re-evaluates a position
            # every ``_de_interval`` seconds; without an optimistic flag a
            # persistent PARTIAL_CLOSE verdict would chip the position away on
            # every cycle. Skip when a partial is already taken/in-flight, then
            # mark it optimistically (mirrors the worker TP1-partial path) and
            # record the pre-mutation values so a rejected partial rolls back and
            # retries instead of being permanently marked done.
            mgmt = self._mgmt_store.get(order_id)
            if mgmt is not None and getattr(mgmt, "partial_closed", False):
                logger.debug(
                    "[de-mgmt] partial-close skipped {} — already partial-closed",
                    symbol,
                )
                return
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
            if mgmt is not None:
                prev_pc = mgmt.partial_closed
                prev_tp1 = mgmt.tp1_hit
                mgmt.partial_closed = True
                mgmt.tp1_hit = True
                self._record_inflight_manage(
                    order_id, IntentType.PARTIAL_CLOSE,
                    {"partial_closed": prev_pc, "tp1_hit": prev_tp1},
                )
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


# -- Execution lifecycle loops (extracted) --
# Phase K: extracted to execution.lifecycle_loops (behaviour-preserving).
from execution.lifecycle_loops import FlushLoop, TickEvalLoop  # noqa: E402


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

        # ── Live candle aggregator (Phase 1 — local tick-to-candle) ──
        # Builds OHLC candles for all timeframes from the live tick stream so
        # the Phase-2 developing analysis can run between candle closes with
        # zero broker fetches. Purely additive — the confirmed analysis path
        # (CandleCloseHandler → broker fetch on close) is untouched.
        from tick.live_candle_aggregator import LiveCandleAggregator
        self._live_candle_aggregator = LiveCandleAggregator(max_candles=200)
        self._tick_router.register_callback(self._live_candle_aggregator.on_tick)

        # ── Developing analysis store (Phase 2 — live/forming-bar view) ──
        # A SEPARATE WorldModelStore holding the continuously-evolving
        # developing analysis. The confirmed CandleCloseHandler reads it to
        # blend developing structure into bias CONFIDENCE (never direction).
        # Kept distinct from ``self._wm_store`` so developing data can never
        # corrupt confirmed, non-repainting WorldModels.
        self._developing_wm_store = WorldModelStore()
        # Session 26 — continuous (sub-candle) thesis re-evaluation state. The
        # developing store fires ``_on_developing_update`` on every publish; a
        # per-symbol monotonic timestamp debounces bursts so several TF publishes
        # landing together can't re-evaluate the same symbol repeatedly.
        self._developing_reeval_at: dict[str, float] = {}
        self._developing_reeval_lock = threading.Lock()

        # ── Learned analysis edge ────────────────────────────────────
        # Bounded, default-neutral multipliers that make the analysis
        # combination data-driven: zone conviction and per-concept bias are
        # scaled by realized trade outcomes recorded on the close path.
        self._zone_edge = ZoneEdgeTracker()
        # Per-ticket entry context (zone_type, regime, concepts) captured at
        # fill time so the close path can attribute the outcome to the right
        # learned keys.
        self._entry_context: dict[Any, dict[str, Any]] = {}
        # Per-ticket candidate provenance (Session 4 multi-opportunity). Links
        # an open position back to the Candidate (and therefore the exact
        # modules + timeframes) that voted it open, so management can be scoped
        # to that same panel rather than the latest net-summed direction.
        self._candidate_positions: dict[str, "CandidatePosition"] = {}

        # Atomic reversal (Session 29) — opposite-direction entries armed at a
        # reversal-viable thesis_flip, keyed by the exit leg's ticket. Shared
        # with the evaluator (which arms them in the management path) via the
        # aliasing below, and drained by _handle_close_result once the exit
        # leg's close confirms (close → open sequencing).
        self._pending_reversals: dict[str, dict[str, Any]] = {}

        # across the system-close path (_handle_close_result) and the
        # external-close reconciliation path (_reconcile_external_closes).
        self._closed_tickets: dict[str, float] = {}
        self._closed_tickets_lock = threading.Lock()
        # Running count of trades closed through the learning feedback path.
        # Seeded from persisted history at startup (_seed_learning_counters)
        # and incremented on every close so the periodic learning-recompute
        # cadence (counterfactual / signal-discovery / interaction / behavior
        # maybe_recompute, all gated on TuneContext.total_trades) actually
        # fires instead of being pinned off by a hardcoded zero.
        self._closed_trade_count: int = 0
        # Running consecutive-loss streak across closed trades. A genuine loss
        # (realized P&L < 0) increments it; any win or scratch resets it to 0.
        # Feeds AdaptiveOptimizer.notify_loss_streak so a losing run can force an
        # out-of-band retrain instead of waiting for the periodic cycle.
        self._consecutive_losses: int = 0
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

        # Symbols whose pip_size lookup fell back to the 0.0001 default. Used to
        # warn once per symbol and to tag entry context (``pip_size_fallback``)
        # so downstream attribution knows the risk sizing used a guessed pip.
        self._pip_size_fallback_symbols: set[str] = set()

        # ── Analysis plane ───────────────────────────────────────────
        # CalibrationEngine (single writer of per-instrument stats). Opt-in via
        # config.calibration.enabled — when off, no provider is registered and
        # get_profile keeps returning the hardcoded category constants.
        self._calibration_engine = None
        _calib_cfg = getattr(self._config, "calibration", None)
        if _calib_cfg is not None and getattr(_calib_cfg, "enabled", False):
            try:
                from brain.calibration_engine import CalibrationEngine
                from brain.instrument_profile import set_stats_provider
                self._calibration_engine = CalibrationEngine(
                    state_path=getattr(
                        _calib_cfg, "state_path", "data/calibration_state.json"
                    ),
                )
                if getattr(_calib_cfg, "persist", True):
                    self._calibration_engine.load()
                set_stats_provider(self._calibration_engine)
                logger.info(
                    "[event-driven] CalibrationEngine ACTIVE — get_profile is "
                    "self-calibrating (state={})",
                    self._calibration_engine.state_path,
                )
            except Exception as exc:
                logger.warning("[event-driven] CalibrationEngine init failed: {}", exc)
                self._calibration_engine = None

        # News-impact measurement trigger — only active when the CalibrationEngine
        # is on. It captures price at high-impact news events on the M5 heartbeat
        # and measures the reaction ~30 min later to feed learned news sensitivity.
        self._news_impact_tracker = None
        if self._calibration_engine is not None:
            try:
                from brain.news_impact_tracker import NewsImpactTracker
                self._news_impact_tracker = NewsImpactTracker(
                    self._calibration_engine,
                    news_guard=getattr(ctx, "news_guard", None) if ctx else None,
                )
            except Exception as exc:
                logger.warning("[event-driven] NewsImpactTracker init failed: {}", exc)
                self._news_impact_tracker = None

        # ── Global compression / market-state detector ───────────────
        # Classifies each instrument as TRENDING / RANGING / COMPRESSING /
        # EXPANDING from a rolling Bollinger-Band-width percentile plus ADX on
        # M5/M15 closes, tuned per-instrument via InstrumentProfile. Fed by the
        # CandleCloseHandler and read (logging-only for now) by the entry
        # orchestrator. Best-effort — a construction fault degrades to no
        # market-state signal rather than breaking startup.
        self._compression_detector = None
        try:
            self._compression_detector = CompressionDetector(entry_config=EntryConfig())
        except Exception as exc:
            logger.warning("[event-driven] CompressionDetector init failed: {}", exc)
            self._compression_detector = None

        # ── Global session classifier ─────────────────────────────────
        # Stateless UTC-time → TradingSession (ASIAN / LONDON / NY /
        # LONDON_NY_OVERLAP) classifier with per-instrument size multipliers and
        # zone weights, tuned via InstrumentProfile (EntryConfig fallback). Read
        # (logging-only for now) by the entry orchestrator on each entry eval;
        # session-aware sizing lands in a later PR. Best-effort — a construction
        # fault degrades to no session signal rather than breaking startup.
        self._session_context = None
        try:
            self._session_context = SessionContext(entry_config=EntryConfig())
        except Exception as exc:
            logger.warning("[event-driven] SessionContext init failed: {}", exc)
            self._session_context = None

        # ── USD-strength meter (Phase 2 Feature D — DXY correlation) ────
        # Ranks USD strength from the major USD pairs so the entry orchestrator
        # can penalise (never block) a gold/USD trade whose direction opposes
        # the USD move. The reading is cached briefly (~5 min) to avoid
        # re-fetching every pair on each entry evaluation. Best-effort — a
        # construction fault degrades to no DXY vote.
        self._currency_strength_meter = None
        self._dxy_strength_cache: Optional[Any] = None
        self._dxy_strength_cache_ts: float = 0.0
        try:
            self._currency_strength_meter = CurrencyStrengthMeter()
        except Exception as exc:
            logger.warning("[event-driven] CurrencyStrengthMeter init failed: {}", exc)
            self._currency_strength_meter = None

        self._candle_handler = CandleCloseHandler(
            event_bus=self._event_bus,
            world_model_store=self._wm_store,
            candle_fetcher=self._fetch_candles,
            edge_weight=self._zone_edge.zone_weight,
            concept_weight=self._zone_edge.concept_weight,
            vote_calibrator=getattr(ctx, "vote_calibrator", None) if ctx else None,
            module_governor=getattr(ctx, "module_governor", None) if ctx else None,
            win_rate_provider=getattr(ctx, "win_rate_provider", None) if ctx else None,
            consensus_config=getattr(self._config, "consensus", None),
            ranker_config=getattr(self._config, "opportunity_ranker", None),
            dynamic_weight_config=getattr(self._config, "dynamic_weights", None),
            calibration_engine=self._calibration_engine,
            get_spread_pips=self._get_spread_pips,
            calibration_spread_tf=getattr(_calib_cfg, "spread_sample_tf", "M5"),
            news_impact_tracker=self._news_impact_tracker,
            developing_store=self._developing_wm_store,
            compression_detector=self._compression_detector,
        )

        # ── Developing analysis loop (Phase 2) ───────────────────────
        # Background thread that runs the brain modules on forming candles from
        # the LiveCandleAggregator and publishes to the developing store. Opt-in
        # via config.developing_analysis.enabled (default True). Purely additive
        # — failures never affect the confirmed path.
        self._developing_loop = None
        _dev_cfg = getattr(self._config, "developing_analysis", None)
        if _dev_cfg is None:
            from config import DevelopingAnalysisConfig
            _dev_cfg = DevelopingAnalysisConfig()
        if getattr(_dev_cfg, "enabled", False):
            try:
                from brain.developing_analysis import DevelopingAnalysisLoop
                self._developing_loop = DevelopingAnalysisLoop(
                    candle_aggregator=self._live_candle_aggregator,
                    developing_store=self._developing_wm_store,
                    symbols=list(self._config.enabled_pairs),
                    config=_dev_cfg,
                )
            except Exception as exc:
                logger.warning(
                    "[event-driven] DevelopingAnalysisLoop init failed: {}", exc,
                )
                self._developing_loop = None

        # Session 26 — wire the developing store's change notification to the
        # continuous thesis re-evaluation. Registered whenever the developing
        # loop is live so a sub-candle probability shift updates the thesis the
        # instant it publishes, not on the next candle close. Fully guarded
        # (``_on_developing_update`` no-ops when the engine/config is absent or
        # continuous re-eval is disabled), so this is behaviour-neutral until
        # both the developing loop and a ThesisEngine are present.
        if self._developing_loop is not None:
            try:
                self._developing_wm_store.set_on_publish(self._on_developing_update)
            except Exception as exc:
                logger.warning(
                    "[event-driven] developing thesis re-eval wiring failed: {}",
                    exc,
                )

        # ── Phase 6: adaptive scheduler (continuous-learning closure) ──
        # Drives the time-based learning cadences (TunerAgent.on_periodic_tick /
        # on_scan_cycle) that were dormant in production — only on_trade_close
        # fired live — plus the AdaptiveWeightProvider recompute. Best-effort.
        self._adaptive_scheduler = None
        _at_cfg = getattr(self._config, "adaptive_tuner", None)
        if _at_cfg is not None and bool(getattr(_at_cfg, "enabled", True)) and ctx is not None:
            try:
                from adaptive.adaptive_scheduler import AdaptiveSchedulerLoop
                self._adaptive_scheduler = AdaptiveSchedulerLoop(
                    tuner_agent=ctx.tuner_agent,
                    weight_provider=ctx.adaptive_weight_provider,
                    trade_count_provider=lambda: getattr(self, "_closed_trade_count", 0),
                    periodic_interval_seconds=float(
                        getattr(_at_cfg, "periodic_interval_seconds", 900.0)
                    ),
                    scan_interval_seconds=float(
                        getattr(_at_cfg, "scan_interval_seconds", 3600.0)
                    ),
                    enabled=True,
                )
            except Exception as exc:
                logger.warning(
                    "[event-driven] AdaptiveSchedulerLoop init failed: {}", exc,
                )
                self._adaptive_scheduler = None

        # ── Cross-instrument opportunity layer (GAP 1) ────────────────
        # The GlobalOpportunityQueue (collect → cross-instrument rank →
        # dispatch) is RETIRED with the legacy entry pipeline — its only sink
        # was the retired entry-decision path. The ProactiveOpportunityScanner
        # (pre-heat watchlist) remains; default OFF via config.cross_instrument.
        self._opportunity_queue = None
        self._proactive_scanner = None
        _ci_cfg = getattr(self._config, "cross_instrument", None)
        if _ci_cfg is not None and ctx is not None:
            try:
                from brain.proactive_scanner import ProactiveOpportunityScanner
                self._proactive_scanner = ProactiveOpportunityScanner(
                    symbols_provider=lambda: list(self._config.enabled_pairs),
                    get_world_model=self._wm_store.get,
                    enabled=bool(getattr(_ci_cfg, "proactive_scan_enabled", False)),
                    interval_seconds=float(
                        getattr(_ci_cfg, "proactive_scan_interval_seconds", 60.0)
                    ),
                    min_ev=float(getattr(_ci_cfg, "proactive_scan_min_ev", 0.5)),
                    density_tracker=getattr(ctx, "opportunity_density_tracker", None),
                    event_publish=self._event_bus.publish,
                )
            except Exception as exc:
                logger.warning(
                    "[event-driven] ProactiveOpportunityScanner init failed: {}", exc,
                )
                self._proactive_scanner = None

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
            developing_world_model_store=self._developing_wm_store,
        )
        # Share ONE candidate-provenance map between this system (fill-time
        # recorder, close-time cleanup, dashboard read) and the evaluator
        # (candidate-scoped management read). Without this, provenance recorded
        # at fill never reaches the manager and the manager raised AttributeError
        # every cycle (it had no _candidate_positions of its own).
        self._evaluator._candidate_positions = self._candidate_positions
        # Share ONE in-flight rollback store between this system (the FlushLoop
        # result callback rolls back / clears here) and the evaluator (which
        # records the optimistic mutation). Without this the evaluator recorded
        # into its own dict that the result callback never read, so the
        # pending-confirmation guard line after the record never ran (the call
        # raised AttributeError) — leaving an unconfirmed/rejected SL live in the
        # snapshot with no guard, the exact phantom stop-hit CLOSE condition.
        self._evaluator._inflight_manage = self._inflight_manage
        self._evaluator._inflight_manage_lock = self._inflight_manage_lock
        # Share ONE pending-reversal map (Session 29): the evaluator arms a
        # reversal in the management path; _handle_close_result (this system)
        # drains it once the exit leg's close confirms. Without this the armed
        # reversal would land in a dict the close-result path never reads.
        self._evaluator._pending_reversals = self._pending_reversals
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
        # Fast-then-slow flip sequencing tracker (Session 16, flip Check 7).
        # Stateful — fed M1/M5 closes below and read by the orchestrator's
        # FlipConfirmer. OFF by default per InstrumentProfile
        # (flip_require_sequence=False), so it only ever gates flips once a
        # profile opts in; feeding it always keeps its state warm.
        self._flip_sequence_tracker = FlipSequenceTracker(
            pip_size_lookup=self._safe_pip_size,
        )

        # ── Zone order stager (Phase 2 Feature B) ────────────────────
        # Pre-stages pending LIMIT orders at zone boundaries as price approaches
        # a zone, running the FULL gate pipeline (orchestrator gates + compliance
        # + portfolio + correlation) BEFORE staging. OFF by default per
        # EntryConfig; a profile (Gold) opts in via pre_staging_enabled. Fed by
        # the tick bus and ZoneWatcher update callbacks (registered here).
        # Best-effort — a construction fault degrades to no pre-staging.
        # Zone order stager RETIRED with the legacy entry pipeline (Cognitive
        # Reasoning Constitution). It only ever staged legacy-decision orders and
        # self-gated off under single-path; left permanently disabled.
        self._zone_stager = None
        self._stage_sizer = PositionSizer()

        # ── News calendar pre-planner (Phase 3 Feature A) ────────────
        # For scheduled HIGH-impact events (CPI/NFP/FOMC) it stages an OCO pair
        # of breakout STOP orders straddling the pre-event range instead of
        # blocking around the event. OFF by default per EntryConfig; a profile
        # (Gold) opts in via news_pre_planning_enabled. Fed every ~60s from the
        # watchdog loop (_run_news_planner_update). Best-effort — a construction
        # fault degrades to no pre-planning (the old news block still applies).
        self._news_planner = None
        try:
            self._news_planner = NewsPlanner(
                config=EntryConfig(),
                get_symbols=lambda: list(self._config.enabled_pairs),
                get_events=self._news_upcoming_events,
                get_candles=self._news_range_candles,
                place_pending_order=self._stage_place_pending,
                cancel_pending_order=self._stage_cancel_pending,
                is_filled=self._news_order_filled,
                pip_size_lookup=self._safe_pip_size,
                size_lookup=self._news_size,
            )
        except Exception as exc:
            logger.warning("[event-driven] NewsPlanner init failed: {}", exc)
            self._news_planner = None

        # ── Compliance Division runtime binding ──────────────────────
        # The ComplianceDivision is constructed in SystemContext.create()
        # with the subsystem references it owns; bind the broker/platform-
        # dependent callables here (the bootstrap holds the PlatformManager
        # and the broker-truth helpers).  This makes Compliance the single
        # authoritative permit layer consulted on the entry origination path.
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

        # ── Governance Division readiness (Department 8) ─────────────
        # Governance is constructed in SystemContext.create() and installed as
        # the authoriser on the Learning→Governance RecommendationGateway there
        # (Learning recommends → Governance authorises → behaviour changes). No
        # runtime binding is needed here — the enforcement arms (ModuleGovernor,
        # TunerAgent) are injected at construction — so the spine only confirms
        # the authoriser is live for visibility. Never raises.
        if ctx is not None and ctx.governance is not None:
            try:
                gw = ctx.recommendation_gateway
                logger.info(
                    "[event-driven] Governance Division active — "
                    "recommendation authoriser={}, governance_required={}",
                    bool(gw is not None and getattr(gw, "has_authorizer", False)),
                    bool(gw is not None and getattr(gw, "governance_required", False)),
                )
            except Exception as exc:
                logger.debug("[event-driven] governance readiness log failed: {}", exc)

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
            should_poll=self._should_poll_symbol,
        )
        self._deriv_adapter = DerivTickAdapter(
            self._pm, self._tick_router, deriv_symbols,
            should_poll=self._should_poll_symbol,
        )

        # ── Event wiring ─────────────────────────────────────────────
        # Single Reasoner (Constitution I.4 / III.2): the one AI Cognitive Brain
        # is the sole entry authority and originates via the cognition loop, so
        # the legacy entry-decision pipeline is never subscribed to the tick /
        # M1-close / world-model events. The analysis feeds (WorldModel,
        # developing analysis, scanner) that populate the Brain's Evidence, and
        # the management / execution / safety wiring below, are untouched — so the
        # Brain still gets full evidence and open positions are still managed and
        # protected.
        # Zone-order stager is retired (self._zone_stager is None); the guard is
        # kept null-safe should a profile ever re-enable pre-staging.
        if self._zone_stager is not None:
            self._event_bus.subscribe("tick", self._zone_stager.on_tick)
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
        # Feed the fast-then-slow flip sequence tracker (flip Check 7): M1 closes
        # arm the fast confirmation, M5 closes provide the slow confirmation and
        # advance the tracker's M5-bar clock. Both dispatched off the entry pool
        # so a slow momentum read never stalls the event bus.
        self._event_bus.subscribe(
            "candle_close:M1",
            lambda ev: self._entry_pool.submit(
                self._feed_flip_sequence_m1, ev.symbol,
            ),
        )
        self._event_bus.subscribe(
            "candle_close:M5",
            lambda ev: self._entry_pool.submit(
                self._feed_flip_sequence_m5, ev.symbol,
            ),
        )
        self._event_bus.subscribe(
            "world_model_update", self._on_world_model_update,
        )

        self._be_stop_cooldown: dict[str, float] = {}
        # Retuned for aggressive gold scalping: after a breakeven stop the
        # system should be ready to re-enter quickly once a new setup forms.
        self._be_cooldown_seconds = 30.0
        # General per-symbol re-entry cooldown for the ZONE entry path: last
        # unix-ts a position closed per symbol. Prevents the zone path from
        # re-arming on the very next M1 close after any exit (not just BE
        # exits). Consensus/trigger entries use their own cooldown below.
        # Retuned to 15s for aggressive gold scalping: long enough to prevent
        # duplicate entries on the same candle, short enough to re-enter on the
        # next zone touch so the system can ride a trend in pieces.
        self._last_close_time: dict[str, float] = {}
        self._zone_reentry_cooldown_seconds = 15.0
        # Re-fire debounce for the ACTIVE consensus entry trigger (Phase 4):
        # last unix-ts a zoneless consensus entry was dispatched per symbol, so
        # a standing thesis is not re-submitted every candle close between fills.
        self._consensus_entry_cooldown: dict[str, float] = {}
        self._paused = False
        # Edge-triggered market-open logging state: symbol -> last-logged open?
        # Lets a CLOSED symbol log once then go silent (no weekend flood); only a
        # transition (closed↔open) logs again. Cosmetic — never gates decisions.
        self._market_open_state: dict[str, bool] = {}
        self._market_open_log_lock = threading.Lock()
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
            for sym in self._config.enabled_pairs:
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

        # VoteCalibrator / ModuleGovernor / Counterfactual / InteractionAnalyzer
        # / SignalDiscovery / VirtualSignalManager tunables — RETIRED with the
        # directional vote/consensus + offline-adaptive subsystem. Nothing to
        # register (their engines and ctx fields are deleted).

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
            except Exception as exc:
                logger.debug(
                    "[tuner] ConsumerTunable register failed for {}: {}",
                    name, exc,
                )

        # Wire set_tuner_agent on components that support it
        for comp in (ctx.ml_adapter, ctx.gate_tuner, ctx.calibrator,
                     ctx.signal_ledger, ctx.capital_allocator,
                     ctx.execution_profiles, ctx.regime_detector):
            if comp is not None and hasattr(comp, "set_tuner_agent"):
                try:
                    comp.set_tuner_agent(agent)
                except Exception as exc:
                    logger.debug(
                        "[tuner] set_tuner_agent failed for {}: {}",
                        type(comp).__name__, exc,
                    )

        logger.info(
            "[tuner] registered {} tunable adapters with TunerAgent", registered,
        )

    def _build_llm_evidence_source(self):
        """Return a callable yielding ``{symbol: structured_evidence}`` for the
        LLM worker.

        Reads the ThesisEngine's competing-thesis status (and per-symbol campaign
        context where available) — a read of already-computed state, so it never
        touches the hot path, blocks, or triggers analysis. Fully fail-safe.
        """
        ctx = self._ctx

        def _source() -> "dict[str, dict]":
            out: dict[str, dict] = {}
            try:
                engine = getattr(ctx, "thesis_engine", None) if ctx is not None else None
                if engine is None:
                    return out
                status = engine.get_status() or {}
                theses = status.get("theses") or {}
                camp_live: dict[str, dict] = {}
                registry = getattr(ctx, "campaign_registry", None) if ctx is not None else None
                if registry is not None:
                    try:
                        for c in (registry.get_status() or {}).get("live", []) or []:
                            camp_live[str(c.get("symbol"))] = c
                    except Exception:  # noqa: BLE001
                        camp_live = {}
                for sym, tdict in theses.items():
                    ev: dict = {"thesis": tdict}
                    if str(sym) in camp_live:
                        ev["campaign"] = camp_live[str(sym)]
                    out[str(sym)] = ev
            except Exception as exc:  # noqa: BLE001
                logger.debug("[llm-worker] evidence build fault: {}", exc)
            return out

        return _source

    def _make_origination_sink(self):
        """Build the LIVE origination sink for Brain-originated entries (Phase G).

        The returned callable accepts a
        :class:`cognition.campaign_translator.OriginationIntent` and submits an
        :class:`~execution.intents.Intent` OPEN onto the shared aggregator — the
        SAME execution plane (aggregator → RiskGate → broker) as every other
        entry. Fail-safe: a malformed intent, a missing protective stop, or any
        fault is logged and dropped (never raises into the cognition loop). A
        missing stop is refused because an entry without a stop would bypass the
        constitutional deterministic-safety floor (Part X).
        """
        def _sink(origination: Any) -> None:
            try:
                symbol = str(getattr(origination, "symbol", "") or "")
                direction = str(getattr(origination, "direction", "") or "").upper()
                sl = getattr(origination, "sl", None)
                tp = getattr(origination, "tp", None)
                if not symbol or direction not in ("LONG", "SHORT"):
                    return
                # Part X — every entry MUST carry a protective stop. When the
                # Brain's spec omits one, derive it deterministically from the
                # live price + structure rather than refusing to trade.
                if sl is None or tp is None:
                    sl, tp = self._derive_origination_levels(symbol, direction)
                if sl is None or tp is None:
                    logger.info(
                        "[origination-sink] {} {} skipped — could not derive a "
                        "protective stop/target (Part X safety floor)",
                        symbol, direction,
                    )
                    return
                # The Brain 'chooses' a concrete, broker-valid lot (never 0 →
                # broker) — conviction-scaled risk snapped to the instrument's
                # volume ladder. Decline cleanly if none can be formed.
                confidence = float(getattr(origination, "confidence", 0.0) or 0.0)
                lots = self._origination_lot_size(symbol, direction, sl, confidence)
                if lots <= 0:
                    logger.info(
                        "[origination-sink] {} {} skipped — no broker-valid lot "
                        "(risk below broker minimum; enable opportunity-harvest to take min lot)",
                        symbol, direction,
                    )
                    return
                # Part XIX Art 1/7 — the market is presumed noise: only proceed
                # if expected NET value after real round-trip cost is positive.
                if not self._opportunity_qualifies(
                    symbol, direction, sl, tp, lots, confidence,
                ):
                    return
                stake = getattr(origination, "stake_usd", None)
                idem_key = generate_idempotency_key(symbol, direction, lots)
                comment = build_order_comment(
                    "APEX", idem_key,
                    score=round(confidence, 4),
                )
                self._aggregator.submit([Intent.open(
                    symbol=symbol,
                    direction=direction,
                    lots=lots,
                    sl=float(sl),
                    tp=float(tp),
                    stake_usd=None,
                    comment=comment,
                    idempotency_key=idem_key,
                    source="ai_brain",
                    reason=str(getattr(origination, "reason", "") or "brain_origination")[:200],
                )])
                logger.info(
                    "[origination-sink] submitted {} {} ({:.2f} lots) from campaign {}",
                    symbol, direction, lots,
                    getattr(origination, "campaign_id", ""),
                )
            except Exception as exc:  # noqa: BLE001 — sink must never break the loop
                logger.warning("[origination-sink] submit failed: {}", exc)

        return _sink

    def _derive_origination_levels(self, symbol: str, direction: str):
        """Deterministic protective ``(sl, tp)`` for a Brain-originated entry.

        Reads the live price (tick) and the WorldModel's structural targets, then
        defers the arithmetic to the pure
        :func:`cognition.campaign_translator.derive_protective_levels`. Honors the
        Part X safety floor: returns ``(None, None)`` when it cannot form a valid,
        correctly-ordered stop/target (the sink then declines to submit). Never
        raises.
        """
        try:
            from cognition.campaign_translator import derive_protective_levels

            tick = self._tick_store.get_latest(symbol) if self._tick_store is not None else None
            if tick is None:
                return (None, None)
            is_long = direction == "LONG"
            entry = float(getattr(tick, "ask", 0.0) if is_long
                          else getattr(tick, "bid", 0.0)) or float(getattr(tick, "mid", 0.0) or 0.0)
            if entry <= 0:
                return (None, None)
            cog = getattr(self._config, "cognition", None)
            stop_fraction = float(getattr(cog, "origination_stop_fraction", 0.004)
                                  if cog is not None else 0.004)
            reward_multiple = float(getattr(cog, "origination_reward_multiple", 2.0)
                                    if cog is not None else 2.0)
            min_rr = float(getattr(cog, "origination_min_rr", 1.0)
                           if cog is not None else 1.0)
            targets = None
            try:
                wm = self._wm_store.get(symbol) if self._wm_store is not None else None
                if wm is not None:
                    targets = wm.get_structural_targets(
                        direction, entry, min_distance=entry * stop_fraction * max(min_rr, 0.0),
                    )
            except Exception as exc:  # noqa: BLE001 — structure is best-effort
                logger.debug("[origination-sink] structural targets failed for {}: {}", symbol, exc)
                targets = None
            return derive_protective_levels(
                direction, entry,
                stop_fraction=stop_fraction,
                reward_multiple=reward_multiple,
                min_rr=min_rr,
                structural_targets=targets,
            )
        except Exception as exc:  # noqa: BLE001 — never break the sink
            logger.debug("[origination-sink] level derivation failed for {}: {}", symbol, exc)
            return (None, None)

    def _origination_lot_size(self, symbol, direction, sl, confidence, entry_price=None):
        """Deterministic, broker-valid lot the Brain 'chooses' for an entry.

        Like a human trader who names an exact lot aware of the broker minimum:
        converts a conviction-scaled risk budget into lots via the instrument's
        tick value, snaps to the broker's volume step/min/max, and applies the
        optional operator cap. Returns ``0.0`` (caller skips) when no broker-valid
        lot can be formed and the opportunity-harvest opt-in is off. Fail-safe —
        never returns a sub-minimum or zero-through-to-broker value.
        """
        try:
            from cognition.campaign_translator import compute_lot_size

            is_long = str(direction or "").upper() == "LONG"
            entry = entry_price
            if entry is None:
                tick = self._tick_store.get_latest(symbol) if self._tick_store is not None else None
                if tick is not None:
                    entry = float(getattr(tick, "ask", 0.0) if is_long
                                  else getattr(tick, "bid", 0.0)) or float(getattr(tick, "mid", 0.0) or 0.0)
            entry = float(entry or 0.0)
            if entry <= 0 or sl is None:
                return 0.0
            spec = {}
            try:
                spec = self._pm.get_symbol_spec(symbol) or {} if self._pm is not None else {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("[origination-sink] symbol spec failed for {}: {}", symbol, exc)
                spec = {}
            vol_min = float(spec.get("volume_min") or 0.01)
            vol_step = float(spec.get("volume_step") or 0.01)
            vol_max = float(spec.get("volume_max") or 1e9)
            tick_value = float(spec.get("trade_tick_value") or 0.0)
            tick_size = float(spec.get("trade_tick_size") or 0.0)
            try:
                balance = float(self._pm.get_platform_balance(symbol) or 0.0) if self._pm is not None else 0.0
            except Exception:  # noqa: BLE001
                balance = 0.0
            cog = getattr(self._config, "cognition", None)
            risk_frac = float(getattr(cog, "origination_risk_fraction", 0.01) if cog is not None else 0.01)
            max_lots = float(getattr(cog, "origination_max_lots", 0.0) if cog is not None else 0.0)
            # Conviction scaling — higher confidence ⇒ larger size within a
            # bounded 0.5×–1.5× band, so the Brain varies lot size with the read.
            conf = max(0.0, min(1.0, float(confidence or 0.0)))
            risk_usd = balance * risk_frac * (0.5 + conf)
            risk_cfg = getattr(self._config, "risk", None)
            floor_to_min = bool(
                getattr(risk_cfg, "allow_min_lot_over_risk", False)
                if risk_cfg is not None else False
            )
            return compute_lot_size(
                risk_usd=risk_usd, stop_distance=abs(entry - float(sl)),
                tick_value=tick_value, tick_size=tick_size,
                vol_min=vol_min, vol_step=vol_step, vol_max=vol_max,
                max_lots=max_lots, floor_to_min=floor_to_min,
            )
        except Exception as exc:  # noqa: BLE001 — sizing must never break the sink
            logger.debug("[origination-sink] lot sizing failed for {}: {}", symbol, exc)
            return 0.0

    def _money_per_price(self, symbol: str, lots: float) -> float:
        """Account currency per 1.0 unit of price move for ``lots`` (0 if unknown)."""
        try:
            spec = self._pm.get_symbol_spec(symbol) or {} if self._pm is not None else {}
            tv = float(spec.get("trade_tick_value") or 0.0)
            ts = float(spec.get("trade_tick_size") or 0.0)
            if tv <= 0.0 or ts <= 0.0:
                return 0.0
            return (tv / ts) * float(lots or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def _estimate_round_trip_cost(self, symbol: str, lots: float) -> float:
        """Estimate round-trip cost in account currency (Part XIX Art 7).

        cost = spread + commission + slippage. Spread/slippage are converted from
        price via money-per-price; commission is per-lot (self-calibrated from
        realised closed deals, else the configured fallback). Returns 0.0 when it
        cannot be estimated (the caller then fail-opens rather than block trading).
        """
        try:
            lots = float(lots or 0.0)
            mpp = self._money_per_price(symbol, lots)
            if mpp <= 0.0 or lots <= 0.0:
                return 0.0
            pip = self._safe_pip_size(symbol)
            spread_pips = 0.0
            try:
                spread_pips = float(self._get_spread_pips(symbol) or 0.0)
            except Exception:  # noqa: BLE001
                spread_pips = 0.0
            cog = getattr(self._config, "cognition", None)
            slippage_pips = 1.0  # conservative default entry+exit slippage buffer
            spread_ccy = max(0.0, spread_pips) * pip * mpp
            slippage_ccy = slippage_pips * pip * mpp
            comm_per_lot = float(
                getattr(self, "_commission_per_lot_est", 0.0) or 0.0
            )
            if comm_per_lot <= 0.0 and cog is not None:
                comm_per_lot = float(getattr(cog, "commission_per_lot_round_trip", 0.0) or 0.0)
            commission_ccy = max(0.0, comm_per_lot) * lots
            return spread_ccy + slippage_ccy + commission_ccy
        except Exception:  # noqa: BLE001
            return 0.0

    def _opportunity_qualifies(
        self, symbol: str, direction: str, sl, tp, lots: float, confidence: float,
    ) -> bool:
        """Gate an entry/scale on positive expected NET value (Part XIX Art 1/7/8).

        Fail-closed: when the cost or money conversion cannot be estimated
        (missing broker spec / spread), the gate DECLINES the trade — expected
        value cannot be established, so the opportunity does not qualify. Gated
        off via config (opportunity_qualification_enabled=False restores the
        unconditional pass).
        """
        try:
            cog = getattr(self._config, "cognition", None)
            if cog is not None and not bool(
                getattr(cog, "opportunity_qualification_enabled", True)
            ):
                return True
            tick = self._tick_store.get_latest(symbol) if self._tick_store is not None else None
            is_long = str(direction or "").upper() == "LONG"
            entry = 0.0
            if tick is not None:
                entry = float(
                    (getattr(tick, "ask", 0.0) if is_long else getattr(tick, "bid", 0.0))
                    or getattr(tick, "mid", 0.0) or 0.0
                )
            mpp = self._money_per_price(symbol, lots)
            cost = self._estimate_round_trip_cost(symbol, lots)
            if entry <= 0.0 or mpp <= 0.0 or cost <= 0.0:
                return False  # cannot qualify → fail-closed (decline the trade)
            from cognition.opportunity import qualify_net_ev
            q = qualify_net_ev(
                entry=entry, sl=float(sl), tp=float(tp), confidence=confidence,
                money_per_price=mpp, cost_ccy=cost,
                min_edge_ccy=float(getattr(cog, "opportunity_min_net_ev", 0.0) if cog else 0.0),
                cost_multiple=float(getattr(cog, "opportunity_cost_multiple", 2.0) if cog else 2.0),
            )
            if not q.qualified:
                logger.info(
                    "[opportunity] {} {} REJECTED — {} (Part XIX Art 7)",
                    symbol, direction, q.reason,
                )
            return q.qualified
        except Exception as exc:  # noqa: BLE001 — qualification must never break the sink
            logger.debug("[opportunity] qualify fault for {}: {}", symbol, exc)
            return False

    def _make_management_sink(self):
        """Build the LIVE management sink: Brain manage() verdict → MT5 op.

        The returned callable accepts ``(ManagementAction, PositionView)`` and
        submits the corresponding :class:`~execution.intents.Intent`(s) onto the
        SAME aggregator → RiskGate → executor → MT5 plane the mechanical manager
        uses. Resolves the live broker position (ticket, lots, price, stop) from
        the platform manager. Fail-safe: a missing position or any fault is
        logged and dropped (never raises into the cognition loop).

        Stop moves are one-directional-safe: a tighten/protect only ever moves
        the stop to REDUCE risk, never widens it.
        """
        def _open_position_for(symbol: str, direction: str):
            try:
                positions = self._pm.get_all_open_positions() if self._pm is not None else []
            except Exception as exc:  # noqa: BLE001
                logger.debug("[mgmt-sink] positions fetch failed for {}: {}", symbol, exc)
                return None
            # Broker positions report BUY/SELL; the Brain verdict carries
            # LONG/SHORT. Compare on the canonical axis or the match ALWAYS fails
            # and every EXIT/PARTIAL/PROTECT/TIGHTEN is silently dropped.
            from cognition.position_adapter import canonical_side as _canon
            want = _canon(direction)
            for p in positions or []:
                if str(getattr(p, "symbol", "") or "") != symbol:
                    continue
                if want and _canon(getattr(p, "direction", "")) != want:
                    continue
                return p
            return None

        def _protective_sl(pos, kind: str):
            # Compute a strictly-safer stop for tighten/protect; None if no safe move.
            try:
                is_long = str(getattr(pos, "direction", "") or "").upper() == "LONG"
                cur_sl = float(getattr(pos, "sl", 0.0) or 0.0)
                entry = float(getattr(pos, "open_price", 0.0) or 0.0)
                price = float(getattr(pos, "current_price", 0.0) or 0.0)
                if price <= 0:
                    return None
                cog = getattr(self._config, "cognition", None)
                frac = float(getattr(cog, "origination_stop_fraction", 0.004)
                             if cog is not None else 0.004)
                if kind == "protect_sl":
                    # Lock in at least breakeven (only when actually in profit).
                    if is_long and price > entry > 0:
                        cand = entry
                    elif (not is_long) and 0 < price < entry:
                        cand = entry
                    else:
                        return None
                else:  # tighten_sl — halve the distance from price
                    dist = price * frac * 0.5
                    cand = price - dist if is_long else price + dist
                cand = round(cand, 6)
                # Never widen risk: only move the stop toward price.
                if is_long and (cur_sl <= 0 or cand > cur_sl) and cand < price:
                    return cand
                if (not is_long) and (cur_sl <= 0 or cand < cur_sl) and cand > price:
                    return cand
                return None
            except Exception:  # noqa: BLE001
                return None

        def _sink(action: Any, position: Any) -> None:
            try:
                symbol = str(getattr(action, "symbol", "") or "")
                kind = str(getattr(action, "kind", "") or "")
                held = str(getattr(action, "direction", "") or "").upper()
                reason = str(getattr(action, "reason", "") or "brain_management")[:200]
                if not symbol or not kind:
                    return
                intents: list = []

                if kind == "scale_in":
                    # Part XVIII Art 11 — never stack correlated risk: skip a
                    # scale when the symbol's currency/asset leg is already
                    # carried by an over-concentrated cluster of campaigns.
                    reg = getattr(self._ctx, "campaign_registry", None) \
                        if self._ctx is not None else None
                    if reg is not None:
                        try:
                            if reg.is_component_saturated(symbol):
                                logger.info(
                                    "[mgmt-sink] {} scale_in skipped — correlated "
                                    "cluster saturated (Art 11)", symbol,
                                )
                                return
                        except Exception:  # noqa: BLE001
                            pass
                    sl, tp = self._derive_origination_levels(symbol, held)
                    if sl is None or tp is None:
                        logger.info("[mgmt-sink] {} scale_in skipped — no protective stop", symbol)
                        return
                    conf = float(getattr(action, "confidence", 0.0) or 0.0)
                    add_lots = self._origination_lot_size(symbol, held, sl, conf)
                    if add_lots <= 0:
                        logger.info("[mgmt-sink] {} scale_in skipped — no broker-valid lot", symbol)
                        return
                    # Part XIX Art 7 — a scale must also clear positive net EV.
                    if not self._opportunity_qualifies(symbol, held, sl, tp, add_lots, conf):
                        return
                    idem = generate_idempotency_key(symbol, held, add_lots)
                    intents.append(Intent.open(
                        symbol=symbol, direction=held, lots=add_lots, sl=float(sl), tp=float(tp),
                        source="ai_brain", reason="brain_scale_in", stake_usd=None,
                        comment=build_order_comment("APEX", idem), idempotency_key=idem,
                    ))
                    self._aggregator.submit(intents)
                    logger.info("[mgmt-sink] submitted scale_in {} {} ({:.2f} lots)",
                                symbol, held, add_lots)
                    return

                pos = _open_position_for(symbol, held)
                if pos is None:
                    logger.info("[mgmt-sink] {} {} skipped — no matching open position",
                                symbol, kind)
                    return
                ticket = str(getattr(pos, "order_id", "") or "")
                if not ticket:
                    return

                if kind == "close":
                    intents.append(Intent.close(
                        symbol=symbol, ticket=ticket, source="ai_brain",
                        reason=reason, direction=held,
                    ))
                elif kind == "partial_close":
                    frac = float(getattr(action, "fraction", 0.0) or 0.0)
                    if not (0.0 < frac < 1.0):
                        return
                    intents.append(Intent.partial_close(
                        symbol=symbol, ticket=ticket, fraction=frac,
                        source="ai_brain", reason=reason,
                    ))
                elif kind in ("tighten_sl", "protect_sl"):
                    new_sl = _protective_sl(pos, kind)
                    if new_sl is None:
                        logger.info("[mgmt-sink] {} {} — no safer stop available", symbol, kind)
                        return
                    intents.append(Intent.modify_sl(
                        symbol=symbol, ticket=ticket, new_sl=float(new_sl),
                        source="ai_brain", reason=reason,
                    ))
                elif kind == "reverse":
                    target = str(getattr(action, "target_direction", "") or "").upper()
                    if target not in ("LONG", "SHORT"):
                        return
                    intents.append(Intent.close(
                        symbol=symbol, ticket=ticket, source="ai_brain",
                        reason="brain_reverse_close", direction=held,
                    ))
                    sl, tp = self._derive_origination_levels(symbol, target)
                    conf = float(getattr(action, "confidence", 0.0) or 0.0)
                    new_lots = (self._origination_lot_size(symbol, target, sl, conf)
                                if sl is not None else 0.0)
                    if sl is not None and tp is not None and new_lots > 0:
                        idem = generate_idempotency_key(symbol, target, new_lots)
                        intents.append(Intent.open(
                            symbol=symbol, direction=target, lots=new_lots,
                            sl=float(sl), tp=float(tp), source="ai_brain",
                            reason="brain_reverse_open", stake_usd=None,
                            comment=build_order_comment("APEX", idem), idempotency_key=idem,
                        ))
                    else:
                        logger.info("[mgmt-sink] {} reverse — closing only (no re-entry lot/stop)",
                                    symbol)
                else:
                    return

                if intents:
                    self._aggregator.submit(intents)
                    logger.info("[mgmt-sink] submitted {} {} (ticket={})", symbol, kind, ticket)
            except Exception as exc:  # noqa: BLE001 — sink must never break the loop
                logger.warning("[mgmt-sink] submit failed: {}", exc)

        return _sink

    def _cognition_developing_bias(self, symbol: str) -> dict:
        """Fresher forming-bar directional bias for one symbol, for cognition.

        Returns the DEVELOPING WorldModel's ``bias`` dict — the between-close
        directional synthesis computed on the still-forming bar. The consolidator
        turns it into one short-lived multi-timeframe Evidence so the Brain reacts
        to developing shifts without waiting for the next candle close. Fail-safe:
        returns ``{}`` when the store is empty or on any fault.
        """
        try:
            store = self._developing_wm_store
            if store is None:
                return {}
            wm = store.get(symbol)
            if wm is None:
                return {}
            return wm.bias_dict()
        except Exception as exc:  # noqa: BLE001 — never break the cognition loop
            logger.debug("[cognition] developing bias fetch failed for {}: {}", symbol, exc)
            return {}

    def _cognition_price_snapshot(self, symbol: str) -> list:
        """Reconstruct the live chart for one symbol as Evidence (Part XIX Art 2).

        Reads recent multi-timeframe OHLC (D1→M1, cache-backed), the live tick,
        the recent tick tape (microstructure), the order book (depth, when the
        feed publishes it) and the open position, then builds a compact snapshot
        so the Brain observes the actual market — price action, spread, micro-
        moves, pullbacks, session — like a trader on the chart, not only derived
        module verdicts. Fail-safe: ``[]`` on any fault (the Brain simply
        reasons without the chart that cycle).
        """
        try:
            import time as _time
            from cognition.market_snapshot import (
                build_price_snapshot, snapshot_to_evidence,
            )
            if self._pm is None:
                return []
            tfs = ["D1", "H4", "H1", "M15", "M5", "M1"]
            try:
                data = self._pm.fetch_market_data(
                    symbol, tfs, count=16, include_forming=True,
                ) or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("[cognition] price fetch failed for {}: {}", symbol, exc)
                return []
            candles_by_tf: dict = {}
            for tf, df in data.items():
                rows = self._ohlc_rows(df, max_bars=6)
                if rows:
                    candles_by_tf[tf] = rows
            if not candles_by_tf:
                return []
            tick = None
            recent_ticks: list = []
            try:
                if self._tick_store is not None:
                    tick = self._tick_store.get_latest(symbol)
                    recent_ticks = self._tick_store.get_recent(symbol, count=40) or []
            except Exception:  # noqa: BLE001
                tick = tick
            try:
                depth = self._pm.get_market_depth(symbol)
            except Exception:  # noqa: BLE001
                depth = []
            position = self._cognition_live_position(symbol)
            snap = build_price_snapshot(
                symbol, candles_by_tf, tick=tick, position=position,
                ticks=recent_ticks, depth=depth, now_epoch=_time.time(),
                max_bars=6,
            )
            return snapshot_to_evidence(symbol, snap)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition] price snapshot failed for {}: {}", symbol, exc)
            return []

    @staticmethod
    def _ohlc_rows(df: Any, max_bars: int = 8) -> list:
        """Extract the last ``max_bars`` (open,high,low,close) rows from an OHLCV
        DataFrame as plain tuples (no pandas leaks to the pure builder)."""
        try:
            if df is None or len(df) == 0:
                return []
            tail = df.tail(max_bars)
            cols = {c.lower(): c for c in tail.columns}
            oc = cols.get("open"); hc = cols.get("high")
            lc = cols.get("low"); cc = cols.get("close")
            if not (oc and hc and lc and cc):
                return []
            out = []
            for _, r in tail.iterrows():
                out.append((float(r[oc]), float(r[hc]), float(r[lc]), float(r[cc])))
            return out
        except Exception:  # noqa: BLE001
            return []

    def _cognition_live_position(self, symbol: str) -> Any:
        """A lightweight view of the open position on ``symbol`` for the snapshot
        (direction/profit_r/hold/size), or None. Fail-safe."""
        try:
            from types import SimpleNamespace
            from cognition.position_adapter import (
                canonical_side, broker_profit_r, hold_seconds_from,
            )
            if self._pm is None:
                return None
            positions = self._pm.get_all_open_positions() or []
            for p in positions:
                if str(getattr(p, "symbol", "") or "") != symbol:
                    continue
                side = canonical_side(getattr(p, "direction", ""))
                if side not in ("LONG", "SHORT"):
                    continue
                return SimpleNamespace(
                    direction=side,
                    profit_r=broker_profit_r(
                        side, getattr(p, "open_price", 0.0),
                        getattr(p, "current_price", 0.0), getattr(p, "sl", 0.0),
                    ),
                    hold_seconds=hold_seconds_from(getattr(p, "open_time", None)),
                    size=float(getattr(p, "lots", 0.0) or 0.0),
                )
            return None
        except Exception:  # noqa: BLE001
            return None

    def _on_developing_update(self, symbol: str) -> None:
        """Developing-store publish hook — wake the Brain on a sub-candle shift.

        Registered via ``WorldModelStore.set_on_publish`` so a forming-bar
        probability shift re-engages cognition the instant it publishes, rather
        than waiting on the next candle-close EventBus tick. The continuous
        thesis re-evaluation this once drove was retired with the ThesisEngine;
        the live continuous-cognition equivalent is the event-driven Brain nudge,
        which this delegates to. Fully guarded (the nudge no-ops when the
        cognition loop or a developing bias is absent) and never raises — a
        consumer fault must never break the developing-publish path.
        """
        try:
            self._nudge_cognition_on_developing(symbol)
        except Exception as exc:  # noqa: BLE001 — never disturb the publish path
            logger.debug(
                "[event-driven] developing update hook failed for {}: {}",
                symbol, exc,
            )

    def _nudge_cognition_on_developing(self, symbol: str) -> None:
        """Wake the Brain on a meaningful forming-bar shift (event-driven cadence).

        Hands the loop a NON-DIRECTIONAL change magnitude (the forming-bar
        conviction/activity level), never a direction: cognition must not be
        gated on a precomputed directional bias (Constitution §XXIV / §VII Q25).
        The loop decides — under its own magnitude-delta trigger and per-symbol
        floor — whether to reason immediately, on its OWN thread, so this never
        blocks the developing-publish path on an LLM call. Best-effort.
        """
        try:
            ctx = self._ctx
            loop = getattr(ctx, "cognition_loop", None) if ctx is not None else None
            if loop is None:
                return
            nudge = getattr(loop, "maybe_reason_on_change", None)
            if not callable(nudge):
                return
            bias = self._cognition_developing_bias(symbol)
            if not bias:
                return
            # Non-directional magnitude only — how much the forming-bar read has
            # shifted, not which way. Direction is never passed to the wake.
            try:
                magnitude = float(bias.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                magnitude = 0.0
            nudge(symbol, magnitude)
        except Exception as exc:  # noqa: BLE001 — never disturb the developing loop
            logger.debug("[cognition] developing nudge failed for {}: {}", symbol, exc)

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the event-driven system."""
        if self._running:
            return
        self._running = True

        logger.info("=" * 60)
        logger.info("  APEX TRADER — EVENT-DRIVEN MODE")
        logger.info("=" * 60)

        # NOTE:
        # clean-start now runs in main.py before SystemContext/bootstrap work so
        # git sync/reset can touch data/apex_events.db before the EventStore
        # logger sink opens it on Windows.

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
            # A corrupt event DB makes crash-window reconciliation unreliable
            # (a malformed DB folds to zero open positions and looks "clean").
            # Degrade safety so new OPEN entries are refused until restarted on
            # a clean DB; existing-position management continues.
            if report is not None and getattr(report, "store_degraded", False):
                ctx = self._ctx
                if ctx is not None and hasattr(ctx, "_mark_safety_degraded"):
                    ctx._mark_safety_degraded("EventStore")
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

        # ── Seed the risk layer from broker truth before any trading ─────
        # The RiskEngine is constructed (in SystemContext) before the brokers
        # connect, so its balance starts at the config placeholder.  Pull live
        # broker balances in now — once, at startup — so the very first entry
        # is sized/gated and the drawdown guard tracks against real account
        # equity instead of the placeholder, rather than waiting for the first
        # watchdog reconcile (which could be after the first trade).
        self._seed_broker_truth()

        # Seed the closed-trade counter from persisted attribution history so
        # the periodic learning recompute (gated on total_trades) reflects the
        # full track record after a restart instead of re-warming from zero.
        self._seed_learning_counters()

        symbols = list(self._config.enabled_pairs)
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

        # ── Seed LiveCandleAggregator from broker history ────────────────
        # Must run after the CandleCloseHandler warmup (which seeds WorldModels)
        # and before the live tick loops start (which begin updating forming
        # candles). Best-effort: any failure leaves the aggregator to backfill
        # from the live tick stream instead.
        try:
            from tick.candle_close_detector import SUPPORTED_TIMEFRAMES
            self._live_candle_aggregator.warmup(
                symbols, list(SUPPORTED_TIMEFRAMES), self._fetch_candles,
            )
        except Exception as exc:
            logger.warning(
                "[event-driven] LiveCandleAggregator warmup failed (continuing): {}",
                exc,
            )

        self._tick_router.start()
        self._candle_detector.start()
        self._mt5_poller.start()
        self._deriv_adapter.start()
        self._flush_loop.start()
        self._tick_eval_loop.start()

        # ── Start developing analysis loop (Phase 2) ─────────────────
        # Started after the live loops so ticks are already flowing into the
        # LiveCandleAggregator it reads from.
        if self._developing_loop is not None:
            try:
                self._developing_loop.start()
            except Exception as exc:
                logger.warning(
                    "[event-driven] developing analysis start failed: {}", exc,
                )

        # Phase 6 — start the adaptive scheduler (drives periodic/scan tuning +
        # weight recompute). Daemon; never blocks shutdown.
        if getattr(self, "_adaptive_scheduler", None) is not None:
            try:
                self._adaptive_scheduler.start()
            except Exception as exc:
                logger.warning(
                    "[event-driven] adaptive scheduler start failed: {}", exc,
                )

        # ── Start ProactiveOpportunityScanner (GAP 5) ────────────────
        # Daemon; only starts when cross_instrument.proactive_scan_enabled. Never
        # triggers entries — it pre-heats the watchlist + density tracker.
        if getattr(self, "_proactive_scanner", None) is not None:
            try:
                self._proactive_scanner.start()
            except Exception as exc:
                logger.warning(
                    "[event-driven] proactive scanner start failed: {}", exc,
                )

        # ── Start LLM reasoning worker (off the hot path) ────────────
        # Drives the LLM reasoner on its own daemon thread — a blocking provider
        # round-trip must never run in the tick/analysis loop. Only spins up when
        # a provider is actually configured (reasoner.available); otherwise a
        # pure no-op. Best-effort; never blocks startup.
        self._llm_worker = None
        try:
            _reasoner = self._ctx.llm_reasoner if self._ctx is not None else None
            if _reasoner is not None and getattr(_reasoner, "available", False):
                from llm.worker import LLMReasoningWorker as _LLMReasoningWorker
                _llm_cfg = getattr(self._config, "llm", None)
                self._llm_worker = _LLMReasoningWorker(
                    _reasoner,
                    self._build_llm_evidence_source(),
                    interval_seconds=float(
                        getattr(_llm_cfg, "worker_interval_seconds", 60.0)
                        if _llm_cfg is not None else 60.0
                    ),
                    max_symbols_per_cycle=int(
                        getattr(_llm_cfg, "max_symbols_per_cycle", 8)
                        if _llm_cfg is not None else 8
                    ),
                )
                self._llm_worker.start()
        except Exception as exc:
            logger.warning("[event-driven] LLM reasoning worker start failed: {}", exc)

        # ── Start the AI Cognitive Brain loop (Single Reasoner, shadow) ──
        # Background daemon that drives the one Brain over consolidated evidence
        # and records its decisions. Shadow by default — observational, off the
        # hot path. Guarded + best-effort; never blocks startup.
        try:
            _cog_loop = self._ctx.cognition_loop if self._ctx is not None else None
            _cog_cfg = getattr(self._config, "cognition", None)
            if _cog_loop is not None and bool(
                getattr(_cog_cfg, "enabled", True) if _cog_cfg is not None else True
            ):
                # Also feed the fresher forming-bar read: the developing store's
                # bias becomes one short-lived multi-timeframe Evidence so the
                # Brain reacts between candle closes, not only on close.
                try:
                    _cog_loop.set_developing_source(self._cognition_developing_bias)
                except Exception as exc:
                    logger.debug("[event-driven] developing-source wiring failed: {}", exc)
                # Part XIX Art 2 — let the Brain observe the actual chart: recent
                # multi-timeframe OHLC + live price/spread + the open position,
                # reconstructed as one compact price-action Evidence per symbol.
                try:
                    _cog_loop.set_price_source(self._cognition_price_snapshot)
                except Exception as exc:
                    logger.debug("[event-driven] price-source wiring failed: {}", exc)
                # Reuse the same broker-truth market-open check EntryOrchestrator
                # already applies, but at the reasoning stage: a closed market
                # (weekend forex/index/commodity) previously still burned the
                # full advisory-council fan-out — every provider, every retry —
                # for a symbol that could never be traded until the gate later
                # blocked the order. 24/7 instruments (Deriv synthetics, crypto)
                # are unaffected via is_always_open() inside the check itself.
                try:
                    _cog_loop.set_market_open_source(self._check_market_open)
                except Exception as exc:
                    logger.debug("[event-driven] market-open-source wiring failed: {}", exc)
                # Phase G — when origination is LIVE, wire the executor sink so the
                # Brain's originated entries reach the SAME execution plane as
                # every other entry (aggregator → RiskGate → broker). Shadow/off
                # leave the sink unset, so the loop only records intended orders.
                if str(getattr(_cog_loop, "origination_mode", "shadow")) == "live":
                    _cog_loop.set_origination_sink(self._make_origination_sink())
                # Part VI — when management is LIVE, wire the sink so the Brain's
                # EXIT/REVERSE/SCALE/PROTECT/TIGHTEN verdicts reach MT5 through the
                # same governed execution plane. Shadow/off record only.
                if str(getattr(_cog_loop, "management_mode", "shadow")) == "live":
                    _cog_loop.set_management_sink(self._make_management_sink())
                _cog_loop.start()
        except Exception as exc:
            logger.warning("[event-driven] cognition loop start failed: {}", exc)

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

    def _seed_broker_truth(self) -> None:
        """Pull live broker balances into the risk layer once at startup.

        ``RiskEngine`` is built before the brokers connect, so its balance is
        the construction-time placeholder until the first watchdog reconcile.
        Seeding here — after the brokers are online but before the trading
        loops start — guarantees the first trade is sized and the drawdown
        guard tracks against real broker equity (pooled across accounts), and
        seeds the per-account silo balances that the heat gate divides by.
        Fully fail-safe: any failure leaves the existing balances untouched
        (the watchdog reconcile remains the ongoing source of truth).
        """
        ctx = self._ctx
        if ctx is None:
            return
        # RiskEngine global balance ← per-platform broker truth (independent).
        if ctx.risk_engine is not None:
            try:
                platform_balances = self._pm.get_balances_by_platform()
                if platform_balances:
                    ctx.risk_engine.reconcile_platform_balances(platform_balances)
            except Exception as exc:
                logger.debug("[startup] risk-engine balance seed failed: {}", exc)
        # Per-account silos ← per-account broker balance (heat denominator,
        # daily-loss %).  Resolved per open position's account on the watchdog
        # too, but seeding from any held position now avoids a first-cycle gap.
        if ctx.account_risk is not None:
            try:
                snap = self._pm.get_open_positions_snapshot()
                seeded: set[str] = set()
                for pos in snap.positions:
                    sym = getattr(pos, "symbol", "")
                    if not sym:
                        continue
                    acct = ctx.account_key(sym, self._pm)
                    if acct in seeded:
                        continue
                    bal = self._pm.get_platform_balance(sym)
                    if bal and bal > 0:
                        ctx.account_risk.update_balance(acct, bal)
                        seeded.add(acct)
            except Exception as exc:
                logger.debug("[startup] account-risk balance seed failed: {}", exc)

    def _seed_learning_counters(self) -> None:
        """Seed the running closed-trade counter from persisted history.

        ``self._closed_trade_count`` drives the ``TuneContext.total_trades``
        the close path hands to the TunerAgent, which forwards it to every
        periodic ``maybe_recompute`` (counterfactual attribution, signal /
        interaction / behavior discovery). Those recomputes gate on a
        ``total_trades >= min_trades`` floor, so without a seed the system
        would have to re-accumulate the floor in fresh closes after every
        restart before any recompute fired again. The counterfactual store
        records one attribution row per opened trade and is the most direct
        persisted proxy for "trades the system has closed". Best-effort.
        """
        ctx = self._ctx
        if ctx is None:
            return
        engine = getattr(ctx, "counterfactual_engine", None)
        if engine is None:
            return
        try:
            seeded = int(engine.count_closed_trades())
            if seeded > 0:
                self._closed_trade_count = seeded
                logger.info(
                    "[event-driven] learning counter seeded — {} closed trade(s) "
                    "in persisted attribution history",
                    seeded,
                )
        except Exception as exc:
            logger.debug("[startup] learning-counter seed failed: {}", exc)

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
        # Stop the developing analysis loop before the tick router so it stops
        # reading the aggregator once ticks stop flowing.
        if getattr(self, "_developing_loop", None) is not None:
            try:
                self._developing_loop.stop()
            except Exception:
                pass
        if getattr(self, "_adaptive_scheduler", None) is not None:
            try:
                self._adaptive_scheduler.stop()
            except Exception:
                pass
        if getattr(self, "_proactive_scanner", None) is not None:
            try:
                self._proactive_scanner.stop()
            except Exception:
                pass
        if getattr(self, "_llm_worker", None) is not None:
            try:
                self._llm_worker.stop()
            except Exception:
                pass
        if self._ctx is not None and getattr(self._ctx, "cognition_loop", None) is not None:
            try:
                self._ctx.cognition_loop.stop()
            except Exception:
                pass
        if self._ctx is not None and getattr(self._ctx, "campaign_memory", None) is not None:
            try:
                self._ctx.campaign_memory.close()
            except Exception:
                pass
        if getattr(self, "_opportunity_queue", None) is not None:
            try:
                self._opportunity_queue.stop()
            except Exception:
                pass
        self._tick_router.stop()
        self._candle_detector.stop()
        self._candle_handler.shutdown()
        # Detach the live candle aggregator from the tick router so it stops
        # receiving ticks once the router is down.
        try:
            self._tick_router.unregister_callback(
                self._live_candle_aggregator.on_tick
            )
        except Exception:
            pass
        # Persist learned calibration so warmup history survives a restart.
        if self._calibration_engine is not None:
            _calib_cfg = getattr(self._config, "calibration", None)
            if _calib_cfg is None or getattr(_calib_cfg, "persist", True):
                try:
                    self._calibration_engine.save()
                except Exception as exc:
                    logger.warning("[event-driven] calibration save failed: {}", exc)
        # Persist pending news-impact baselines so a restart inside the
        # measurement window does not lose the captured price_at_event.
        if self._news_impact_tracker is not None:
            try:
                self._news_impact_tracker.save()
            except Exception as exc:
                logger.warning("[event-driven] news-impact save failed: {}", exc)
        # Cancel any resting news-breakout pending orders so a shutdown never
        # leaves staged OCO stops unmanaged on the broker.
        if getattr(self, "_news_planner", None) is not None:
            try:
                self._news_planner.cancel_all()
            except Exception as exc:
                logger.debug("[event-driven] news-planner cancel_all failed: {}", exc)
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
        samples = int(prof.get("samples", 0) or 0)
        ticks_routed = int(getattr(self._tick_router, "ticks_routed", 0) or 0)
        candle_events = int(getattr(self._candle_detector, "events_emitted", 0) or 0)
        # Tick block uses the legacy TickProfiler key names (tick_count /
        # slow_tick_count) so the operations page renders it identically.
        tick = {
            "samples": samples,
            "last_ms": prof.get("last_ms", 0.0),
            "avg_ms": prof.get("avg_ms", 0.0),
            "p50_ms": prof.get("p50_ms", 0.0),
            "p95_ms": prof.get("p95_ms", 0.0),
            "max_ms": prof.get("max_ms", 0.0),
            "tick_count": samples,
            "slow_tick_count": len(slow),
        }
        # Component rows use the legacy field names (component / calls / avg_ms);
        # the throughput counters have no per-call latency so report zeros there.
        components = [
            {"component": "position_eval", "calls": samples, "samples": samples,
             "avg_ms": prof.get("avg_ms", 0.0), "p50_ms": prof.get("p50_ms", 0.0),
             "p95_ms": prof.get("p95_ms", 0.0), "max_ms": prof.get("max_ms", 0.0)},
            {"component": "ticks_routed", "calls": ticks_routed, "samples": ticks_routed,
             "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0},
            {"component": "candle_events", "calls": candle_events, "samples": candle_events,
             "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0},
        ]
        recommendations: list[dict[str, Any]] = []
        if prof.get("p95_ms", 0.0) >= self._tick_eval_loop._slow_threshold_ms:
            recommendations.append({
                "component": "position_eval",
                "severity": "high",
                "suggestion": (
                    "position-eval p95 latency is high — consider raising the "
                    "tick-eval interval or reducing per-position work"
                ),
            })
        return {
            "enabled": samples > 0,
            "tick": tick,
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

            # ── Deriv token-refresh degradation → safety_degraded ─────
            # If the Deriv connector reports repeated token-refresh failures, an
            # expiring token will soon make open contracts unmanageable. Degrade
            # safety so new OPEN entries are refused (management still runs)
            # until the token is rotated.
            try:
                deriv = getattr(self._pm, "deriv", None)
                if (
                    deriv is not None
                    and getattr(deriv, "token_refresh_degraded", False)
                    and ctx is not None
                    and hasattr(ctx, "_mark_safety_degraded")
                    and not getattr(self, "_deriv_token_degraded_marked", False)
                ):
                    ctx._mark_safety_degraded("DerivTokenRefresh")
                    self._deriv_token_degraded_marked = True
                    logger.critical(
                        "[event-driven] Deriv token refresh degraded — SAFETY "
                        "DEGRADED; new entries refused until token rotates",
                    )
            except Exception as exc:
                logger.debug("[event-driven] deriv token-degrade check failed: {}", exc)

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
                        event_count = None
                        try:
                            event_count = get_event_store().count()
                        except Exception:
                            event_count = None
                        result = ctx.daily_maintenance.run(event_count=event_count)
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
                        # Reset the per-session atomic-reversal tally so the
                        # anti-ping-pong session cap starts fresh each day.
                        if getattr(ctx, "reversal_manager", None) is not None:
                            try:
                                ctx.reversal_manager.reset_session()
                            except Exception as exc:
                                logger.debug("[event-driven] reversal-manager session reset failed: {}", exc)
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

            # ── Interval data-junction sync ───────────────────────────
            # The daily block above only fires on a UTC day-roll. A restart
            # hard-resets the junction to the remote, so any intra-day data not
            # yet pushed is discarded. Push on the configured interval (default
            # hourly) to shrink that loss window from a full day to the interval.
            if ctx is not None and ctx.daily_maintenance is not None:
                try:
                    if ctx.daily_maintenance.should_sync():
                        sync_event_count = None
                        try:
                            sync_event_count = get_event_store().count()
                        except Exception:
                            sync_event_count = None
                        sync_res = ctx.daily_maintenance.sync_data(
                            event_count=sync_event_count
                        )
                        logger.info("[event-driven] data-junction sync — {}", sync_res)
                except Exception as exc:
                    logger.debug("[watchdog] data-junction sync failed: {}", exc)

            # ── Portfolio heat monitoring — active position reduction ──
            if ctx is not None:
                self._run_portfolio_heat_check(ctx)

            # ── Periodic learning tasks ──────────────────────────────
            if ctx is not None:
                self._run_periodic_learning(ctx)

            # ── System-wide volatility state (cross-instrument) ───────
            # Aggregate per-symbol RegimeAnalysis (carried on each WorldModel)
            # into the SystemVolatilityMonitor so the size-multiplier it already
            # feeds into entry sizing reflects live volatility spikes instead of
            # a permanent 1.0.  Throttled so the read stays cheap.
            if ctx is not None:
                self._run_system_volatility_update(ctx)

            # ── News calendar pre-planning (Phase 3 Feature A) ────────
            # Stage / monitor / reap the OCO breakout orders around scheduled
            # high-impact events. Throttled to ~60s inside the helper.
            self._run_news_planner_update()

            _time.sleep(10.0)

    def _run_news_planner_update(self) -> None:
        """Drive the news pre-planner ~every 60s (staged breakouts + OCO)."""
        planner = getattr(self, "_news_planner", None)
        if planner is None:
            return
        now = _time.monotonic()
        last = getattr(self, "_last_news_planner_update", 0.0)
        if (now - last) < 60.0:
            return
        self._last_news_planner_update = now
        try:
            planner.update()
        except Exception as exc:  # noqa: BLE001 — planning never breaks the watchdog
            logger.debug("[news-planner] periodic update failed: {}", exc)

    def _run_system_volatility_update(self, ctx: SystemContext) -> None:
        """Feed the SystemVolatilityMonitor with per-symbol regime analyses."""
        monitor = getattr(ctx, "system_volatility_monitor", None)
        if monitor is None or self._wm_store is None:
            return
        now = _time.monotonic()
        last = getattr(self, "_last_sysvol_update", 0.0)
        if (now - last) < 60.0:
            return
        self._last_sysvol_update = now
        try:
            # Per Part XXV the facts-only WorldModel no longer carries a per-
            # symbol RegimeAnalysis, so the system-volatility monitor has no
            # regime source in the live path and is left unfed here.
            analyses: list = []
            if analyses:
                state = monitor.update(analyses)
                if state is not None and state.state != "NORMAL":
                    logger.info(
                        "[event-driven] system volatility {} — size×{:.2f} ({})",
                        state.state, state.size_multiplier, state.note,
                    )
        except Exception as exc:
            logger.debug("[event-driven] system volatility update failed: {}", exc)

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
        # accumulate.  Reconciles each platform INDEPENDENTLY so a reconnecting
        # leg's transient balance can't collide with the other platform.
        # Fail-safe — leaves balances untouched on any failure.
        if ctx.risk_engine is not None:
            try:
                platform_balances = self._pm.get_balances_by_platform()
                if platform_balances:
                    ctx.risk_engine.reconcile_platform_balances(platform_balances)
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
                # Broker-truth money-per-pip — MUST match the sizing path so the
                # heat monitor and the position sizer agree on capital-at-risk.
                # Reading the registry alone (a 1.0 forex-scale placeholder on
                # sub-$10 crypto) inflated heat by orders of magnitude and
                # tripped phantom EMERGENCY force-closes.
                pip_value = self._effective_pip_value(symbol, pip_size)
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
                live_accts = set(acct_risk_dollars.keys())
                # Zero heat for accounts that no longer hold ANY open position.
                # Heat is only ever re-computed for accounts that still have
                # positions (the loop below), so without this an account whose
                # positions have all closed keeps its last (non-zero) reading
                # forever and permanently blocks new entries despite zero live
                # exposure.  Re-evaluated every watchdog cycle.
                for stale_acct in ctx.account_risk.heat_accounts():
                    if stale_acct not in live_accts:
                        ctx.account_risk.set_heat(stale_acct, 0.0)
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
                # Issue #4: heat must be PER ACCOUNT, not against combined
                # equity. A $27 MT5 account carrying a risky position would
                # look harmless when diluted against a $9,999 Deriv balance —
                # so the state machine is driven by the WORST single-account
                # heat (risk_$ / that account's own balance). This keeps a
                # multi-broker setup from masking one account's danger behind
                # another's idle capital. Falls back to combined-equity heat
                # only when per-account data is unavailable.
                heat_pct = 0.0
                equity_basis = 0.0
                if ctx.account_risk is not None and acct_risk_dollars:
                    for _acct, _risk_d in acct_risk_dollars.items():
                        _bal = ctx.account_risk.balance(_acct)
                        _h = (_risk_d / _bal * 100.0) if _bal > 0 else 0.0
                        if _h > heat_pct:
                            heat_pct = _h
                            equity_basis = _bal
                else:
                    equity = 0.0
                    try:
                        equity = float(self._pm.get_total_equity() or 0.0)
                    except Exception:
                        equity = 0.0
                    if equity <= 0 and ctx.account_risk is not None:
                        equity = ctx.account_risk.total_balance()
                    equity_basis = equity
                    heat_pct = (
                        compute_live_heat_pct(position_risks, equity)
                        if equity > 0 else 0.0
                    )
                # Self-heal the heat ladder to the live micro-account equity.
                # The SM is constructed once at startup when the broker balance
                # is often still unknown, leaving the base ~$10k thresholds in
                # place — which force-close every min-lot trade seconds after
                # entry. Recalibrating each cycle (idempotent, from the same
                # equity the heat is measured against) fixes the calibration
                # once the balance is known. Fully fail-safe.
                if equity_basis <= 0.0:
                    try:
                        equity_basis = float(self._pm.get_total_equity() or 0.0)
                    except Exception:
                        equity_basis = 0.0
                if equity_basis > 0.0:
                    try:
                        ctx.portfolio_risk_sm.recalibrate_for_equity(equity_basis)
                    except Exception as exc:
                        logger.debug(
                            "[risk-state] heat recalibrate failed: {}", exc,
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
            logger.warning(
                "[risk-state] correlation read failed — exposure check "
                "defaulting to permissive (True, 0.0): {}", exc,
            )
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
                # Issue #4: only flatten positions on accounts that are
                # ACTUALLY hot. A multi-broker book must not nuke a calm
                # account's positions because a different account overheated.
                # When per-account heat is unavailable, fall back to flattening
                # everything (fail-safe — better to over-close than leave risk).
                emerg_pct = getattr(
                    ctx.portfolio_risk_sm, "heat_emergency_pct", 4.0,
                )
                acct_risk = getattr(ctx, "account_risk", None)
                closed = 0
                skipped = 0
                for pos in positions:
                    ticket = str(getattr(pos, "order_id", getattr(pos, "ticket", "")))
                    symbol = getattr(pos, "symbol", "")
                    if not ticket:
                        continue
                    if acct_risk is not None and symbol:
                        try:
                            acct = ctx.account_key(symbol, self._pm)
                            if acct and acct_risk.heat(acct) < emerg_pct:
                                skipped += 1
                                continue
                        except Exception:
                            pass
                    self._aggregator.submit([Intent.close(
                        symbol=symbol,
                        ticket=ticket,
                        source="heat_monitor",
                        reason="portfolio_heat_emergency",
                    )])
                    closed += 1
                logger.warning(
                    "[heat-monitor] EMERGENCY — {} CLOSE intent(s) for hot "
                    "account(s); {} position(s) on calm accounts left open",
                    closed, skipped,
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
        except Exception as exc:
            logger.error(
                "[external-close] pnl_pips computation failed for {} — "
                "realized pip P&L booked as 0 (learning fed wrong PnL): {}",
                ticket, exc,
            )

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
        except Exception as exc:
            logger.debug(
                "[external-close] mgmt-store cleanup failed for {}: {}",
                ticket, exc,
            )
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
                # The ticket is being dropped from tracking without a booked
                # close, so its management + in-flight rollback state would
                # otherwise leak forever. Clear both so a stale phantom-SL
                # snapshot can never resurface against a future ticket reuse.
                self._clear_inflight_manage_ticket(str(ticket))
                try:
                    self._mgmt_store.remove(str(ticket))
                except Exception as exc:
                    logger.warning(
                        "[reconcile] mgmt-state remove failed for dropped "
                        "ticket {} — stale state may linger: {}", ticket, exc,
                    )
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
            # Part XIX Art 7 — self-calibrate the round-trip commission-per-lot
            # from realised deals so the net-EV opportunity gate uses the broker's
            # true cost. EMA over |commission+fee| / lots. Best-effort.
            try:
                _lots = 0.0
                for _src in (
                    getattr(deal_info, "lots_closed", None),
                    getattr(resp, "lots_closed", None),
                    (self._known_open.get(ticket, {}) or {}).get("lots") if ticket else None,
                    (self._entry_context.get(ticket, {}) or {}).get("lots") if ticket else None,
                ):
                    if _src:
                        _lots = float(_src)
                        break
                _cost = abs(broker_commission) + abs(broker_fee)
                if _lots > 0.0 and _cost > 0.0:
                    # get_deal_close_info sums commission across ALL deals of the
                    # position (entry + exit), so this is already round-trip.
                    rt = _cost / _lots
                    prev = float(getattr(self, "_commission_per_lot_est", 0.0) or 0.0)
                    self._commission_per_lot_est = rt if prev <= 0.0 else (0.8 * prev + 0.2 * rt)
            except Exception:  # noqa: BLE001
                pass
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
            except Exception as exc:
                logger.warning(
                    "[close-pnl] pnl_pips calculation failed for {} ticket {}: "
                    "{} — attribution will use 0.0 pips", symbol, ticket, exc,
                )

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
        except Exception as exc:
            logger.warning(
                "[close] mgmt-state remove failed for booked ticket {} — "
                "orphan management state may linger: {}", ticket, exc,
            )
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
            is_sl = int(itype) == int(IntentType.MODIFY_SL)
            key = (ticket, int(itype))
            with self._inflight_manage_lock:
                prev = self._inflight_manage.pop(key, None)

            success = bool(getattr(result, "success", False))
            err = str(getattr(result, "error", "") or "").lower()
            market_closed = (
                "market_closed" in err
                or "market closed" in err
                or "market is closed" in err
            )

            # Order matters. On a real failure the optimistic mutation (the
            # unconfirmed SL the worker wrote into the management state) MUST be
            # rolled back BEFORE the pending-confirmation guard is cleared. The
            # eval thread derives ``sl_pending_confirmation`` from
            # ``sl_modify_pending_until`` and reads ``stop_loss`` to build the
            # snapshot; if the guard were dropped first, a concurrent snapshot
            # could pair the cleared guard with the still-rejected SL and fire a
            # phantom stop-hit CLOSE in the window before the rollback landed.
            if not success and prev is not None and not market_closed:
                mgmt = self._mgmt_store.get(ticket)
                if mgmt is not None:
                    for field_name, value in prev.items():
                        try:
                            setattr(mgmt, field_name, value)
                        except Exception:
                            pass
                    try:
                        self._mgmt_store.persist(mgmt, force=True)
                    except Exception as exc:
                        logger.warning(
                            "[manage] {} {} rollback persist failed — rolled-back "
                            "state not durable: {}", itype.name, ticket, exc,
                        )
                    # A circuit-open failure is transient and self-healing: the
                    # executor already announced the trip once (and will announce
                    # recovery), so keep this per-cycle rollback line at DEBUG to
                    # avoid flooding while the breaker cools down. A real broker
                    # failure still warns.
                    if "circuit open" in err:
                        logger.debug(
                            "[manage] {} {} deferred — circuit open, rolled back "
                            "optimistic state ({})",
                            itype.name, ticket, getattr(result, "error", ""),
                        )
                    else:
                        logger.warning(
                            "[manage] {} {} failed — rolled back optimistic state ({})",
                            itype.name, ticket, getattr(result, "error", ""),
                        )

            # The SL modify attempt has now concluded (success, rolled-back
            # failure, or expected market-closed skip): clear the pending guard
            # so the synthetic stop-hit check resumes next cycle against the
            # now-authoritative SL — the confirmed new level on success, or the
            # just-restored original level on failure. On a market-closed skip
            # the optimistic level stands, but the market is shut so no tick can
            # fire a synthetic stop; clearing the guard keeps it from sticking.
            if is_sl:
                mgmt_pending = self._mgmt_store.get(ticket)
                if mgmt_pending is not None:
                    mgmt_pending.sl_modify_pending_until = 0.0
                    mgmt_pending.sl_pending_confirmation = False
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
                    pip_size = get_pip_size(resolve_to_internal(getattr(pos, "symbol", "")))
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
        """Split enabled symbols by platform (MT5 vs Deriv).

        Only ``config.enabled_pairs`` are classified so the tick pollers
        subscribe to the active universe (e.g. XAUUSD when running as a Gold
        specialist), not every instrument in the registry. The registry is
        still consulted for each symbol's platform metadata.
        """
        mt5: list[str] = []
        deriv: list[str] = []
        for sym in self._config.enabled_pairs:
            info = INSTRUMENT_REGISTRY.get(sym)
            if info is None:
                continue
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
            return get_pip_size(resolve_to_internal(symbol))
        except Exception as exc:
            self._note_pip_size_fallback(symbol, exc)
            return 0.0001

    def _note_pip_size_fallback(self, symbol: str, exc: Exception) -> None:
        """Record + warn (once per symbol) that pip_size fell back to 0.0001.

        The 0.0001 fallback is correct only for FX majors.  For indices
        (0.01), metals (0.01) or synthetics (0.1–1.0) it is wrong by
        100×–10,000×, so any risk_pips/lot sizing derived from it is
        unreliable.  Tracking the symbol also lets the entry path tag the
        trade's context (``pip_size_fallback``) so the journal/attribution
        know the value was a guess.  Warns once per symbol to avoid floods.
        """
        fb = self._pip_size_fallback_symbols
        if symbol not in fb:
            fb.add(symbol)
            logger.warning(
                "[pip-size] {} fallback to 0.0001 — risk sizing may be "
                "inaccurate: {}", symbol, exc,
            )

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
        """Broker-truth money-per-pip-per-lot from the (cached) symbol spec.

        Thin instance wrapper around :func:`_broker_pip_value_from_spec` that
        reuses the per-symbol spec cache. Returns ``fallback`` (config value)
        on any gap so sizing/P&L never break.
        """
        return _broker_pip_value_from_spec(
            self._symbol_spec(symbol), pip_size, fallback,
        )

    def _effective_pip_value(
        self, symbol: str, pip_size: Optional[float] = None,
    ) -> float:
        """Centralised money-per-pip-per-lot: registry fallback → broker truth.

        Reads the config registry value as a cold-start fallback, then overrides
        it with the live broker symbol spec whenever available. This is the
        single entry point shared by the heat monitor AND the sizing path so the
        risk view and the sizing view of a position can never diverge on pip
        value — the divergence that let a wrong registry ``pip_value_per_lot``
        (e.g. the 1.0 placeholder on a sub-$10 crypto) trigger phantom EMERGENCY
        force-closes.
        """
        info = INSTRUMENT_REGISTRY.get(symbol)
        fallback = info.pip_value_per_lot if info else 10.0
        if pip_size is None:
            pip_size = self._safe_pip_size(symbol)
        return self._broker_pip_value(symbol, pip_size, fallback)

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
        except Exception as exc:
            logger.debug(
                "[spread] {} spread read failed — defaulting to 1.0 pip: {}",
                symbol, exc,
            )
            return 1.0

    def _get_m1_dataframe(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            data = self._pm.fetch_market_data(symbol, ["M1"], 100)
            return data.get("M1")
        except Exception:
            return None

    def _flip_sequence_active(self, symbol: str) -> bool:
        """True when the fast-then-slow sequence gate is enabled for ``symbol``.

        The tracker is only fed when a profile opts in (flip_require_sequence),
        so the extra M1 momentum reads stay zero-cost while the feature is OFF.
        """
        try:
            from brain.instrument_profile import get_profile as _get_profile
            return bool(getattr(_get_profile(symbol), "flip_require_sequence", False))
        except Exception:  # noqa: BLE001 — a tuning read never breaks the feed
            return False

    def _feed_flip_sequence_m1(self, symbol: str) -> None:
        """Feed an M1 close into the flip-sequence tracker (fast confirmation).

        Evaluates M1 momentum for BOTH flip directions so the tracker arms
        whichever way the fast timeframe shifted, then reaps any lapsed arming.
        Best-effort — a fetch/read fault never disturbs the entry pipeline.
        """
        if not self._flip_sequence_active(symbol):
            return
        try:
            m1_df = self._get_m1_dataframe(symbol)
            if m1_df is None:
                return
            for direction in ("LONG", "SHORT"):
                self._flip_sequence_tracker.on_m1_close(symbol, direction, m1_df)
            self._flip_sequence_tracker.check_timeouts(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[flip-seq] M1 feed failed for {}: {}", symbol, exc)

    def _feed_flip_sequence_m5(self, symbol: str) -> None:
        """Feed an M5 close into the flip-sequence tracker (slow confirmation).

        Reads the confirmed M5 structure trend from the WorldModel and advances
        the tracker's per-symbol M5-bar clock exactly once. Best-effort.
        """
        if not self._flip_sequence_active(symbol):
            return
        try:
            wm = self._wm_store.get(symbol)
            structure = wm.structure_by_tf() if wm is not None else {}
            m5_trend, _ = _struct_trend_conf(structure, "M5")
            self._flip_sequence_tracker.on_m5_close(symbol, "", m5_trend)
            self._flip_sequence_tracker.check_timeouts(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[flip-seq] M5 feed failed for {}: {}", symbol, exc)

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

    def _should_poll_symbol(self, symbol: str) -> bool:
        """True when *symbol*'s market is currently open and worth polling.

        Polling gate for the tick adapters.  24/7 instruments (Deriv synthetics
        and crypto) are always polled.  Session-gated 24/5 instruments (forex,
        commodities, indices) are skipped while the FX market is closed
        (weekend), which stops the stale-tick warning flood and prevents the
        consecutive-failure removal from dropping symbols that are merely
        closed.  Polling resumes automatically when the market reopens — no
        symbol is permanently removed for a weekend closure.

        Fail-open: any uncertainty (unknown symbol, SessionEngine error)
        defaults to polling so a real feed is never silently starved.
        """
        try:
            if is_always_open(symbol):
                return True
        except Exception:
            return True
        try:
            return not self._is_fx_market_closed()
        except Exception:
            return True

    def _build_worker_config(self) -> WorkerConfig:
        """Build the PositionWorker config, mapping AppConfig weekend-protection
        settings onto the worker so live config changes take effect.

        Defensive: only primitive values are consumed (tests construct the
        system with a MagicMock config, which would otherwise inject mocks).
        """
        cfg = WorkerConfig()
        # Constitution Part VI/X — when the AI Cognitive Brain is the LIVE position
        # manager, hand every DISCRETIONARY exit (TP / breakeven / trailing /
        # stall / invalidation / conviction / HTF / dynamic-SL / opportunity-cost)
        # to the Brain and leave the worker only the always-on catastrophic safety
        # floor (hard SL + weekend / session / spread + absolute-profit backstop).
        # The broker-side protective stop attached at entry stays the hard capital
        # floor. Guarded so a MagicMock config (tests) never flips it: only an
        # exact "live" management_mode with cognition enabled qualifies.
        cog_cfg = getattr(self._config, "cognition", None)
        if cog_cfg is not None:
            mgmt_mode = str(
                getattr(cog_cfg, "management_mode", "shadow") or "shadow"
            ).strip().lower()
            if mgmt_mode == "live" and bool(getattr(cog_cfg, "enabled", False)) is True:
                cfg.discretionary_exits_enabled = False
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
        """Broker-truth market-open gate (origination + Compliance).

        Two independent CLOSED signals, either of which blocks:

        * **Permission** — the broker's ``trade_mode`` (MT5): DISABLED (0) or
          CLOSEONLY (3) means new positions are refused.
        * **Session** — tick freshness. ``trade_mode`` stays "full" straight
          through a weekend/holiday, so it never detects a session close; a tick
          far older than a normal gap (weekend = hours/days) reveals it. Reuses
          the connector's own staleness math via ``has_fresh_tick``.

        24/7 instruments (Deriv synthetics) are always open. Fail-OPEN on any
        unknown (no connector/spec/probe, or a fault) so a quiet-but-open market
        — or a feed without these signals — is never wrongly blocked; the
        SessionEngine gate still applies downstream.
        """
        is_open, reason = self._market_open_status(symbol)
        self._log_market_transition(symbol, is_open, reason)
        return is_open

    def _market_open_status(self, symbol: str) -> "tuple[bool, str]":
        """Pure (is_open, reason) market-open decision — no logging (that is
        edge-triggered in :meth:`_log_market_transition`)."""
        try:
            if is_always_open(symbol):
                return True, "24/7"
        except Exception:  # noqa: BLE001
            pass
        try:
            connector = self._pm.get_connector(symbol)
        except Exception:  # noqa: BLE001
            return True, "no connector (fail-open)"
        # (1) Permission flag — explicit broker disable / close-only.
        spec_fn = getattr(connector, "get_symbol_spec", None)
        if callable(spec_fn):
            try:
                spec = spec_fn(symbol)
                mode = spec.get("trade_mode") if isinstance(spec, dict) else None
            except Exception as exc:  # noqa: BLE001
                logger.debug("[market-open] symbol spec lookup failed for {}: {}", symbol, exc)
                mode = None
            if mode in (0, 3):
                return False, f"broker trade_mode={mode}"
        # (2) Session signal — no fresh tick ⇒ closed (weekend / holiday).
        # Non-raising; None ⇒ unknown ⇒ fail-open.
        fresh_fn = getattr(connector, "has_fresh_tick", None)
        if callable(fresh_fn):
            try:
                fresh = fresh_fn(symbol, _MARKET_CLOSED_TICK_AGE_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[market-open] tick-freshness check failed for {}: {}", symbol, exc)
                fresh = None
            if fresh is False:
                return False, f"no fresh tick within {_MARKET_CLOSED_TICK_AGE_SECONDS:.0f}s (session closed)"
        return True, "open"

    def _log_market_transition(self, symbol: str, is_open: bool, reason: str) -> None:
        """Edge-triggered market-open logging. Announce a symbol going CLOSED
        ONCE (then stay silent while it stays closed) and REOPENING once — so a
        weekend never floods the log with the same 'closed' line every cycle;
        active symbols are surfaced by their own reasoning logs. Purely cosmetic
        (never affects the gate). Fail-safe."""
        try:
            with self._market_open_log_lock:
                prev = self._market_open_state.get(symbol)  # None | True | False
                if prev is is_open:
                    return  # unchanged — silence
                self._market_open_state[symbol] = is_open
            if not is_open:
                logger.info(
                    "[market-open] {} closed — {} (silencing until it reopens)",
                    symbol, reason,
                )
            elif prev is False:
                logger.info("[market-open] {} reopened — resuming analysis", symbol)
        except Exception:  # noqa: BLE001 — logging must never break the gate
            pass

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
        """NewsGuard callback for EntryOrchestrator gate.

        Phase 3: when news pre-planning is enabled for the symbol, the planner
        stages breakout orders around the event instead of blocking, so this
        gate stops vetoing near news (return clear) and lets the staged OCO pair
        capture the move. When pre-planning is disabled the legacy fail-closed
        block is preserved unchanged.
        """
        ctx = self._ctx
        if ctx is None or ctx.news_guard is None:
            return True
        try:
            if self._news_pre_planning_enabled(symbol):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[news-gate] pre-planning check failed for {}: {}", symbol, exc)
        try:
            news_status = ctx.news_guard.check([symbol])
            return news_status.is_clear
        except Exception as exc:
            logger.debug("[news-gate] NewsGuard check failed: {}", exc)
            return True

    def _news_pre_planning_enabled(self, symbol: str) -> bool:
        """Resolve news_pre_planning_enabled: profile first, then EntryConfig."""
        try:
            from brain.instrument_profile import get_profile as _get_profile
            prof = _get_profile(symbol)
            val = getattr(prof, "news_pre_planning_enabled", None)
            if val is not None:
                return bool(val)
        except Exception:  # noqa: BLE001
            pass
        return bool(getattr(self._config_entry(), "news_pre_planning_enabled", False))

    def _config_entry(self) -> EntryConfig:
        """The active EntryConfig (cached), for news pre-planning resolution."""
        cfg = getattr(self, "_entry_config_cache", None)
        if cfg is None:
            cfg = EntryConfig()
            self._entry_config_cache = cfg
        return cfg

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

        # Feed the shadow store a counterfactual for each TUNABLE quality gate
        # that blocked this setup, so the GateTuner can learn whether those
        # gates are over-filtering profitable setups in the LIVE path (it
        # previously only saw decision-engine / risk-governor rejections, never
        # the entry gate's own score / HTF-alignment bars). Observational only —
        # shadow contracts never affect a live decision. Best-effort.
        if not passed:
            try:
                self._record_entry_gate_shadow_rejections(symbol, direction, results, meta)
            except Exception as exc:
                logger.debug(
                    "[shadow] entry-gate rejection record failed for {}: {}", symbol, exc,
                )

    # Maps EntryGate quality-gate names → the GateTuner family they feed. Only
    # whitelisted QUALITY gates appear here; safety / physical gates
    # (market_open, spread, news, drawdown, zone_valid, risk_reward, …) are
    # never auto-tuned and never recorded as tunable-gate counterfactuals.
    _TUNABLE_GATE_FAMILY: dict[str, str] = {
        "score_minimum": "entry_engine",
        "alignment": "htf_alignment",
    }

    def _record_entry_gate_shadow_rejections(
        self,
        symbol: str,
        direction: str,
        results: Any,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record one shadow rejection per TUNABLE quality gate that blocked.

        A setup can fail several gates at once (e.g. score AND alignment); each
        tunable quality gate gets its own contract so the counterfactual is
        attributed to that gate independently. Safety gates are skipped.
        """
        m = dict(meta or {})
        entry_price = float(m.get("entry_price", 0.0) or 0.0)
        sl = float(m.get("stop_loss", 0.0) or 0.0)
        tp1 = float(m.get("tp1", 0.0) or 0.0)
        conviction = float(m.get("conviction", 0.0) or 0.0)
        if entry_price <= 0.0 or sl <= 0.0 or tp1 <= 0.0:
            return
        for g in results or []:
            if getattr(g, "passed", True):
                continue
            family = self._TUNABLE_GATE_FAMILY.get(str(getattr(g, "gate_name", "")))
            if family is None:
                continue
            self._record_shadow_rejection(
                symbol, direction, entry_price, sl, tp1,
                f"{family}:{getattr(g, 'reason', '')}", conviction,
            )

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

        # ── ThesisEngine: maintain competing Long/Short/Flat theses ───
        # Fed on EVERY WorldModel update (not only when the consensus active
        # trigger is enabled) so the per-symbol thesis is current for BOTH the
        # zone and consensus entry paths. Uses the full confirmed vote panel so
        # the thesis reflects all the symbol's evidence, not a candidate subset.
        # Best-effort — a tracking fault must never disrupt the analysis cycle.
        try:
            sym = getattr(event, "symbol", "")
            if sym:
                wm = self._wm_store.get(sym) if self._wm_store is not None else None
                votes = list(getattr(wm, "votes", ()) or []) if wm is not None else []
                self._feed_thesis_engine(sym, votes)
        except Exception as exc:
            logger.debug("[thesis-engine] world-model feed failed: {}", exc)
        if ctx.opportunity_density_tracker is not None:
            try:
                ready_symbols = []
                for sym in self._config.enabled_pairs:
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

    def _build_open_attribution(
        self, symbol: str, direction: str, order_id: Any, wm: Any,
    ) -> Any:
        """Build the entry-time decision snapshot for counterfactual replay.

        The leave-one-out attribution (``CounterfactualEngine``) re-runs the
        EXACT consensus math the live system used with one module removed, so
        it needs the actual vote panel plus the consensus thresholds and ranker
        kwargs that were in force at entry. Previously the snapshot captured
        only ``trade_id/pair/direction/timestamp`` — leaving ``votes`` empty —
        which made every module classify as ABSENT and the whole attribution
        inert (an orphaned data path). This captures the published per-module
        votes from the WorldModel together with the live consensus + ranker
        config so the replay is faithful.
        """
        # Counterfactual attribution retired with the directional vote/consensus
        # subsystem (CounterfactualEngine deleted). No snapshot is produced.
        return None

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

    def _capture_entry_tf_trends(self, symbol: str) -> dict:
        """Snapshot the confirmed per-TF structure trend at entry time.

        Returns ``{tf: "BULLISH"/"BEARISH"/"RANGING"}`` from the live confirmed
        WorldModel. Best-effort — any fault yields an empty dict (which simply
        means this trade contributes no evidence to weight adaptation).
        """
        try:
            wm = self._wm_store.get(symbol)
            if wm is None:
                return {}
            trends: dict[str, str] = {}
            for tf, sa in wm.structure_by_tf().items():
                if sa is None:
                    continue
                trend = getattr(sa, "trend", None)
                trend = getattr(trend, "value", trend)
                if trend:
                    trends[str(tf)] = str(trend)
            return trends
        except Exception as exc:  # noqa: BLE001
            logger.debug("[entry-tf-trends] capture failed: {}", exc)
            return {}

    def _capture_gate_snapshot(self, order_id) -> dict:
        """Snapshot gate parameters at entry for close-path attribution.

        Records the learned GateTuner offsets together with the effective
        (default + offset) and default thresholds so the close path can replay
        the gate decision on the operator's defaults. Best-effort — any fault
        (no context, no tuner, no attributor) yields an empty dict, which simply
        means this trade contributes no gate attribution.
        """
        ctx = self._ctx
        if ctx is None or getattr(ctx, "gate_tuner", None) is None:
            return {}
        attributor = getattr(ctx, "gate_attributor", None)
        if attributor is None:
            return {}
        try:
            offsets = ctx.gate_tuner.all_offsets()
            defaults = attributor.default_thresholds()
            effective = {
                fam: defaults.get(fam, 0.0) + float(offsets.get(fam, 0.0))
                for fam in defaults
            }
            snap = attributor.record_entry(
                order_id=str(order_id),
                gate_offsets=offsets,
                effective_thresholds=effective,
                default_thresholds=defaults,
            )
            return snap.to_dict() if snap is not None else {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[gate-attr] snapshot capture failed: {}", exc)
            return {}

    def _is_learner_enabled(self) -> bool:
        """True when any GateTuner offset is non-zero (learning influenced entry).

        Fixes the long-standing gap where ``learner_enabled`` was never
        populated in the entry context, so the HealthAssessor's learner-enabled
        loss rate was always computed over an empty set.
        """
        ctx = self._ctx
        if ctx is None or getattr(ctx, "gate_tuner", None) is None:
            return False
        try:
            return any(abs(float(v)) > 1e-9 for v in ctx.gate_tuner.all_offsets().values())
        except Exception:  # noqa: BLE001
            return False

    # Major USD pairs used to rank USD strength for the DXY filter. Kept small
    # (the majors + gold) so the reading stays cheap; USD appears in every one,
    # so its ranking is well-determined from these alone.
    _DXY_USD_PAIRS = (
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
        "AUDUSD", "NZDUSD", "USDCAD", "XAUUSD",
    )

    # ── Phase 2 Feature B: zone-order stager wiring helpers ──────────
    def _stage_place_pending(
        self,
        symbol: str,
        order_kind: str,
        entry_price: float,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        idempotency_key: str = "",
    ):
        """Place a pending LIMIT via the per-symbol broker connector."""
        connector = self._pm.get_connector(symbol)
        if connector is None:
            return None
        return connector.place_pending_order(
            symbol, order_kind, entry_price, lots, sl, tp, comment, idempotency_key,
        )

    def _stage_cancel_pending(self, symbol: str, order_id: str) -> bool:
        """Cancel a resting pending order via the per-symbol broker connector."""
        connector = self._pm.get_connector(symbol)
        if connector is None:
            return False
        try:
            return bool(connector.cancel_pending_order(order_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[zone-stager] cancel failed for {} {}: {}", symbol, order_id, exc)
            return False

    def _stage_size(
        self, symbol: str, direction: str, entry_price: float, sl: float,
    ) -> float:
        """Size a pre-staged order using the shared sizer + session multiplier.

        Uses the configured per-trade risk scaled by the session multiplier
        (Phase 2 Feature C parity), the live platform balance, and the shared
        PositionSizer. Returns 0.0 (skip staging) when balance is unavailable or
        sizing resolves to nothing. Never raises.
        """
        try:
            balance = float(self._pm.get_platform_balance(symbol) or 0.0)
            if balance <= 0:
                return 0.0
            risk_pct = self._config.risk.risk_per_trade_pct / 100.0
            if self._session_context is not None:
                try:
                    session = self._session_context.get_session()
                    mult = float(
                        self._session_context.get_session_multiplier(symbol, session)
                    )
                    if mult > 0:
                        risk_pct *= mult
                except Exception:  # noqa: BLE001
                    pass
            try:
                context = build_context_for_symbol(symbol)
            except Exception:  # noqa: BLE001
                context = None
            res = self._stage_sizer.calculate_for_instrument(
                symbol=symbol,
                account_balance=balance,
                risk_pct=risk_pct,
                entry_price=entry_price,
                stop_loss=sl,
                context=context,
            )
            return float(getattr(res, "lots", 0.0) or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[zone-stager] sizing failed for {}: {}", symbol, exc)
            return 0.0

    # ── Phase 3 Feature A: news-planner wiring helpers ───────────────
    def _news_upcoming_events(
        self, symbol: str, within_minutes: float, now: datetime,
    ) -> list:
        """Upcoming HIGH-impact events for a symbol within the pre-stage window."""
        ctx = self._ctx
        if ctx is None or ctx.news_guard is None:
            return []
        try:
            return ctx.news_guard.upcoming_high_impact([symbol], within_minutes, now)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[news-planner] upcoming events read failed for {}: {}", symbol, exc)
            return []

    def _news_range_candles(self, symbol: str, bars: int):
        """Last ``bars`` M5 candles for the pre-event breakout range."""
        return self._fetch_candles(symbol, "M5", max(int(bars), 1))

    def _news_order_filled(self, symbol: str, order_id: str) -> bool:
        """True when a staged news order has filled into an open position."""
        if not order_id:
            return False
        try:
            for pos in self._pm.get_all_open_positions():
                if str(getattr(pos, "order_id", "")) == str(order_id):
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[news-planner] fill check failed for {} {}: {}", symbol, order_id, exc)
        return False

    def _news_size(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl: float,
        risk_multiplier: float,
    ) -> float:
        """Size a staged news order: per-trade risk × session × news multiplier.

        Mirrors ``_stage_size`` but auto-sizes DOWN for the news window (news
        spreads widen) by the configured ``news_risk_multiplier``. Returns 0.0
        (skip staging) when balance is unavailable or sizing resolves to
        nothing. Never raises.
        """
        try:
            balance = float(self._pm.get_platform_balance(symbol) or 0.0)
            if balance <= 0:
                return 0.0
            risk_pct = self._config.risk.risk_per_trade_pct / 100.0
            mult = float(risk_multiplier)
            if mult > 0:
                risk_pct *= mult
            if self._session_context is not None:
                try:
                    session = self._session_context.get_session()
                    smult = float(
                        self._session_context.get_session_multiplier(symbol, session)
                    )
                    if smult > 0:
                        risk_pct *= smult
                except Exception:  # noqa: BLE001
                    pass
            try:
                context = build_context_for_symbol(symbol)
            except Exception:  # noqa: BLE001
                context = None
            res = self._stage_sizer.calculate_for_instrument(
                symbol=symbol,
                account_balance=balance,
                risk_pct=risk_pct,
                entry_price=entry_price,
                stop_loss=sl,
                context=context,
            )
            return float(getattr(res, "lots", 0.0) or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[news-planner] sizing failed for {}: {}", symbol, exc)
            return 0.0

    def _dxy_currency_strength(self, symbol: str, lookback_bars: int = 20):
        """Phase 2 Feature D provider — cached USD-strength analysis.

        Builds M5 price data for the major USD pairs and returns a
        ``StrengthAnalysis`` (from ``CurrencyStrengthMeter``) the orchestrator
        reads for the USD ranking. ``lookback_bars`` sizes the candle window
        (floored at 60 so the RSI/percentile maths stay valid). Cached ~5 min to
        avoid re-fetching every pair on each entry. Returns ``None`` for
        non-USD symbols or on any failure — the DXY vote is then a no-op.
        """
        if self._currency_strength_meter is None:
            return None
        if "USD" not in symbol.upper():
            return None
        try:
            now = _time.monotonic()
            if (
                self._dxy_strength_cache is not None
                and (now - self._dxy_strength_cache_ts) < 300.0
            ):
                return self._dxy_strength_cache
            count = max(int(lookback_bars or 0), 60)
            price_data: dict[str, Any] = {}
            for pair in self._DXY_USD_PAIRS:
                if pair not in CURRENCY_PAIRS:
                    continue
                try:
                    df = self._fetch_candles(pair, "M5", count)
                    if df is not None and len(df) >= 30:
                        price_data[pair] = df
                except Exception as exc:
                    logger.debug("[dxy] fetch {} failed: {}", pair, exc)
            if not price_data:
                return None
            analysis = self._currency_strength_meter.calculate(price_data)
            self._dxy_strength_cache = analysis
            self._dxy_strength_cache_ts = now
            return analysis
        except Exception as exc:
            logger.debug("[dxy] currency-strength analysis failed: {}", exc)
            return None

    def _calibrated_atr_pips(self, symbol: str, tf: str) -> float:
        """M5/other-TF ATR in pips from the CalibrationEngine (0.0 if absent).

        Feeds the hardened FlipConfirmer's ATR-normalised magnitude check.
        Guarded end-to-end — no calibration engine, no stats for the symbol, or
        any read fault degrades to 0.0, which the confirmer treats as a graceful
        skip of the magnitude check rather than a block.
        """
        try:
            if self._calibration_engine is None:
                return 0.0
            stats = self._calibration_engine.store.get(symbol)
            if stats is None:
                return 0.0
            return float(stats.atr_pips(tf) or 0.0)
        except Exception:
            return 0.0

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
            # Release candidate provenance now the position has left the book.
            self._candidate_positions.pop(str(ticket), None)
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
            # CalibrationEngine zone outcome — the trade reached its zone (touched)
            # and "held" if it resolved profitably (≈ reached TP1). Feeds the
            # learned zone_hold_rate. Single-writer; best-effort.
            if self._calibration_engine is not None:
                self._calibration_engine.record_zone_outcome(
                    symbol, touched=True, held=bool(won),
                )
        except Exception as exc:
            logger.debug("[close-learn] zone-edge record failed: {}", exc)

        ctx = self._ctx
        if ctx is None:
            return

        # ── Evolving campaigns: record the closed leg / campaign end ────
        # Observational (default OFF via ``campaign.enabled``). Ties the realised
        # outcome back to the continuing idea so the post-mortem reads a full
        # campaign narrative. Best-effort — never affects close accounting.
        registry = getattr(ctx, "campaign_registry", None)
        if registry is not None and getattr(registry, "enabled", False):
            try:
                won_flag = (pnl_dollars or 0.0) > 0.0 or (
                    (pnl_dollars or 0.0) == 0.0 and (pnl_pips or 0.0) > 0.0
                )
                registry.observe_close(
                    symbol, direction,
                    exit_cause=str(exit_reason or ""),
                    pnl=float(pnl_dollars or 0.0),
                    won=won_flag,
                    ticket=str(ticket or ""),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[campaign] close feed failed for {}: {}", symbol, exc)

        # ── Phase 6: feed per-TF structure agreement into the adaptive
        # evidence-weight provider so the probabilistic-bias weights learn
        # which timeframes actually predict winners. Best-effort.
        awp = getattr(ctx, "adaptive_weight_provider", None)
        if awp is not None:
            try:
                tf_trends = info.get("entry_tf_trends") or {}
                if tf_trends:
                    awp.record_trade_outcome(direction, tf_trends, bool(won))
            except Exception as exc:
                logger.debug("[close-learn] weight-provider record failed: {}", exc)

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
        except Exception as exc:
            logger.warning(
                "[close-audit] realized-close audit_trade/audit_risk failed "
                "for {}: {}", ticket, exc,
            )

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

        # SignalLedger — attach outcome to driving signals
        if ctx.signal_ledger is not None:
            try:
                ctx.signal_ledger.attach_trade_outcome(str(ticket), {
                    "pnl_pips": round(float(pnl_pips), 2),
                    "pnl_dollars": round(float(pnl_dollars), 2),
                    "won": pnl_dollars > 0,
                    "outcome": outcome,
                    # ── Candidate attribution (Session 4) ──────────────────
                    # Thread the opening idea's provenance onto every driving
                    # signal's outcome so the learning loop can attribute wins
                    # per (module × timeframe × source × regime), not just per
                    # module. Empty when the trade had no captured candidate.
                    "candidate_id": str(info.get("candidate_id", "") or ""),
                    "timeframe_class": str(info.get("timeframe_class", "") or ""),
                    "source": str(info.get("source", "") or ""),
                    "regime_at_entry": str(info.get("regime_at_entry", "") or ""),
                    "contributing_modules": list(
                        info.get("contributing_modules", []) or []
                    ),
                    "contributing_timeframes": list(
                        info.get("contributing_timeframes", []) or []
                    ),
                    "pnl_r": round(pnl_r, 4),
                })
            except Exception as exc:
                logger.debug("[close-learn] SignalLedger attach failed: {}", exc)

        # ML Adapter — register new trade
        if ctx.ml_adapter is not None:
            try:
                ctx.ml_adapter.register_new_trade(exit_cause=cause_value)
            except Exception as exc:
                logger.debug("[close-learn] MLAdapter register failed: {}", exc)

        # Event-driven retrain — loss-streak trigger. Track the running
        # consecutive-loss count and notify the optimiser so a losing run forces
        # an out-of-band retrain (gated by the optimiser's threshold + cooldown)
        # instead of waiting for the next periodic cycle. A genuine loss
        # (realized P&L < 0) extends the streak; a win or scratch resets it.
        # Best-effort: a counter/notify failure must never break the close path.
        try:
            if float(pnl_dollars) < 0.0:
                self._consecutive_losses += 1
                if ctx.ml_adapter is not None:
                    ctx.ml_adapter.notify_loss_streak(self._consecutive_losses)
            else:
                self._consecutive_losses = 0
        except Exception as exc:
            logger.debug("[close-learn] loss-streak notify failed: {}", exc)

        # Governance aggregate-health thermostat — feed the realized outcome to
        # the HealthAssessor and let Governance freeze/resume the whole learning
        # layer on a CRITICAL/HEALTHY transition. Best-effort: a monitoring
        # fault must never break the close path.
        if ctx.governance is not None and hasattr(ctx.governance, "check_health_after_close"):
            try:
                ctx.governance.check_health_after_close(
                    realized_r=pnl_r,
                    entry_path=str(info.get("source", "") or ""),
                    learner_enabled=bool(info.get("learner_enabled", False)),
                )
            except Exception as exc:
                logger.debug("[close-learn] health assessment failed: {}", exc)

        # Gate attribution — replay the gate decision with the operator's
        # default (no-learning) thresholds to determine whether a learned gate
        # loosening was DECISIVE in opening this trade (it would have been
        # rejected on the defaults) or merely SUPPORTING (it would have passed
        # anyway). Best-effort: attribution must never break the close path.
        gate_attributor = (
            getattr(ctx, "gate_attributor", None) if ctx is not None else None
        )
        if gate_attributor is not None:
            try:
                snap = info.get("gate_snapshot", {}) or {}
                eff = snap.get("effective_thresholds", {}) if isinstance(snap, dict) else {}
                gate_attributor.record_close(
                    order_id=str(ticket),
                    realized_r=float(pnl_r or 0.0),
                    # The trade's actual EV is not threaded out of the entry
                    # gate; the effective EV threshold is the close-path proxy
                    # (a loosened ev_gate then reads as DECISIVE — conservative).
                    entry_ev=float(eff.get("ev_gate", 0.0) or 0.0),
                    entry_score=float(info.get("score", 0) or 0),
                )
            except Exception as exc:
                logger.debug("[close-learn] gate attribution failed: {}", exc)

        # TunerAgent — route trade-close tuning
        if ctx.tuner_agent is not None:
            try:
                from adaptive.tunable import TuneContext
                # Running total of closed trades (seeded from persisted history
                # at startup). Forwarded as total_trades so the periodic
                # learning recompute engines (counterfactual attribution,
                # signal / interaction / behavior discovery) clear their
                # min-trades floor and actually fire — previously this was
                # hardcoded to 0, which pinned every recompute permanently off.
                # getattr-guarded so callers that bypass __init__ stay safe.
                self._closed_trade_count = getattr(self, "_closed_trade_count", 0) + 1
                tune_ctx = TuneContext(
                    total_trades=self._closed_trade_count,
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
                    # Forward MFE/MAE checks must be scheduled from the ACTUAL
                    # entry time (stored as an ISO string at fill), not the
                    # close time. Passing now_dt here measured the wrong price
                    # window (T+5/15/30/60m from close), making entry_accuracy
                    # and management_score garbage. Falls back to now_dt only
                    # when the entry time is unavailable.
                    entry_timestamp=info.get("entry_time") or now_dt,
                    exit_timestamp=now_dt,
                    entry_score=int(info.get("score", 0) or 0),
                    entry_confluences=list(info.get("confluences", []) or []),
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
                    score=int(info.get("score", 0) or 0),
                    confluences=list(info.get("confluences", []) or []),
                    regime=str(info.get("regime_at_entry", "") or ""),
                    session=str(info.get("session_at_entry", "") or ""),
                    spread=0.0,
                    slippage=0.0,
                    entry_type="event_driven",
                    time_to_tp1=0.0,
                    time_to_exit=0.0,
                    outcome=outcome,
                    pnl_dollars=round(float(pnl_dollars), 2),
                    exit_cause=cause_value,
                    source=info.get("source", "") or "",
                    # ── Candidate provenance (Session 4 multi-opportunity) ──
                    candidate_id=str(info.get("candidate_id", "") or ""),
                    timeframe_class=str(info.get("timeframe_class", "") or ""),
                    candidate_score=float(info.get("candidate_score", 0.0) or 0.0),
                    contributing_modules=list(
                        info.get("contributing_modules", []) or []
                    ),
                    competing_candidates=int(
                        info.get("competing_candidates", 0) or 0
                    ),
                    regime_at_entry=str(info.get("regime_at_entry", "") or ""),
                )
                try:
                    _loop = asyncio.new_event_loop()
                    _loop.run_until_complete(ctx.trade_journal.log_trade(record))
                    _loop.close()
                except Exception as exc_j:
                    logger.debug("[close-journal] async log failed: {}", exc_j)
            except Exception as exc:
                logger.debug("[close-journal] TradeJournal write failed: {}", exc)

        # ── MULTI-TENANT REPORTING (best-effort, no-op standalone) ───
        # When this instance was spawned by the multi-tenant API (env vars
        # APEX_TRADE_REPORT_DB + APEX_USER_ID set), mirror the closed trade into
        # the API's shared trade_history table so the dashboard can serve it.
        # Fully decoupled and non-fatal — never disturbs the close path.
        try:
            from api.trade_reporter import report_trade_close
            _tick = self._tick_store.get_latest(symbol)
            _exit_px = close_price or (_tick.mid if _tick else 0.0)
            report_trade_close(
                symbol=symbol,
                direction=direction,
                pnl_dollars=float(pnl_dollars),
                pnl_pips=float(pnl_pips),
                ticket=str(ticket),
                entry_price=float(info.get("entry_price", 0.0) or 0.0),
                exit_price=float(_exit_px or 0.0),
                exit_reason=cause_value,
                opened_at=info.get("entry_time"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[close-report] multi-tenant trade report skipped: {}", exc)

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
                    except Exception as exc:
                        logger.warning(
                            "[close-evo] regime read failed for {} — capital "
                            "allocator outcome fingerprint missing regime: {}",
                            symbol, exc,
                        )
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

        # ParameterEvolver (L5a) — SHADOW ONLY: advance active shadows on the
        # newly-closed trade and (cooldown permitting) seed a fresh tournament.
        # Recommends promotions through the gateway; never mutates live config.
        # Fully exception-safe — a fault here never affects trading.
        if ctx.param_evolver is not None:
            try:
                ctx.param_evolver.run_cycle()
            except Exception as exc:
                logger.debug("[close-evo] ParameterEvolver run_cycle failed: {}", exc)

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

        # Planner calibration is owned by the TunerAgent (PlannerCalibratorTunable,
        # ON_TRADE_BATCH) which already ran above via ``tuner_agent.on_trade_close``
        # and applies the new PlannerConfig through the authorised path.  The old
        # direct ``ctx.calibrator.calibrate()`` call here was blocked by the agent
        # (sole authority) every close — it logged a "TUNING BYPASS BLOCKED"
        # warning, then applied the *unchanged* config and logged a misleading
        # "auto-calibrated" line.  Removed to keep tuning on the single authorised
        # route.

        logger.info(
            "EVENT-DRIVEN CLOSE FEEDBACK | {} {} ticket={} pnl=${:.2f} ({:.1f}pip) | risk+learning+evolution",
            direction, symbol, ticket, pnl_dollars, pnl_pips,
        )

        # ── BE-stop cooldown — prevent chop re-entry ────────────────
        is_breakeven_exit = abs(pnl_dollars) < 0.01 and abs(pnl_pips) < 2.0
        if is_breakeven_exit:
            self._be_stop_cooldown[symbol] = _time.monotonic() + self._be_cooldown_seconds
            logger.info("[be-cooldown] {} cooldown for {:.0f}s (breakeven exit)", symbol, self._be_cooldown_seconds)

        # ── Zone re-entry cooldown — record EVERY close ─────────────
        # Records the last close time per symbol for the re-entry cooldown
        # bookkeeping consulted by the re-entry evaluation below.
        self._last_close_time[symbol] = _time.time()

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
