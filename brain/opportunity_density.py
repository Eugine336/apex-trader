"""
APEX TRADER — Opportunity Density Tracker
When the market is handing out many setups, each one is worth slightly less
(the edge is commoditised). When setups are rare, size up on the few that appear.

Logic:
  - Track READY signals per rolling 60-minute window
  - Density tiers: LOW (<3/hr), NORMAL (3-8/hr), HIGH (>8/hr)
  - LOW  → position size multiplier 1.10 (10% boost  — rare quality signal)
  - NORMAL → 1.00 (no adjustment)
  - HIGH  → 0.80 (20% cut — diversification, market may be noisy)
"""

from collections import deque
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from loguru import logger


@dataclass
class DensitySnapshot:
    ready_count_1h: int
    tier: str           # "LOW", "NORMAL", "HIGH"
    size_multiplier: float
    volatility_adjusted: bool


class OpportunityDensityTracker:
    """
    Rolling-window counter for READY scan signals.
    Consumes scan reports from the main loop and exposes a sizing multiplier
    that main_loop applies on top of the ML position-size adjustment.
    """

    LOW_THRESHOLD  = 3   # fewer than this/hr → LOW density
    HIGH_THRESHOLD = 8   # more than this/hr  → HIGH density

    LOW_MULTIPLIER    = 1.10
    NORMAL_MULTIPLIER = 1.00
    HIGH_MULTIPLIER   = 0.80

    def __init__(self, window_minutes: int = 60):
        self._window = timedelta(minutes=window_minutes)
        # deque of (timestamp, ready_count) tuples
        self._history: deque[tuple[datetime, int]] = deque()
        self._last_snapshot: DensitySnapshot | None = None

    # ── Feed ──────────────────────────────────────────────────────────

    def record_scan(self, ready_count: int, utc_now: datetime | None = None) -> DensitySnapshot:
        """
        Call once per scan cycle with the number of READY setups found.
        Returns the current density snapshot.
        """
        utc_now = utc_now or datetime.now(timezone.utc)
        self._history.append((utc_now, ready_count))
        self._evict(utc_now)
        snapshot = self._compute(utc_now)
        self._last_snapshot = snapshot
        logger.debug(
            "DensityTracker — {}/hr READY signals → {} tier (×{:.2f})",
            snapshot.ready_count_1h, snapshot.tier, snapshot.size_multiplier,
        )
        return snapshot

    # ── Query ─────────────────────────────────────────────────────────

    def get_size_multiplier(self) -> float:
        """Return the most recent density multiplier (1.0 if no data yet)."""
        if self._last_snapshot is None:
            return 1.0
        return self._last_snapshot.size_multiplier

    def get_snapshot(self) -> DensitySnapshot | None:
        return self._last_snapshot

    # ── Internal ──────────────────────────────────────────────────────

    def _evict(self, utc_now: datetime) -> None:
        """Drop records older than the rolling window."""
        cutoff = utc_now - self._window
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def _compute(self, utc_now: datetime) -> DensitySnapshot:
        total_ready = sum(count for _, count in self._history)

        if total_ready < self.LOW_THRESHOLD:
            tier = "LOW"
            mult = self.LOW_MULTIPLIER
        elif total_ready > self.HIGH_THRESHOLD:
            tier = "HIGH"
            mult = self.HIGH_MULTIPLIER
        else:
            tier = "NORMAL"
            mult = self.NORMAL_MULTIPLIER

        return DensitySnapshot(
            ready_count_1h=total_ready,
            tier=tier,
            size_multiplier=mult,
            volatility_adjusted=False,
        )
