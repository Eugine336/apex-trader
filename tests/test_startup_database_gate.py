"""
Tests for startup database gating — a degraded position store must block launch.

Covers:
  (a) Degraded store → _check_database passed=False → run_all all_critical_passed=False.
  (b) Fresh empty healthy store → _check_database passed=True → run_all passes.
  (c) Healthy store with positions → _check_database passed=True.
  (d) database is in the critical gating set.
  (e) Exception during DB check → passed=False.
"""

import pytest
from unittest.mock import patch, MagicMock

from platforms.startup_check import StartupCheck, CheckResult

PATCH_TARGET = "persistence.position_store.PositionStore"


def _mock_store(healthy: bool, count: int, degraded_reason: str = ""):
    store = MagicMock()
    store.is_healthy.return_value = healthy
    store.count.return_value = count
    store.degraded_reason.return_value = degraded_reason
    store.close.return_value = None
    return store


def _make_other_criticals_pass(checker: StartupCheck):
    """Patch the three other critical checks to pass so we isolate database."""
    pass_result = lambda name: CheckResult(
        name=name, passed=True, message="ok", duration_ms=0.1
    )
    checker._check_imports = lambda: pass_result("imports")
    checker._check_config = lambda: pass_result("config")
    checker._check_instrument_registry = lambda: pass_result("instrument_registry")
    checker._check_disk_space = lambda: pass_result("disk_space")


class TestDatabaseGating:

    def test_database_is_critical(self):
        """database must be in the gating set so its failure blocks launch."""
        checker = StartupCheck()
        _make_other_criticals_pass(checker)
        store = _mock_store(healthy=False, count=0, degraded_reason="test failure")
        with patch(PATCH_TARGET, return_value=store):
            passed, results = checker.run_all()
        assert not passed, "run_all must fail when database is degraded"

    def test_degraded_store_fails_check(self):
        store = _mock_store(
            healthy=False, count=0, degraded_reason="consecutive_failures=3"
        )
        checker = StartupCheck()
        with patch(PATCH_TARGET, return_value=store):
            result = checker._check_database()
        assert not result.passed
        assert "DEGRADED" in result.message
        assert "consecutive_failures=3" in result.message

    def test_degraded_store_with_positions_still_fails(self):
        store = _mock_store(
            healthy=False, count=5, degraded_reason="disk error"
        )
        checker = StartupCheck()
        with patch(PATCH_TARGET, return_value=store):
            result = checker._check_database()
        assert not result.passed

    def test_healthy_empty_store_passes(self):
        store = _mock_store(healthy=True, count=0)
        checker = StartupCheck()
        with patch(PATCH_TARGET, return_value=store):
            result = checker._check_database()
        assert result.passed
        assert "0 persisted positions" in result.message

    def test_healthy_store_with_positions_passes(self):
        store = _mock_store(healthy=True, count=7)
        checker = StartupCheck()
        with patch(PATCH_TARGET, return_value=store):
            result = checker._check_database()
        assert result.passed
        assert "7 persisted positions" in result.message

    def test_exception_during_check_fails(self):
        checker = StartupCheck()
        with patch(
            PATCH_TARGET,
            side_effect=RuntimeError("connection refused"),
        ):
            result = checker._check_database()
        assert not result.passed
        assert "connection refused" in result.message

    def test_run_all_passes_when_database_healthy(self):
        checker = StartupCheck()
        _make_other_criticals_pass(checker)
        store = _mock_store(healthy=True, count=3)
        with patch(PATCH_TARGET, return_value=store):
            passed, results = checker.run_all()
        assert passed
        db_result = next(r for r in results if r.name == "database")
        assert db_result.passed
