"""
APEX TRADER — Daily Self-Maintenance
Keeps the system clean without human intervention.
Runs once per day: backs up the database, rotates old logs, cleans stale files.
"""

import shutil
import sqlite3
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
        auto_sync_data_repo: bool = False,
        sync_branch: str = "main",
        sync_orphan_branch: bool = False,
        sync_exclude_patterns: Optional[list] = None,
    ):
        self._data_dir = Path(data_dir)
        self._log_dir = Path(log_dir)
        self._max_log_days = max_log_days
        self._max_backup_days = max_backup_days
        self._auto_sync = auto_sync_data_repo
        self._sync_branch = sync_branch
        self._sync_orphan_branch = sync_orphan_branch
        self._sync_exclude_patterns = sync_exclude_patterns
        self._last_maintenance_day: Optional[str] = None

    def should_run(self, timestamp: Optional[datetime] = None) -> bool:
        day_key = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        return day_key != self._last_maintenance_day

    def run(
        self,
        timestamp: Optional[datetime] = None,
        *,
        event_count: Optional[int] = None,
    ) -> dict:
        """Run all maintenance tasks. Returns summary dict."""
        day_key = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        results: dict = {}

        results["db_backup"] = self._backup_database()
        results["logs_cleaned"] = self._rotate_logs()
        results["stale_cleaned"] = self._clean_stale_data()

        # Push the local data junction to its GitHub remote *after* the local
        # backup so the freshest snapshot (including today's backup file) is
        # captured. Non-blocking — never let a git failure abort maintenance.
        if self._auto_sync:
            results["data_sync"] = self._sync_data_repo(event_count=event_count)
            if self._sync_orphan_branch:
                results["orphan_sync"] = self._sync_orphan_branch_backup()

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
            # Use SQLite's online backup API instead of a raw file copy: it
            # takes a consistent snapshot even while the DB is being written
            # (WAL mode), avoiding the torn/corrupt copy a shutil.copy2 can
            # produce mid-write.
            src = sqlite3.connect(str(db_path), timeout=30)
            try:
                dst = sqlite3.connect(str(backup_path), timeout=30)
                try:
                    with dst:
                        src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except Exception as exc:
            logger.warning("Database backup failed: {}", exc)
            return f"error: {exc}"

        deleted = self._cleanup_old_files(backup_dir, "positions_*.db", self._max_backup_days)
        return f"backed_up:{backup_path.name},old_deleted:{deleted}"

    def _count_positions(self) -> Optional[int]:
        """Best-effort row count of the positions DB for the commit message."""
        db_path = self._data_dir / "apex_positions.db"
        if not db_path.exists():
            return None
        try:
            conn = sqlite3.connect(str(db_path), timeout=10)
            try:
                cur = conn.execute("SELECT COUNT(*) FROM positions")
                return int(cur.fetchone()[0])
            finally:
                conn.close()
        except Exception:
            return None

    def _sync_data_repo(self, event_count: Optional[int] = None) -> str:
        """Commit + push the data junction to its GitHub main branch.

        Best-effort: any failure is logged as a warning and reported in the
        summary string without raising, so the trading loop is never affected.
        """
        try:
            from scripts.backup_data import sync_data_repo
        except Exception as exc:
            logger.warning("[maintenance] data-sync unavailable: {}", exc)
            return f"unavailable: {exc}"

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        parts = [f"auto-sync: {today}"]
        pos = self._count_positions()
        parts.append(f"positions: {pos if pos is not None else '?'}")
        if event_count is not None:
            parts.append(f"events: {event_count}")
        message = " | ".join(parts)

        try:
            result = sync_data_repo(
                commit_message=message,
                exclude_patterns=self._sync_exclude_patterns,
                branch=self._sync_branch,
            )
            logger.info("[maintenance] data-repo sync — {}", result)
            return result
        except Exception as exc:
            logger.warning("[maintenance] data-repo sync failed: {}", exc)
            return f"error: {exc}"

    def _sync_orphan_branch_backup(self) -> str:
        """Refresh the ``data-backup`` orphan branch with the current state.

        Keeps ``scripts/restore_data.py`` pointed at clean, current data instead
        of stale pre-migration snapshots. Best-effort, never raises.
        """
        try:
            from scripts.backup_data import run_backup
        except Exception as exc:
            logger.warning("[maintenance] orphan-branch backup unavailable: {}", exc)
            return f"unavailable: {exc}"
        try:
            result = run_backup(
                exclude_patterns=self._sync_exclude_patterns,
            )
            logger.info("[maintenance] orphan-branch backup — {}", result)
            return result
        except Exception as exc:
            logger.warning("[maintenance] orphan-branch backup failed: {}", exc)
            return f"error: {exc}"

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
