"""
APEX TRADER — Scan Scheduler
Controls how often the scanner runs based on session state and news.
Active/overlap sessions = every 5s (M1 scalping). Dead zones = every 300s.
News block = 120s. Active cadence and the with-positions cap are tunable via
PerformanceConfig (scan_interval_active / scan_interval_with_positions).
"""

from datetime import datetime, timezone
from typing import Optional
from brain.session_engine import SessionStatus, NewsStatus


class ScanScheduler:
    """
    A sharp sniper doesn't look away during the action.
    Scan frequency adapts to market conditions.
    When positions are open, never goes slower than the with-positions cap —
    active positions deserve active monitoring regardless of session.
    """

    # Cadences (seconds) for non-active sessions — unchanged, no need to hammer
    # the broker during quiet/dead/news windows.
    _BASE_INTERVALS = {
        "quiet":    60,
        "dead":     300,
        "weekend":  300,   # Sat 00:00 – Sun 21:59 UTC only; Sun 22:00+ is LIVE
        "news":     120,
    }

    # Defaults for the tunable active-trading cadences (M1 scalping = 5s).
    DEFAULT_ACTIVE_INTERVAL = 5
    DEFAULT_MAX_INTERVAL_WITH_POSITIONS = 5

    def __init__(self, config=None) -> None:
        perf = getattr(config, "performance", None)
        active = int(getattr(perf, "scan_interval_active", self.DEFAULT_ACTIVE_INTERVAL))
        self.max_interval_with_positions = int(
            getattr(perf, "scan_interval_with_positions", self.DEFAULT_MAX_INTERVAL_WITH_POSITIONS)
        )
        # Active and overlap sessions share the active cadence.
        self.intervals = dict(self._BASE_INTERVALS)
        self.intervals["overlap"] = active
        self.intervals["active"] = active

    def get_scan_interval(
        self,
        session_status: SessionStatus,
        news_status: NewsStatus,
        has_active_positions: bool = False,
    ) -> int:
        if not news_status.is_clear:
            base = self.intervals["news"]
        else:
            session = session_status.current_session.upper()
            if "OVERLAP" in session:
                base = self.intervals["overlap"]
            elif session in ("LONDON", "NEW_YORK"):
                base = self.intervals["active"]
            elif session in ("DEAD", "WEEKEND"):
                base = self.intervals[session.lower()]
            else:
                base = self.intervals["active"]

        if has_active_positions:
            return min(base, self.max_interval_with_positions)
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
