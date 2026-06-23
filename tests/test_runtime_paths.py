"""Tests for runtime path resolution — per-user isolation vs shared junction.

These lock down the contract that:
  * writeable state follows ``APEX_DATA_DIR`` (per-user isolation),
  * shared read-only data (the junction, checkpoints) always anchors to the
    repo root regardless of ``APEX_DATA_DIR`` or the process cwd,
  * ``APEX_REPO_DIR`` overrides the repo-root anchor (set by the process
    manager so an isolated instance can still find shared resources).
"""

from pathlib import Path

import runtime_paths


def test_data_dir_defaults_to_repo_data(monkeypatch):
    monkeypatch.delenv("APEX_DATA_DIR", raising=False)
    monkeypatch.delenv("APEX_REPO_DIR", raising=False)
    assert runtime_paths.data_dir() == runtime_paths.repo_root() / "data"


def test_data_dir_honours_override(monkeypatch, tmp_path):
    override = tmp_path / "user_7" / "data"
    monkeypatch.setenv("APEX_DATA_DIR", str(override))
    assert runtime_paths.data_dir() == override


def test_shared_data_dir_ignores_per_user_override(monkeypatch, tmp_path):
    """The junction (shared, read-only) must NOT follow APEX_DATA_DIR."""
    monkeypatch.delenv("APEX_REPO_DIR", raising=False)
    monkeypatch.setenv("APEX_DATA_DIR", str(tmp_path / "isolated" / "data"))
    # Writeable state is isolated …
    assert runtime_paths.data_dir() == tmp_path / "isolated" / "data"
    # … but shared reference data still resolves to the repo junction.
    assert runtime_paths.shared_data_dir() == runtime_paths.repo_root() / "data"
    assert runtime_paths.shared_data_dir() != runtime_paths.data_dir()


def test_checkpoints_dir_anchored_to_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("APEX_REPO_DIR", raising=False)
    monkeypatch.setenv("APEX_DATA_DIR", str(tmp_path / "isolated" / "data"))
    assert runtime_paths.checkpoints_dir() == runtime_paths.repo_root() / "checkpoints"


def test_repo_root_honours_apex_repo_dir(monkeypatch, tmp_path):
    fake_repo = tmp_path / "code_repo"
    monkeypatch.setenv("APEX_REPO_DIR", str(fake_repo))
    assert runtime_paths.repo_root() == fake_repo
    # Shared resources follow the overridden repo root …
    assert runtime_paths.shared_data_dir() == fake_repo / "data"
    assert runtime_paths.checkpoints_dir() == fake_repo / "checkpoints"


def test_repo_root_default_is_this_checkout(monkeypatch):
    monkeypatch.delenv("APEX_REPO_DIR", raising=False)
    # runtime_paths.py lives at the repo root.
    assert runtime_paths.repo_root() == Path(runtime_paths.__file__).resolve().parent
