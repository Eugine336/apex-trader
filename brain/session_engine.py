"""
APEX TRADER — Session & News Engine
Knows exactly when to trade and when to stay out.
A professional trader never trades in dead markets
or right before a news bomb drops.
"""

import threading

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
        "LONDON":   {"open": time(7, 0),  "close": time(16, 0), "pairs": ["GBPUSD", "EURGBP", "EURUSD", "GBPJPY", "XAUUSD"]},
        "NEW_YORK": {"open": time(12, 0), "close": time(21, 0), "pairs": ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "XAUUSD"]},
    }

    OVERLAP_SESSIONS = {
        "LONDON_TOKYO":   {"start": time(7, 0),  "end": time(9, 0),  "quality": "MEDIUM"},
        "LONDON_NY":      {"start": time(12, 0), "end": time(16, 0), "quality": "EXCELLENT"},  # Best window
    }

    # Dead zones — avoid trading here
    DEAD_ZONES = [
        {"start": time(20, 0), "end": time(23, 59), "reason": "End of NY, low liquidity pre-Asia"},
    ]

    # Gold (XAUUSD) kill zones in UTC — its highest-probability windows.
    # These are fixed UTC windows used only for the Gold scoring bonus.
    GOLD_KILL_ZONES = [
        (time(7, 0),   time(8, 30)),   # London open — the initial impulse move
        (time(12, 30), time(14, 0)),   # NY open — second major move / reversal
        (time(15, 30), time(16, 0)),   # London close — the "Judas swing" fake move
    ]

    # Canonical session windows in their EXCHANGE-LOCAL time + IANA tz. The
    # UTC SESSIONS table above is the DST-unaware fallback; when zoneinfo is
    # available we derive the real UTC boundaries per date so London/NY shift
    # correctly across DST changes (and Sydney's southern-hemisphere DST too).
    _SESSION_LOCAL = {
        "SYDNEY":   {"tz": "Australia/Sydney", "open": time(7, 0),  "close": time(16, 0)},
        "TOKYO":    {"tz": "Asia/Tokyo",       "open": time(9, 0),  "close": time(18, 0)},
        "LONDON":   {"tz": "Europe/London",    "open": time(8, 0),  "close": time(17, 0)},
        "NEW_YORK": {"tz": "America/New_York", "open": time(8, 0),  "close": time(17, 0)},
    }

    def _resolve_sessions(self, utc_now: datetime) -> tuple[dict, dict]:
        """Return (sessions, overlaps) with DST-correct UTC boundaries.

        Falls back to the static UTC tables if zoneinfo / tz data is missing,
        so this never adds a hard dependency or raises in minimal environments.
        """
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415 — optional, std-lib 3.9+
        except Exception:
            return self.SESSIONS, self.OVERLAP_SESSIONS

        on_date = utc_now.date()
        sessions: dict = {}
        for name, base in self.SESSIONS.items():
            loc = self._SESSION_LOCAL.get(name)
            if not loc:
                sessions[name] = base
                continue
            try:
                tz = ZoneInfo(loc["tz"])
                open_utc = datetime.combine(on_date, loc["open"], tz).astimezone(timezone.utc).time()
                close_utc = datetime.combine(on_date, loc["close"], tz).astimezone(timezone.utc).time()
                sessions[name] = {"open": open_utc, "close": close_utc, "pairs": base["pairs"]}
            except Exception:
                sessions[name] = base

        # Overlaps derived from the DST-adjusted sessions.
        overlaps = {
            "LONDON_TOKYO": {
                "start": sessions["LONDON"]["open"],
                "end": sessions["TOKYO"]["close"],
                "quality": "MEDIUM",
            },
            "LONDON_NY": {
                "start": sessions["NEW_YORK"]["open"],
                "end": sessions["LONDON"]["close"],
                "quality": "EXCELLENT",
            },
        }
        return sessions, overlaps

    def get_status(self, utc_now: Optional[datetime] = None) -> SessionStatus:
        """Get current session status."""
        if utc_now is None:
            utc_now = datetime.now(timezone.utc)

        current_time = utc_now.time()
        weekday = utc_now.weekday()  # 0=Monday, 6=Sunday

        # DST-adjusted session/overlap boundaries for this date.
        sessions, overlaps = self._resolve_sessions(utc_now)

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
        for name, overlap in overlaps.items():
            if self._time_in_range(current_time, overlap["start"], overlap["end"]):
                open_mins = self._minutes_since(current_time, overlap["start"])
                return SessionStatus(
                    current_session=f"OVERLAP_{name}",
                    is_tradeable=True,
                    liquidity="HIGH",
                    best_pairs=sessions["LONDON"]["pairs"] + sessions["NEW_YORK"]["pairs"],
                    minutes_to_next_session=self._minutes_until(current_time, overlap["end"]),
                    session_open_minutes=open_mins,
                )

        # Check individual sessions
        for name, session in sessions.items():
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

    def get_session_score(
        self,
        utc_now: Optional[datetime] = None,
        symbol: Optional[str] = None,
    ) -> int:
        """Return session quality score for entry scoring.

        Base score is 0-10 by session. For XAUUSD the Gold kill-zone bonus is
        folded in on top of the base score (capped at 15) — Gold is extremely
        session-driven, so its best windows deserve extra weight.
        """
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
        base = scores.get(status.current_session, 0)
        if symbol and symbol.upper() == "XAUUSD":
            base = min(base + self.get_gold_kill_zone_bonus(utc_now), 15)
        return base

    def get_gold_kill_zone_bonus(self, utc_now: Optional[datetime] = None) -> int:
        """Gold-specific kill-zone bonus (0-5) for XAUUSD entry scoring.

        Grades how close the current time is to one of Gold's highest-probability
        windows (see ``GOLD_KILL_ZONES``):
          - inside a kill zone                       → 5
          - within 30 min of a kill zone             → 3
          - active London/NY session (no kill zone)  → 1
          - outside sessions                         → 0

        This is a filter/bonus that sharpens entry quality, never a gate.
        """
        if utc_now is None:
            utc_now = datetime.now(timezone.utc)

        now_mins = utc_now.hour * 60 + utc_now.minute

        nearest_gap: Optional[int] = None
        for start, end in self.GOLD_KILL_ZONES:
            s = start.hour * 60 + start.minute
            e = end.hour * 60 + end.minute
            if s <= now_mins < e:
                return 5
            gap = (s - now_mins) if now_mins < s else (now_mins - e)
            nearest_gap = gap if nearest_gap is None else min(nearest_gap, gap)

        if nearest_gap is not None and nearest_gap <= 30:
            return 3

        session = self.get_status(utc_now).current_session
        if "LONDON" in session or "NEW_YORK" in session:
            return 1
        return 0

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

    # Gold reacts to a broader set of events than currency-specific news:
    # geopolitical risk headlines, Treasury yields, and safe-haven flows can
    # move Gold $20-50 even when the event carries no traditional "currency".
    # When XAUUSD is in play these keywords widen the news filter beyond the
    # currency-matched events.
    GOLD_EXTRA_KEYWORDS = {
        "gold", "treasury", "yield", "yields", "bond", "bonds",
        "geopolitical", "war", "tariff", "tariffs", "sanctions",
        "safe haven", "safe-haven", "debt ceiling", "default",
    }

    def __init__(self, pause_before: int = 15, pause_after: int = 5):
        self.pause_before = pause_before
        self.pause_after = pause_after
        self._cache = []
        self._cache_time = None
        # Serializes the freshness-check + network fetch + cache update so
        # concurrent callers (parallel position scan, entry checks on tick
        # threads) don't stampede the feed or read a torn cache.
        self._lock = threading.Lock()

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

        # Feed failure with no cache — fail closed until calendar data returns.
        if events is None:
            return NewsStatus(
                is_clear=False,
                events_nearby=[],
                next_high_impact=None,
                affected_currencies=affected_currencies,
                warning_message="⚠️ NEWS FEED UNAVAILABLE — blocking trades until calendar data is available",
            )

        # Filter to relevant events
        gold_in_play = "XAUUSD" in {str(p).upper() for p in pairs_in_play}
        nearby = []
        for event in events:
            if event.impact != "HIGH":
                continue

            currency_match = event.currency in affected_currencies
            # Gold reacts to a broader set of events than currency-specific
            # news. When XAUUSD is in play, also admit HIGH-impact events whose
            # title matches a Gold-moving keyword even if their currency isn't
            # in the affected set (e.g. geopolitical / Treasury-yield headlines).
            gold_keyword_match = False
            if not currency_match and gold_in_play:
                title_lc = (event.title or "").lower()
                if any(kw in title_lc for kw in self.GOLD_EXTRA_KEYWORDS):
                    gold_keyword_match = True

            if not (currency_match or gold_keyword_match):
                continue

            mins = (event.time_utc - utc_now).total_seconds() / 60

            if -self.pause_after <= mins <= self.pause_before:
                event.minutes_away = int(mins)
                event.direction = "NOW" if abs(mins) <= 1 else ("BEFORE" if mins > 0 else "AFTER")
                if gold_keyword_match:
                    logger.info(
                        "[NewsGuard] Gold extra-keyword news match: '{}' "
                        "({}) — included for XAUUSD despite currency {}",
                        event.title, event.impact, event.currency,
                    )
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
        _KNOWN_CCY = {
            "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
            "CNH", "SGD", "HKD", "MXN", "ZAR", "SEK", "NOK", "TRY",
        }
        currencies = set()
        for pair in pairs:
            p = pair.upper()
            if p in CURRENCY_PAIRS:
                base, quote = CURRENCY_PAIRS[p]
                # Only fiat legs drive the economic calendar. A non-fiat leg
                # like XAU (Gold, registered as a pseudo-currency for strength
                # ranking) has no news events, so keep it out of the affected
                # set — USD still freezes XAUUSD around FOMC/CPI/NFP.
                for leg in (base, quote):
                    if leg in _KNOWN_CCY:
                        currencies.add(leg)
            else:
                # Non-forex (crypto / metals priced in a currency, e.g. BTCUSD,
                # XAUUSD): use the trailing 3-char currency so USD-driven events
                # (FOMC/CPI) still freeze them around the release.
                suffix = p[-3:]
                if suffix in _KNOWN_CCY:
                    currencies.add(suffix)
        return list(currencies)

    def _fetch_events(self, utc_now: datetime) -> Optional[list[NewsEvent]]:
        """Single-flight wrapper: one thread fetches at a time; others waiting
        on the lock re-check freshness and reuse the populated cache."""
        with self._lock:
            return self._fetch_events_locked(utc_now)

    def _fetch_events_locked(self, utc_now: datetime) -> Optional[list[NewsEvent]]:
        """
        Fetch economic calendar events.
        Uses ForexFactory RSS feed as primary source.
        Falls back to cache if available. Returns None when unavailable and
        no cache exists so callers can fail closed.
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

                    # Prefer the feed's OWN impact rating; the keyword list alone
                    # misses high-impact events not in it (spelled-out "Non-Farm
                    # Employment Change", PPI, central-bank speeches, etc.) and
                    # would leave them unblocked.
                    title = entry.get("title", "").lower()
                    ff_impact = str(entry.get("ff_impact", "")).strip().lower()
                    if ff_impact == "high":
                        impact = "HIGH"
                    elif ff_impact == "medium":
                        impact = "MEDIUM"
                    elif ff_impact in ("low", "holiday"):
                        impact = "LOW"
                    else:
                        # Feed gave no rating — fall back to a broadened keyword heuristic.
                        impact = "LOW"
                        if any(k in title for k in [
                            "nfp", "non-farm", "nonfarm", "cpi", "fomc", "gdp",
                            "rate decision", "interest rate", "ppi", "unemployment",
                            "central bank", "press conference", "rate statement",
                        ]):
                            impact = "HIGH"
                        elif any(k in title for k in [
                            "pmi", "retail", "employment", "sentiment", "confidence",
                        ]):
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
            logger.warning("News feed unavailable: {}", e)
            if self._cache:
                return self._cache
            logger.error(
                "NEWS FEED DOWN with no cache — trading will be blocked until feed recovers"
            )
            return None
