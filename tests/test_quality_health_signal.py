"""
Tests for OQ/EQ quality computation failure tracking.

Verifies that quality failures are counted, surfaced in the HealthWatchdog,
and trigger health warnings when ALL directional setups fail consecutively.
"""

from platforms.health_watchdog import HealthWatchdog


# ── HealthWatchdog quality tracking ──────────────────────────────────


class TestHealthWatchdogQualityTracking:
    """Test the record_quality_failures → check_health pipeline."""

    def test_no_quality_failures_healthy(self):
        wd = HealthWatchdog()
        report = wd.check_health()
        assert report.is_healthy is True
        assert report.quality_failures_total == 0
        assert report.quality_all_failed_cycles == 0

    def test_partial_failure_does_not_trigger(self):
        """4 out of 5 fail — NOT total failure, no alarm."""
        wd = HealthWatchdog()
        wd.record_quality_failures(failures=4, total_scans=5)
        wd.record_quality_failures(failures=4, total_scans=5)
        report = wd.check_health()
        assert report.is_healthy is True
        assert report.quality_failures_total == 8
        assert report.quality_all_failed_cycles == 0

    def test_single_total_failure_cycle_no_alarm(self):
        """ALL fail in one cycle — not enough, need 2 consecutive."""
        wd = HealthWatchdog()
        wd.record_quality_failures(failures=5, total_scans=5)
        report = wd.check_health()
        assert report.is_healthy is True
        assert report.quality_all_failed_cycles == 1
        assert len(report.warnings) == 0

    def test_two_consecutive_total_failures_triggers_alarm(self):
        """ALL fail for 2 consecutive cycles — triggers health warning."""
        wd = HealthWatchdog()
        wd.record_quality_failures(failures=5, total_scans=5)
        wd.record_quality_failures(failures=3, total_scans=3)
        report = wd.check_health()
        assert report.is_healthy is False
        assert report.quality_all_failed_cycles == 2
        assert report.quality_failures_total == 8
        assert any("OQ/EQ quality computation failing" in w for w in report.warnings)
        assert any("no trades can reach READY" in w for w in report.warnings)

    def test_success_after_total_failure_resets_streak(self):
        """Total failure, then a partial success resets the consecutive counter."""
        wd = HealthWatchdog()
        wd.record_quality_failures(failures=5, total_scans=5)
        assert wd._quality_all_failed_cycles == 1

        wd.record_quality_failures(failures=4, total_scans=5)
        assert wd._quality_all_failed_cycles == 0

        report = wd.check_health()
        assert report.is_healthy is True
        assert report.quality_failures_total == 9

    def test_three_consecutive_total_failures(self):
        """3 consecutive total-failure cycles → alarm with correct count."""
        wd = HealthWatchdog()
        for _ in range(3):
            wd.record_quality_failures(failures=10, total_scans=10)
        report = wd.check_health()
        assert report.is_healthy is False
        assert report.quality_all_failed_cycles == 3
        assert report.quality_failures_total == 30

    def test_zero_scans_does_not_count(self):
        """No directional setups (all NEUTRAL) → not a failure."""
        wd = HealthWatchdog()
        wd.record_quality_failures(failures=0, total_scans=0)
        wd.record_quality_failures(failures=0, total_scans=0)
        report = wd.check_health()
        assert report.is_healthy is True
        assert report.quality_all_failed_cycles == 0

    def test_health_report_fields_populated(self):
        """HealthReport exposes quality fields correctly."""
        wd = HealthWatchdog()
        wd.record_quality_failures(failures=7, total_scans=7)
        wd.record_quality_failures(failures=3, total_scans=3)
        report = wd.check_health()
        assert report.quality_failures_total == 10
        assert report.quality_all_failed_cycles == 2


# ── Existing health checks unaffected ────────────────────────────────


class TestExistingHealthChecksPreserved:
    """Ensure quality tracking doesn't break scan/trade failure reporting."""

    def test_scan_failures_still_trigger(self):
        wd = HealthWatchdog(max_consecutive_failures=3)
        for _ in range(3):
            wd.record_scan_failure()
        report = wd.check_health()
        assert report.is_healthy is False
        assert any("scan failures" in w for w in report.warnings)

    def test_trade_failures_still_trigger(self):
        wd = HealthWatchdog(max_consecutive_failures=3)
        for _ in range(3):
            wd.record_trade_check_failure()
        report = wd.check_health()
        assert report.is_healthy is False
        assert any("trade check failures" in w for w in report.warnings)

    def test_scanner_blind_unaffected(self):
        wd = HealthWatchdog(max_consecutive_failures=2)
        wd.record_scan_failure()
        wd.record_scan_failure()
        blind, reason = wd.is_scanner_blind()
        assert blind is True
        assert "consecutive scan failures" in reason
