"""
APEX TRADER — Scan Scheduler
Controls how often the scanner runs based on session state and news.
Overlap sessions = every 10s. Dead zones = every 300s. News block = 120s.
"""

from datetime import datetime, timezone
from typing import Optional
from brain.session_engine import SessionStatus, NewsStatus


class ScanScheduler:
    """
    A sharp sniper doesn't look away during the action.
    Scan frequency adapts to market conditions.
    When positions are open, never goes slower than 15s — active positions
    deserve active monitoring regardless of session.
    """

    INTERVALS = {
        "overlap":  10,
        "active":   10,
        "quiet":    60,
        "dead":     300,
        "weekend":  300,   # Sat 00:00 – Sun 21:59 UTC only; Sun 22:00+ is LIVE
        "news":     120,
    }

    MAX_INTERVAL_WITH_POSITIONS = 15

    def get_scan_interval(
        self,
        session_status: SessionStatus,
        news_status: NewsStatus,
        has_active_positions: bool = False,
    ) -> int:
        if not news_status.is_clear:
            base = self.INTERVALS["news"]
        else:
            session = session_status.current_session.upper()
            if "OVERLAP" in session:
                base = self.INTERVALS["overlap"]
            elif session in ("LONDON", "NEW_YORK"):
                base = self.INTERVALS["active"]
            elif session in ("DEAD", "WEEKEND"):
                base = self.INTERVALS[session.lower()]
            else:
                base = self.INTERVALS["active"]

        if has_active_positions:
            return min(base, self.MAX_INTERVAL_WITH_POSITIONS)
        return base

    def should_scan_now(
        self,
        last_scan_time: Optional[datetime],
        session_status: SessionStatus,
        news_status: NewsStatus,
        has_active_positions: bool = False,
    ) -> bool:
        if last_scan_time is None:
            return True
        elapsed = (datetime.now(timezone.utc) - last_scan_time).total_seconds()
        return elapsed >= self.get_scan_interval(
            session_status, news_status,
            has_active_positions=has_active_positions,
        )
