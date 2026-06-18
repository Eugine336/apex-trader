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
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._lock = threading.Lock()
        # (symbol, timeframe) → epoch of the next candle close
        self._next_close: dict[tuple[str, str], float] = {}
        self._timeframes: set[str] = set()
        self._symbols: set[str] = set()
        self._events_emitted: int = 0

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
