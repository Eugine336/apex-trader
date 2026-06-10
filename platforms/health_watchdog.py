"""
APEX TRADER — Health Watchdog
Observes system vitals and reports anomalies.
Does not act — only reports. The circuit breaker decides what to do.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class HealthReport:
    is_healthy: bool
    uptime_seconds: float
    last_scan_age_seconds: float
    last_trade_check_age_seconds: float
    cycles_since_last_scan: int
    consecutive_scan_failures: int
    consecutive_trade_failures: int
    warnings: list[str] = field(default_factory=list)


class HealthWatchdog:
    """Monitors system health and reports anomalies without taking action."""

    def __init__(
        self,
        max_scan_gap_seconds: float = 600,
        max_trade_check_gap_seconds: float = 120,
        max_consecutive_failures: int = 5,
    ):
        self._max_scan_gap = max_scan_gap_seconds
        self._max_trade_check_gap = max_trade_check_gap_seconds
        self._max_consecutive_failures = max_consecutive_failures

        self._start_time = datetime.now(timezone.utc)
        self._last_successful_scan: Optional[datetime] = None
        self._last_successful_trade_check: Optional[datetime] = None
        self._cycles = 0
        self._cycles_since_scan = 0
        self._consecutive_scan_failures = 0
        self._consecutive_trade_failures = 0

    def record_scan_success(self) -> None:
        self._last_successful_scan = datetime.now(timezone.utc)
        self._cycles_since_scan = 0
        self._consecutive_scan_failures = 0

    def record_scan_failure(self) -> None:
        self._consecutive_scan_failures += 1

    def record_trade_check_success(self) -> None:
        self._last_successful_trade_check = datetime.now(timezone.utc)
        self._consecutive_trade_failures = 0

    def record_trade_check_failure(self) -> None:
        self._consecutive_trade_failures += 1

    def record_cycle(self) -> None:
        self._cycles += 1
        self._cycles_since_scan += 1

    def is_scanner_blind(self) -> tuple[bool, str]:
        """Return (True, reason) if the scanner is too degraded to trust."""
        if self._consecutive_scan_failures >= self._max_consecutive_failures:
            return True, f"{self._consecutive_scan_failures} consecutive scan failures"
        if self._last_successful_scan:
            now = datetime.now(timezone.utc)
            age = (now - self._last_successful_scan).total_seconds()
            if age > self._max_scan_gap:
                return True, f"no successful scan for {age:.0f}s (max {self._max_scan_gap:.0f}s)"
        return False, ""

    def check_health(self) -> HealthReport:
        now = datetime.now(timezone.utc)
        uptime = (now - self._start_time).total_seconds()

        scan_age = (
            (now - self._last_successful_scan).total_seconds()
            if self._last_successful_scan
            else uptime
        )
        trade_check_age = (
            (now - self._last_successful_trade_check).total_seconds()
            if self._last_successful_trade_check
            else uptime
        )

        warnings: list[str] = []

        if self._last_successful_scan and scan_age > self._max_scan_gap:
            warnings.append(
                f"No successful scan for {scan_age:.0f}s (threshold {self._max_scan_gap:.0f}s)"
            )

        if (
            self._last_successful_trade_check
            and trade_check_age > self._max_trade_check_gap
        ):
            warnings.append(
                f"No trade check for {trade_check_age:.0f}s (threshold {self._max_trade_check_gap:.0f}s)"
            )

        if self._consecutive_scan_failures >= self._max_consecutive_failures:
            warnings.append(
                f"{self._consecutive_scan_failures} consecutive scan failures"
            )

        if self._consecutive_trade_failures >= self._max_consecutive_failures:
            warnings.append(
                f"{self._consecutive_trade_failures} consecutive trade check failures"
            )

        is_healthy = len(warnings) == 0

        return HealthReport(
            is_healthy=is_healthy,
            uptime_seconds=uptime,
            last_scan_age_seconds=scan_age,
            last_trade_check_age_seconds=trade_check_age,
            cycles_since_last_scan=self._cycles_since_scan,
            consecutive_scan_failures=self._consecutive_scan_failures,
            consecutive_trade_failures=self._consecutive_trade_failures,
            warnings=warnings,
        )
