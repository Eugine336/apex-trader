"""
APEX TRADER — Startup Self-Test
Verifies system health before going live.
Fails fast if critical components are broken — don't risk capital on a broken system.
"""

import shutil
import time as _time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    duration_ms: float


class StartupCheck:
    """Runs pre-flight checks before the trading loop starts."""

    def __init__(self, data_dir: str = "data"):
        self._data_dir = Path(data_dir)

    def run_all(self) -> tuple[bool, list[CheckResult]]:
        """Run all startup checks. Returns (all_critical_passed, results)."""
        results: list[CheckResult] = []
        results.append(self._check_imports())
        results.append(self._check_config())
        results.append(self._check_instrument_registry())
        results.append(self._check_database())
        results.append(self._check_disk_space())

        critical = {"imports", "config", "instrument_registry"}
        all_critical_passed = all(
            r.passed for r in results if r.name in critical
        )
        return all_critical_passed, results

    def _check_imports(self) -> CheckResult:
        """Verify all critical modules can be imported."""
        t0 = _time.monotonic()
        missing: list[str] = []
        for mod in [
            "brain", "risk", "trigger", "management",
            "scanner", "platforms", "config", "persistence",
        ]:
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        elapsed = (_time.monotonic() - t0) * 1000

        if missing:
            return CheckResult(
                name="imports",
                passed=False,
                message=f"Failed to import: {', '.join(missing)}",
                duration_ms=elapsed,
            )
        return CheckResult(
            name="imports",
            passed=True,
            message="All critical modules imported",
            duration_ms=elapsed,
        )

    def _check_config(self) -> CheckResult:
        """Verify AppConfig loads without errors."""
        t0 = _time.monotonic()
        try:
            from config import AppConfig
            cfg = AppConfig()
            elapsed = (_time.monotonic() - t0) * 1000
            return CheckResult(
                name="config",
                passed=True,
                message=f"AppConfig loaded — {len(cfg.enabled_pairs)} pairs enabled",
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (_time.monotonic() - t0) * 1000
            return CheckResult(
                name="config",
                passed=False,
                message=f"AppConfig failed: {exc}",
                duration_ms=elapsed,
            )

    def _check_instrument_registry(self) -> CheckResult:
        """Verify INSTRUMENT_REGISTRY has expected instruments."""
        t0 = _time.monotonic()
        try:
            from config import INSTRUMENT_REGISTRY
            count = len(INSTRUMENT_REGISTRY)
            elapsed = (_time.monotonic() - t0) * 1000

            spot_checks = ["EURUSD", "XAUUSD"]
            missing = [s for s in spot_checks if s not in INSTRUMENT_REGISTRY]

            if count < 50:
                return CheckResult(
                    name="instrument_registry",
                    passed=False,
                    message=f"Only {count} instruments (expected 50+)",
                    duration_ms=elapsed,
                )
            if missing:
                return CheckResult(
                    name="instrument_registry",
                    passed=False,
                    message=f"Missing core instruments: {missing}",
                    duration_ms=elapsed,
                )
            return CheckResult(
                name="instrument_registry",
                passed=True,
                message=f"{count} instruments registered",
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (_time.monotonic() - t0) * 1000
            return CheckResult(
                name="instrument_registry",
                passed=False,
                message=f"Registry check failed: {exc}",
                duration_ms=elapsed,
            )

    def _check_database(self) -> CheckResult:
        """Verify positions database is accessible."""
        t0 = _time.monotonic()
        try:
            from persistence.position_store import PositionStore
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
                store = PositionStore(db_path=tmp.name)
                count = store.count()
                store.close()
            elapsed = (_time.monotonic() - t0) * 1000
            return CheckResult(
                name="database",
                passed=True,
                message="SQLite accessible, schema valid",
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (_time.monotonic() - t0) * 1000
            return CheckResult(
                name="database",
                passed=False,
                message=f"Database check failed: {exc}",
                duration_ms=elapsed,
            )

    def _check_disk_space(self) -> CheckResult:
        """Verify at least 100MB free disk space."""
        t0 = _time.monotonic()
        try:
            usage = shutil.disk_usage(str(self._data_dir.parent))
            free_mb = usage.free / (1024 * 1024)
            elapsed = (_time.monotonic() - t0) * 1000
            if free_mb < 100:
                return CheckResult(
                    name="disk_space",
                    passed=False,
                    message=f"Only {free_mb:.0f}MB free (need 100MB+)",
                    duration_ms=elapsed,
                )
            return CheckResult(
                name="disk_space",
                passed=True,
                message=f"{free_mb:.0f}MB free",
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (_time.monotonic() - t0) * 1000
            return CheckResult(
                name="disk_space",
                passed=False,
                message=f"Disk check failed: {exc}",
                duration_ms=elapsed,
            )
