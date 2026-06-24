"""APEX TRADER — CandleCloseDetector (Phase 2).

Derives candle-close events from a tick stream.  When a tick's timestamp
crosses a candle boundary for a tracked (symbol, timeframe), a
``CandleClose`` event is emitted via the ``EventBus``.

Timeframe close rules (UTC):
  M1  — every minute boundary
  M5  — every 5-minute boundary
  M15 — every 15-minute boundary
  H1  — every hour boundary
  H4  — 00:00, 04:00, 08:00, 12:00, 16:00, 20:00
  D1  — 00:00 UTC

This component is purely computational — it has no broker dependencies
and is fully testable with synthetic tick sequences.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from tick.models import CandleClose, Tick
from tick.event_bus import EventBus


_TF_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

SUPPORTED_TIMEFRAMES = tuple(_TF_SECONDS.keys())


def _candle_boundary(epoch: float, tf_seconds: int) -> float:
    """Return the epoch of the candle boundary that ``epoch`` belongs to.

    For a tick at 12:03:27 with M5 (300s), the candle started at 12:00:00
    and closes at 12:05:00.  This returns 12:00:00 epoch.
    """
    return (int(epoch) // tf_seconds) * tf_seconds


def _next_close(epoch: float, tf_seconds: int) -> float:
    """Return the epoch when the current candle closes."""
    return _candle_boundary(epoch, tf_seconds) + tf_seconds


class CandleCloseDetector:
    """Detect candle closes from a tick stream.

    For each registered (symbol, timeframe) pair, tracks the current candle
    period.  When a new tick's timestamp is at or past the close boundary,
    emits a ``CandleClose`` event on the ``EventBus`` with event type
    ``"candle_close"`` (and also ``"candle_close:{timeframe}"`` for
    per-TF subscribers).

    Thread-safe: ``on_tick`` may be called from any thread.

    A background **watchdog timer** (started via :meth:`start`) fires overdue
    closes for illiquid symbols where no tick arrives to cross the boundary.
    It is fully complementary to the tick path: whichever path observes the
    crossed boundary first advances ``_next_close`` under ``_lock``, so the
    other path sees the future boundary and does nothing — no double-fire.
    """

    def __init__(
        self,
        event_bus: EventBus,
        watchdog_interval: float = 5.0,
        max_catchup_periods: int = 5,
    ) -> None:
        self._bus = event_bus
        self._lock = threading.Lock()
        # (symbol, timeframe) → epoch of the next candle close
        self._next_close: dict[tuple[str, str], float] = {}
        self._timeframes: set[str] = set()
        self._symbols: set[str] = set()
        self._events_emitted: int = 0
        # ── Watchdog timer (close-detection fallback) ────────────────
        self._watchdog_interval = float(watchdog_interval)
        # Cap on how many missed periods a single sweep will fire for one
        # pair. Beyond this (e.g. a multi-day market closure) the pair is
        # re-based to the current boundary instead of emitting a storm of
        # stale closes.
        self._max_catchup_periods = int(max_catchup_periods)
        self._running = False
        self._watchdog_thread: Optional[threading.Thread] = None

    def register(
        self,
        symbol: str,
        timeframes: Optional[list[str]] = None,
    ) -> None:
        """Start tracking candle closes for ``symbol`` on given timeframes.

        If ``timeframes`` is None, all supported timeframes are registered.
        """
        tfs = timeframes or list(SUPPORTED_TIMEFRAMES)
        with self._lock:
            self._symbols.add(symbol)
            for tf in tfs:
                if tf not in _TF_SECONDS:
                    logger.warning("[candle-close] unsupported timeframe: {}", tf)
                    continue
                self._timeframes.add(tf)
                # Next close will be initialised on first tick for this pair

    def unregister(self, symbol: str) -> None:
        with self._lock:
            self._symbols.discard(symbol)
            keys_to_remove = [k for k in self._next_close if k[0] == symbol]
            for k in keys_to_remove:
                del self._next_close[k]

    def on_tick(self, tick: Tick) -> list[CandleClose]:
        """Process a tick. Returns any CandleClose events emitted.

        This is the hot path — called for every stored tick.  Keep it fast.
        """
        events: list[CandleClose] = []
        epoch = tick.epoch

        with self._lock:
            if tick.symbol not in self._symbols:
                return events

            for tf in self._timeframes:
                key = (tick.symbol, tf)
                tf_secs = _TF_SECONDS[tf]
                nc = self._next_close.get(key)

                if nc is None:
                    self._next_close[key] = _next_close(epoch, tf_secs)
                    continue

                if epoch >= nc:
                    close_time = datetime.fromtimestamp(nc, tz=timezone.utc)
                    event = CandleClose(
                        symbol=tick.symbol,
                        timeframe=tf,
                        close_time=close_time,
                        last_tick=tick,
                    )
                    events.append(event)
                    self._events_emitted += 1
                    self._next_close[key] = _next_close(epoch, tf_secs)

        for ev in events:
            self._bus.publish("candle_close", ev)
            self._bus.publish(f"candle_close:{ev.timeframe}", ev)

        return events

    # ── Watchdog timer fallback ──────────────────────────────────────

    def start(self) -> None:
        """Start the background watchdog timer thread.

        Idempotent — calling ``start`` on an already-running detector is a
        no-op. The thread is a daemon so it never blocks process shutdown.
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="candle-close-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()
        logger.info(
            "[candle-close] watchdog started (interval={}s, max_catchup={})",
            self._watchdog_interval,
            self._max_catchup_periods,
        )

    def stop(self) -> None:
        """Stop the watchdog timer thread."""
        thread = None
        with self._lock:
            if not self._running:
                return
            self._running = False
            thread = self._watchdog_thread
            self._watchdog_thread = None
        if thread is not None:
            thread.join(timeout=self._watchdog_interval + 1.0)
        logger.info("[candle-close] watchdog stopped")

    def _watchdog_loop(self) -> None:
        """Sweep loop: fire any overdue closes the tick path missed."""
        while self._running:
            time.sleep(self._watchdog_interval)
            if not self._running:
                break
            try:
                self.fire_overdue_closes()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "[candle-close] watchdog sweep error: {} — {}",
                    type(exc).__name__,
                    exc,
                )

    def fire_overdue_closes(self, now: Optional[float] = None) -> list[CandleClose]:
        """Fire closes for any (symbol, tf) whose boundary has passed.

        Used by the watchdog thread (and directly testable). For each tracked
        pair where wall-clock ``now`` is at or past ``_next_close``, a
        ``CandleClose`` is emitted with ``last_tick=None`` (timer-triggered).
        Missed periods are fired sequentially — one event per boundary — up to
        ``max_catchup_periods``; beyond that the pair is re-based to avoid an
        event storm after a prolonged gap. The boundary check-and-advance runs
        under ``_lock`` so it can never double-fire with the tick path.
        """
        if now is None:
            now = time.time()

        events: list[CandleClose] = []

        with self._lock:
            for key, nc in list(self._next_close.items()):
                if nc is None or now < nc:
                    continue
                symbol, tf = key
                tf_secs = _TF_SECONDS[tf]
                overdue = int((now - nc) // tf_secs) + 1
                if overdue > self._max_catchup_periods:
                    # Prolonged gap — skip the stale closes and re-base.
                    self._next_close[key] = _next_close(now, tf_secs)
                    logger.debug(
                        "[candle-close] watchdog re-based {} {} after {} missed "
                        "periods (> max_catchup {})",
                        symbol, tf, overdue, self._max_catchup_periods,
                    )
                    continue
                boundary = nc
                while now >= boundary:
                    close_time = datetime.fromtimestamp(boundary, tz=timezone.utc)
                    events.append(
                        CandleClose(
                            symbol=symbol,
                            timeframe=tf,
                            close_time=close_time,
                            last_tick=None,
                        )
                    )
                    self._events_emitted += 1
                    boundary += tf_secs
                self._next_close[key] = boundary

        for ev in events:
            self._bus.publish("candle_close", ev)
            self._bus.publish(f"candle_close:{ev.timeframe}", ev)

        return events

    @property
    def events_emitted(self) -> int:
        with self._lock:
            return self._events_emitted

    @property
    def tracked_pairs(self) -> int:
        with self._lock:
            return len(self._next_close)

    def registered_symbols(self) -> list[str]:
        with self._lock:
            return list(self._symbols)

    def registered_timeframes(self) -> list[str]:
        with self._lock:
            return list(self._timeframes)
