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
