"""
APEX TRADER — Data Auto-Sync & Clean-Start Tests

Covers:
- scripts.backup_data.sync_data_repo: in-place commit/push to main, CSV
  exclusion, idempotent "no changes", non-git guard.
- DailyMaintenance auto-sync wiring (run() calls the sync, builds the commit
  message, stays non-fatal).
- platforms.clean_start: fetch+hard-reset sync + schema-version-gated purge.
"""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from platforms.clean_start import (
    discard_corrupt_event_store,
    purge_stale_learned_data,
    run_startup_clean_start,
    sync_clean_state_from_remote,
)
from platforms.maintenance import DailyMaintenance


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.io"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)


# ── sync_data_repo ────────────────────────────────────────────────────────


class TestSyncDataRepo:
    def test_no_data_directory(self):
        from scripts import backup_data
        with patch.object(backup_data, "_DATA_DIR", Path("/nope/does/not/exist")):
            assert backup_data.sync_data_repo(commit_message="x") == "no data directory"

    def test_not_a_git_repo(self):
        from scripts import backup_data
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(backup_data, "_DATA_DIR", Path(tmp)):
                assert (
                    backup_data.sync_data_repo(commit_message="x")
                    == "data dir is not a git repo"
                )

    def test_commits_and_excludes_csv(self):
        from scripts import backup_data
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _init_repo(d)
            (d / "apex_positions.db").write_text("rows")
            (d / "market.csv").write_text("a,b,c")

            with patch.object(backup_data, "_DATA_DIR", d):
                # push=False so we don't need a remote in the test sandbox
                res = backup_data.sync_data_repo(
                    commit_message="auto-sync test", push=False,
                )
            assert "committed" in res

            # The DB is committed; the CSV is excluded and remains untracked.
            tracked = subprocess.check_output(
                ["git", "-C", str(d), "ls-files"], text=True,
            )
            assert "apex_positions.db" in tracked
            assert "market.csv" not in tracked

    def test_idempotent_no_changes(self):
        from scripts import backup_data
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _init_repo(d)
            (d / "f.db").write_text("x")
            subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)

            with patch.object(backup_data, "_DATA_DIR", d):
                assert (
                    backup_data.sync_data_repo(commit_message="x", push=False)
                    == "no changes"
                )


# ── DailyMaintenance auto-sync wiring ──────────────────────────────────────


class TestMaintenanceAutoSync:
    def test_sync_disabled_by_default(self):
        # Default ctor keeps auto-sync OFF so existing callers/tests are inert.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(data_dir=str(data_dir), log_dir=str(Path(tmp) / "logs"))
            result = m.run()
            assert "data_sync" not in result

    def test_sync_called_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(
                data_dir=str(data_dir),
                log_dir=str(Path(tmp) / "logs"),
                auto_sync_data_repo=True,
                sync_orphan_branch=False,
            )
            with patch(
                "scripts.backup_data.sync_data_repo", return_value="synced → origin/main",
            ) as mock_sync:
                result = m.run(event_count=42)
            assert result["data_sync"] == "synced → origin/main"
            # Commit message carries metadata.
            kwargs = mock_sync.call_args.kwargs
            assert "events: 42" in kwargs["commit_message"]
            assert kwargs["branch"] == "main"

    def test_sync_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(
                data_dir=str(data_dir),
                log_dir=str(Path(tmp) / "logs"),
                auto_sync_data_repo=True,
                sync_orphan_branch=False,
            )
            with patch(
                "scripts.backup_data.sync_data_repo", side_effect=RuntimeError("boom"),
            ):
                result = m.run()
            assert result["data_sync"].startswith("error:")
            # Other maintenance steps still ran.
            assert "db_backup" in result

    def test_orphan_branch_sync_called(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(
                data_dir=str(data_dir),
                log_dir=str(Path(tmp) / "logs"),
                auto_sync_data_repo=True,
                sync_orphan_branch=True,
            )
            with patch(
                "scripts.backup_data.sync_data_repo", return_value="no changes",
            ), patch(
                "scripts.backup_data.run_backup", return_value="backed up 3 files",
            ) as mock_orphan:
                result = m.run()
            assert result["orphan_sync"] == "backed up 3 files"
            mock_orphan.assert_called_once()


# ── DailyMaintenance interval-gated sync ───────────────────────────────────


class TestMaintenanceIntervalSync:
    def test_should_sync_disabled_when_auto_sync_off(self):
        m = DailyMaintenance(auto_sync_data_repo=False)
        assert m.should_sync() is False

    def test_should_sync_first_time_true_then_waits_interval(self):
        m = DailyMaintenance(auto_sync_data_repo=True, sync_interval_hours=1.0)
        # Never synced → due immediately.
        assert m.should_sync(now=1000.0) is True
        with patch("scripts.backup_data.sync_data_repo", return_value="synced → origin/main"):
            res = m.sync_data(event_count=7, now=1000.0)
        assert res == "synced → origin/main"
        # Within the interval → not due.
        assert m.should_sync(now=1000.0 + 1800.0) is False
        # After the interval → due again.
        assert m.should_sync(now=1000.0 + 3600.0) is True

    def test_zero_interval_disables_interval_sync(self):
        m = DailyMaintenance(auto_sync_data_repo=True, sync_interval_hours=0)
        assert m.should_sync(now=1000.0) is False

    def test_daily_run_records_sync_time(self):
        # A daily run() push counts as a sync, so the interval gate waits after it.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            m = DailyMaintenance(
                data_dir=str(data_dir),
                log_dir=str(Path(tmp) / "logs"),
                auto_sync_data_repo=True,
                sync_orphan_branch=False,
                sync_interval_hours=1.0,
            )
            with patch(
                "scripts.backup_data.sync_data_repo", return_value="synced → origin/main",
            ):
                m.run()
            # run() just pushed → interval gate should not fire again immediately.
            assert m.should_sync() is False

    def test_sync_data_disabled_returns_disabled(self):
        m = DailyMaintenance(auto_sync_data_repo=False)
        assert m.sync_data() == "disabled"


# ── clean_start ─────────────────────────────────────────────────────────────


class TestCleanStart:
    def test_purge_removes_learned_artifacts_on_schema_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            version_file = Path(tmp) / ".schema_version"
            d.mkdir()
            version_file = Path(tmp) / ".local_schema_version"
            version_file.write_text("1\n")
            (d / "scoring_weights.json").write_text("{}")
            (d / "zone_edge.json").write_text("{}")
            (d / "apex_positions.db").write_text("live")  # must be preserved
            version_file.write_text("1")

            res = purge_stale_learned_data(
                str(d),
                schema_version="2",
                local_schema_version_file=version_file,
            )
            assert "purged" in res
            assert not (d / "scoring_weights.json").exists()
            assert not (d / "zone_edge.json").exists()
            assert (d / "apex_positions.db").exists()  # operational store kept
            assert version_file.read_text().strip() == "2"

    def test_purge_skips_when_schema_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            version_file = Path(tmp) / ".schema_version"
            d.mkdir()
            (d / "scoring_weights.json").write_text("{}")
            version_file.write_text("2")
            assert (
                purge_stale_learned_data(
                    str(d),
                    schema_version="2",
                    local_schema_version_file=version_file,
                )
                == "already clean"
            )
            assert (d / "scoring_weights.json").exists()

    def test_sync_uses_fetch_then_hard_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            commands: list[list[str]] = []

            def _fake_run(cmd, capture_output=True, text=True):
                commands.append(list(cmd))
                if cmd[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
                if cmd[-2:] == ["fetch", "origin"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                if cmd[-3:] == ["reset", "--hard", "origin/main"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

            with patch("platforms.clean_start.subprocess.run", side_effect=_fake_run):
                res = sync_clean_state_from_remote(str(d), branch="main")

            assert res == "reset to origin/main"
            assert commands[1][-2:] == ["fetch", "origin"]
            assert commands[2][-3:] == ["reset", "--hard", "origin/main"]

    def test_sync_falls_back_to_master_when_main_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            commands: list[list[str]] = []

            def _fake_run(cmd, capture_output=True, text=True):
                commands.append(list(cmd))
                if cmd[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
                if cmd[-2:] == ["fetch", "origin"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                if cmd[-3:] == ["reset", "--hard", "origin/main"]:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unknown ref")
                if cmd[-3:] == ["reset", "--hard", "origin/master"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

            with patch("platforms.clean_start.subprocess.run", side_effect=_fake_run):
                res = sync_clean_state_from_remote(str(d), branch="main")

            assert res == "reset to origin/master (fallback)"
            assert commands[2][-3:] == ["reset", "--hard", "origin/main"]
            assert commands[3][-3:] == ["reset", "--hard", "origin/master"]

    def test_pull_non_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert (
                sync_clean_state_from_remote(tmp) == "data dir is not a git repo"
            )

    def test_pull_missing_dir(self):
        assert (
            sync_clean_state_from_remote("/nope/missing/dir")
            == "no data directory"
        )

    def test_sync_uses_fetch_then_hard_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            calls = []

            def _fake_run(cmd, capture_output, text):  # noqa: ANN001
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch("platforms.clean_start._is_git_repo", return_value=True), patch(
                "platforms.clean_start.subprocess.run", side_effect=_fake_run,
            ):
                result = sync_clean_state_from_remote(str(d), branch="main")

            assert result == "synced remote state (main)"
            assert calls[0][-2:] == ["fetch", "origin"]
            assert calls[1][-3:] == ["reset", "--hard", "origin/main"]

    def test_sync_falls_back_to_master_when_main_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)

            responses = [
                subprocess.CompletedProcess(["git"], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    ["git"],
                    1,
                    stdout="",
                    stderr="fatal: ambiguous argument 'origin/main'",
                ),
                subprocess.CompletedProcess(["git"], 0, stdout="", stderr=""),
            ]

            with patch("platforms.clean_start._is_git_repo", return_value=True), patch(
                "platforms.clean_start.subprocess.run", side_effect=responses,
            ):
                result = sync_clean_state_from_remote(str(d), branch="main")

            assert result == "synced remote state (master)"

    def test_startup_runner_is_context_free(self):
        with patch(
            "platforms.clean_start.sync_clean_state_from_remote",
            return_value="synced remote state (main)",
        ) as mock_sync, patch(
            "platforms.clean_start.discard_corrupt_event_store",
            return_value="event-store healthy",
        ) as mock_discard, patch(
            "platforms.clean_start.purge_stale_learned_data",
            return_value="already clean",
        ) as mock_purge:
            result = run_startup_clean_start(
                data_dir="data",
                branch="main",
                schema_version="2",
                local_schema_version_file=".local_schema_version",
            )

        assert result == (
            "synced remote state (main)",
            "event-store healthy",
            "already clean",
        )
        mock_sync.assert_called_once_with(data_dir="data", branch="main")
        mock_discard.assert_called_once_with(data_dir="data")
        mock_purge.assert_called_once_with(
            data_dir="data",
            schema_version="2",
            local_schema_version_file=".local_schema_version",
        )


class TestStartupPurgeToggle:
    """The learned-data purge is gated two ways: the ``startup_purge_enabled``
    toggle AND a schema-version mismatch. The one-time Opportunistic
    Intelligence rewire migration purge has already run, so the toggle is now
    disabled by default to prevent any further wiping of learned state on
    restart."""

    def test_startup_purge_disabled_by_default(self):
        from config import DataBackupConfig

        # Disabled: the one-time migration purge has already run. Learned state
        # must persist across restarts from now on.
        assert DataBackupConfig().startup_purge_enabled is False

    def test_clean_start_on_first_boot_enabled_by_default(self):
        from config import DataBackupConfig

        # Declared field (not just a dynamic attr) so the single-user clean-start
        # contract is explicit and multi-tenant can force it off.
        assert DataBackupConfig().clean_start_on_first_boot is True


# ── discard_corrupt_event_store ───────────────────────────────────────────


def _make_healthy_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def _make_corrupt_db(path: Path) -> None:
    """Write a malformed SQLite image (valid header, scrambled pages) so
    PRAGMA integrity_check fails, mirroring 'database disk image is malformed'."""
    _make_healthy_db(path)
    data = bytearray(path.read_bytes())
    for i in range(100, min(len(data), 4000)):
        data[i] = (data[i] + 137) & 0xFF
    path.write_bytes(data)


class TestDiscardCorruptEventStore:
    """The event store is operational, machine-local state. A corrupt copy
    restored by the data-junction reset must be dropped before the store opens
    so it starts fresh — without the endless restore/rotate loop."""

    def test_corrupt_db_and_sidecars_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            db = d / "apex_events.db"
            _make_corrupt_db(db)
            (d / "apex_events.db-wal").write_bytes(b"wal")
            (d / "apex_events.db-shm").write_bytes(b"shm")

            res = discard_corrupt_event_store(str(d))

            assert not db.exists()
            assert not (d / "apex_events.db-wal").exists()
            assert not (d / "apex_events.db-shm").exists()
            assert "discarded corrupt event-store" in res

    def test_healthy_db_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            db = d / "apex_events.db"
            _make_healthy_db(db)
            before = db.read_bytes()

            res = discard_corrupt_event_store(str(d))

            assert db.exists()
            assert db.read_bytes() == before
            assert res == "event-store healthy"

    def test_missing_db_is_noop_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)

            res = discard_corrupt_event_store(str(d))

            assert not (d / "apex_events.db").exists()
            assert res == "event-store healthy"

    def test_no_data_directory(self):
        assert (
            discard_corrupt_event_store("/nope/missing/dir") == "no data directory"
        )

    def test_old_corrupt_rotations_pruned_keeping_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # Five forensic rotations with increasing mtimes; newest 3 kept.
            rotations = []
            for i in range(5):
                p = d / f"apex_events.db.corrupt.2026010{i}"
                p.write_bytes(b"x")
                import os

                os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
                rotations.append(p)

            res = discard_corrupt_event_store(str(d))

            survivors = sorted(d.glob("apex_events.db.corrupt.*"))
            assert len(survivors) == 3
            # The three highest-numbered (newest mtime) survive.
            assert rotations[0] not in survivors
            assert rotations[1] not in survivors
            assert rotations[4] in survivors
            assert "pruned 2 rotation(s)" in res
