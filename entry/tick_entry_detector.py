"""APEX TRADER — Tick Entry Detector (Phase 7).

Receives tick updates and checks whether price is within proximity
of any active entry zone.  When a zone touch is detected, creates a
PendingEntry and invokes a callback for M1 confirmation.

Stateless per tick — zone state is owned by ZoneWatcher; pending
entries are tracked here until confirmed or expired.

Uses Protocol-based interfaces so it can accept ticks from any
source (TickStore, direct feed, backtest harness).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from loguru import logger

from entry.models import (
    EntryConfig,
    EntryState,
    EntryZone,
    PendingEntry,
)
from entry.zone_watcher import ZoneWatcher


class TickData(Protocol):
    """Minimal tick interface — any object with these fields qualifies."""

    symbol: str
    bid: float
    ask: float
    timestamp: datetime


class TickEntryDetector:
    """Detects when live price enters a registered entry zone."""

    def __init__(
        self,
        zone_watcher: ZoneWatcher,
        config: Optional[EntryConfig] = None,
        on_zone_touch: Optional[Callable[[PendingEntry], None]] = None,
        pip_size_lookup: Optional[Callable[[str], float]] = None,
    ) -> None:
        self._watcher = zone_watcher
        self._config = config or EntryConfig()
        self._on_zone_touch = on_zone_touch
        self._pip_size_lookup = pip_size_lookup or (lambda _s: 0.0001)

        self._pending: dict[str, PendingEntry] = {}
        self._triggered_zones: set[tuple[str, float, float]] = set()
        self._lock = threading.Lock()

    @property
    def pending_entries(self) -> dict[str, PendingEntry]:
        with self._lock:
            return dict(self._pending)

    def on_tick(self, tick: TickData) -> Optional[PendingEntry]:
        """Process a single tick.  Returns a PendingEntry if a new zone
        touch was detected, else None.
        """
        symbol = tick.symbol
        zones = self._watcher.get_active_zones(symbol)
        if not zones:
            return None

        with self._lock:
            if len(self._pending) >= self._config.max_concurrent_pending:
                return None

            if symbol in self._pending:
                return None

        pip_size = self._pip_size_lookup(symbol)
        proximity = self._config.zone_proximity_pips * pip_size

        for zone in zones:
            zone_key = (zone.symbol, zone.top, zone.bottom)

            with self._lock:
                if zone_key in self._triggered_zones:
                    continue

            price = tick.ask if zone.direction == "LONG" else tick.bid

            if self._check_invalidation(price, zone):
                with self._lock:
                    self._triggered_zones.add(zone_key)
                continue

            spread = abs(tick.ask - tick.bid)
            max_spread = self._config.max_spread_multiplier * pip_size * 10
            if spread > max_spread:
                continue

            if self._is_within_zone(price, zone, proximity):
                pending = PendingEntry(
                    symbol=symbol,
                    direction=zone.direction,
                    zone=zone,
                    touch_price=price,
                    touch_time=tick.timestamp,
                    state=EntryState.ZONE_TOUCHED,
                )

                with self._lock:
                    self._pending[symbol] = pending
                    self._triggered_zones.add(zone_key)

                logger.info(
                    "[tick-entry] {} zone touch: {} @ {:.5f} (zone {:.5f}–{:.5f})",
                    symbol, zone.direction, price, zone.bottom, zone.top,
                )

                if self._on_zone_touch is not None:
                    try:
                        self._on_zone_touch(pending)
                    except Exception:
                        logger.exception("[tick-entry] on_zone_touch callback failed")

                return pending

        return None

    def cancel_pending(self, symbol: str, reason: str = "cancelled") -> None:
        """Remove a pending entry for *symbol*."""
        with self._lock:
            removed = self._pending.pop(symbol, None)
        if removed:
            logger.info("[tick-entry] {} pending cancelled: {}", symbol, reason)

    def mark_confirmed(self, symbol: str) -> None:
        """Mark a pending entry as confirmed (M1 passed)."""
        with self._lock:
            pe = self._pending.get(symbol)
            if pe is None:
                return
            now = datetime.now(timezone.utc)
            self._pending[symbol] = PendingEntry(
                symbol=pe.symbol,
                direction=pe.direction,
                zone=pe.zone,
                touch_price=pe.touch_price,
                touch_time=pe.touch_time,
                state=EntryState.CONFIRMED,
                m1_candles_seen=pe.m1_candles_seen,
                confirmed_at=now,
            )

    def reset(self) -> None:
        """Clear all pending entries and triggered zone history."""
        with self._lock:
            self._pending.clear()
            self._triggered_zones.clear()

    @staticmethod
    def _is_within_zone(
        price: float, zone: EntryZone, proximity: float,
    ) -> bool:
        if zone.bottom <= price <= zone.top:
            return True
        if zone.direction == "LONG":
            return price <= zone.top + proximity and price >= zone.bottom - proximity
        return price >= zone.bottom - proximity and price <= zone.top + proximity

    @staticmethod
    def _check_invalidation(price: float, zone: EntryZone) -> bool:
        if zone.direction == "LONG":
            return price < zone.invalidation_level
        return price > zone.invalidation_level
