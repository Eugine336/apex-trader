"""
APEX TRADER — Data Backup to GitHub

Pushes irreplaceable runtime data (decision journal, trade journal, event
store, shadow contracts) to a ``data-backup`` orphan branch on the repo's
GitHub remote.

Files that exceed ``max_file_size_mb`` or match ``exclude_patterns`` are
silently skipped — this avoids GitHub's 100 MB file-size limit and keeps
re-downloadable market-data CSVs out of the backup.

Usage:
    python scripts/backup_data.py
    python scripts/backup_data.py --force
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from loguru import logger

_DATA_DIR = Path("data")
_BRANCH = "data-backup"
_DEFAULT_MAX_FILE_SIZE_MB: float = 95.0
_DEFAULT_EXCLUDE_PATTERNS: list[str] = ["*.csv"]


# ── Filtering helpers ────────────────────────────────────────────────────


def _should_skip(
    file_path: Path,
    max_bytes: int,
    exclude_patterns: list[str],
) -> str | None:
    """Return a reason string if *file_path* should be excluded, else None."""
    name = file_path.name
    for pat in exclude_patterns:
        if fnmatch.fnmatch(name, pat):
            return f"matches exclude pattern '{pat}'"
    try:
        size = file_path.stat().st_size
    except OSError:
        return "stat failed"
    if size > max_bytes:
        mb = size / (1024 * 1024)
        return f"size {mb:.1f} MB exceeds limit"
    return None


def _copy_filtered(
    src: Path,
    dest: Path,
    max_bytes: int,
    exclude_patterns: list[str],
) -> tuple[int, int]:
    """Copy *src* tree to *dest*, skipping oversized / excluded files.

    Returns ``(copied, skipped)`` counts.
    """
    copied = skipped = 0
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        dest_root = dest / rel_root
        dest_root.mkdir(parents=True, exist_ok=True)
        for fname in files:
            src_file = Path(root) / fname
            reason = _should_skip(src_file, max_bytes, exclude_patterns)
            if reason:
                logger.debug("[backup] skip {}: {}", src_file, reason)
                skipped += 1
                continue
            shutil.copy2(str(src_file), str(dest_root / fname))
            copied += 1
    return copied, skipped


# ── Git helpers ──────────────────────────────────────────────────────────


def _get_remote_url() -> str | None:
    # Resolve the remote from the data directory rather than the current
    # working directory. ``_DATA_DIR`` may be a junction/symlink pointing at a
    # separate data repository, which owns the correct ``origin`` remote.
    data_dir = str(_DATA_DIR.resolve())
    try:
        return (
            subprocess.check_output(
                ["git", "-C", data_dir, "remote", "get-url", "origin"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _run_git(args: list[str], cwd: str) -> tuple[bool, str]:
    try:
        out = subprocess.check_output(
            ["git"] + args,
            cwd=cwd,
            stderr=subprocess.STDOUT,
        ).decode()
        return True, out
    except subprocess.CalledProcessError as exc:
        return False, exc.output.decode() if exc.output else str(exc)


# ── Public API ───────────────────────────────────────────────────────────


def run_backup(
    *,
    force: bool = False,
    max_file_size_mb: float = _DEFAULT_MAX_FILE_SIZE_MB,
    exclude_patterns: list[str] | None = None,
) -> str:
    """Push filtered ``data/`` contents to the *data-backup* branch.

    Returns a short summary string.
    """
    if exclude_patterns is None:
        exclude_patterns = list(_DEFAULT_EXCLUDE_PATTERNS)

    if not _DATA_DIR.is_dir():
        return "no data directory"

    remote_url = _get_remote_url()
    if not remote_url:
        return "no git remote"

    max_bytes = int(max_file_size_mb * 1024 * 1024)

    tmpdir = tempfile.mkdtemp(prefix="apex_backup_")
    try:
        _run_git(["init"], tmpdir)
        _run_git(["checkout", "--orphan", _BRANCH], tmpdir)

        dest = Path(tmpdir) / "data"
        copied, skipped = _copy_filtered(
            _DATA_DIR, dest, max_bytes, exclude_patterns,
        )

        if copied == 0:
            return "nothing to back up"

        logger.info(
            "[data-backup] copied {} files, skipped {} (>{:.0f} MB or excluded)",
            copied, skipped, max_file_size_mb,
        )

        _run_git(["add", "-A"], tmpdir)

        ok, diff_out = _run_git(["diff", "--cached", "--stat"], tmpdir)
        if not force and ok and not diff_out.strip():
            return "no changes"

        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _run_git(
            ["commit", "-m", f"data backup {ts}", "--allow-empty"],
            tmpdir,
        )

        _run_git(["remote", "add", "origin", remote_url], tmpdir)
        ok, push_out = _run_git(
            ["push", "--force", "origin", _BRANCH], tmpdir,
        )
        if not ok:
            logger.warning("[data-backup] push failed: {}", push_out)
            return f"push failed: {push_out.splitlines()[0] if push_out else 'unknown'}"

        return f"backed up {copied} files ({skipped} skipped)"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def sync_data_repo(
    *,
    commit_message: str,
    exclude_patterns: list[str] | None = None,
    remote: str = "origin",
    branch: str = "main",
    push: bool = True,
) -> str:
    """Commit + push the live ``data/`` junction to its own GitHub remote.

    Unlike :func:`run_backup` (which force-pushes a filtered snapshot to the
    ``data-backup`` orphan branch), this commits the data repository in place on
    its working branch — the everyday auto-sync. Raw market CSVs (and anything
    matching ``exclude_patterns``) are kept out of the commit via git pathspec
    exclusion. Idempotent: returns ``"no changes"`` when the tree is clean.

    Non-fatal by contract — callers should treat any non-success string as a
    soft warning and keep running.
    """
    if exclude_patterns is None:
        exclude_patterns = list(_DEFAULT_EXCLUDE_PATTERNS)

    if not _DATA_DIR.is_dir():
        return "no data directory"

    data_dir = str(_DATA_DIR.resolve())

    ok, _ = _run_git(["rev-parse", "--is-inside-work-tree"], data_dir)
    if not ok:
        return "data dir is not a git repo"

    # Stage everything except excluded patterns. Default git pathspec matching
    # treats '*' as crossing '/', so ':(exclude)*.csv' drops CSVs at any depth.
    add_args = ["add", "-A", "."]
    add_args += [f":(exclude){pat}" for pat in exclude_patterns]
    ok, add_out = _run_git(add_args, data_dir)
    if not ok:
        logger.warning("[data-sync] stage failed: {}", add_out)
        return f"stage failed: {add_out.splitlines()[0] if add_out else 'unknown'}"

    ok, diff_out = _run_git(["diff", "--cached", "--stat"], data_dir)
    if ok and not diff_out.strip():
        return "no changes"

    ok, commit_out = _run_git(["commit", "-m", commit_message], data_dir)
    if not ok:
        logger.warning("[data-sync] commit failed: {}", commit_out)
        return f"commit failed: {commit_out.splitlines()[0] if commit_out else 'unknown'}"

    if not push:
        return "committed (push skipped)"

    ok, push_out = _run_git(["push", remote, branch], data_dir)
    if not ok:
        logger.warning("[data-sync] push failed: {}", push_out)
        return f"push failed: {push_out.splitlines()[0] if push_out else 'unknown'}"

    return f"synced → {remote}/{branch}"


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up APEX data to GitHub")
    parser.add_argument(
        "--force", action="store_true", help="Push even if nothing changed",
    )
    args = parser.parse_args()

    result = run_backup(force=args.force)
    logger.info("[data-backup] {}", result)


if __name__ == "__main__":
    main()
