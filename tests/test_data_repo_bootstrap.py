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
        assert args[0] == "clone"
        target.mkdir()
        (target / ".git").mkdir()
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
    assert len(commands) == 1
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
    monkeypatch.setattr(bootstrap, "_run_git", lambda *a, **k: "")

    result = bootstrap.bootstrap_data_repo(source_root=source)

    assert result.is_symlink()
    assert (result / "apex_positions.db").read_bytes() == b"local"
    assert (target / ".apex-bootstrap-preserved").is_dir()
    backups = list(tmp_path.glob("apex-trader-data-local-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "apex_positions.db").read_bytes() == b"local"
