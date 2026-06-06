"""
APEX TRADER — Phase 4 Resilience Tests
Tests for auto-reconnect, health watchdog, circuit breaker, maintenance, and startup checks.
"""

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


from platforms.circuit_breaker import CircuitBreaker, CircuitState
from platforms.health_watchdog import HealthWatchdog
from platforms.maintenance import DailyMaintenance
from platforms.startup_check import StartupCheck


# ═══════════════════════════════════════════════════════════════════════
# 4.1 — Auto-Reconnect
# ═══════════════════════════════════════════════════════════════════════

class TestAutoReconnect:

    def _make_manager(self):
        from platforms.platform_manager import PlatformManager
        mgr = PlatformManager.__new__(PlatformManager)
        mgr.config = MagicMock()
        mgr.mt5 = MagicMock()
        mgr.deriv = MagicMock()
        mgr.mt5_connectors = [mgr.mt5]
        mgr._mt5_connected_flags = [False]
        mgr._deriv_connected = False
        mgr._mt5_was_connected = [True]
        mgr._deriv_was_connected = True
        mgr._reconnect_delays = [5, 10, 20, 40, 60]
        mgr._mt5_reconnect_attempts = [0]
        mgr._deriv_reconnect_attempt = 0
        mgr._mt5_next_reconnects = [0.0]
        mgr._deriv_next_reconnect = 0.0
        return mgr

    def test_reconnect_platform_success(self):
        mgr = self._make_manager()
        mgr.mt5_connectors[0].connect.return_value = True
        assert mgr.reconnect_platform("mt5") is True
        assert mgr._mt5_connected is True
        assert mgr._mt5_reconnect_attempts[0] == 0

    def test_reconnect_platform_failure(self):
        mgr = self._make_manager()
        mgr.mt5_connectors[0].connect.return_value = False
        assert mgr.reconnect_platform("mt5") is False
        assert mgr._mt5_connected is False
        assert mgr._mt5_reconnect_attempts[0] == 1

    def test_reconnect_exhausted(self):
        mgr = self._make_manager()
        mgr._mt5_reconnect_attempts = [5]
        mgr.mt5_connectors[0].connect.return_value = False
        assert mgr.reconnect_platform("mt5") is False

    def test_check_connections_detects_drop(self):
        mgr = self._make_manager()
        mgr._mt5_connected_flags = [True]
        mgr._deriv_connected = True
        mgr.mt5_connectors[0].is_connected.return_value = False
        mgr.deriv.is_connected.return_value = True
        state = mgr.check_connections()
        assert state["mt5"] is False
        assert state["deriv"] is True
        assert mgr._mt5_connected is False

    def test_should_attempt_reconnect_timing(self):
        mgr = self._make_manager()
        mgr._mt5_connected_flags = [False]
        mgr._mt5_was_connected = [True]
        mgr._mt5_next_reconnects = [time.monotonic() + 100]
        assert mgr.should_attempt_reconnect("mt5") is False
        mgr._mt5_next_reconnects = [0.0]
        assert mgr.should_attempt_reconnect("mt5") is True

    def test_should_not_reconnect_if_never_connected(self):
        mgr = self._make_manager()
        mgr._mt5_connected_flags = [False]
        mgr._mt5_was_connected = [False]
        assert mgr.should_attempt_reconnect("mt5") is False

    def test_reconnect_deriv_success(self):
        mgr = self._make_manager()
        mgr.deriv.connect.return_value = True
        assert mgr.reconnect_platform("deriv") is True
        assert mgr._deriv_connected is True
        assert mgr._deriv_reconnect_attempt == 0

    def test_reconnect_resets_on_success(self):
        mgr = self._make_manager()
        mgr._mt5_reconnect_attempts = [3]
        mgr._mt5_next_reconnects = [999999]
        mgr.mt5_connectors[0].connect.return_value = True
        assert mgr.reconnect_platform("mt5") is True
        assert mgr._mt5_reconnect_attempts[0] == 0
        assert mgr._mt5_next_reconnects[0] == 0.0


# ═══════════════════════════════════════════════════════════════════════
# 4.2 — Health Watchdog
# ═══════════════════════════════════════════════════════════════════════

class TestHealthWatchdog:

    def test_healthy_initial(self):
        wd = HealthWatchdog()
        report = wd.check_health()
        assert report.is_healthy is True
        assert report.consecutive_scan_failures == 0
        assert report.warnings == []

    def test_scan_success_resets_counter(self):
        wd = HealthWatchdog()
        wd.record_scan_failure()
        wd.record_scan_failure()
        assert wd._consecutive_scan_failures == 2
        wd.record_scan_success()
        assert wd._consecutive_scan_failures == 0

    def test_stale_scan_warning(self):
        wd = HealthWatchdog(max_scan_gap_seconds=1)
        wd.record_scan_success()
        time.sleep(1.1)
        report = wd.check_health()
        assert report.is_healthy is False
        assert any("No successful scan" in w for w in report.warnings)

    def test_consecutive_failures_warning(self):
        wd = HealthWatchdog(max_consecutive_failures=3)
        for _ in range(3):
            wd.record_scan_failure()
        report = wd.check_health()
        assert report.is_healthy is False
        assert any("consecutive scan failures" in w for w in report.warnings)

    def test_trade_check_success_resets(self):
        wd = HealthWatchdog()
        wd.record_trade_check_failure()
        wd.record_trade_check_failure()
        wd.record_trade_check_success()
        assert wd._consecutive_trade_failures == 0

    def test_record_cycle_increments(self):
        wd = HealthWatchdog()
        wd.record_cycle()
        wd.record_cycle()
        assert wd._cycles == 2
        assert wd._cycles_since_scan == 2

    def test_trade_check_failure_warning(self):
        wd = HealthWatchdog(max_consecutive_failures=2)
        wd.record_trade_check_failure()
        wd.record_trade_check_failure()
        report = wd.check_health()
        assert any("trade check failures" in w for w in report.warnings)

    def test_uptime_tracking(self):
        wd = HealthWatchdog()
        time.sleep(0.1)
        report = wd.check_health()
        assert report.uptime_seconds >= 0.1


# ═══════════════════════════════════════════════════════════════════════
# 4.3 — Daily Maintenance
# ═══════════════════════════════════════════════════════════════════════

class TestDailyMaintenance:

    def test_should_run_first_time(self):
        m = DailyMaintenance()
        assert m.should_run() is True

    def test_should_not_run_same_day(self):
        m = DailyMaintenance()
        now = datetime.now(timezone.utc)
        m.run(now)
        assert m.should_run(now) is False

    def test_should_run_next_day(self):
        m = DailyMaintenance()
        today = datetime.now(timezone.utc)
        m.run(today)
        tomorrow = today + timedelta(days=1)
        assert m.should_run(tomorrow) is True

    def test_backup_database_creates_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            db_path = data_dir / "apex_positions.db"
            db_path.write_text("test")

            m = DailyMaintenance(data_dir=str(data_dir))
            result = m._backup_database()
            assert "backed_up" in result

            backup_dir = data_dir / "backups"
            assert backup_dir.exists()
            backups = list(backup_dir.glob("positions_*.db"))
            assert len(backups) == 1

    def test_backup_no_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = DailyMaintenance(data_dir=str(Path(tmp) / "nonexistent"))
            result = m._backup_database()
            assert result == "no_database"

    def test_rotate_logs_deletes_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()

            old_log = log_dir / "old.log"
            old_log.write_text("old")
            os.utime(str(old_log), (0, 0))

            new_log = log_dir / "new.log"
            new_log.write_text("new")

            m = DailyMaintenance(log_dir=str(log_dir), max_log_days=1)
            deleted = m._rotate_logs()
            assert deleted == 1
            assert not old_log.exists()
            assert new_log.exists()

    def test_clean_stale_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            (data_dir / "test.tmp").write_text("tmp")
            (data_dir / "test.pyc").write_text("pyc")

            m = DailyMaintenance(data_dir=str(data_dir))
            cleaned = m._clean_stale_data()
            assert cleaned == 2

    def test_run_returns_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(data_dir=str(data_dir), log_dir=str(Path(tmp) / "logs"))
            result = m.run()
            assert "db_backup" in result
            assert "logs_cleaned" in result
            assert "stale_cleaned" in result


# ═══════════════════════════════════════════════════════════════════════
# 4.4 — Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:

    def test_closed_allows_execution(self):
        cb = CircuitBreaker("test")
        assert cb.can_execute() is True

    def test_opens_after_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.can_execute() is True
        cb.record_failure()
        assert cb.can_execute() is False
        assert cb.get_status().state == CircuitState.OPEN

    def test_blocks_when_open(self):
        cb = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=300)
        cb.record_failure()
        cb.record_failure()
        assert cb.can_execute() is False

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=0)
        cb.record_failure()
        assert cb._state == CircuitState.OPEN
        assert cb.can_execute() is True
        assert cb._state == CircuitState.HALF_OPEN

    def test_closes_on_success_in_half_open(self):
        cb = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=0)
        cb.record_failure()
        cb.can_execute()
        cb.record_success()
        assert cb._state == CircuitState.CLOSED
        assert cb._failure_count == 0

    def test_reopens_on_failure_in_half_open(self):
        cb = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=0, half_open_max_failures=2)
        cb.record_failure()
        cb.can_execute()
        cb.record_failure()
        cb.record_failure()
        assert cb._state == CircuitState.OPEN

    def test_get_status_reports_correctly(self):
        cb = CircuitBreaker("test_cb", failure_threshold=2)
        status = cb.get_status()
        assert status.state == CircuitState.CLOSED
        assert status.failure_count == 0
        cb.record_failure()
        status = cb.get_status()
        assert status.failure_count == 1

    def test_cooldown_remaining(self):
        cb = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=600)
        cb.record_failure()
        status = cb.get_status()
        assert status.cooldown_remaining_seconds > 500

    def test_success_records_time(self):
        cb = CircuitBreaker("test")
        assert cb._last_success_time is None
        cb.record_success()
        assert cb._last_success_time is not None


# ═══════════════════════════════════════════════════════════════════════
# 4.5 — Startup Self-Test
# ═══════════════════════════════════════════════════════════════════════

class TestStartupCheck:

    def test_all_pass(self):
        sc = StartupCheck()
        passed, results = sc.run_all()
        assert passed is True
        for r in results:
            assert r.duration_ms >= 0

    def test_imports_check(self):
        sc = StartupCheck()
        result = sc._check_imports()
        assert result.passed is True
        assert result.name == "imports"

    def test_config_check(self):
        sc = StartupCheck()
        result = sc._check_config()
        assert result.passed is True
        assert "pairs enabled" in result.message

    def test_instrument_registry_check(self):
        sc = StartupCheck()
        result = sc._check_instrument_registry()
        assert result.passed is True
        assert "instruments" in result.message

    def test_database_check(self):
        sc = StartupCheck()
        result = sc._check_database()
        assert result.passed is True

    def test_disk_space_check(self):
        sc = StartupCheck()
        result = sc._check_disk_space()
        assert result.passed is True
        assert "MB free" in result.message

    def test_critical_failure_aborts(self):
        sc = StartupCheck()
        with patch.object(sc, "_check_imports") as mock_imp:
            mock_imp.return_value = type(
                "CheckResult", (), {"name": "imports", "passed": False, "message": "fail", "duration_ms": 0}
            )()
            passed, results = sc.run_all()
            assert passed is False
