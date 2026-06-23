"""Tests for the per-user writeable vs shared read-only path split.

Multi-tenant isolation depends on two distinct anchors:

* ``data_dir()`` / ``log_dir()`` follow ``APEX_DATA_DIR`` / ``APEX_LOG_DIR`` so
  every user's *mutable* state (feedback, learning, evolution artifacts) is
  isolated under their own workdir.
* ``repo_root()`` / ``shared_data_dir()`` / ``checkpoints_dir()`` follow
  ``APEX_REPO_DIR`` so *shared, read-only* assets (trained RL checkpoints,
  reference configs) resolve to the code repo — never the per-user cwd, where
  they would not exist and the RL subsystem would stay INACTIVE_NO_CHECKPOINT.
"""

from __future__ import annotations

import importlib

import runtime_paths


def _reload(monkeypatch, **env):
    for key in ("APEX_DATA_DIR", "APEX_LOG_DIR", "APEX_REPO_DIR", "APEX_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(runtime_paths)


def test_single_user_defaults_to_repo(monkeypatch):
    rp = _reload(monkeypatch)
    # No env overrides → everything anchors to the repo (historical behaviour).
    assert rp.data_dir() == rp.repo_root() / "data"
    assert rp.shared_data_dir() == rp.repo_root() / "data"
    assert rp.checkpoints_dir() == rp.repo_root() / "checkpoints"


def test_multi_tenant_isolates_writeable_state(monkeypatch):
    rp = _reload(
        monkeypatch,
        APEX_DATA_DIR="/tmp/apex_u7/data",
        APEX_LOG_DIR="/tmp/apex_u7/logs",
        APEX_REPO_DIR="/opt/apex-trader",
        APEX_USER_ID="7",
    )
    # Per-user writeable state follows APEX_DATA_DIR.
    assert str(rp.data_dir()) == "/tmp/apex_u7/data"
    assert str(rp.log_dir()) == "/tmp/apex_u7/logs"
    assert rp.user_id() == "7"
    # Shared read-only assets follow APEX_REPO_DIR, NOT the per-user data dir.
    assert str(rp.repo_root()) == "/opt/apex-trader"
    assert str(rp.checkpoints_dir()) == "/opt/apex-trader/checkpoints"
    assert str(rp.shared_data_dir()) == "/opt/apex-trader/data"


def test_shared_checkpoint_never_under_user_data(monkeypatch):
    """Regression: the shared checkpoint must not resolve under the per-user
    writeable tree (the bug that left RL permanently INACTIVE_NO_CHECKPOINT)."""
    rp = _reload(
        monkeypatch,
        APEX_DATA_DIR="/tmp/apex_u7/data",
        APEX_REPO_DIR="/opt/apex-trader",
    )
    ckpt = str(rp.checkpoints_dir())
    assert not ckpt.startswith("/tmp/apex_u7/data")
    assert ckpt.startswith("/opt/apex-trader")


def test_reset_env_for_other_tests(monkeypatch):
    # Restore the module to its unenv'd state so subsequent imports see defaults.
    _reload(monkeypatch)
