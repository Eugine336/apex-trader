"""
APEX TRADER — Clean-Start Helpers

Two best-effort, non-fatal startup steps:

1. ``sync_clean_state_from_remote`` — ``git fetch`` + ``git reset --hard`` the
   data repo so the local working tree matches the remote branch exactly.

2. ``purge_stale_learned_data`` — schema-version-gated cleanup. If the local
   schema version file does not match ``config.SCHEMA_VERSION``, remove residual
   learned/adaptive artifacts (scoring weights, ML profiles, edge trackers,
   tuner/discovery DBs) so the equal-weight system learns from scratch.

Both functions swallow their own errors and return a short summary string;
callers should log the result and continue regardless.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from config import SCHEMA_VERSION
from loguru import logger

# Operational, machine-local event-store DB. It is recreated empty on first
# write and must never be restored from a shared remote — see
# ``discard_corrupt_event_store`` below for why this matters.
_EVENT_STORE_DB_NAME = "apex_events.db"
# How many rotated ``apex_events.db.corrupt.<ts>`` forensic copies to retain.
_KEEP_CORRUPT_ROTATIONS = 3

# Learned / adaptive artifacts that encoded the old structure-biased weights.
# These are safe to delete — they are regenerated empty on first write.
_STALE_LEARNED_FILES: tuple[str, ...] = (
    "scoring_weights.json",
    "zone_edge.json",
    "symbol_conviction.json",
    "ml_pair_profiles.json",
    "ml_session_profiles.json",
    "ml_regime_strategies.json",
    "gate_tuning.json",
    "planner_config.json",
    "governor_state.json",
    "outcome_feedback.jsonl",
    "plan_journal.jsonl",
    "trade_journal.db",
    "post_close_tracker.db",
    "module_governor.db",
    "signal_ledger.db",
    "tuner_audit.db",
    "counterfactual.db",
    "interaction_discovery.db",
    "param_evolution.db",
    "signal_discovery.db",
    "virtual_modules.db",
    "capital_allocation.db",
    "execution_profiles.db",
    "regime_detection.db",
    "risk_management.db",
    "behavior_discovery.db",
)

_CODE_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_SCHEMA_VERSION_NAME = ".local_schema_version"


def _is_git_repo(data_dir: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(data_dir.resolve()), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (FileNotFoundError, OSError):
        return False


def _read_local_schema_version(version_file: Path) -> str | None:
    try:
        value = version_file.read_text(encoding="utf-8").strip()
        return value or None
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("[clean-start] schema-version read failed: {}", exc)
        return None


def _write_local_schema_version(version_file: Path, schema_version: str) -> bool:
    try:
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(f"{schema_version}\n", encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("[clean-start] schema-version write failed: {}", exc)
        return False


def _backup_local_state(d: Path, *, branch_name: str = "clean-start-backup") -> None:
    """Force-update a backup branch to the data repo's current HEAD.

    Captures committed-but-unpushed local state before a destructive
    ``git reset --hard`` so it is always recoverable. Best-effort and silent on
    failure — never blocks the sync.
    """
    try:
        subprocess.run(
            ["git", "-C", str(d.resolve()), "branch", "-f", branch_name, "HEAD"],
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        logger.debug("[clean-start] local-state backup branch failed: {}", exc)


def sync_clean_state_from_remote(
    data_dir: str = "data",
    *,
    remote: str = "origin",
    branch: str = "main",
) -> str:
    """Fetch + hard-reset the data junction to the remote state."""
    d = Path(data_dir)
    if not d.is_dir():
        return "no data directory"
    if not _is_git_repo(d):
        return "data dir is not a git repo"
    try:
        fetch = subprocess.run(
            ["git", "-C", str(d.resolve()), "fetch", remote],
            capture_output=True,
            text=True,
        )
        if fetch.returncode != 0:
            msg = (fetch.stderr or fetch.stdout or "unknown").strip().splitlines()
            reason = msg[0] if msg else "unknown"
            logger.warning("[clean-start] data-repo fetch failed: {}", reason)
            return f"fetch failed: {reason}"

        targets = [branch]
        if branch == "main":
            targets.append("master")

        # Safeguard against data loss: ``git reset --hard`` discards any local
        # commits not yet on the remote AND any uncommitted changes to tracked
        # files (up to the un-pushed intraday window of DB/journal writes).
        # Snapshot the current local state onto a force-updated backup branch
        # first so nothing is ever permanently lost — the operator can recover
        # or push it later. Non-blocking: a backup failure never aborts the sync.
        _backup_local_state(d)

        last_reason = "unknown"
        for idx, target in enumerate(targets):
            reset = subprocess.run(
                ["git", "-C", str(d.resolve()), "reset", "--hard", f"{remote}/{target}"],
                capture_output=True,
                text=True,
            )
            if reset.returncode == 0:
                if idx == 0:
                    return f"reset to {remote}/{target}"
                logger.warning(
                    "[clean-start] data-repo branch '{}' missing, fell back to '{}/{}'",
                    branch,
                    remote,
                    target,
                )
                return f"reset to {remote}/{target} (fallback)"

            msg = (reset.stderr or reset.stdout or "unknown").strip().splitlines()
            last_reason = msg[0] if msg else "unknown"
            if idx == 0 and branch == "main":
                logger.warning(
                    "[clean-start] data-repo reset failed for {}/{}: {} — trying {}/master fallback",
                    remote,
                    target,
                    last_reason,
                    remote,
                )
            else:
                logger.warning(
                    "[clean-start] data-repo reset failed for {}/{}: {}",
                    remote,
                    target,
                    last_reason,
                )
        return f"reset failed: {last_reason}"
    except Exception as exc:
        logger.warning("[clean-start] data-repo sync errored: {}", exc)
        return f"error: {exc}"


def _resolve_local_schema_version_file(local_schema_version_file: str | Path | None) -> Path:
    if local_schema_version_file is None:
        return _CODE_REPO_ROOT / _LOCAL_SCHEMA_VERSION_NAME
    return Path(local_schema_version_file)


def purge_stale_learned_data(
    data_dir: str = "data",
    *,
    schema_version: str = SCHEMA_VERSION,
    local_schema_version_file: str | Path | None = None,
) -> str:
    """Schema-version-gated removal of residual learned/adaptive artifacts."""
    version_file = _resolve_local_schema_version_file(local_schema_version_file)
    target_schema = str(schema_version)
    try:
        current_schema = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
    except Exception as exc:
        logger.warning("[clean-start] local schema version read failed: {}", exc)
        current_schema = ""

    if current_schema == target_schema:
        return "already clean"

    d = Path(data_dir)
    if not d.is_dir():
        logger.warning("[clean-start] data directory missing during purge: {}", d)
        removed = []
    else:
        removed: list[str] = []
        for name in _STALE_LEARNED_FILES:
            target = d / name
            if target.exists():
                try:
                    target.unlink()
                    removed.append(name)
                except Exception as exc:
                    logger.warning("[clean-start] could not remove {}: {}", name, exc)

    try:
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(target_schema, encoding="utf-8")
    except Exception as exc:
        logger.warning("[clean-start] local schema version write failed: {}", exc)

    if removed:
        logger.warning(
            "[clean-start] purged {} stale learned artifact(s) so the "
            "equal-weight system starts fresh: {}",
            len(removed),
            ", ".join(removed),
        )
        return f"purged {len(removed)}: {', '.join(removed)}"
    if not d.is_dir():
        return "no data directory"
    return "nothing stale to purge"


def _event_db_is_corrupt(db_path: Path) -> bool:
    """Return True only when an existing, non-empty event-store DB fails its
    SQLite integrity check.

    A missing file is NOT corrupt (connecting would otherwise create an empty
    DB as a side effect, so existence/size is checked up front). A transient
    lock (``OperationalError``) is NOT treated as corruption either — only a
    malformed image or a non-``ok`` integrity result counts.
    """
    try:
        if not db_path.is_file() or db_path.stat().st_size == 0:
            return False
    except OSError:
        return False

    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.OperationalError as exc:
        # Locked/busy or otherwise unreadable for a non-structural reason —
        # leave the file untouched rather than risk deleting a healthy DB.
        logger.warning(
            "[clean-start] event-store integrity check could not run for {}: {}",
            db_path,
            exc,
        )
        return False
    except sqlite3.DatabaseError:
        # "database disk image is malformed" / "file is not a database".
        return True
    except Exception as exc:  # noqa: BLE001 — never let hygiene abort startup
        logger.warning(
            "[clean-start] event-store integrity check errored for {}: {}",
            db_path,
            exc,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return (row[0] if row else "").strip().lower() != "ok"


def _prune_corrupt_rotations(
    data_dir: Path,
    *,
    db_name: str = _EVENT_STORE_DB_NAME,
    keep: int = _KEEP_CORRUPT_ROTATIONS,
) -> int:
    """Delete old ``<db_name>.corrupt.*`` rotation files, keeping the newest
    ``keep`` for forensics. Returns the number removed."""
    try:
        rotations = sorted(
            data_dir.glob(f"{db_name}.corrupt.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0

    removed = 0
    for stale in rotations[max(keep, 0):]:
        try:
            stale.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("[clean-start] could not prune {}: {}", stale, exc)
    return removed


def discard_corrupt_event_store(
    data_dir: str = "data",
    *,
    db_name: str = _EVENT_STORE_DB_NAME,
) -> str:
    """Remove a corrupt operational event-store DB before it is opened.

    The ``data`` directory is a separate git repo (junction). ``clean-start``
    hard-resets it to the remote, which restores whatever ``apex_events.db`` is
    committed there. The event store is operational, machine-local state and
    should never be version-controlled — if a corrupt copy is committed, every
    restart restores it, the store logs a CRITICAL integrity failure, rotates
    it to ``apex_events.db.corrupt.<ts>``, and starts fresh — an endless
    restore/rotate loop that also bloats the disk with rotation files.

    This best-effort step breaks that loop: run after the data-repo reset and
    BEFORE the event store opens. If the restored DB is corrupt it is deleted
    (with its ``-wal``/``-shm`` sidecars) so the store starts on a clean slate
    with no CRITICAL spam and no new rotation file. A healthy DB is always left
    untouched, so once the data repo stops tracking the file (the proper fix)
    legitimate local event history is preserved. Accumulated rotation files are
    pruned to a small forensic window regardless.
    """
    d = Path(data_dir)
    if not d.is_dir():
        return "no data directory"

    db_path = d / db_name
    removed: list[str] = []
    if _event_db_is_corrupt(db_path):
        for suffix in ("", "-wal", "-shm"):
            sidecar = d / f"{db_name}{suffix}"
            try:
                if sidecar.exists():
                    sidecar.unlink()
                    removed.append(sidecar.name)
            except OSError as exc:
                logger.warning("[clean-start] could not remove {}: {}", sidecar, exc)
        if removed:
            logger.warning(
                "[clean-start] discarded corrupt event-store DB so a fresh one "
                "is created cleanly (removed: {})",
                ", ".join(removed),
            )

    pruned = _prune_corrupt_rotations(d)

    if removed and pruned:
        return f"discarded corrupt event-store ({len(removed)}); pruned {pruned} rotation(s)"
    if removed:
        return f"discarded corrupt event-store ({len(removed)})"
    if pruned:
        return f"pruned {pruned} rotation(s)"
    return "event-store healthy"


def run_startup_clean_start(
    *,
    data_dir: str = "data",
    branch: str = "main",
    schema_version: str = SCHEMA_VERSION,
    local_schema_version_file: str | Path | None = None,
) -> tuple[str, str, str]:
    """Convenience wrapper: sync remote state, discard a corrupt event-store DB
    restored by that sync, then run the schema-gated purge."""
    pull_res = sync_clean_state_from_remote(data_dir=data_dir, branch=branch)
    event_store_res = discard_corrupt_event_store(data_dir=data_dir)
    purge_res = purge_stale_learned_data(
        data_dir=data_dir,
        schema_version=schema_version,
        local_schema_version_file=local_schema_version_file,
    )
    return pull_res, event_store_res, purge_res
