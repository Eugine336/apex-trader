"""
APEX TRADER — Runtime path resolution.

Single source of truth for the runtime ``data`` and ``logs`` directories.

In single-user (standalone) mode the defaults resolve to the repository's own
``data/`` and ``logs/`` directories — identical to the historical behaviour, so
nothing changes for existing runs or tests.

In multi-tenant mode the API's process manager launches each user's APEX
instance with ``APEX_DATA_DIR`` / ``APEX_LOG_DIR`` (and ``APEX_USER_ID``) set to
a per-user working directory, giving every user a fully isolated state tree
without forking the codebase.

Modules that previously hard-coded ``Path(__file__).parent.parent / "data"``
should call :func:`data_dir` instead so they honour the override transparently.

The data junction
-----------------
On the operator's machine ``<repo>/data`` is a directory junction/symlink that
points at the separate ``apex-trader-data`` git repo. Two distinct concerns are
served out of it, and they must NOT be conflated in multi-tenant mode:

* **Writeable per-user state** (positions, journals, calibration, learned
  weights). In single-user mode this lands in the junction and is git-synced /
  backed up. In multi-tenant mode it must be *isolated per user* — every
  instance writes to its own ``APEX_DATA_DIR`` and the junction / git-sync is
  deliberately bypassed (you cannot have N users sharing one git data repo).
  Resolve this via :func:`data_dir`.

* **Shared read-only reference data** (e.g. the trained RL checkpoint, a swap
  rate table). This is the *same* for every user and lives at the repo root
  (junction-adjacent). It must stay reachable even when a per-user instance has
  an isolated, empty ``APEX_DATA_DIR``. Resolve this via :func:`shared_data_dir`
  / :func:`checkpoints_dir`, which always anchor to the repo root regardless of
  the per-user override or the process's current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repository root — this file lives at ``<repo>/runtime_paths.py`` (top-level,
# deliberately NOT inside a package, so importing it never triggers a package
# ``__init__`` and cannot create import cycles with the trading engine).
_FALLBACK_REPO_ROOT = Path(__file__).resolve().parent

_DATA_DIR_ENV = "APEX_DATA_DIR"
_LOG_DIR_ENV = "APEX_LOG_DIR"
_USER_ID_ENV = "APEX_USER_ID"
_REPO_DIR_ENV = "APEX_REPO_DIR"


def repo_root() -> Path:
    """Return the repository root holding the engine code + shared resources.

    Resolution order:
      1. ``APEX_REPO_DIR`` environment variable (set by the multi-tenant process
         manager so spawned instances — whose cwd is the per-user workdir — can
         still locate shared, read-only assets that live with the code).
      2. ``<repo>`` (this file's directory; correct for single-user runs).

    Use this ONLY for read-only resources shipped with the code (trained RL
    checkpoints, reference configs). Mutable per-user state must use
    :func:`data_dir`.
    """
    override = os.getenv(_REPO_DIR_ENV, "").strip()
    return Path(override) if override else _FALLBACK_REPO_ROOT


def data_dir() -> Path:
    """Return the active (writeable, per-user-isolated) data directory.

    Resolution order:
      1. ``APEX_DATA_DIR`` environment variable (multi-tenant per-user override)
      2. ``<repo>/data`` (single-user default — the data junction)
    """
    override = os.getenv(_DATA_DIR_ENV, "").strip()
    base = Path(override) if override else (repo_root() / "data")
    return base


def shared_data_dir() -> Path:
    """Return the shared, read-only reference data directory (the junction).

    Always ``<repo>/data`` regardless of ``APEX_DATA_DIR`` — so reference data
    that is identical for every user (and lives in the junction) stays reachable
    from per-user instances whose writeable :func:`data_dir` is isolated and
    empty.

    In single-user mode this equals :func:`data_dir`; in multi-tenant mode it
    points at the operator's junction while :func:`data_dir` points at the
    per-user isolated tree.
    """
    return repo_root() / "data"


def checkpoints_dir() -> Path:
    """Return the directory holding trained model checkpoints (repo-relative).

    Checkpoints are shared, read-only artifacts produced by training and are the
    same for every user, so they are anchored to the repo root rather than the
    per-user (isolated) data directory.
    """
    return repo_root() / "checkpoints"


def log_dir() -> Path:
    """Return the active log directory.

    Resolution order:
      1. ``APEX_LOG_DIR`` environment variable (multi-tenant per-user override)
      2. ``<repo>/logs`` (single-user default)
    """
    override = os.getenv(_LOG_DIR_ENV, "").strip()
    base = Path(override) if override else (repo_root() / "logs")
    return base


def user_id() -> str | None:
    """Return the current instance's owning user id, if running multi-tenant."""
    uid = os.getenv(_USER_ID_ENV, "").strip()
    return uid or None


def ensure_data_dir() -> Path:
    """Return :func:`data_dir`, creating it if absent."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_log_dir() -> Path:
    """Return :func:`log_dir`, creating it if absent."""
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
