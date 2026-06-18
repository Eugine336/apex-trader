"""APEX TRADER — Intraday Candle Cache (Phase 5, Fix #1).

A transparent, thread-safe, TTL-based cache that sits in front of per-symbol /
per-timeframe broker candle fetches. Its only job is to stop the scan loop from
re-fetching the same M5/M15/H1/H4 history every cycle — the dominant cost when
Deriv synthetics serialise candle requests at ~0.5s each (15 symbols × 4
timeframes = a ~30s floor before any decision is even made).

Design constraints (these are safety properties, not optimisations):
  * TTLs sit *under* each timeframe's bar period so a freshly closed bar is
    always available — better to re-fetch than to ever trade on stale intrabar
    data.
  * The cache is keyed by ``(symbol, timeframe, count)``. A request for more
    bars than were cached is a miss (never silently serve a shorter frame).
  * Empty / ``None`` frames are never cached — a failed fetch must not be
    remembered as a valid (empty) result.
  * Thread-safe: ``fetch_all_market_data`` fans symbols across a thread pool, so
    multiple workers hit this cache concurrently.
  * Stored frames are returned as-is (callers treat them as read-only); the
    cache never mutates them.
"""

from __future__ import annotations

import threading
import time as _time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from loguru import logger


# Default TTL (seconds) per timeframe — kept just under the bar period so the
# latest closed bar is always fresh. Overridable via PerformanceConfig.
_DEFAULT_TTL: dict[str, float] = {
    "M1": 1.0,
    "M5": 5.0,
    "M15": 15.0,
    "M30": 25.0,
    "H1": 55.0,
    "H4": 240.0,
    "D1": 3600.0,
}


@dataclass
class _Entry:
    df: pd.DataFrame
    count: int
    stored_monotonic: float


@dataclass
class CandleCacheStats:
    hits: int = 0
    misses: int = 0
    expired: int = 0
    stores: int = 0
    per_tf_hits: dict[str, int] = field(default_factory=dict)
    per_tf_misses: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        t = self.total
        return (self.hits / t) if t else 0.0

    def to_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "expired": self.expired,
            "stores": self.stores,
            "total": self.total,
            "hit_rate": round(self.hit_rate, 4),
            "per_tf_hits": dict(self.per_tf_hits),
            "per_tf_misses": dict(self.per_tf_misses),
        }


class CandleCache:
    """In-memory TTL cache for OHLCV DataFrames keyed by (symbol, timeframe, count)."""

    def __init__(
        self,
        ttl_by_tf: Optional[dict[str, float]] = None,
        default_ttl: float = 5.0,
        enabled: bool = True,
        max_size: int = 500,
    ) -> None:
        self.enabled = enabled
        self._ttl_by_tf = dict(_DEFAULT_TTL)
        if ttl_by_tf:
            self._ttl_by_tf.update(ttl_by_tf)
        self._default_ttl = float(default_ttl)
        # Bounded LRU: without an eviction cap the store grows without limit
        # across many symbols × timeframes × counts over a long session.
        self._max_size = max(1, int(max_size))
        self._store: "OrderedDict[tuple[str, str, int], _Entry]" = OrderedDict()
        self._lock = threading.Lock()
        self._stats = CandleCacheStats()

    def _ttl(self, timeframe: str) -> float:
        return self._ttl_by_tf.get(timeframe, self._default_ttl)

    def get(self, symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
        """Return a cached frame if present, fresh, and at least ``count`` bars.

        Returns ``None`` on miss/expiry so the caller fetches from the broker.
        """
        if not self.enabled:
            return None
        now = _time.monotonic()
        key = (symbol, timeframe, count)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._stats.misses += 1
                self._stats.per_tf_misses[timeframe] = self._stats.per_tf_misses.get(timeframe, 0) + 1
                return None
            age = now - entry.stored_monotonic
            if age >= self._ttl(timeframe):
                # Expired — drop it and treat as a miss.
                self._store.pop(key, None)
                self._stats.expired += 1
                self._stats.misses += 1
                self._stats.per_tf_misses[timeframe] = self._stats.per_tf_misses.get(timeframe, 0) + 1
                return None
            self._stats.hits += 1
            self._stats.per_tf_hits[timeframe] = self._stats.per_tf_hits.get(timeframe, 0) + 1
            # Mark as most-recently-used for LRU eviction.
            self._store.move_to_end(key)
            df = entry.df
        logger.debug("[candle-cache] HIT {} {} (count={}, age={:.1f}s)", symbol, timeframe, count, age)
        return df

    def put(self, symbol: str, timeframe: str, count: int, df: Optional[pd.DataFrame]) -> None:
        """Store a freshly fetched frame. No-ops on empty/None frames."""
        if not self.enabled:
            return
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            # Never cache a failed/empty fetch.
            return
        key = (symbol, timeframe, count)
        with self._lock:
            self._store[key] = _Entry(df=df, count=count, stored_monotonic=_time.monotonic())
            self._store.move_to_end(key)
            self._stats.stores += 1
            # Evict least-recently-used entries past the size cap.
            while len(self._store) > self._max_size:
                evicted_key, _ = self._store.popitem(last=False)
                self._stats.expired += 1
                logger.debug("[candle-cache] LRU evict {}", evicted_key)
        logger.debug("[candle-cache] MISS→store {} {} (count={}, rows={})", symbol, timeframe, count, len(df))

    def invalidate(self, symbol: Optional[str] = None, timeframe: Optional[str] = None) -> None:
        """Drop entries. No args = clear all; symbol/timeframe narrow the scope."""
        with self._lock:
            if symbol is None and timeframe is None:
                self._store.clear()
                return
            for key in [
                k for k in self._store
                if (symbol is None or k[0] == symbol) and (timeframe is None or k[1] == timeframe)
            ]:
                self._store.pop(key, None)

    def cache_stats(self) -> dict:
        """Snapshot of hit/miss counters for the dashboard performance panel."""
        with self._lock:
            stats = self._stats.to_dict()
            stats["entries"] = len(self._store)
        return stats
