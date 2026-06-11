"""
APEX TRADER — Session & News Engine
Knows exactly when to trade and when to stay out.
A professional trader never trades in dead markets
or right before a news bomb drops.
"""

import pandas as pd
from datetime import datetime, time, timezone
from loguru import logger
from dataclasses import dataclass
from typing import Optional


@dataclass
class SessionStatus:
    current_session: str       # "TOKYO", "LONDON", "NEW_YORK", "OVERLAP", "DEAD"
    is_tradeable: bool
    liquidity: str             # "HIGH", "MEDIUM", "LOW", "DEAD"
    best_pairs: list[str]      # Which pairs are most active now
    minutes_to_next_session: int
    session_open_minutes: int  # How long current session has been open


@dataclass
class NewsEvent:
    title: str
    currency: str
    impact: str       # "HIGH", "MEDIUM", "LOW"
    time_utc: datetime
    minutes_away: int
    direction: str    # "BEFORE", "AFTER", "NOW"


@dataclass
class NewsStatus:
    is_clear: bool          # True = safe to trade
    events_nearby: list[NewsEvent]
    next_high_impact: Optional[NewsEvent]
    affected_currencies: list[str]
    warning_message: str


class SessionEngine:
    """
    Knows the market clock better than anyone.
    Tracks all sessions, overlaps, and identifies
    the highest-liquidity windows for each pair.
    """

    SESSIONS = {
        "SYDNEY":   {"open": time(21, 0), "close": time(6, 0),  "pairs": ["AUDUSD", "AUDNZD", "AUDJPY"]},
        "TOKYO":    {"open": time(0, 0),  "close": time(9, 0),  "pairs": ["USDJPY", "AUDJPY", "EURJPY", "CADJPY"]},
        "LONDON":   {"open": time(7, 0),  "close": time(16, 0), "pairs": ["GBPUSD", "EURGBP", "EURUSD", "GBPJPY"]},
        "NEW_YORK": {"open": time(12, 0), "close": time(21, 0), "pairs": ["EURUSD", "GBPUSD", "USDJPY", "USDCAD"]},
    }

    OVERLAP_SESSIONS = {
        "LONDON_TOKYO":   {"start": time(7, 0),  "end": time(9, 0),  "quality": "MEDIUM"},
        "LONDON_NY":      {"start": time(12, 0), "end": time(16, 0), "quality": "EXCELLENT"},  # Best window
    }

    # Dead zones — avoid trading here
    DEAD_ZONES = [
        {"start": time(20, 0), "end": time(23, 59), "reason": "End of NY, low liquidity pre-Asia"},
    ]

    def get_status(self, utc_now: Optional[datetime] = None) -> SessionStatus:
        """Get current session status."""
        if utc_now is None:
            utc_now = datetime.now(timezone.utc)

        current_time = utc_now.time()
        weekday = utc_now.weekday()  # 0=Monday, 6=Sunday

        # Weekend check — FX closed Sat 00:00 UTC through Sun 21:59 UTC.
        # Sunday >= 22:00 UTC the market is live — do NOT treat as weekend.
        fx_weekend = (
            weekday == 5                              # Saturday — always closed
            or (weekday == 6 and utc_now.hour < 22)  # Sunday before 22:00 UTC open
        )
        if fx_weekend:
            return SessionStatus(
                current_session="WEEKEND",
                is_tradeable=False,
                liquidity="DEAD",
                best_pairs=[],
                minutes_to_next_session=self._minutes_to_fx_open(utc_now),
                session_open_minutes=0,
            )

        # Check for overlap first (highest priority)
        for name, overlap in self.OVERLAP_SESSIONS.items():
            if self._time_in_range(current_time, overlap["start"], overlap["end"]):
                open_mins = self._minutes_since(current_time, overlap["start"])
                return SessionStatus(
                    current_session=f"OVERLAP_{name}",
                    is_tradeable=True,
                    liquidity="HIGH",
                    best_pairs=self.SESSIONS["LONDON"]["pairs"] + self.SESSIONS["NEW_YORK"]["pairs"],
                    minutes_to_next_session=self._minutes_until(current_time, overlap["end"]),
                    session_open_minutes=open_mins,
                )

        # Check individual sessions
        for name, session in self.SESSIONS.items():
            if self._time_in_range(current_time, session["open"], session["close"]):
                open_mins = self._minutes_since(current_time, session["open"])
                liquidity = "HIGH" if name in ["LONDON", "NEW_YORK"] else "MEDIUM"
                tradeable = True

                return SessionStatus(
                    current_session=name,
                    is_tradeable=tradeable,
                    liquidity=liquidity,
                    best_pairs=session["pairs"],
                    minutes_to_next_session=self._minutes_until(current_time, session["close"]),
                    session_open_minutes=open_mins,
                )

        # Dead zones only if no active overlap/session matched.
        for dz in self.DEAD_ZONES:
            if self._time_in_range(current_time, dz["start"], dz["end"]):
                return SessionStatus(
                    current_session="DEAD",
                    is_tradeable=False,
                    liquidity="DEAD",
                    best_pairs=[],
                    minutes_to_next_session=self._minutes_until(current_time, time(0, 0)),
                    session_open_minutes=0,
                )

        return SessionStatus(
            current_session="TRANSITION",
            is_tradeable=False,
            liquidity="LOW",
            best_pairs=[],
            minutes_to_next_session=30,
            session_open_minutes=0,
        )

    def is_pair_active(self, pair: str, utc_now: Optional[datetime] = None) -> bool:
        """Check if a specific pair is in its active session."""
        status = self.get_status(utc_now)
        return pair in status.best_pairs or status.liquidity == "HIGH"

    def get_session_score(self, utc_now: Optional[datetime] = None) -> int:
        """Return session quality score (0-10) for entry scoring."""
        status = self.get_status(utc_now)
        scores = {
            "OVERLAP_LONDON_NY":    10,
            "OVERLAP_LONDON_TOKYO": 6,
            "LONDON":               8,
            "NEW_YORK":             7,
            "TOKYO":                4,
            "SYDNEY":               3,
            "DEAD":                 0,
            "WEEKEND":              0,
            "TRANSITION":           1,
        }
        return scores.get(status.current_session, 0)

    def _time_in_range(self, t: time, start: time, end: time) -> bool:
        """Check if time is within range, handles overnight ranges."""
        if start <= end:
            return start <= t < end
        else:  # Overnight
            return t >= start or t < end

    def _minutes_until(self, current: time, target: time) -> int:
        """Minutes from current time until target time."""
        now_mins = current.hour * 60 + current.minute
        tgt_mins = target.hour * 60 + target.minute
        diff = tgt_mins - now_mins
        if diff < 0:
            diff += 24 * 60
        return diff

    def _minutes_since(self, current: time, start: time) -> int:
        """Minutes since a start time."""
        cur_mins = current.hour * 60 + current.minute
        st_mins  = start.hour * 60 + start.minute
        diff = cur_mins - st_mins
        if diff < 0:
            diff += 24 * 60
        return diff

    def _minutes_to_fx_open(self, utc_now: datetime) -> int:
        """Minutes until FX opens (Sunday 22:00 UTC).
        Replaces _minutes_to_monday — FX opens Sunday evening, not Monday morning."""
        wd = utc_now.weekday()
        current_mins = utc_now.hour * 60 + utc_now.minute
        open_mins = 22 * 60  # 22:00 UTC

        if wd == 5:  # Saturday — next open is Sunday 22:00
            return (24 * 60 - current_mins) + open_mins
        if wd == 6 and current_mins < open_mins:  # Sunday before open
            return open_mins - current_mins
        return 0  # Market already open

    def minutes_to_fx_close(self, utc_now: Optional[datetime] = None) -> int:
        """Minutes until the next Friday 21:00 UTC FX close.

        Returns a small positive number only on Friday before 21:00 UTC.
        All other times return 99999 (sentinel = not approaching close).
        """
        if utc_now is None:
            utc_now = datetime.now(timezone.utc)
        if utc_now.weekday() != 4:  # Only Friday (weekday 4)
            return 99999
        close_mins = 21 * 60  # 21:00 UTC
        current_mins = utc_now.hour * 60 + utc_now.minute
        if current_mins >= close_mins:
            return 99999  # Already past Friday close
        return close_mins - current_mins


class NewsGuard:
    """
    Monitors economic calendar for high-impact news events.
    The bot FREEZES trading 15 minutes before and 5 minutes after
    any HIGH impact news that affects the pairs we're trading.
    """

    def __init__(self, pause_before: int = 15, pause_after: int = 5):
        self.pause_before = pause_before
        self.pause_after = pause_after
        self._cache = []
        self._cache_time = None

    def check(self, pairs_in_play: list[str], utc_now: Optional[datetime] = None) -> NewsStatus:
        """
        Check if it's safe to trade given upcoming/recent news events.
        """
        if utc_now is None:
            utc_now = datetime.now(timezone.utc)

        # Get affected currencies from pairs
        affected_currencies = self._get_currencies_from_pairs(pairs_in_play)

        # Fetch news events (with cache)
        events = self._fetch_events(utc_now)

        # Filter to relevant events
        nearby = []
        for event in events:
            if event.impact != "HIGH":
                continue
            if event.currency not in affected_currencies:
                continue

            mins = (event.time_utc - utc_now).total_seconds() / 60

            if -self.pause_after <= mins <= self.pause_before:
                event.minutes_away = int(mins)
                event.direction = "NOW" if abs(mins) <= 1 else ("BEFORE" if mins > 0 else "AFTER")
                nearby.append(event)

        is_clear = len(nearby) == 0

        # Find next high impact event
        future_events = [e for e in events if e.impact == "HIGH"
                        and e.currency in affected_currencies
                        and (e.time_utc - utc_now).total_seconds() > 0]
        next_event = min(future_events, key=lambda x: x.time_utc) if future_events else None

        warning = ""
        if not is_clear:
            event_names = [e.title for e in nearby]
            warning = f"⚠️ NEWS BLOCK: {', '.join(event_names)}"

        return NewsStatus(
            is_clear=is_clear,
            events_nearby=nearby,
            next_high_impact=next_event,
            affected_currencies=affected_currencies,
            warning_message=warning,
        )

    def _get_currencies_from_pairs(self, pairs: list[str]) -> list[str]:
        """Extract unique currencies from pair list."""
        from brain.currency_strength import CURRENCY_PAIRS
        currencies = set()
        for pair in pairs:
            if pair in CURRENCY_PAIRS:
                base, quote = CURRENCY_PAIRS[pair]
                currencies.add(base)
                currencies.add(quote)
        return list(currencies)

    def _fetch_events(self, utc_now: datetime) -> list[NewsEvent]:
        """
        Fetch economic calendar events.
        Uses ForexFactory RSS feed as primary source.
        Falls back to empty list if unavailable.
        """
        # Return cached events if fresh (< 30 min old)
        if (self._cache_time and
            (utc_now - self._cache_time).total_seconds() < 1800):
            return self._cache

        try:
            import feedparser
            feed = feedparser.parse("https://nfs.faireconomy.media/ff_calendar_thisweek.xml")
            events = []

            for entry in feed.entries:
                try:
                    event_time = pd.Timestamp(entry.get("published", "")).to_pydatetime()
                    if event_time.tzinfo is None:
                        event_time = event_time.replace(tzinfo=timezone.utc)

                    impact = "LOW"
                    title = entry.get("title", "").lower()
                    if any(k in title for k in ["nfp", "cpi", "fomc", "gdp", "rate decision", "interest rate"]):
                        impact = "HIGH"
                    elif any(k in title for k in ["pmi", "retail", "employment"]):
                        impact = "MEDIUM"

                    currency = entry.get("ff_currency", "USD").upper()
                    mins_away = (event_time - utc_now).total_seconds() / 60

                    events.append(NewsEvent(
                        title=entry.get("title", "Unknown"),
                        currency=currency,
                        impact=impact,
                        time_utc=event_time,
                        minutes_away=int(mins_away),
                        direction="BEFORE" if mins_away > 0 else "AFTER",
                    ))
                except Exception as exc:
                    logger.debug("[session_engine] failed to parse news event, skipping: {}", exc)
                    continue

            self._cache = events
            self._cache_time = utc_now
            return events

        except Exception as e:
            logger.warning(f"News feed unavailable: {e}")
            return self._cache or []
