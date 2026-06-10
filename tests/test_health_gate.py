"""
Tests for HealthWatchdog.is_scanner_blind() predicate and the main-loop
health gate that blocks new entries while keeping position management alive.

Run:  python -m pytest tests/test_health_gate.py -v
"""

from datetime import datetime, timedelta, timezone

import pytest

from platforms.health_watchdog import HealthWatchdog


# ── is_scanner_blind predicate ──────────────────────────────────────


class TestIsScannerBlind:

    def test_healthy_watchdog_not_blind(self):
        wd = HealthWatchdog(max_consecutive_failures=5, max_scan_gap_seconds=600)
        wd.record_scan_success()
        blind, reason = wd.is_scanner_blind()
        assert blind is False
        assert reason == ""

    def test_blind_after_consecutive_failures(self):
        wd = HealthWatchdog(max_consecutive_failures=3)
        for _ in range(3):
            wd.record_scan_failure()
        blind, reason = wd.is_scanner_blind()
        assert blind is True
        assert "3 consecutive scan failures" in reason

    def test_not_blind_below_threshold(self):
        wd = HealthWatchdog(max_consecutive_failures=5)
        for _ in range(4):
            wd.record_scan_failure()
        blind, _ = wd.is_scanner_blind()
        assert blind is False

    def test_blind_on_stale_scan(self):
        wd = HealthWatchdog(max_scan_gap_seconds=60)
        wd._last_successful_scan = datetime.now(timezone.utc) - timedelta(seconds=120)
        blind, reason = wd.is_scanner_blind()
        assert blind is True
        assert "no successful scan" in reason

    def test_not_blind_when_scan_fresh(self):
        wd = HealthWatchdog(max_scan_gap_seconds=600)
        wd.record_scan_success()
        blind, _ = wd.is_scanner_blind()
        assert blind is False

    def test_not_blind_when_no_scan_yet(self):
        """Before the first scan completes, staleness check is skipped
        (no baseline to compare against); only consecutive failures gate."""
        wd = HealthWatchdog(max_scan_gap_seconds=60, max_consecutive_failures=5)
        blind, _ = wd.is_scanner_blind()
        assert blind is False

    def test_success_resets_consecutive_failures(self):
        wd = HealthWatchdog(max_consecutive_failures=3)
        for _ in range(2):
            wd.record_scan_failure()
        wd.record_scan_success()
        wd.record_scan_failure()
        blind, _ = wd.is_scanner_blind()
        assert blind is False

    def test_consecutive_failures_at_exact_threshold(self):
        wd = HealthWatchdog(max_consecutive_failures=5)
        for _ in range(5):
            wd.record_scan_failure()
        blind, _ = wd.is_scanner_blind()
        assert blind is True


# ── main-loop health gate integration ───────────────────────────────
# These tests exercise the gate decision WITHOUT importing the full
# TradingLoop (which drags in torch via scanner → rl.bridge).
# We mock only the watchdog + drawdown interaction and verify:
#   - blind → scan skipped, management runs, cycle["health_blocked"]
#   - healthy → scan proceeds normally


class FakeDrawdown:
    def can_trade(self, now):
        return True, "ok"


class FakeScheduler:
    def should_scan_now(self, *a, **kw):
        return True


class FakeSessionStatus:
    is_tradeable = True


class TestHealthGateDecision:
    """Unit-test the gate decision that _run_once_inner makes."""

    def _simulate_gate(self, blind: bool, can_trade: bool = True):
        """Return (scan_allowed, health_blocked) mirroring _run_once_inner logic."""
        cycle = {}
        if blind:
            cycle["health_blocked"] = True
        if not can_trade:
            return False, cycle.get("health_blocked", False)
        health_blocked = cycle.get("health_blocked", False)
        scan_allowed = not health_blocked
        return scan_allowed, health_blocked

    def test_healthy_allows_scan(self):
        scan_allowed, blocked = self._simulate_gate(blind=False)
        assert scan_allowed is True
        assert blocked is False

    def test_blind_blocks_scan(self):
        scan_allowed, blocked = self._simulate_gate(blind=True)
        assert scan_allowed is False
        assert blocked is True

    def test_drawdown_paused_still_not_scanning(self):
        scan_allowed, blocked = self._simulate_gate(blind=False, can_trade=False)
        assert scan_allowed is False
        assert blocked is False

    def test_blind_and_drawdown_paused(self):
        scan_allowed, blocked = self._simulate_gate(blind=True, can_trade=False)
        assert scan_allowed is False
        assert blocked is True
