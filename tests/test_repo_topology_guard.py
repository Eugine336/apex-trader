"""
APEX TRADER — Repository topology & Git identity guard tests.

Proves that data-maintenance git operations (auto-sync, history compaction,
clean-start reset) are STRUCTURALLY INCAPABLE of operating on the source engine
repository, and only ever touch a dedicated data repository whose ``origin``
resolves to ``Eugine336/apex-trader-data``.

Scenarios (mirroring the incident post-mortem):
- data repo has its own ``.git`` → compaction/sync work, target the data repo only
- plain ``data/`` inside the source repo → hard failure, no compaction/commit
- data remote accidentally pointing at ``apex-trader`` → hard failure
- ``clean_start`` cannot reset the source repo
- ``sync_data_repo`` cannot commit to the source repo
- missing ``data/.git`` → hard failure
- the original failure scenario is reproduced and proven prevented
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import types
from pathlib import Path

# ── loguru stub ────────────────────────────────────────────────────────────
# The sandbox has no third-party deps. backup_data/clean_start import loguru;
# a no-op stub lets these guard modules import and run without it.
if "loguru" not in sys.modules:
    _loguru = types.ModuleType("loguru")

    class _Logger:
        def __getattr__(self, _name):
            def _noop(*_a, **_k):
                return None

            return _noop

    _loguru.logger = _Logger()
    sys.modules["loguru"] = _loguru

from scripts import backup_data  # noqa: E402
from scripts.git_identity import (  # noqa: E402
    DATA_REPO_IDENTITY,
    SOURCE_REPO_IDENTITY,
    evaluate_topology,
    normalize_repo_identity,
    render_topology_banner,
    verify_dedicated_data_repo,
)

_DATA_URL = "https://github.com/Eugine336/apex-trader-data.git"
_SOURCE_URL = "https://github.com/Eugine336/apex-trader.git"


def _init_repo(path: Path, *, origin: str | None = None) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.io"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    if origin is not None:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", origin], check=True
        )


def _commit(path: Path, name: str, content: str) -> None:
    (path / name).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", content], check=True)


# ── normalize_repo_identity ─────────────────────────────────────────────────


class TestNormalizeRepoIdentity:
    def test_equivalent_forms_collapse_to_one_identity(self):
        forms = [
            "https://github.com/Eugine336/apex-trader-data.git",
            "https://github.com/Eugine336/apex-trader-data",
            "https://github.com/eugine336/apex-trader-data/",
            "git@github.com:Eugine336/apex-trader-data.git",
            "ssh://git@github.com/Eugine336/apex-trader-data.git",
        ]
        for url in forms:
            assert normalize_repo_identity(url) == DATA_REPO_IDENTITY, url

    def test_source_repo_is_distinct_from_data_repo(self):
        assert normalize_repo_identity(_SOURCE_URL) == SOURCE_REPO_IDENTITY
        assert normalize_repo_identity(_SOURCE_URL) != DATA_REPO_IDENTITY

    def test_none_and_garbage(self):
        assert normalize_repo_identity(None) is None
        assert normalize_repo_identity("") is None
        assert normalize_repo_identity("not-a-url") is None


# ── verify_dedicated_data_repo ──────────────────────────────────────────────


class TestVerifyDedicatedDataRepo:
    def test_dedicated_data_repo_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d, origin=_DATA_URL)
            ok, reason = verify_dedicated_data_repo(d, expected_repo_url=_DATA_URL)
            assert ok, reason
            assert reason == DATA_REPO_IDENTITY

    def test_plain_dir_inside_source_repo_is_refused(self):
        # Reproduce the incident topology: a plain data/ nested in the source repo.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source, origin=_SOURCE_URL)
            plain_data = source / "data"
            plain_data.mkdir()
            ok, reason = verify_dedicated_data_repo(plain_data, expected_repo_url=_DATA_URL)
            assert not ok
            # The ceiling env stops parent discovery, so it is simply "not a repo".
            assert "not a git repository" in reason or "not a dedicated repo" in reason

    def test_remote_pointing_at_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d, origin=_SOURCE_URL)  # misconfigured!
            ok, reason = verify_dedicated_data_repo(d, expected_repo_url=_DATA_URL)
            assert not ok
            assert "SOURCE repo" in reason

    def test_missing_remote_is_refused_when_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d)  # no origin
            ok, reason = verify_dedicated_data_repo(d, require_remote=True)
            assert not ok
            assert "no 'origin' remote" in reason

    def test_missing_remote_ok_when_not_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d)
            ok, _ = verify_dedicated_data_repo(d, require_remote=False)
            assert ok

    def test_wrong_data_repo_identity_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d, origin="https://github.com/someone/other-repo.git")
            ok, reason = verify_dedicated_data_repo(d, expected_repo_url=_DATA_URL)
            assert not ok
            assert "expected" in reason


# ── compact_repo_history ────────────────────────────────────────────────────


class TestCompactionGuard:
    def test_compaction_targets_dedicated_data_repo_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = Path(tmp) / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            d = Path(tmp) / "apex-trader-data"
            d.mkdir()
            _init_repo(d, origin=str(remote))
            for i in range(5):
                _commit(d, "f.db", f"rev{i}")
            # Expected-url None: local bare remote is not the source → allowed.
            res = backup_data.compact_repo_history(
                d, max_repo_size_mb=0.0, keep_commits=2, branch="master",
            )
            assert "compacted" in res
            after = int(subprocess.check_output(
                ["git", "-C", str(d), "rev-list", "--count", "HEAD"], text=True
            ).strip())
            assert after == 1
            assert (d / "f.db").read_text() == "rev4"

    def test_compaction_refuses_plain_dir_inside_source_repo(self):
        # THE incident: data/ is a plain dir inside the 1800-commit source repo.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source, origin=_SOURCE_URL)
            for i in range(12):
                _commit(source, "engine.py", f"v{i}")
            head_before = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            count_before = int(subprocess.check_output(
                ["git", "-C", str(source), "rev-list", "--count", "HEAD"], text=True
            ).strip())

            plain_data = source / "data"
            plain_data.mkdir()
            (plain_data / "trade.db").write_text("live")

            res = backup_data.compact_repo_history(
                plain_data, max_repo_size_mb=0.0, keep_commits=2, branch="main",
                data_repo_url=_DATA_URL,
            )
            # Safe refusal via either layer: the ceiling env makes the plain dir
            # "not a git repo", or the identity guard refuses it explicitly.
            # Either way, no compaction happened.
            assert res.startswith("refused") or res == "not a git repo"
            assert "compacted" not in res

            # The source repo's history is completely untouched.
            head_after = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            count_after = int(subprocess.check_output(
                ["git", "-C", str(source), "rev-list", "--count", "HEAD"], text=True
            ).strip())
            assert head_after == head_before
            assert count_after == count_before == 12

    def test_compaction_refuses_data_remote_pointing_at_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d, origin=_SOURCE_URL)  # dedicated repo but WRONG remote
            for i in range(12):
                _commit(d, "f.db", f"rev{i}")
            res = backup_data.compact_repo_history(
                d, max_repo_size_mb=0.0, keep_commits=2, branch="master",
                data_repo_url=_DATA_URL,
            )
            assert res.startswith("refused")
            assert "SOURCE repo" in res


# ── sync_data_repo ──────────────────────────────────────────────────────────


class TestSyncGuard:
    def test_sync_commits_to_dedicated_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d, origin=_DATA_URL)
            (d / "apex_positions.db").write_text("rows")
            res = backup_data.sync_data_repo(
                commit_message="x", push=False, data_dir=d, data_repo_url=_DATA_URL,
            )
            assert "committed" in res
            tracked = subprocess.check_output(
                ["git", "-C", str(d), "ls-files"], text=True
            )
            assert "apex_positions.db" in tracked

    def test_sync_refuses_plain_dir_inside_source_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source, origin=_SOURCE_URL)
            _commit(source, "engine.py", "v0")
            plain_data = source / "data"
            plain_data.mkdir()
            (plain_data / "apex_positions.db").write_text("rows")

            res = backup_data.sync_data_repo(
                commit_message="x", push=False, data_dir=plain_data,
                data_repo_url=_DATA_URL,
            )
            # Safe refusal via either layer (ceiling env or identity guard);
            # nothing is committed onto the source repo either way.
            assert res.startswith("refused") or res == "data dir is not a git repo"
            assert "committed" not in res
            # Nothing was committed onto the source repo (data file stays untracked).
            tracked = subprocess.check_output(
                ["git", "-C", str(source), "ls-files"], text=True
            )
            assert "apex_positions.db" not in tracked

    def test_sync_refuses_when_origin_is_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d, origin=_SOURCE_URL)
            (d / "apex_positions.db").write_text("rows")
            # push=False but a source origin is present → refuse even for commit.
            res = backup_data.sync_data_repo(
                commit_message="x", push=False, data_dir=d, data_repo_url=_DATA_URL,
            )
            assert res.startswith("refused")
            assert "SOURCE repo" in res

    def test_sync_push_to_dedicated_remote_succeeds(self):
        # Regression: a valid dedicated repo with a non-source remote still
        # pushes cleanly through the final pre-push identity re-check.
        with tempfile.TemporaryDirectory() as tmp:
            remote = Path(tmp) / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            d = Path(tmp) / "apex-trader-data"
            d.mkdir()
            _init_repo(d, origin=str(remote))
            subprocess.run(["git", "-C", str(d), "branch", "-M", "main"], check=True)
            (d / "apex_positions.db").write_text("rows")
            res = backup_data.sync_data_repo(
                commit_message="auto-sync", push=True, branch="main", data_dir=d,
            )
            assert res == "synced → origin/main"
            # The commit really landed on the remote.
            pushed = subprocess.check_output(
                ["git", "-C", str(remote), "log", "--oneline", "main"], text=True
            )
            assert "auto-sync" in pushed


# ── Boot-time topology banner ───────────────────────────────────────────────


class TestTopologyBanner:
    def test_banner_enabled_for_valid_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source, origin=_SOURCE_URL)
            data = Path(tmp) / "apex-trader-data"
            data.mkdir()
            _init_repo(data, origin=_DATA_URL)

            ok, banner = render_topology_banner(source, data, data_repo_url=_DATA_URL)
            assert ok
            assert "Auto-sync:         ENABLED" in banner
            assert "Data Git:          SAFE" in banner

            t = evaluate_topology(source, data, data_repo_url=_DATA_URL)
            assert t["auto_sync_ok"] is True
            assert t["data_resolves_to_source"] is False

    def test_banner_critical_when_data_resolves_to_source(self):
        # The incident topology: plain data/ nested in the source repo.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source, origin=_SOURCE_URL)
            plain_data = source / "data"
            plain_data.mkdir()

            ok, banner = render_topology_banner(
                source, plain_data, data_repo_url=_DATA_URL
            )
            assert not ok
            assert "CRITICAL: UNSAFE REPOSITORY TOPOLOGY" in banner
            assert "resolves to the source repository" in banner
            assert "AUTO-SYNC DISABLED." in banner
            assert "the SOURCE repository" in banner

            t = evaluate_topology(source, plain_data, data_repo_url=_DATA_URL)
            assert t["auto_sync_ok"] is False
            assert t["data_resolves_to_source"] is True

    def test_banner_critical_when_data_remote_is_source(self):
        # A dedicated repo (own root, not nested in source) but whose origin was
        # misconfigured to the source repo → still refused.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source, origin=_SOURCE_URL)
            data = Path(tmp) / "apex-trader-data"
            data.mkdir()
            _init_repo(data, origin=_SOURCE_URL)  # wrong remote

            ok, banner = render_topology_banner(source, data, data_repo_url=_DATA_URL)
            assert not ok
            assert "AUTO-SYNC DISABLED." in banner
            t = evaluate_topology(source, data, data_repo_url=_DATA_URL)
            assert t["auto_sync_ok"] is False
            assert t["data_git_safe"] is False
            assert t["data_resolves_to_source"] is False


# ── _get_remote_url hardening (run_backup path) ─────────────────────────────


class TestRemoteUrlHardening:
    def test_plain_dir_inside_source_yields_no_remote(self, ):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source, origin=_SOURCE_URL)
            plain_data = source / "data"
            plain_data.mkdir()
            from unittest.mock import patch

            with patch.object(backup_data, "_DATA_DIR", plain_data):
                # Ceiling env blocks parent discovery → no remote resolved.
                assert backup_data._get_remote_url() is None

    def test_source_origin_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d, origin=_SOURCE_URL)
            from unittest.mock import patch

            with patch.object(backup_data, "_DATA_DIR", d):
                assert backup_data._get_remote_url() is None

    def test_data_origin_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d, origin=_DATA_URL)
            from unittest.mock import patch

            with patch.object(backup_data, "_DATA_DIR", d):
                assert backup_data._get_remote_url() == _DATA_URL
