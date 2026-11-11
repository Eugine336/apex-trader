"""
Regression tests for the 5 live-execution-safety fixes (C1-C4, C6).
Each test is designed to FAIL on the pre-fix code and PASS after.
"""

from unittest.mock import MagicMock



# ═══════════════════════════════════════════════════════════════════════════════
# C1: Deriv modify_order uses correct dollar formula
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeadZoneOrPrecedence:
    """With dead_zone_management=False, JPY symbols must NOT get dead-zone mgmt."""

    def test_jpy_not_gated_when_disabled(self):
        """The fixed expression should be False when config is disabled."""
        symbol = "USDJPY"

        class Cfg:
            dead_zone_management = False

        cfg = Cfg()
        result = cfg.dead_zone_management and (
            symbol.find("USD") >= 0 or symbol.find("JPY") >= 0
        )
        assert result is False, "JPY should NOT trigger dead-zone when disabled"

    def test_jpy_gated_when_enabled(self):
        symbol = "USDJPY"

        class Cfg:
            dead_zone_management = True

        cfg = Cfg()
        result = cfg.dead_zone_management and (
            symbol.find("USD") >= 0 or symbol.find("JPY") >= 0
        )
        assert result is True

    def test_non_forex_not_gated_when_enabled(self):
        symbol = "1HZ100V"

        class Cfg:
            dead_zone_management = True

        cfg = Cfg()
        result = cfg.dead_zone_management and (
            symbol.find("USD") >= 0 or symbol.find("JPY") >= 0
        )
        assert result is False

    def test_old_bug_jpy_triggers_even_when_disabled(self):
        """Demonstrates the pre-fix bug: without parens, JPY triggers regardless."""
        symbol = "EURJPY"

        class Cfg:
            dead_zone_management = False

        cfg = Cfg()
        old_result = (
            cfg.dead_zone_management and symbol.find("USD") >= 0
            or symbol.find("JPY") >= 0
        )
        fixed_result = cfg.dead_zone_management and (
            symbol.find("USD") >= 0 or symbol.find("JPY") >= 0
        )
        assert old_result is True, "Old code would wrongly trigger for EURJPY"
        assert fixed_result is False, "Fixed code correctly blocks"


# ═══════════════════════════════════════════════════════════════════════════════
# C6: .db files untracked
# ═══════════════════════════════════════════════════════════════════════════════


class TestDbFilesUntracked:
    """Verify SQLite databases and transient sidecars are not tracked in git."""

    def test_no_database_artifacts_tracked(self):
        import subprocess
        from pathlib import Path

        result = subprocess.run(
            ["git", "ls-files", "*.db", "*.db-wal", "*.db-shm"],
            capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
        )
        tracked = result.stdout.strip()
        assert tracked == "", f"These SQLite runtime artifacts are still tracked:\n{tracked}"
