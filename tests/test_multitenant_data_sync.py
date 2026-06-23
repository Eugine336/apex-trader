"""
APEX TRADER — Multi-Tenant Data Sync Tests

Covers the per-user → shared-junction sync added for multi-tenant mode:
- scripts.backup_data.mirror_instances: per-user namespacing, CSV exclusion,
  no cross-user overwrite, ignores non-user dirs.
- scripts.backup_data.sync_instances_to_data_repo: mirror + commit, idempotent
  "no changes", graceful non-git junction handling.
- scripts.backup_data.sync_data_repo(data_dir=...): explicit work-tree override.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from scripts import backup_data


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.io"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)


def _make_instances(root: Path, user_ids) -> Path:
    inst = root / "instances"
    for uid in user_ids:
        d = inst / f"user_{uid}" / "data"
        d.mkdir(parents=True)
        (d / "trade_journal.db").write_text(f"db-{uid}")
        (d / "outcome_feedback.jsonl").write_text(f'{{"u":{uid}}}\n')
        (d / "market.csv").write_text("a,b,c")  # must be excluded
    # a non-user dir that must be ignored
    (inst / "scratch").mkdir(parents=True, exist_ok=True)
    return inst


class TestMirrorInstances:
    def test_namespaces_per_user_and_excludes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst = _make_instances(root, (1, 2))
            dest = root / "junction"
            dest.mkdir()

            users, copied, skipped = backup_data.mirror_instances(inst, dest)

            assert users == 2
            assert copied == 4  # 2 dbs + 2 jsonl
            assert skipped == 2  # 2 csv
            assert (dest / "instances/user_1/data/trade_journal.db").read_text() == "db-1"
            assert (dest / "instances/user_2/data/trade_journal.db").read_text() == "db-2"
            # CSV excluded
            assert not (dest / "instances/user_1/data/market.csv").exists()
            # non-user dir ignored
            assert not (dest / "instances/scratch").exists()

    def test_no_cross_user_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst = _make_instances(root, (1, 2, 3))
            dest = root / "junction"
            dest.mkdir()
            backup_data.mirror_instances(inst, dest)
            # Each user's identical filename stays isolated under its own namespace.
            contents = {
                p.parent.parent.name: p.read_text()
                for p in dest.glob("instances/user_*/data/trade_journal.db")
            }
            assert contents == {"user_1": "db-1", "user_2": "db-2", "user_3": "db-3"}

    def test_missing_instances_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "junction"
            dest.mkdir()
            assert backup_data.mirror_instances(Path(tmp) / "nope", dest) == (0, 0, 0)


class TestSyncInstancesToDataRepo:
    def test_commits_namespaced_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst = _make_instances(root, (1, 2))
            junction = root / "junction"
            junction.mkdir()
            _init_repo(junction)

            result = backup_data.sync_instances_to_data_repo(
                instances_dir=inst,
                junction_dir=junction,
                commit_message="initial",
                push=False,  # no remote in sandbox
            )
            assert result.startswith("committed (push skipped)")
            assert "users=2" in result

            log = subprocess.check_output(
                ["git", "-C", str(junction), "log", "--name-only", "--oneline"]
            ).decode()
            assert "instances/user_1/data/trade_journal.db" in log
            assert "instances/user_2/data/trade_journal.db" in log
            assert "market.csv" not in log  # excluded

    def test_idempotent_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst = _make_instances(root, (1,))
            junction = root / "junction"
            junction.mkdir()
            _init_repo(junction)

            backup_data.sync_instances_to_data_repo(
                instances_dir=inst, junction_dir=junction,
                commit_message="first", push=False,
            )
            again = backup_data.sync_instances_to_data_repo(
                instances_dir=inst, junction_dir=junction,
                commit_message="second", push=False,
            )
            assert again.startswith("no changes")

    def test_non_git_junction_is_soft_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst = _make_instances(root, (1,))
            junction = root / "junction"
            junction.mkdir()  # NOT a git repo
            result = backup_data.sync_instances_to_data_repo(
                instances_dir=inst, junction_dir=junction,
                commit_message="x", push=False,
            )
            assert result.startswith("data dir is not a git repo")

    def test_missing_junction(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _make_instances(Path(tmp), (1,))
            result = backup_data.sync_instances_to_data_repo(
                instances_dir=inst, junction_dir=Path(tmp) / "nope",
                commit_message="x", push=False,
            )
            assert result == "no data junction"

    def test_no_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "instances").mkdir()
            junction = root / "junction"
            junction.mkdir()
            _init_repo(junction)
            result = backup_data.sync_instances_to_data_repo(
                instances_dir=root / "instances", junction_dir=junction,
                commit_message="x", push=False,
            )
            assert result == "no instances"


class TestSyncDataRepoDataDirOverride:
    def test_explicit_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "repo"
            d.mkdir()
            _init_repo(d)
            (d / "x.db").write_text("rows")
            result = backup_data.sync_data_repo(
                commit_message="x", push=False, data_dir=d,
            )
            assert result.startswith("committed (push skipped)")
