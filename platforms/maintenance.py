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
        sync_interval_hours: float = 1.0,
        vacuum_size_threshold_mb: float = 50.0,
        compact_repo_size_mb: float = 500.0,
        max_rss_mb: float = 2048.0,
    ):
        self._data_dir = Path(data_dir)
        self._log_dir = Path(log_dir)
        self._max_log_days = max_log_days
        self._max_backup_days = max_backup_days
        self._auto_sync = auto_sync_data_repo
        self._sync_branch = sync_branch
        self._sync_orphan_branch = sync_orphan_branch
        self._sync_exclude_patterns = sync_exclude_patterns
        self._sync_interval_hours = sync_interval_hours
        # Reclaim disk freed by DELETE/prune: VACUUM a DB only once it grows past
        # this size (VACUUM is heavy — never run it on small files).
        self._vacuum_size_threshold_mb = vacuum_size_threshold_mb
        # Squash the data-repo git history once the pack exceeds this size, so a
        # year of hourly binary-snapshot commits never makes the repo unclonable.
        self._compact_repo_size_mb = compact_repo_size_mb
        # Memory-pressure self-restart ceiling: above this RSS the daily run
        # requests a graceful restart (writes a marker — never kills positions).
        self._max_rss_mb = max_rss_mb
        self._last_maintenance_day: Optional[str] = None
        self._last_sync_monotonic: Optional[float] = None

    def should_run(self, timestamp: Optional[datetime] = None) -> bool:
        day_key = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        return day_key != self._last_maintenance_day

    def should_sync(self, *, now: Optional[float] = None) -> bool:
        """True when the data-sync interval has elapsed since the last push.

        Independent of the once-per-day ``should_run`` gate. The daily gate
        drives heavy housekeeping (db backup, log rotation, daily risk resets)
        that must only fire on a day-roll, but the data-junction push needs to
        run on its own (sub-daily) cadence: a restart hard-resets the junction
        to the remote, so any intra-day data not yet pushed is discarded. Pushing
        on ``sync_interval_hours`` shrinks that loss window from a full day to the
        interval (default 1h).
        """
        if not self._auto_sync or self._sync_interval_hours <= 0:
            return False
        mono = now if now is not None else _time.monotonic()
        last = self._last_sync_monotonic
        if last is None:
            return True
        return (mono - last) >= (self._sync_interval_hours * 3600.0)

    def sync_data(
        self,
        *,
        event_count: Optional[int] = None,
        now: Optional[float] = None,
    ) -> str:
        """Interval data-junction push (no daily housekeeping). Best-effort.

        Records the push time so :meth:`should_sync` waits a full interval
        before the next push. The heavier orphan-branch snapshot stays on the
        daily cadence in :meth:`run`.
        """
        if not self._auto_sync:
            return "disabled"
        result = self._sync_data_repo(event_count=event_count)
        self._last_sync_monotonic = now if now is not None else _time.monotonic()
        return result

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
        results["event_store_pruned"] = self._prune_event_store()
        # Reclaim disk space freed by the prune above (DELETE leaves pages in the
        # file; VACUUM rebuilds it). Runs after the prune so the freed pages are
        # actually reclaimable this pass.
        results["databases_vacuumed"] = self._vacuum_databases()
        # gc sweep + RSS check. Writes a restart marker (never exits) if the
        # process has crept past the memory ceiling over a long uptime.
        results["memory"] = self._memory_maintenance()

        # Push the local data junction to its GitHub remote *after* the local
        # backup so the freshest snapshot (including today's backup file) is
        # captured. Non-blocking — never let a git failure abort maintenance.
        if self._auto_sync:
            results["data_sync"] = self._sync_data_repo(event_count=event_count)
            # Mark the push time so the interval gate (`should_sync`) waits a
            # full interval before the next sub-daily push.
            self._last_sync_monotonic = _time.monotonic()
            if self._sync_orphan_branch:
                results["orphan_sync"] = self._sync_orphan_branch_backup()
            # Compact the data-repo history after the push so a year of hourly
            # binary commits never bloats the repo past a clonable size.
            results["repo_compaction"] = self._compact_repo_history()

        self._last_maintenance_day = day_key
        return results

    def _prune_event_store(self) -> int:
        """Trim the append-only event store to its retention limits.

        ``EventStore.prune`` enforces the configured max-age / max-rows policy
        but was never invoked anywhere, so ``apex_events.db`` grew without bound
        (observed at 200+ MB in production). Running it on the daily cadence
        keeps the store bounded; failures never abort maintenance.
        """
        try:
            from persistence.event_store import get_event_store

            deleted = int(get_event_store().prune())
            if deleted:
                logger.info("[maintenance] event store pruned — {} rows removed", deleted)
            return deleted
        except Exception as exc:
            logger.warning("[maintenance] event-store prune failed: {}", exc)
            return 0

    def _vacuum_databases(self) -> dict:
        """Reclaim disk space across every SQLite store on the daily cadence.

        A plain ``DELETE`` (as ``EventStore.prune`` and the learners do) frees
        pages for reuse but never shrinks the file on disk, so over months the
        ``.db`` files grow monotonically even as their row counts stay bounded.
        This checkpoints the WAL and, for any file already past
        ``vacuum_size_threshold_mb``, runs ``VACUUM`` to rebuild it and return
        the freed space to the OS. VACUUM is heavy, so small files are skipped.

        Never raises — every per-database failure is logged and skipped so a
        single bad file can't abort maintenance. Returns a summary dict.
        """
        summary: dict = {"event_store": "skipped", "vacuumed": 0, "checked": 0}

        # The event store owns its own connection + lock; let it vacuum itself.
        try:
            from persistence.event_store import get_event_store

            get_event_store().vacuum()
            summary["event_store"] = "ok"
        except Exception as exc:
            logger.warning("[maintenance] event-store vacuum failed: {}", exc)
            summary["event_store"] = f"error: {exc}"

        if not self._data_dir.is_dir():
            return summary

        threshold_bytes = self._vacuum_size_threshold_mb * 1024 * 1024
        # The event store has already vacuumed itself above; skip it here so we
        # never open a second connection to it mid-vacuum.
        event_store_names = {"apex_events.db"}
        for db_path in sorted(self._data_dir.glob("*.db")):
            if db_path.name in event_store_names:
                continue
            summary["checked"] += 1
            try:
                size_before = db_path.stat().st_size
            except OSError as exc:
                logger.debug("[maintenance] stat failed for {}: {}", db_path.name, exc)
                continue
            try:
                conn = sqlite3.connect(str(db_path), timeout=30)
                try:
                    # Flush WAL first so VACUUM sees the latest committed state.
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    if size_before <= threshold_bytes:
                        # Small file — checkpoint is enough, VACUUM not worth it.
                        continue
                    conn.execute("VACUUM")
                finally:
                    conn.close()
            except Exception as exc:
                logger.warning(
                    "[maintenance] vacuum skipped for {}: {}", db_path.name, exc
                )
                continue
            try:
                size_after = db_path.stat().st_size
            except OSError:
                size_after = size_before
            summary["vacuumed"] += 1
            logger.info(
                "[maintenance] VACUUM {} — {:.1f} MB → {:.1f} MB",
                db_path.name,
                size_before / (1024 * 1024),
                size_after / (1024 * 1024),
            )
        return summary

    def _compact_repo_history(self) -> str:
        """Squash the data-repo git history once it grows too large.

        Every auto-sync commits full binary DB snapshots, so a year of hourly
        commits accumulates thousands of binary diffs that make ``git clone``
        slow and the on-disk repo huge. This delegates to
        :func:`scripts.backup_data.compact_repo_history`, which squashes history
        into a single commit + force-pushes once the pack exceeds the configured
        size. Best-effort — never raises, returns a status string.
        """
        try:
            from scripts.backup_data import compact_repo_history
        except Exception as exc:
            logger.warning("[maintenance] repo-compaction unavailable: {}", exc)
            return f"unavailable: {exc}"
        try:
            result = compact_repo_history(
                self._data_dir,
                max_repo_size_mb=self._compact_repo_size_mb,
                branch=self._sync_branch,
            )
            logger.info("[maintenance] repo-compaction — {}", result)
            return result
        except Exception as exc:
            logger.warning("[maintenance] repo-compaction failed: {}", exc)
            return f"error: {exc}"

    def _memory_maintenance(self) -> dict:
        """Force a gc sweep and act on memory pressure over a long uptime.

        Python long-running processes accumulate uncollectable cycles and a
        fragmented heap; nothing else triggers a full collection. This runs
        ``gc.collect()`` across all generations, records RSS, and — if RSS has
        crept past ``max_rss_mb`` — writes a ``.restart_requested`` marker and
        logs CRITICAL so the supervisor/operator can recycle the process. It
        deliberately never calls ``sys.exit``/``os.kill``: the system's graceful
        shutdown path must close positions cleanly, so we only signal intent.

        Never raises. Returns ``{"collected": N, "rss_mb": M}``.
        """
        import gc

        from ops.lifecycle import _process_memory_mb

        out: dict = {"collected": 0, "rss_mb": None}
        try:
            out["collected"] = int(gc.collect())
        except Exception as exc:
            logger.warning("[maintenance] gc.collect failed: {}", exc)
        try:
            rss = _process_memory_mb()
            out["rss_mb"] = rss
        except Exception as exc:
            logger.debug("[maintenance] rss read failed: {}", exc)
            rss = None
        logger.info(
            "[maintenance] memory — gc collected {} object(s), rss={}",
            out["collected"],
            f"{rss:.1f} MB" if rss is not None else "n/a",
        )
        if rss is not None and self._max_rss_mb > 0 and rss > self._max_rss_mb:
            out["restart_requested"] = True
            logger.critical(
                "[maintenance] RSS {:.1f} MB exceeds ceiling {:.1f} MB — "
                "requesting graceful restart (marker written; positions are "
                "NOT force-closed)",
                rss,
                self._max_rss_mb,
            )
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                marker = self._data_dir / ".restart_requested"
                ts = datetime.now(timezone.utc).isoformat()
                marker.write_text(
                    f"reason=memory_pressure rss_mb={rss:.1f} "
                    f"ceiling_mb={self._max_rss_mb:.1f} at={ts}\n"
                )
            except Exception as exc:
                logger.warning(
                    "[maintenance] could not write restart marker: {}", exc
                )
        return out

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
