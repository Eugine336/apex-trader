"""
APEX TRADER — Proactive Opportunity Scanner (GAP 5)

The entry plane is zone-REACTIVE: it waits for price to touch a pre-identified
zone before evaluating. It never proactively asks "across all instruments right
now, which are approaching the best setup?". This daemon does exactly that.

On a fixed interval it reads each instrument's ranked candidates from the
WorldModel, keeps those whose expected value clears ``min_ev`` (and, when a
proximity check is wired, those whose price is near a zone), and publishes the
resulting WATCHLIST — best-EV-first — to:
  * the :class:`brain.opportunity_density.OpportunityDensityTracker`, replacing
    the per-scan count with real symbol names, and
  * the EventBus (``proactive_watchlist``) for the dashboard.

It NEVER triggers an entry. It only pre-heats the pipeline so when the tick
finally touches the zone the entry is ready. Mirrors the
:class:`adaptive.adaptive_scheduler.AdaptiveSchedulerLoop` daemon pattern:
monotonic-clock cadence, fully exception-safe, never blocks the trading loop.

Leaf-ish: stdlib + loguru; all data access is via injected callables (duck-typed
WorldModel), so there is no import cycle and it is trivially testable.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from loguru import logger


@dataclass
class WatchlistEntry:
    symbol: str
    direction: str
    ev: float


class ProactiveOpportunityScanner:
    """Periodically rank all instruments and publish a high-EV watchlist."""

    def __init__(
        self,
        *,
        symbols_provider: Callable[[], Iterable[str]],
        get_world_model: Callable[[str], Any],
        enabled: bool = False,
        interval_seconds: float = 60.0,
        min_ev: float = 0.5,
        proximity_check: Optional[Callable[[str], bool]] = None,
        density_tracker: Optional[Any] = None,
        event_publish: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        self._symbols = symbols_provider
        self._get_wm = get_world_model
        self.enabled = bool(enabled)
        self._interval = max(1.0, float(interval_seconds))
        self._min_ev = float(min_ev)
        self._proximity = proximity_check
        self._density = density_tracker
        self._publish = event_publish

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_run = 0.0

        self._scans = 0
        self._last_watchlist: list[WatchlistEntry] = []
        self._lock = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running or not self.enabled:
            if not self.enabled:
                logger.info("[proactive-scan] disabled by config — not starting")
            return
        self._running = True
        self._last_run = time.monotonic()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="proactive-scan",
        )
        self._thread.start()
        logger.info(
            "[proactive-scan] started — interval={}s min_ev={:.2f}R",
            self._interval, self._min_ev,
        )

    def stop(self) -> None:
        self._running = False
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        logger.info("[proactive-scan] stopped (scans={})", self._scans)

    # ── Loop ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                now = time.monotonic()
                if (now - self._last_run) >= self._interval:
                    self._last_run = now
                    self.scan_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("[proactive-scan] loop error: {}", exc)
            time.sleep(1.0)

    def scan_once(self) -> list[WatchlistEntry]:
        """Build, publish and return the current high-EV watchlist."""
        watchlist: list[WatchlistEntry] = []
        try:
            symbols = list(self._symbols() or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("[proactive-scan] symbols provider failed: {}", exc)
            symbols = []

        for sym in symbols:
            entry = self._best_for_symbol(sym)
            if entry is not None:
                watchlist.append(entry)

        watchlist.sort(key=lambda e: e.ev, reverse=True)
        self._scans += 1
        with self._lock:
            self._last_watchlist = watchlist

        ready = [e.symbol for e in watchlist]
        if self._density is not None:
            try:
                self._density.record_scan(ready)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[proactive-scan] density feed failed: {}", exc)
        if self._publish is not None:
            try:
                self._publish(
                    "proactive_watchlist",
                    [{"symbol": e.symbol, "direction": e.direction, "ev": e.ev}
                     for e in watchlist],
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[proactive-scan] publish failed: {}", exc)

        if watchlist:
            logger.debug(
                "[proactive-scan] watchlist ({}): {}",
                len(watchlist),
                ", ".join(f"{e.symbol}:{e.direction}={e.ev:+.2f}R" for e in watchlist),
            )
        return watchlist

    def _best_for_symbol(self, symbol: str) -> Optional[WatchlistEntry]:
        try:
            wm = self._get_wm(symbol)
            if wm is None:
                return None
            cands = (
                wm.candidates_list()
                if hasattr(wm, "candidates_list")
                else list(getattr(wm, "candidates", ()) or [])
            )
            if not cands:
                return None
            best = max(
                cands,
                key=lambda c: float(getattr(c, "ev_estimate", 0.0) or 0.0),
            )
            ev = float(getattr(best, "ev_estimate", 0.0) or 0.0)
            if ev < self._min_ev:
                return None
            if self._proximity is not None:
                try:
                    if not self._proximity(symbol):
                        return None
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[proactive-scan] proximity check failed: {}", exc)
            return WatchlistEntry(
                symbol=symbol,
                direction=str(getattr(best, "direction", "") or ""),
                ev=ev,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[proactive-scan] scan failed for {}: {}", symbol, exc)
            return None

    # ── Query ─────────────────────────────────────────────────────────────

    def get_watchlist(self) -> list[dict]:
        with self._lock:
            return [
                {"symbol": e.symbol, "direction": e.direction, "ev": e.ev}
                for e in self._last_watchlist
            ]

    def stats(self) -> dict:
        with self._lock:
            size = len(self._last_watchlist)
        return {
            "enabled": self.enabled,
            "running": self._running,
            "interval_seconds": self._interval,
            "min_ev": self._min_ev,
            "scans": self._scans,
            "watchlist_size": size,
        }


__all__ = ["ProactiveOpportunityScanner", "WatchlistEntry"]
