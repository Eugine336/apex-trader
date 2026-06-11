"""
APEX TRADER — Restore Data from GitHub Backup

Pulls the data/ directory from the orphan `data-backup` branch
on the same GitHub remote into the local data/ folder.

Usage:
    python scripts/restore_data.py          # restore, skip existing files
    python scripts/restore_data.py --force  # overwrite existing files
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from loguru import logger

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
BRANCH = "data-backup"


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


def run_restore(*, force: bool = False) -> str:
    """Pull data/ contents from the data-backup branch.

    Returns a short status message.
    """
    if not _has_remote():
        return "fail: no git remote configured"

    origin_url = _git("remote", "get-url", "origin").stdout.strip()

    tmp = Path(tempfile.mkdtemp(prefix="apex_restore_"))
    try:
        clone_result = _git(
            "clone",
            "--depth", "1",
            "--branch", BRANCH,
            "--single-branch",
            origin_url,
            str(tmp / "repo"),
            cwd=tmp,
            check=False,
        )
        if clone_result.returncode != 0:
            stderr = clone_result.stderr.strip()
            if "not found" in stderr.lower() or "could not find" in stderr.lower():
                return f"fail: branch '{BRANCH}' does not exist on remote — no backup to restore"
            return f"fail: clone error — {stderr}"

        src = tmp / "repo" / "data"
        if not src.exists():
            return f"fail: branch '{BRANCH}' exists but contains no data/ directory"

        DATA_DIR.mkdir(parents=True, exist_ok=True)

        copied = 0
        skipped = 0
        for item in src.rglob("*"):
            if item.is_dir():
                continue
            if item.name == ".last_backup_ts":
                continue
            if item.name.startswith(".git"):
                continue
            rel = item.relative_to(src)
            dest = DATA_DIR / rel
            if dest.exists() and not force:
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            copied += 1

        msg = f"ok: restored {copied} files"
        if skipped:
            msg += f" (skipped {skipped} existing — use --force to overwrite)"
        return msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore data/ from GitHub data-backup branch")
    parser.add_argument("--force", action="store_true", help="Overwrite existing local files")
    args = parser.parse_args()

    result = run_restore(force=args.force)
    logger.info("[data-restore] {}", result)
    if result.startswith("fail"):
        sys.exit(1)


if __name__ == "__main__":
    main()
