"""
APEX TRADER — Daily Self-Maintenance
Keeps the system clean without human intervention.
Runs once per day: backs up the database, rotates old logs, cleans stale files.
"""

import shutil
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


class DailyMaintenance:
    """Automated daily housekeeping — backups, log rotation, stale cleanup."""

    def __init__(
        self,
        data_dir: str = "data",
        log_dir: str = "logs",
        max_log_days: int = 30,
        max_backup_days: int = 14,
    ):
        self._data_dir = Path(data_dir)
        self._log_dir = Path(log_dir)
        self._max_log_days = max_log_days
        self._max_backup_days = max_backup_days
        self._last_maintenance_day: Optional[str] = None

    def should_run(self, timestamp: Optional[datetime] = None) -> bool:
        day_key = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        return day_key != self._last_maintenance_day

    def run(self, timestamp: Optional[datetime] = None) -> dict:
        """Run all maintenance tasks. Returns summary dict."""
        day_key = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        results: dict = {}

        results["db_backup"] = self._backup_database()
        results["logs_cleaned"] = self._rotate_logs()
        results["stale_cleaned"] = self._clean_stale_data()

        self._last_maintenance_day = day_key
        return results

    def _backup_database(self) -> str:
        """Copy positions.db to data/backups/positions_YYYY-MM-DD.db"""
        db_path = self._data_dir / "apex_positions.db"
        if not db_path.exists():
            return "no_database"

        backup_dir = self._data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        backup_path = backup_dir / f"positions_{today}.db"

        try:
            shutil.copy2(str(db_path), str(backup_path))
        except Exception as exc:
            logger.warning("Database backup failed: {}", exc)
            return f"error: {exc}"

        deleted = self._cleanup_old_files(backup_dir, "positions_*.db", self._max_backup_days)
        return f"backed_up:{backup_path.name},old_deleted:{deleted}"

    def _rotate_logs(self) -> int:
        """Delete log files older than max_log_days. Return count deleted."""
        if not self._log_dir.exists():
            return 0
        return self._cleanup_old_files(self._log_dir, "*.log", self._max_log_days)

    def _clean_stale_data(self) -> int:
        """Clean up temp/stale files. Return count."""
        count = 0
        for pattern in ("*.tmp", "*.pyc"):
            for f in self._data_dir.glob(pattern):
                try:
                    f.unlink()
                    count += 1
                except Exception as exc:
                    logger.debug("[maintenance] temp file deletion failed: {}", exc)
                    pass
        for cache_dir in self._data_dir.rglob("__pycache__"):
            try:
                shutil.rmtree(str(cache_dir))
                count += 1
            except Exception as exc:
                logger.debug("[maintenance] pycache removal failed: {}", exc)
                pass
        return count

    def _cleanup_old_files(self, directory: Path, pattern: str, max_age_days: int) -> int:
        """Delete files matching pattern that are older than max_age_days."""
        cutoff = _time.time() - (max_age_days * 86400)
        deleted = 0
        for f in directory.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
            except Exception as exc:
                logger.debug("[maintenance] old file cleanup failed: {}", exc)
                pass
        return deleted
