"""APEX TRADER — Entry Orchestrator (Phase 7).

Wires together ZoneWatcher → TickEntryDetector → M1CandleConfirmer
→ EntryGate, and emits the final entry decision.

The orchestrator is the single public interface for the entry plane.
Callers provide:
    - A ``WorldModelStore`` for zone extraction.
    - A ``pip_size_lookup`` callable ``(symbol) → float``.
    - An ``on_entry_decision`` callback that receives the final
      entry dict (or submits an OPEN intent to the IntentAggregator).
    - Optional external gate state callbacks for market_open, session,
      news, drawdown — decoupling the entry plane from broker details.

Usage::

    orchestrator = EntryOrchestrator(
        world_model_store=wm_store,
        pip_size_lookup=get_pip_size,
        on_entry_decision=lambda d: aggregator.submit(build_open_intent(d)),
    )
    # Wire events:
    tick_router.subscribe("tick", orchestrator.on_tick)
    candle_close_bus.subscribe("M1", orchestrator.on_m1_close)
    wm_store_bus.subscribe("world_model", orchestrator.on_world_model_update)
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import pandas as pd
from loguru import logger

from brain.world_model import WorldModelStore
from entry.models import EntryConfig, EntryState
from entry.zone_watcher import ZoneWatcher
from entry.tick_entry_detector import TickData, TickEntryDetector
from entry.m1_confirmation import ConfirmationResult, M1CandleConfirmer
from entry.entry_gate import EntryGate


class EntryOrchestrator:
    """Top-level entry plane controller."""

    def __init__(
        self,
        world_model_store: WorldModelStore,
        config: Optional[EntryConfig] = None,
        pip_size_lookup: Optional[Callable[[str], float]] = None,
        on_entry_decision: Optional[Callable[[dict[str, Any]], None]] = None,
        is_market_open: Optional[Callable[[str], bool]] = None,
        is_session_active: Optional[Callable[[str], bool]] = None,
        is_news_clear: Optional[Callable[[str], bool]] = None,
        is_drawdown_ok: Optional[Callable[[], bool]] = None,
        is_instrument_known: Optional[Callable[[str], bool]] = None,
        get_spread_pips: Optional[Callable[[str], float]] = None,
        get_m1_dataframe: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
        on_gate_trace: Optional[Callable[..., None]] = None,
        gate_tuner: Optional[object] = None,
    ) -> None:
        self._config = config or EntryConfig()
        self._pip_size = pip_size_lookup or (lambda _: 0.0001)
        self._on_entry = on_entry_decision
        self._on_gate_trace = on_gate_trace

        self._is_market_open = is_market_open or (lambda _: True)
        self._is_session_active = is_session_active or (lambda _: True)
        self._is_news_clear = is_news_clear or (lambda _: True)
        self._is_drawdown_ok = is_drawdown_ok or (lambda: True)
        self._is_instrument_known = is_instrument_known or (lambda _: True)
        self._get_spread = get_spread_pips or (lambda _: 0.5)
        self._get_m1 = get_m1_dataframe

        self._zone_watcher = ZoneWatcher(world_model_store, self._config)
        self._m1_confirmer = M1CandleConfirmer(self._config, self._pip_size)
        self._gate = EntryGate(self._config, gate_tuner=gate_tuner)
        self._tick_detector = TickEntryDetector(
            zone_watcher=self._zone_watcher,
            config=self._config,
            on_zone_touch=self._handle_zone_touch,
            pip_size_lookup=self._pip_size,
        )

        self._confirming: dict[str, dict] = {}
        self._lock = threading.Lock()

        self._stats = {
            "zones_extracted": 0,
            "zone_touches": 0,
            "m1_confirmations": 0,
            "m1_rejections": 0,
            "gate_passes": 0,
            "gate_failures": 0,
            "entries_emitted": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def zone_watcher(self) -> ZoneWatcher:
        return self._zone_watcher

    @property
    def tick_detector(self) -> TickEntryDetector:
        return self._tick_detector

    def on_world_model_update(self, symbol: str) -> None:
        """Handle WorldModel publish for a symbol."""
        try:
            self._zone_watcher.on_world_model_update(symbol)
            zones = self._zone_watcher.get_active_zones(symbol)
            self._stats["zones_extracted"] += len(zones)
        except Exception:
            logger.exception("[entry-orch] WorldModel update failed for {}", symbol)

    def on_tick(self, tick: TickData) -> None:
        """Process an incoming tick for entry zone detection."""
        try:
            self._tick_detector.on_tick(tick)
        except Exception:
            logger.exception("[entry-orch] tick processing failed for {}", tick.symbol)

    def on_m1_close(self, symbol: str) -> None:
        """Handle M1 candle close for pending confirmations."""
        with self._lock:
            info = self._confirming.get(symbol)
            if info is None:
                return
            info = dict(info)

        m1_df = None
        if self._get_m1 is not None:
            try:
                m1_df = self._get_m1(symbol)
            except Exception:
                logger.exception("[entry-orch] failed to fetch M1 for {}", symbol)

        if m1_df is None:
            logger.debug("[entry-orch] no M1 data for {}", symbol)
            return

        zone = info["zone"]
        direction = info["direction"]

        result = self._m1_confirmer.on_m1_close(symbol, direction, zone, m1_df)

        if result.confirmed:
            self._stats["m1_confirmations"] += 1
            self._tick_detector.mark_confirmed(symbol)
            self._run_gates_and_emit(symbol, info, m1_df)
        elif result.method == "none" and "timeout" in result.reason.lower():
            self._stats["m1_rejections"] += 1
            logger.info(
                "[entry-orch] {} M1 confirmation timed out", symbol,
            )
            self._tick_detector.cancel_pending(symbol, "M1 timeout")
            with self._lock:
                self._confirming.pop(symbol, None)

    def reset(self) -> None:
        """Reset all state."""
        self._zone_watcher.clear()
        self._tick_detector.reset()
        self._m1_confirmer.reset()
        with self._lock:
            self._confirming.clear()

    def _handle_zone_touch(self, pending) -> None:
        """Callback from TickEntryDetector when a zone touch is detected."""
        self._stats["zone_touches"] += 1
        with self._lock:
            self._confirming[pending.symbol] = {
                "symbol": pending.symbol,
                "direction": pending.direction,
                "zone": pending.zone,
                "touch_price": pending.touch_price,
                "touch_time": pending.touch_time,
            }

    def _run_gates_and_emit(
        self,
        symbol: str,
        info: dict,
        m1_df: pd.DataFrame,
    ) -> None:
        """After M1 confirmation, run all gates and emit entry if passed."""
        zone = info["zone"]
        direction = info["direction"]
        entry_price = info["touch_price"]
        pip_size = self._pip_size(symbol)

        risk_pips = abs(entry_price - zone.invalidation_level) / pip_size
        if risk_pips <= 0:
            risk_pips = 10.0

        if direction == "LONG":
            sl = zone.invalidation_level
            tp1 = entry_price + abs(entry_price - sl) * 1.5
            tp2 = entry_price + abs(entry_price - sl) * 3.0
        else:
            sl = zone.invalidation_level
            tp1 = entry_price - abs(entry_price - sl) * 1.5
            tp2 = entry_price - abs(entry_price - sl) * 3.0

        spread_pips = self._get_spread(symbol)

        passed, results = self._gate.validate_all(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            score=zone.conviction,
            current_spread_pips=spread_pips,
            zone=zone,
            is_instrument_known=self._is_instrument_known(symbol),
            is_market_open=self._is_market_open(symbol),
            is_session_active=self._is_session_active(symbol),
            is_news_clear=self._is_news_clear(symbol),
            is_drawdown_ok=self._is_drawdown_ok(),
        )

        # Feed the entry-gate verdict chain to the decision-trace recorder
        # (best-effort) so the event-driven system populates the dashboard's
        # decision-trace panel.  Never affects the entry flow.
        if self._on_gate_trace is not None:
            try:
                self._on_gate_trace(
                    symbol,
                    direction,
                    results,
                    passed,
                    {
                        "entry_price": entry_price,
                        "stop_loss": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "conviction": zone.conviction,
                        "zone_type": zone.zone_type.value,
                        "timeframe": zone.timeframe,
                        "risk_pips": round(risk_pips, 2),
                        "spread_pips": round(spread_pips, 2),
                    },
                )
            except Exception:
                logger.exception("[entry-orch] gate trace callback failed")

        if passed:
            self._stats["gate_passes"] += 1
            self._stats["entries_emitted"] += 1

            decision = {
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss": sl,
                "tp1": tp1,
                "tp2": tp2,
                "conviction": zone.conviction,
                "zone_type": zone.zone_type.value,
                "timeframe": zone.timeframe,
                "gates_passed": [g.gate_name for g in results if g.passed],
                "risk_pips": risk_pips,
                "spread_pips": spread_pips,
                "has_sweep": zone.has_sweep,
            }

            logger.info(
                "[entry-orch] {} {} ENTRY @ {:.5f} SL={:.5f} TP1={:.5f} TP2={:.5f} score={}",
                symbol, direction, entry_price, sl, tp1, tp2, zone.conviction,
            )

            if self._on_entry is not None:
                try:
                    self._on_entry(decision)
                except Exception:
                    logger.exception("[entry-orch] on_entry callback failed")
        else:
            self._stats["gate_failures"] += 1
            failed = [g for g in results if not g.passed]
            logger.info(
                "[entry-orch] {} {} REJECTED — {}",
                symbol, direction,
                "; ".join(f"{g.gate_name}: {g.reason}" for g in failed),
            )

        self._tick_detector.cancel_pending(symbol, "processed")
        with self._lock:
            self._confirming.pop(symbol, None)
