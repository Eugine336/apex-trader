#!/usr/bin/env python3
"""One-time setup (Windows): link data/ to the dedicated apex-trader-data repo
the same way deploy/setup-vps.sh does on Linux -- clone it as a SIBLING
directory, then replace data/ with a directory junction pointing at it.

Why not `git init` inside data/ directly? Because apex-trader-data already
has its own real history (main, data-backup, and dev branches) -- cloning it
properly preserves that, whereas initializing in place would orphan it.

This script NEVER deletes anything with live content:
  - If data/ has real files in it, they are copied into the fresh
    apex-trader-data clone first (so nothing is lost), then the OLD data/
    directory is renamed to data_preclone_backup/ rather than removed. You
    can compare and delete that backup yourself once you're satisfied.
  - If data/ is already a junction/symlink, or already its own git repo,
    the script does nothing and reports that.

Usage (run from the apex-trader repo root, e.g.
    C:\\Users\\Administrator\\apex-trader):

    python scripts/link_data_repo.py
    python scripts/link_data_repo.py --dry-run
    python scripts/link_data_repo.py --data-repo-url https://github.com/Eugine336/apex-trader-data.git
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_DEFAULT_REMOTE_URL = "https://github.com/Eugine336/apex-trader-data.git"


def _run(args: list[str], cwd: str | None = None, dry_run: bool = False) -> tuple[bool, str]:
    printable = " ".join(args)
    if dry_run:
        print(f"  [dry-run] {printable}")
        return True, ""
    try:
        out = subprocess.check_output(args, cwd=cwd, stderr=subprocess.STDOUT).decode(errors="replace")
        return True, out
    except subprocess.CalledProcessError as exc:
        return False, (exc.output.decode(errors="replace") if exc.output else str(exc))
    except FileNotFoundError as exc:
        return False, str(exc)


def _is_junction_or_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Link data/ to the dedicated apex-trader-data repo (sibling clone + junction)")
    parser.add_argument("--data-repo-url", default=_DEFAULT_REMOTE_URL)
    parser.add_argument("--repo-root", default=".", help="apex-trader repo root (default: current directory)")
    parser.add_argument("--dry-run", action="store_true")
    ns = parser.parse_args()

    repo_root = Path(ns.repo_root).resolve()
    data_dir = repo_root / "data"
    sibling_dir = repo_root.parent / "apex-trader-data"

    print(f"repo root:    {repo_root}")
    print(f"data dir:     {data_dir}")
    print(f"sibling repo: {sibling_dir}")
    print()

    if not data_dir.exists():
        print("data/ doesn't exist yet -- nothing to preserve, will just clone + junction.")
    elif _is_junction_or_symlink(data_dir):
        print("data/ is already a junction/symlink. Nothing to do.")
        return 0
    elif (data_dir / ".git").exists():
        print("WARNING: data/ is already its own git repo (not a junction). Leaving it as-is --")
        print("         this script only handles the plain-directory case. If this is stale,")
        print("         handle it manually.")
        return 0

    # --- Step 1: clone apex-trader-data as a sibling, if not already there ---
    if (sibling_dir / ".git").exists():
        print(f"[1/4] {sibling_dir} already exists as a git repo -- pulling latest instead of cloning.")
        _run(["git", "pull"], cwd=str(sibling_dir), dry_run=ns.dry_run)
    else:
        print(f"[1/4] Cloning {ns.data_repo_url} -> {sibling_dir}")
        ok, out = _run(["git", "clone", ns.data_repo_url, str(sibling_dir)], dry_run=ns.dry_run)
        if not ok:
            print(f"FAILED: {out}")
            return 1

    # --- Step 2: copy current live data/ contents into the sibling clone ---
    if data_dir.exists() and any(data_dir.iterdir()) and not ns.dry_run:
        print(f"[2/4] Copying live files from {data_dir} into {sibling_dir} (never overwritten, only added/updated)")
        for item in data_dir.iterdir():
            if item.name == ".git":
                continue
            dest = sibling_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        print("  copy complete.")
    elif ns.dry_run:
        print(f"[2/4] [dry-run] would copy live files from {data_dir} into {sibling_dir}")
    else:
        print("[2/4] data/ is empty or missing -- nothing to copy.")

    # --- Step 3: move the old plain data/ aside (never delete outright) ---
    backup_dir = repo_root / "data_preclone_backup"
    if data_dir.exists():
        print(f"[3/4] Renaming old {data_dir} -> {backup_dir} (kept for your own review/deletion)")
        if not ns.dry_run:
            if backup_dir.exists():
                print(f"  ERROR: {backup_dir} already exists from a previous run. Resolve manually before re-running.")
                return 1
            data_dir.rename(backup_dir)
        else:
            print(f"  [dry-run] would rename {data_dir} -> {backup_dir}")
    else:
        print("[3/4] No existing data/ to move aside.")

    # --- Step 4: create the junction ---
    print(f"[4/4] Creating junction: {data_dir} -> {sibling_dir}")
    if sys.platform == "win32":
        ok, out = _run(["cmd", "/c", "mklink", "/J", str(data_dir), str(sibling_dir)], dry_run=ns.dry_run)
    else:
        if not ns.dry_run:
            data_dir.symlink_to(sibling_dir, target_is_directory=True)
            ok, out = True, ""
        else:
            print(f"  [dry-run] would symlink {data_dir} -> {sibling_dir}")
            ok, out = True, ""
    if not ok:
        print(f"FAILED: {out}")
        print(f"  Your live files are safe in {backup_dir} -- nothing was lost.")
        return 1

    print("\nDone. data/ is now a junction into the dedicated apex-trader-data repo.")
    print(f"Once you've confirmed everything looks right, you can delete {backup_dir}.")
    print("sync_data_repo()/compact_repo_history() will now recognize data/ as dedicated and resume normal auto-sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
