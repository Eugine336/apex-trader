"""
APEX TRADER — Restore Data from GitHub Backup

Pulls the ``data-backup`` branch and copies its contents into the local
``data/`` directory.

Usage:
    python scripts/restore_data.py
    python scripts/restore_data.py --force   # overwrite existing files
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from loguru import logger

# Ensure the repo root is importable whether this runs as ``scripts.restore_data``
# or as a bare ``python scripts/restore_data.py`` invocation.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from runtime_paths import data_dir_is_safe  # noqa: E402

_DATA_DIR = Path("data")
_BRANCH = "data-backup"


def _get_remote_url() -> str | None:
    try:
        return (
            subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def run_restore(*, force: bool = False) -> str:
    """Pull *data-backup* branch and copy files into ``data/``."""
    remote_url = _get_remote_url()
    if not remote_url:
        return "no git remote"

    tmpdir = tempfile.mkdtemp(prefix="apex_restore_")
    try:
        result = subprocess.run(
            ["git", "clone", "--branch", _BRANCH, "--depth", "1", remote_url, tmpdir],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            msg = result.stderr.strip().splitlines()[0] if result.stderr else "unknown"
            return f"clone failed: {msg}"

        src = Path(tmpdir) / "data"
        if not src.is_dir():
            return "backup branch has no data/ directory"

        # Never materialize a plain ``data/`` inside the source checkout — the
        # operator must provision the junction/dedicated data repo first.
        safe, reason = data_dir_is_safe(_DATA_DIR)
        if not safe:
            logger.critical("[restore] REFUSING to restore — {}", reason)
            return f"refused: {reason}"
        _DATA_DIR.mkdir(parents=True, exist_ok=True)

        copied = skipped = 0
        for item in src.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(src)
            dest = _DATA_DIR / rel
            if dest.exists() and not force:
                logger.debug("[restore] skip existing: {}", rel)
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(dest))
            copied += 1

        return f"restored {copied} files ({skipped} skipped, existing)"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore APEX data from GitHub backup")
    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing local files",
    )
    args = parser.parse_args()

    result = run_restore(force=args.force)
    logger.info("[data-restore] {}", result)


if __name__ == "__main__":
    main()
