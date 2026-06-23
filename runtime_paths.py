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
"""

from __future__ import annotations

import os
from pathlib import Path

# Repository root — this file lives at ``<repo>/runtime_paths.py`` (top-level,
# deliberately NOT inside a package, so importing it never triggers a package
# ``__init__`` and cannot create import cycles with the trading engine).
_REPO_ROOT = Path(__file__).resolve().parent

_DATA_DIR_ENV = "APEX_DATA_DIR"
_LOG_DIR_ENV = "APEX_LOG_DIR"
_USER_ID_ENV = "APEX_USER_ID"


def data_dir() -> Path:
    """Return the active data directory.

    Resolution order:
      1. ``APEX_DATA_DIR`` environment variable (multi-tenant per-user override)
      2. ``<repo>/data`` (single-user default)
    """
    override = os.getenv(_DATA_DIR_ENV, "").strip()
    base = Path(override) if override else (_REPO_ROOT / "data")
    return base


def log_dir() -> Path:
    """Return the active log directory.

    Resolution order:
      1. ``APEX_LOG_DIR`` environment variable (multi-tenant per-user override)
      2. ``<repo>/logs`` (single-user default)
    """
    override = os.getenv(_LOG_DIR_ENV, "").strip()
    base = Path(override) if override else (_REPO_ROOT / "logs")
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
