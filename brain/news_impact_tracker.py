"""News-impact measurement trigger for the CalibrationEngine.

The CalibrationEngine learns how strongly each instrument reacts to a currency's
high-impact news (``record_news_impact``) so news sensitivity is empirical, not a
hardcoded prior.  This module supplies the measurement trigger that feeds it.

How it works (piggybacks on the existing M5 candle-close heartbeat — no new
threads):

1. On each M5 close the tracker asks the :class:`~brain.session_engine.NewsGuard`
   for the high-impact events affecting the symbol.  When one has just fired it
   records the symbol's ``price_at_event`` (plus the event-time ATR) in a pending
   dict, keyed by ``symbol|currency|event_time``.
2. On subsequent M5 closes, once ``measure_after_min`` (default 30) has elapsed
   since the event, it computes the realised reaction
   ``|price_now - price_at_event|`` in pips and calls
   ``calibration_engine.record_news_impact(symbol, currency, move_pips,
   atr_pips)``, then drops the pending entry.
3. Pending entries older than ``max_pending_age_min`` are expired so the dict
   never grows unbounded (e.g. if a symbol stops ticking before maturity).

Pending baselines are persisted to JSON so a restart inside the measurement
window does not lose the captured ``price_at_event``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger


class NewsImpactTracker:
    """Captures price at news-event time and measures the reaction 30 min later."""

    def __init__(
        self,
        calibration_engine: Any,
        *,
        news_guard: Optional[Any] = None,
        measure_after_min: float = 30.0,
        capture_window_min: float = 6.0,
        max_pending_age_min: float = 180.0,
        atr_tf: str = "M5",
        state_path: str = "data/news_impact_pending.json",
    ) -> None:
        self._calibration_engine = calibration_engine
        self._news_guard = news_guard
        self._measure_after_min = float(measure_after_min)
        self._capture_window_min = float(capture_window_min)
        self._max_pending_age_min = float(max_pending_age_min)
        self._atr_tf = atr_tf
        self.state_path = state_path
        # key -> {symbol, currency, event_iso, price_at_event, atr_pips, pip_size}
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.load()

    # ── Heartbeat hook ────────────────────────────────────────────────────

    def on_m5_close(
        self,
        symbol: str,
        price: float,
        *,
        now: Optional[datetime] = None,
        events: Optional[list[Any]] = None,
    ) -> None:
        """Capture freshly-fired events and measure matured ones for ``symbol``.

        Best-effort and non-fatal: any failure is logged at debug and never
        interrupts the candle-close path that drives it.
        """
        try:
            if not symbol or price is None or float(price) <= 0:
                return
            now = now or datetime.now(timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)

            if events is None:
                events = self._fetch_events(symbol, now)

            changed = False
            changed |= self._capture_fired_events(symbol, float(price), now, events)
            changed |= self._measure_matured(symbol, float(price), now)
            changed |= self._expire_stale(now)
            if changed:
                self.save()
        except Exception as exc:  # noqa: BLE001 — never break the candle feed
            logger.debug("[news-impact] on_m5_close failed for {}: {}", symbol, exc)

    # ── Internals ─────────────────────────────────────────────────────────

    def _fetch_events(self, symbol: str, now: datetime) -> list[Any]:
        if self._news_guard is None:
            return []
        try:
            status = self._news_guard.check([symbol], now)
            return list(getattr(status, "events_nearby", None) or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("[news-impact] news_guard.check failed for {}: {}", symbol, exc)
            return []

    def _capture_fired_events(
        self, symbol: str, price: float, now: datetime, events: list[Any],
    ) -> bool:
        changed = False
        for ev in events:
            if str(getattr(ev, "impact", "")).upper() != "HIGH":
                continue
            event_time = getattr(ev, "time_utc", None)
            currency = getattr(ev, "currency", None)
            if event_time is None or not currency:
                continue
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            mins_since = (now - event_time).total_seconds() / 60.0
            # Only capture once the event has fired and within a short window so
            # ``price_at_event`` is a tight baseline.
            if not (0.0 <= mins_since <= self._capture_window_min):
                continue
            key = self._key(symbol, currency, event_time)
            with self._lock:
                if key in self._pending:
                    continue
                self._pending[key] = {
                    "symbol": symbol,
                    "currency": str(currency),
                    "event_iso": event_time.isoformat(),
                    "price_at_event": price,
                    "atr_pips": self._atr_pips(symbol),
                    "pip_size": self._pip_size(symbol),
                }
            changed = True
        return changed

    def _measure_matured(self, symbol: str, price: float, now: datetime) -> bool:
        changed = False
        with self._lock:
            for key, rec in list(self._pending.items()):
                if rec.get("symbol") != symbol:
                    continue
                event_time = self._parse_iso(rec.get("event_iso"))
                if event_time is None:
                    self._pending.pop(key, None)
                    changed = True
                    continue
                elapsed = (now - event_time).total_seconds() / 60.0
                if elapsed < self._measure_after_min:
                    continue
                pip = float(rec.get("pip_size") or 0.0) or self._pip_size(symbol)
                if pip <= 0:
                    pip = 0.0001
                move_pips = abs(price - float(rec.get("price_at_event", price))) / pip
                atr_pips = float(rec.get("atr_pips") or 0.0)
                if atr_pips <= 0:
                    atr_pips = self._atr_pips(symbol)
                self._pending.pop(key, None)
                changed = True
                if atr_pips > 0:
                    try:
                        self._calibration_engine.record_news_impact(
                            symbol, str(rec.get("currency")), move_pips, atr_pips,
                        )
                        logger.debug(
                            "[news-impact] {} {} reaction {:.1f} pips / atr {:.1f}",
                            symbol, rec.get("currency"), move_pips, atr_pips,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[news-impact] record_news_impact failed: {}", exc)
        return changed

    def _expire_stale(self, now: datetime) -> bool:
        changed = False
        with self._lock:
            for key, rec in list(self._pending.items()):
                event_time = self._parse_iso(rec.get("event_iso"))
                if event_time is None:
                    self._pending.pop(key, None)
                    changed = True
                    continue
                if (now - event_time).total_seconds() / 60.0 > self._max_pending_age_min:
                    self._pending.pop(key, None)
                    changed = True
        return changed

    def _atr_pips(self, symbol: str) -> float:
        try:
            stats = self._calibration_engine(symbol)
            if stats is None:
                return 0.0
            return float(stats.atr_pips(self._atr_tf))
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _pip_size(symbol: str) -> float:
        try:
            from config import get_pip_size
            return float(get_pip_size(symbol))
        except Exception:  # noqa: BLE001
            return 0.0001

    @staticmethod
    def _key(symbol: str, currency: str, event_time: datetime) -> str:
        return f"{symbol}|{currency}|{event_time.isoformat()}"

    @staticmethod
    def _parse_iso(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:  # noqa: BLE001
            return None

    # ── Persistence ───────────────────────────────────────────────────────

    def save(self, path: Optional[str] = None) -> bool:
        """Atomically persist pending baselines to JSON. Returns True on success."""
        target = path or self.state_path
        try:
            with self._lock:
                blob = {"pending": dict(self._pending)}
            directory = os.path.dirname(target) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".news_impact_", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(blob, fh)
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[news-impact] save failed ({}): {}", target, exc)
            return False

    def load(self, path: Optional[str] = None) -> int:
        """Reload pending baselines from JSON. Returns the number loaded."""
        target = path or self.state_path
        if not os.path.exists(target):
            return 0
        try:
            with open(target, encoding="utf-8") as fh:
                blob = json.load(fh)
            pending = blob.get("pending", {}) if isinstance(blob, dict) else {}
            with self._lock:
                self._pending = {str(k): dict(v) for k, v in pending.items()}
            return len(self._pending)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[news-impact] load failed ({}): {}", target, exc)
            return 0
