"""
APEX TRADER — Opportunity Density Tracker
When the market is handing out many setups, each one is worth slightly less
(the edge is commoditised).

Logic:
  - Track DISTINCT READY symbols per rolling 60-minute window
  - A pair that stays READY across consecutive scans counts once, not once per scan
  - The count is cadence-independent: changing scan_interval_seconds does not
    change the density metric or position sizing
  - Density tiers: LOW (<3 distinct/hr), NORMAL (3-8), HIGH (>8)
  - LOW  → position size multiplier 1.00 (no boost — low density may signal
    degraded data, not genuine rarity; and any >1.0 boost is clamped away by
    the downstream risk_ceiling anyway)
  - NORMAL → 1.00 (no adjustment)
  - HIGH  → 0.80 (20% cut — diversification, market may be noisy)
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Iterable, Union

from loguru import logger


@dataclass
class DensitySnapshot:
    ready_count_1h: int  # distinct READY symbols in window
    tier: str  # "LOW", "NORMAL", "HIGH"
    size_multiplier: float
    volatility_adjusted: bool


class OpportunityDensityTracker:
    """
    Rolling-window counter for DISTINCT READY symbols.
    Consumes scan reports from the main loop and exposes a sizing multiplier
    that main_loop applies on top of the ML position-size adjustment.
    """

    LOW_THRESHOLD = 3  # fewer than this distinct symbols/hr → LOW density
    HIGH_THRESHOLD = 8  # more than this distinct symbols/hr  → HIGH density

    LOW_MULTIPLIER = 1.00
    NORMAL_MULTIPLIER = 1.00
    HIGH_MULTIPLIER = 0.80

    def __init__(self, window_minutes: int = 60):
        self._window = timedelta(minutes=window_minutes)
        self._history: deque[tuple[datetime, frozenset[str]]] = deque()
        self._last_snapshot: DensitySnapshot | None = None

    # ── Feed ──────────────────────────────────────────────────────────

    def record_scan(
        self,
        ready: Union[Iterable[str], int],
        utc_now: datetime | None = None,
    ) -> DensitySnapshot:
        """
        Call once per scan cycle with the READY symbols found.

        ``ready`` may be:
          - An iterable of symbol strings (preferred) — each unique symbol
            is counted once across all scans in the window.
          - A legacy ``int`` count — treated as that many anonymous distinct
            setups for this single scan (backward-compatible fallback).
        """
        utc_now = utc_now or datetime.now(timezone.utc)

        if isinstance(ready, int):
            symbols: frozenset[str] = frozenset(f"__anon_{i}" for i in range(ready))
        else:
            symbols = frozenset(ready)

        self._history.append((utc_now, symbols))
        self._evict(utc_now)
        snapshot = self._compute()
        self._last_snapshot = snapshot
        logger.debug(
            "DensityTracker — {} distinct READY symbols/hr → {} tier (×{:.2f})",
            snapshot.ready_count_1h,
            snapshot.tier,
            snapshot.size_multiplier,
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

    def _compute(self) -> DensitySnapshot:
        all_symbols: set[str] = set()
        for _, symbols in self._history:
            all_symbols.update(symbols)
        distinct_count = len(all_symbols)

        if distinct_count < self.LOW_THRESHOLD:
            tier = "LOW"
            mult = self.LOW_MULTIPLIER
        elif distinct_count > self.HIGH_THRESHOLD:
            tier = "HIGH"
            mult = self.HIGH_MULTIPLIER
        else:
            tier = "NORMAL"
            mult = self.NORMAL_MULTIPLIER

        return DensitySnapshot(
            ready_count_1h=distinct_count,
            tier=tier,
            size_multiplier=mult,
            volatility_adjusted=False,
        )
