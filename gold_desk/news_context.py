"""Verified Gold event-window adapter.

NewsGuard supplies the actual high-impact event timestamp and affected event
metadata. The range comes only from completed M5 candles that closed before
the scheduled release. This module is observation-only and cannot stage orders.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from gold_desk.models import Candle, EventWindow


def build_verified_event_window(
    symbol: str,
    observed_at: datetime,
    candles: Mapping[str, Sequence[Candle]],
    news_guard: Any,
) -> EventWindow | None:
    """Build an active event context from a real calendar event and broker bars.

    Returns None if the symbol, news status, timestamps, or pre-event data are
    missing/uncertain. It never estimates release times or creates a price range
    from bars that closed at/after the scheduled event time.
    """
    try:
        symbol = str(symbol or "").strip()
        if not symbol:
            return None
        if news_guard is None:
            return None
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return None
        observed_at = observed_at.astimezone(timezone.utc)

        status = news_guard.check([symbol], observed_at)
        events = list(getattr(status, "events_nearby", None) or [])
        events = [
            event for event in events
            if str(getattr(event, "impact", "") or "").upper() in {"HIGH", "CRITICAL"}
        ]
        if not events:
            return None

        # Prefer the release closest to the current market observation.
        event = min(
            events,
            key=lambda item: abs((item.time_utc - observed_at).total_seconds()),
        )
        event_time = getattr(event, "time_utc", None)
        if not isinstance(event_time, datetime):
            return None
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        else:
            event_time = event_time.astimezone(timezone.utc)

        pause_before = max(1.0, float(getattr(news_guard, "pause_before", 15) or 15))
        pause_after = max(0.0, float(getattr(news_guard, "pause_after", 5) or 0))
        starts_at = event_time - timedelta(minutes=pause_before)
        ends_at = event_time + timedelta(minutes=pause_after)
        if not starts_at <= observed_at <= ends_at:
            return None

        m5_bars = list((candles or {}).get("M5", ()) or ())
        pre_event_bars = [
            bar for bar in m5_bars
            if isinstance(bar, Candle)
            and bar.timestamp < event_time
            and bar.timestamp <= observed_at
        ]
        if len(pre_event_bars) < 3:
            return None
        window_bars = pre_event_bars[-12:]
        pre_event_high = max(float(bar.high) for bar in window_bars)
        pre_event_low = min(float(bar.low) for bar in window_bars)
        if pre_event_high <= pre_event_low:
            return None

        return EventWindow(
            name=str(getattr(event, "title", "High-impact event") or "High-impact event"),
            starts_at=starts_at,
            ends_at=ends_at,
            pre_event_high=pre_event_high,
            pre_event_low=pre_event_low,
            impact=str(getattr(event, "impact", "HIGH") or "HIGH").upper(),
        )
    except Exception:
        # A news/data fault must not crash the market observer or invent context.
        return None
