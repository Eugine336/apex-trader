"""APEX TRADER — TickStore (Phase 2).

Thread-safe ring buffer holding recent ticks per symbol, with optional
Hz-based coalescing.  Modelled on ``platforms/candle_cache.py``.

Tick coalescing:  When the arrival rate for a symbol exceeds ``max_hz``
(default 15), intermediate ticks are silently dropped — only the latest
is kept.  This prevents CPU saturation during volatility spikes on
instruments like BTCUSD (100+ ticks/s).

Critical-level bypass:  If a tick is within ``critical_distance_pips`` of
any registered critical price level (SL, TP, entry zone), coalescing is
bypassed and the tick is always stored and forwarded.
"""

from __future__ import annotations

import threading
import time as _time
from collections import deque
from dataclasses import dataclass
from typing import Optional


from tick.models import Tick


@dataclass
class TickStoreStats:
    ticks_received: int = 0
    ticks_stored: int = 0
    ticks_coalesced: int = 0
    critical_bypasses: int = 0

    def to_dict(self) -> dict:
        return {
            "ticks_received": self.ticks_received,
            "ticks_stored": self.ticks_stored,
            "ticks_coalesced": self.ticks_coalesced,
            "critical_bypasses": self.critical_bypasses,
            "coalesce_rate": (
                round(self.ticks_coalesced / self.ticks_received, 4)
                if self.ticks_received
                else 0.0
            ),
        }


class TickStore:
    """Per-symbol ring buffer for recent ticks with Hz-based coalescing.

    Parameters
    ----------
    max_ticks_per_symbol : int
        Ring buffer capacity per symbol (default 1000).
    max_hz : float
        Maximum tick processing rate per symbol in Hz (default 15).
        Ticks arriving faster are dropped unless they hit a critical level.
    critical_distance : float
        When a tick's mid-price is within this absolute price distance
        of a registered critical level, coalescing is bypassed (default
        0.0005, roughly 5 pips on major forex pairs).
    """

    def __init__(
        self,
        max_ticks_per_symbol: int = 1000,
        max_hz: float = 15.0,
        critical_distance: float = 0.0005,
    ) -> None:
        self._max_ticks = max(1, int(max_ticks_per_symbol))
        self._min_interval = 1.0 / max(0.1, float(max_hz))
        self._critical_distance = float(critical_distance)
        self._buffers: dict[str, deque[Tick]] = {}
        self._latest: dict[str, Tick] = {}
        self._last_store_time: dict[str, float] = {}
        self._critical_levels: dict[str, set[float]] = {}
        self._lock = threading.Lock()
        self._stats = TickStoreStats()

    def put(self, tick: Tick) -> bool:
        """Store a tick. Returns True if stored, False if coalesced away.

        Thread-safe. The tick is always recorded as ``latest`` even when
        coalesced, so ``get_latest()`` always returns the freshest price.
        """
        now = _time.monotonic()
        with self._lock:
            self._stats.ticks_received += 1
            self._latest[tick.symbol] = tick

            last_t = self._last_store_time.get(tick.symbol, 0.0)
            elapsed = now - last_t

            if elapsed < self._min_interval:
                if not self._is_near_critical(tick):
                    self._stats.ticks_coalesced += 1
                    return False
                self._stats.critical_bypasses += 1

            buf = self._buffers.get(tick.symbol)
            if buf is None:
                buf = deque(maxlen=self._max_ticks)
                self._buffers[tick.symbol] = buf
            buf.append(tick)
            self._last_store_time[tick.symbol] = now
            self._stats.ticks_stored += 1
            return True

    def get_latest(self, symbol: str) -> Optional[Tick]:
        """Return the most recent tick for ``symbol`` (even if coalesced)."""
        with self._lock:
            return self._latest.get(symbol)

    def get_recent(self, symbol: str, count: int = 10) -> list[Tick]:
        """Return up to ``count`` most recent stored ticks for ``symbol``."""
        with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None:
                return []
            return list(buf)[-count:]

    def snapshot(self) -> dict[str, Tick]:
        """Return a {symbol: latest_tick} snapshot of all symbols."""
        with self._lock:
            return dict(self._latest)

    def register_critical_level(self, symbol: str, price: float) -> None:
        """Register a price level that bypasses coalescing."""
        with self._lock:
            if symbol not in self._critical_levels:
                self._critical_levels[symbol] = set()
            self._critical_levels[symbol].add(price)

    def unregister_critical_level(self, symbol: str, price: float) -> None:
        with self._lock:
            levels = self._critical_levels.get(symbol)
            if levels:
                levels.discard(price)

    def clear_critical_levels(self, symbol: Optional[str] = None) -> None:
        with self._lock:
            if symbol is None:
                self._critical_levels.clear()
            else:
                self._critical_levels.pop(symbol, None)

    def stats(self) -> dict:
        with self._lock:
            return self._stats.to_dict()

    def symbols(self) -> list[str]:
        with self._lock:
            return list(self._latest.keys())

    def _is_near_critical(self, tick: Tick) -> bool:
        """Check if tick is near any registered critical level (lock held)."""
        levels = self._critical_levels.get(tick.symbol)
        if not levels:
            return False
        mid = tick.mid
        for lvl in levels:
            if abs(mid - lvl) <= self._critical_distance:
                return True
        return False
