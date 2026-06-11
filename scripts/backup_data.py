"""
APEX TRADER — Data Backup to GitHub

Pushes the data/ directory to an orphan `data-backup` branch
on the same GitHub remote.  Can be run standalone or imported
from the trading loop for periodic auto-backup.

Usage:
    python scripts/backup_data.py          # one-shot backup
    python scripts/backup_data.py --force  # skip "nothing changed" check
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
BRANCH = "data-backup"
LAST_BACKUP_MARKER = DATA_DIR / ".last_backup_ts"


def _git(*args: str, cwd: Path = REPO_ROOT, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _has_remote() -> bool:
    result = _git("remote", check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def _data_changed_since_last_backup() -> bool:
    if not LAST_BACKUP_MARKER.exists():
        return True
    marker_mtime = LAST_BACKUP_MARKER.stat().st_mtime
    for root, _dirs, files in os.walk(DATA_DIR):
        for f in files:
            fp = Path(root) / f
            if fp == LAST_BACKUP_MARKER:
                continue
            if fp.stat().st_mtime > marker_mtime:
                return True
    return False


def run_backup(*, force: bool = False) -> str:
    """Push data/ contents to the orphan data-backup branch.

    Returns a short status message.
    """
    if not DATA_DIR.exists() or not any(DATA_DIR.iterdir()):
        return "skip: data/ directory is empty or missing"

    if not _has_remote():
        return "skip: no git remote configured"

    if not force and not _data_changed_since_last_backup():
        return "skip: no data changes since last backup"

    now = datetime.now(timezone.utc)
    commit_msg = f"data backup {now.strftime('%Y-%m-%dT%H:%M:%SZ')}"

    tmp = Path(tempfile.mkdtemp(prefix="apex_backup_"))
    try:
        _git("init", cwd=tmp)

        origin_url = _git("remote", "get-url", "origin").stdout.strip()
        _git("remote", "add", "origin", origin_url, cwd=tmp)

        _git("checkout", "--orphan", BRANCH, cwd=tmp)

        dest = tmp / "data"
        shutil.copytree(DATA_DIR, dest, dirs_exist_ok=True)

        marker = dest / ".last_backup_ts"
        if marker.exists():
            marker.unlink()

        _git("add", "-A", cwd=tmp)

        status = _git("status", "--porcelain", cwd=tmp)
        if not status.stdout.strip():
            return "skip: nothing staged (data identical to last push)"

        _git(
            "-c", "user.name=APEX Backup",
            "-c", "user.email=backup@apex-trader.local",
            "commit", "-m", commit_msg,
            cwd=tmp,
        )

        push_result = _git(
            "push", "--force", "origin", BRANCH,
            cwd=tmp,
            check=False,
        )
        if push_result.returncode != 0:
            return f"push failed: {push_result.stderr.strip()}"

        LAST_BACKUP_MARKER.parent.mkdir(parents=True, exist_ok=True)
        LAST_BACKUP_MARKER.write_text(now.isoformat())

        return f"ok: pushed to {BRANCH} at {now.strftime('%H:%M:%S')} UTC"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup data/ to GitHub data-backup branch")
    parser.add_argument("--force", action="store_true", help="Skip the 'nothing changed' check")
    args = parser.parse_args()

    result = run_backup(force=args.force)
    logger.info("[data-backup] {}", result)
    if result.startswith("push failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
