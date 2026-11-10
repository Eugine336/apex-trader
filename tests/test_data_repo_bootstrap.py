from pathlib import Path

import pytest

from scripts import data_repo_bootstrap as bootstrap


def test_rejects_unexpected_public_repository_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_DATA_DIR", raising=False)
    called = []
    monkeypatch.setattr(bootstrap, "_run_git", lambda *a, **k: called.append(a))
    with pytest.raises(bootstrap.DataRepoBootstrapError, match="unexpected data repository"):
        bootstrap.bootstrap_data_repo(source_root=tmp_path, data_repo_url="https://github.com/example/other.git")
    assert called == []


def test_existing_local_files_win_and_remote_conflicts_are_preserved(tmp_path):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    backup = tmp_path / "backup"
    local.mkdir()
    remote.mkdir()
    (local / "apex_positions.db").write_bytes(b"local-live-state")
    (remote / "apex_positions.db").write_bytes(b"remote-old-state")
    (local / "new-state.json").write_text('{"local":true}', encoding="utf-8")

    copied = bootstrap._copy_local_data(local, remote, backup)

    assert copied == 2
    assert (remote / "apex_positions.db").read_bytes() == b"local-live-state"
    assert (backup / "apex_positions.db").read_bytes() == b"remote-old-state"
    assert (remote / "new-state.json").read_text(encoding="utf-8") == '{"local":true}'


def test_identical_local_files_are_not_rewritten(tmp_path):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    backup = tmp_path / "backup"
    local.mkdir()
    remote.mkdir()
    payload = b"same"
    (local / "state.db").write_bytes(payload)
    (remote / "state.db").write_bytes(payload)

    assert bootstrap._copy_local_data(local, remote, backup) == 0
    assert not backup.exists()


def test_multi_tenant_data_override_skips_shared_repository_bootstrap(tmp_path, monkeypatch):
    isolated = tmp_path / "user-data"
    monkeypatch.setenv("APEX_DATA_DIR", str(isolated))
    monkeypatch.setattr(
        "runtime_paths.ensure_data_dir",
        lambda: isolated,
    )
    monkeypatch.setattr(
        bootstrap, "_run_git",
        lambda *a, **k: pytest.fail("shared data repository must not be cloned"),
    )

    assert bootstrap.bootstrap_data_repo(source_root=tmp_path) == isolated


def test_existing_data_link_must_target_expected_sibling_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_DATA_DIR", raising=False)
    expected = tmp_path / "apex-trader-data"
    unexpected = tmp_path / "other-data"
    expected.mkdir()
    unexpected.mkdir()
    (tmp_path / "apex-trader").mkdir()
    link = tmp_path / "apex-trader" / "data"
    link.symlink_to(unexpected, target_is_directory=True)

    monkeypatch.setattr(
        bootstrap, "verify_dedicated_data_repo",
        lambda *a, **k: (True, "verified"),
    )
    with pytest.raises(bootstrap.DataRepoBootstrapError, match="expected"):
        bootstrap.bootstrap_data_repo(source_root=tmp_path / "apex-trader")

def test_fresh_clone_automatically_clones_and_links_sibling_data_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_DATA_DIR", raising=False)
    source = tmp_path / "apex-trader"
    source.mkdir()
    target = tmp_path / "apex-trader-data"
    commands = []

    def fake_git(args, **kwargs):
        commands.append(args)
        if args[0] == "clone":
            target.mkdir()
            (target / ".git").mkdir()
            return ""
        if args[:2] == ["branch", "--show-current"]:
            return "main"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return "origin/main"
        if args[:2] == ["config", "--get"]:
            return "git-lfs-command"
        if args[:2] == ["lfs", "version"]:
            return "git-lfs/3.8.1"
        return ""

    monkeypatch.setattr(bootstrap, "_run_git", fake_git)
    monkeypatch.setattr(
        bootstrap, "verify_dedicated_data_repo",
        lambda *a, **k: (True, "eugine336/apex-trader-data"),
    )

    result = bootstrap.bootstrap_data_repo(source_root=source)

    assert result == source / "data"
    assert result.is_symlink()
    assert result.resolve() == target
    assert commands[0][0] == "clone"
    assert any(args[:2] == ["lfs", "install"] for args in commands)
    assert "https://github.com/Eugine336/apex-trader-data.git" in commands[0]


def test_nonempty_local_data_is_retained_after_automatic_routing(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_DATA_DIR", raising=False)
    source = tmp_path / "apex-trader"
    local = source / "data"
    target = tmp_path / "apex-trader-data"
    local.mkdir(parents=True)
    target.mkdir()
    (target / ".git").mkdir()
    (local / "apex_positions.db").write_bytes(b"local")
    (target / "apex_positions.db").write_bytes(b"remote")

    monkeypatch.setattr(
        bootstrap, "verify_dedicated_data_repo",
        lambda *a, **k: (True, "eugine336/apex-trader-data"),
    )
    def fake_git(args, **kwargs):
        if args[:2] == ["branch", "--show-current"]:
            return "main"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return "origin/main"
        if args[:2] == ["config", "--get"]:
            return "git-lfs-command"
        if args[:2] == ["lfs", "version"]:
            return "git-lfs/3.8.1"
        return ""
    monkeypatch.setattr(bootstrap, "_run_git", fake_git)

    result = bootstrap.bootstrap_data_repo(source_root=source)

    assert result.is_symlink()
    # Existing sibling data remains active; the conflicting source copy is
    # preserved intact in the timestamped source-directory backup.
    assert (result / "apex_positions.db").read_bytes() == b"remote"
    backups = list(tmp_path.glob("apex-trader-data-local-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "apex_positions.db").read_bytes() == b"local"



def test_preserve_conflicts_keeps_active_target_and_source_copy(tmp_path):
    local = tmp_path / "source-data"
    target = tmp_path / "target-data"
    backup = tmp_path / "conflicts"
    local.mkdir()
    target.mkdir()
    (local / "apex_events.db").write_bytes(b"source-event-store")
    (target / "apex_events.db").write_bytes(b"target-event-store")

    copied = bootstrap._copy_local_data(
        local, target, backup, preserve_conflicts=True
    )

    assert copied == 0
    assert (target / "apex_events.db").read_bytes() == b"target-event-store"
    assert (local / "apex_events.db").read_bytes() == b"source-event-store"
    assert not backup.exists()


def test_adoption_creates_verified_full_backup_and_configures_git_lfs(tmp_path, monkeypatch):
    target = tmp_path / "apex-trader-data"
    target.mkdir()
    (target / "apex_events.db").write_bytes(b"large-local-database")
    (target / "state.json").write_text('{"preserve":true}', encoding="utf-8")
    commands = []

    def fake_git(args, *, cwd=None, timeout=180):
        commands.append((list(args), cwd))
        if args == ["init"]:
            (cwd / ".git").mkdir()
        if args[:2] == ["branch", "--show-current"]:
            return "main"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return "origin/main"
        if args[:2] == ["config", "--get"]:
            return "git-lfs-command"
        if args[:2] == ["lfs", "version"]:
            return "git-lfs/3.8.1"
        return ""

    monkeypatch.setattr(bootstrap, "_run_git", fake_git)
    monkeypatch.setattr(
        bootstrap, "verify_dedicated_data_repo",
        lambda *a, **k: (True, "eugine336/apex-trader-data"),
    )

    backup = bootstrap._adopt_existing_data_repo(
        target, bootstrap.DEFAULT_DATA_REPO_URL
    )

    assert backup.is_dir()
    assert (backup / "apex_events.db").read_bytes() == b"large-local-database"
    assert (backup / "state.json").read_text(encoding="utf-8") == '{"preserve":true}'
    assert (target / "apex_events.db").read_bytes() == b"large-local-database"
    assert (target / "state.json").read_text(encoding="utf-8") == '{"preserve":true}'
    assert (target / ".git").is_dir()
    assert any(args[:2] == ["lfs", "install"] for args, _ in commands)
    assert any(args[:2] == ["read-tree", "origin/main"] for args, _ in commands)
    assert any(args[:2] == ["checkout-index", "--all"] for args, _ in commands)


def test_adoption_backup_failure_leaves_original_directory_untouched(tmp_path, monkeypatch):
    target = tmp_path / "apex-trader-data"
    target.mkdir()
    database = target / "apex_events.db"
    database.write_bytes(b"do-not-touch")

    def fail_copy(*args, **kwargs):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(bootstrap, "_copy_tree_verified", fail_copy)

    with pytest.raises(bootstrap.DataRepoBootstrapError, match="original untouched"):
        bootstrap._adopt_existing_data_repo(
            target, bootstrap.DEFAULT_DATA_REPO_URL
        )

    assert database.read_bytes() == b"do-not-touch"
    assert not (target / ".git").exists()



def test_adoption_git_failure_keeps_original_data_and_verified_backup(tmp_path, monkeypatch):
    target = tmp_path / "apex-trader-data"
    target.mkdir()
    database = target / "apex_events.db"
    database.write_bytes(b"original-database")

    def fail_fetch(args, *, cwd=None, timeout=180):
        if args[:2] == ["fetch", "--no-tags"]:
            raise bootstrap.DataRepoBootstrapError("simulated fetch failure")
        if args == ["init"]:
            (cwd / ".git").mkdir()
        return ""

    monkeypatch.setattr(bootstrap, "_run_git", fail_fetch)

    with pytest.raises(bootstrap.DataRepoBootstrapError, match="simulated fetch failure"):
        bootstrap._adopt_existing_data_repo(
            target, bootstrap.DEFAULT_DATA_REPO_URL
        )

    backups = list(tmp_path.glob("apex-trader-data-pre-adoption-*"))
    assert len(backups) == 1
    assert (backups[0] / "apex_events.db").read_bytes() == b"original-database"
    assert database.read_bytes() == b"original-database"
