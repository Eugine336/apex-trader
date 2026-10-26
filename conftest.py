"""Pytest bootstrap: isolate runtime state before importing application modules.

No test may open the repository's real trading-state databases. Runtime paths
are redirected before collection, explicit SQLite paths into the checked-out
data tree are redirected as a second line of defence, and the real data tree
is fingerprinted before/after the suite to detect non-SQLite writes.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sqlite3.dbapi2 as sqlite3_dbapi2
import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

_REPO_ROOT = Path(__file__).resolve().parent
_REAL_DATA_DIR = _REPO_ROOT / "data"
_TEST_STATE_ROOT = Path(tempfile.mkdtemp(prefix="apex-pytest-state-"))
_TEST_DATA_DIR = _TEST_STATE_ROOT / "data"
_TEST_LOG_DIR = _TEST_STATE_ROOT / "logs"
_TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
_TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Set these before pytest imports any test module. Several persistence modules
# capture data_dir() into module-level constants during import.
os.environ["APEX_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["APEX_LOG_DIR"] = str(_TEST_LOG_DIR)
os.environ["APEX_TRADE_REPORT_DB"] = str(_TEST_DATA_DIR / "trade_report.db")


def _iter_protected_state_files():
    """Yield files whose content must not change during a test run."""
    if _REAL_DATA_DIR.exists():
        for path in _REAL_DATA_DIR.rglob("*"):
            if path.is_file():
                yield path
    # Historical RL shadow state has also existed at the source-repo root.
    for path in _REPO_ROOT.glob("shadow_journal.db*"):
        if path.is_file():
            yield path


def _protected_state_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(_iter_protected_state_files(), key=lambda item: str(item)):
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            # Refuse to run without a usable baseline; a partial snapshot is
            # not evidence that the database/state files were protected.
            raise RuntimeError(
                f"Cannot fingerprint protected trading state file {path}: {exc}"
            ) from exc
        try:
            key = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            key = str(path)
        snapshot[key] = digest.hexdigest()
    return snapshot


_INITIAL_PROTECTED_STATE = _protected_state_snapshot()
_ORIGINAL_SQLITE_CONNECT = sqlite3.connect


def _redirected_sqlite_database(database, *, uri: bool = False) -> str | None:
    """Redirect SQLite paths aimed at repository data into the temp sandbox."""
    try:
        database_text = os.fspath(database)
    except (TypeError, ValueError):
        return None
    if isinstance(database_text, bytes):
        database_text = database_text.decode(errors="replace")
    if not isinstance(database_text, str) or database_text in ("", ":memory:"):
        return None

    is_uri = database_text.startswith("file:") and uri
    if database_text.startswith("file:") and not uri:
        # Without SQLite URI mode, "file:..." is a literal filename, not a
        # path into data/. Leave its existing semantics untouched.
        return None

    query = ""
    if is_uri:
        uri_value = database_text[5:]
        path_text, separator, query = uri_value.partition("?")
        path_text = unquote(path_text)
        if path_text in ("", ":memory:"):
            return None
        candidate = Path(path_text)
    else:
        candidate = Path(database_text)

    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve(strict=False)
    data_root = _REAL_DATA_DIR.resolve(strict=False)

    if candidate.parent == _REPO_ROOT.resolve(strict=False) and candidate.name.startswith(
        "shadow_journal.db"
    ):
        relative = Path(candidate.name)
    else:
        try:
            relative = candidate.relative_to(data_root)
        except ValueError:
            return None

    target = _TEST_DATA_DIR / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_uri:
        suffix = f"?{query}" if separator else ""
        return "file:" + quote(str(target), safe="/:") + suffix
    return str(target)


def _isolated_sqlite_connect(*args, **kwargs):
    """Keep SQLite writes out of the real state tree, including legacy paths."""
    if args:
        database, remaining_args = args[0], args[1:]
        replacement = _redirected_sqlite_database(
            database, uri=bool(kwargs.get("uri", False))
        )
        if replacement is None:
            return _ORIGINAL_SQLITE_CONNECT(*args, **kwargs)
        return _ORIGINAL_SQLITE_CONNECT(replacement, *remaining_args, **kwargs)

    if "database" not in kwargs:
        return _ORIGINAL_SQLITE_CONNECT(*args, **kwargs)
    database = kwargs["database"]
    replacement = _redirected_sqlite_database(
        database, uri=bool(kwargs.get("uri", False))
    )
    if replacement is None:
        return _ORIGINAL_SQLITE_CONNECT(*args, **kwargs)
    isolated_kwargs = dict(kwargs)
    isolated_kwargs["database"] = replacement
    return _ORIGINAL_SQLITE_CONNECT(**isolated_kwargs)


# Patch both public module references so "import sqlite3" and
# "import sqlite3.dbapi2" callers receive the same protection.
sqlite3.connect = _isolated_sqlite_connect
sqlite3_dbapi2.connect = _isolated_sqlite_connect


@pytest.fixture(autouse=True)
def _isolate_runtime_persistence(monkeypatch):
    """Re-assert the sandbox paths for each test, before the test body runs."""
    monkeypatch.setenv("APEX_DATA_DIR", str(_TEST_DATA_DIR))
    monkeypatch.setenv("APEX_LOG_DIR", str(_TEST_LOG_DIR))
    monkeypatch.setenv("APEX_TRADE_REPORT_DB", str(_TEST_DATA_DIR / "trade_report.db"))
    _TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)


def pytest_sessionfinish(session, exitstatus):
    """Fail the suite if any protected real-state file changed."""
    try:
        final_state = _protected_state_snapshot()
    except Exception as exc:  # noqa: BLE001
        print(f"CRITICAL: could not verify protected trading state: {exc}", file=sys.stderr)
        session.exitstatus = 1
        return

    if final_state != _INITIAL_PROTECTED_STATE:
        before = _INITIAL_PROTECTED_STATE
        after = final_state
        changed = sorted(
            key for key in set(before) | set(after) if before.get(key) != after.get(key)
        )
        preview = ", ".join(changed[:20])
        suffix = " ..." if len(changed) > 20 else ""
        print(
            "CRITICAL: tests changed protected repository trading state: "
            f"{preview}{suffix}",
            file=sys.stderr,
        )
        session.exitstatus = 1


def pytest_unconfigure(config):
    """Remove only the disposable sandbox created by this conftest."""
    shutil.rmtree(_TEST_STATE_ROOT, ignore_errors=True)


def _ensure_mt5_stub():
    """Create a minimal MetaTrader5 stub with required constants."""
    if "MetaTrader5" in sys.modules:
        return
    mt5 = types.ModuleType("MetaTrader5")
    for tf_name in [
        "TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15",
        "TIMEFRAME_H1", "TIMEFRAME_H4", "TIMEFRAME_D1",
    ]:
        setattr(mt5, tf_name, 1)
    for const in [
        "ORDER_TYPE_BUY", "ORDER_TYPE_SELL",
        "TRADE_ACTION_DEAL", "TRADE_ACTION_SLTP",
        "ORDER_FILLING_IOC", "ORDER_TIME_GTC",
        "POSITION_TYPE_BUY", "POSITION_TYPE_SELL",
    ]:
        setattr(mt5, const, 0)
    mt5.initialize = lambda **kw: True
    mt5.shutdown = lambda: None
    mt5.last_error = lambda: (0, "OK")
    mt5.account_info = lambda: None
    mt5.terminal_info = lambda: None
    _si = types.SimpleNamespace(trade_mode=4, visible=True, spread=10)
    mt5.symbol_info = lambda s: _si
    mt5.symbol_info_tick = lambda s: None
    mt5.symbol_select = lambda s, e=True: True
    mt5.copy_rates_from_pos = lambda *a, **kw: None
    mt5.positions_get = lambda: ()
    mt5.order_send = lambda req: None
    sys.modules["MetaTrader5"] = mt5


_ensure_mt5_stub()
