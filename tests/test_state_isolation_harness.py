"""Regression tests for the pytest trading-state safety harness."""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def test_default_persistence_paths_are_in_the_test_sandbox():
    import execution.management_state as management_state
    import persistence.backfill as backfill
    import persistence.event_store as event_store
    import persistence.position_store as position_store
    import persistence.shadow_store as shadow_store

    sandbox = Path(os.environ["APEX_DATA_DIR"]).resolve()
    paths = [
        event_store._DB_PATH,
        position_store._DB_PATH,
        shadow_store._DB_PATH,
        backfill._JOURNAL_DB,
        backfill._POSITION_DB,
        backfill._SHADOW_DB,
        backfill._EVENTS_DB,
        management_state._DB_DIR,
    ]
    for path in paths:
        assert _is_within(Path(path), sandbox), f"Persistence path escaped test sandbox: {path}"


def test_legacy_sqlite_path_is_redirected_before_file_creation():
    """An explicit repo/data path must be redirected without touching real data."""
    probe_name = f".pytest-state-redirect-probe-{uuid.uuid4().hex}.db"
    repository_path = _REPO_ROOT / "data" / probe_name
    sandbox_path = Path(os.environ["APEX_DATA_DIR"]) / probe_name

    # This passes a path string to the SQLite boundary; it never opens a file
    # in the repository data directory because conftest redirects the target.
    connection = sqlite3.connect(str(repository_path))
    try:
        connection.execute("CREATE TABLE isolation_probe (value INTEGER)")
        connection.commit()
    finally:
        connection.close()

    assert not repository_path.exists()
    assert sandbox_path.exists()
