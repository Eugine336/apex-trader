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
    """

    INTERVALS = {
        "overlap":  10,
        "active":   10,
        "quiet":    60,
        "dead":     300,
        "weekend":  300,
        "news":     120,
    }

    def get_scan_interval(
        self,
        session_status: SessionStatus,
        news_status: NewsStatus,
    ) -> int:
        if not news_status.is_clear:
            return self.INTERVALS["news"]
        session = session_status.current_session.upper()
        if "OVERLAP" in session:
            return self.INTERVALS["overlap"]
        if session in ("LONDON", "NEW_YORK"):
            return self.INTERVALS["active"]
        if session in ("DEAD", "WEEKEND"):
            return self.INTERVALS[session.lower()]
        return self.INTERVALS["quiet"]

    def should_scan_now(
        self,
        last_scan_time: Optional[datetime],
        session_status: SessionStatus,
        news_status: NewsStatus,
    ) -> bool:
        if last_scan_time is None:
            return True
        elapsed = (datetime.now(timezone.utc) - last_scan_time).total_seconds()
        return elapsed >= self.get_scan_interval(session_status, news_status)
