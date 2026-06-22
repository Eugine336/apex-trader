"""
APEX TRADER — Data Auto-Sync & Clean-Start Tests

Covers:
- scripts.backup_data.sync_data_repo: in-place commit/push to main, CSV
  exclusion, idempotent "no changes", non-git guard.
- DailyMaintenance auto-sync wiring (run() calls the sync, builds the commit
  message, stays non-fatal).
- platforms.clean_start: git-pull sync + one-time sentinel-guarded purge.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from platforms.clean_start import (
    purge_stale_learned_data,
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


# ── clean_start ─────────────────────────────────────────────────────────────


class TestCleanStart:
    def test_purge_removes_learned_artifacts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            (d / "scoring_weights.json").write_text("{}")
            (d / "zone_edge.json").write_text("{}")
            (d / "apex_positions.db").write_text("live")  # must be preserved

            res = purge_stale_learned_data(str(d))
            assert "purged" in res
            assert not (d / "scoring_weights.json").exists()
            assert not (d / "zone_edge.json").exists()
            assert (d / "apex_positions.db").exists()  # operational store kept
            assert (d / ".clean_start_done").exists()

            # Second run is a no-op even if a fresh learned file reappears.
            (d / "scoring_weights.json").write_text("{}")
            assert purge_stale_learned_data(str(d)) == "already clean"
            assert (d / "scoring_weights.json").exists()

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
