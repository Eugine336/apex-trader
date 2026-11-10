"""APEX TRADER — TickRouter (Phase 2).

Central hub that receives ticks from broker connectors (MT5, Deriv) and
routes them to:

1. ``TickStore``          — ring buffer with coalescing
2. ``CandleCloseDetector`` — derives candle-close events
3. Registered callbacks    — for future Position Workers (Phase 4)

The router does NOT own broker connections.  It provides ``on_tick()`` as
the ingestion point — broker-specific adapters (MT5 poll thread, Deriv WS
subscription) call this method with normalised ``Tick`` objects.

Thread-safe: ``on_tick()`` may be called concurrently from multiple
broker threads.
"""

from __future__ import annotations

import threading
from typing import Callable

from loguru import logger

from tick.models import Tick
from tick.tick_store import TickStore
from tick.candle_close_detector import CandleCloseDetector
from tick.event_bus import EventBus


class TickRouter:
    """Routes ticks from broker adapters to consumers.

    Parameters
    ----------
    tick_store : TickStore
        Ring buffer for tick storage and coalescing.
    candle_close_detector : CandleCloseDetector
        Derives candle-close events from the tick stream.
    event_bus : EventBus
        Event bus for publishing tick events (event type ``"tick"``
        and ``"tick:{symbol}"``).
    """

    def __init__(
        self,
        tick_store: TickStore,
        candle_close_detector: CandleCloseDetector,
        event_bus: EventBus,
    ) -> None:
        self._store = tick_store
        self._detector = candle_close_detector
        self._bus = event_bus
        self._callbacks: list[Callable[[Tick], None]] = []
        # Raw callbacks observe every tick before the bounded/coalesced store.
        # Consumers that build candles or measure tick dynamics must use this
        # path; legacy callbacks retain the sampled/stored semantics.
        self._raw_callbacks: list[Callable[[Tick], None]] = []
        self._lock = threading.Lock()
        self._running = False
        self._ticks_routed: int = 0

    def on_tick(self, tick: Tick) -> None:
        """Ingest a tick from a broker adapter.

        Called by MT5 poll thread or Deriv WS subscription handler.
        Thread-safe.
        """
        # Boundary detection and raw consumers run BEFORE the bounded store's
        # coalescing gate. This preserves the complete tick sequence delivered
        # by broker adapters for candle construction and market-state tracking.
        self._detector.on_tick(tick)
        self._ticks_routed += 1
        self._bus.publish("raw_tick", tick)
        self._bus.publish(f"raw_tick:{tick.symbol}", tick)

        with self._lock:
            raw_cbs = list(self._raw_callbacks)
        for cb in raw_cbs:
            try:
                cb(tick)
            except Exception as exc:
                logger.warning(
                    "[tick-router] raw callback error: {} — {}",
                    type(exc).__name__, exc,
                )

        # The store and legacy "tick" events remain deliberately coalesced for
        # consumers that need a bounded, rate-limited working set. This is not
        # the raw ingestion record and must not be used as a complete tick tape.
        stored = self._store.put(tick)
        if stored:
            self._bus.publish("tick", tick)
            self._bus.publish(f"tick:{tick.symbol}", tick)

            with self._lock:
                cbs = list(self._callbacks)
            for cb in cbs:
                try:
                    cb(tick)
                except Exception as exc:
                    logger.warning(
                        "[tick-router] callback error: {} — {}", type(exc).__name__, exc
                    )

    def register_raw_callback(self, callback: Callable[[Tick], None]) -> None:
        """Register a callback that receives every routed tick before coalescing."""
        with self._lock:
            self._raw_callbacks.append(callback)

    def unregister_raw_callback(self, callback: Callable[[Tick], None]) -> None:
        with self._lock:
            try:
                self._raw_callbacks.remove(callback)
            except ValueError:  # SAFE-FALLBACK: unregister is idempotent when already absent
                pass

    def register_callback(self, callback: Callable[[Tick], None]) -> None:
        """Register a callback for stored ticks after coalescing."""
        with self._lock:
            self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable[[Tick], None]) -> None:
        with self._lock:
            try:
                self._callbacks.remove(callback)
            except ValueError:  # SAFE-FALLBACK: unregister is idempotent when already absent
                pass

    def start(self) -> None:
        self._running = True
        logger.info("[tick-router] started")

    def stop(self) -> None:
        self._running = False
        logger.info("[tick-router] stopped (routed {} ticks)", self._ticks_routed)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def ticks_routed(self) -> int:
        return self._ticks_routed

    @property
    def tick_store(self) -> TickStore:
        return self._store

    @property
    def candle_close_detector(self) -> CandleCloseDetector:
        return self._detector

    @property
    def event_bus(self) -> EventBus:
        return self._bus
