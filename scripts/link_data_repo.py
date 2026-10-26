#!/usr/bin/env python3
"""One-time setup (Windows): link data/ to the dedicated apex-trader-data repo
the same way deploy/setup-vps.sh does on Linux -- clone it as a SIBLING
directory, then replace data/ with a directory junction pointing at it.

Why not `git init` inside data/ directly? Because apex-trader-data already
has its own real history (main, data-backup, and dev branches) -- cloning it
properly preserves that, whereas initializing in place would orphan it.

This script NEVER deletes anything with live content:
  - Existing data-repo files are NEVER overwritten. Identical files are left
    unchanged; missing files are copied only after every path is preflighted.
    Any non-identical collision aborts before data/ is renamed or relinked.
  - An existing data-repo checkout is never pulled or mutated by this helper.
  - The old data/ directory is renamed to data_preclone_backup/ and retained.
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
    if (sibling_dir.exists() and not (sibling_dir / ".git").exists()):
        print(f"ERROR: {sibling_dir} exists but is not a git repository. Leave data/ untouched and resolve it manually.")
        return 1
    if (sibling_dir / ".git").exists():
        # Never mutate an existing data repository during a migration. Pulling can
        # change remote state or conflict with local databases before we compare it.
        ok, status = _run(["git", "status", "--porcelain"], cwd=str(sibling_dir), dry_run=ns.dry_run)
        if not ok:
            print(f"ERROR: cannot inspect data-repo working tree: {status}")
            return 1
        if status.strip():
            print("ERROR: data-repo working tree is not clean. No pull, copy, rename or junction switch performed.")
            print(status)
            return 1
        ok, remote = _run(["git", "remote", "get-url", "origin"], cwd=str(sibling_dir), dry_run=ns.dry_run)
        if not ok or remote.strip().rstrip("/") != ns.data_repo_url.rstrip("/"):
            print("ERROR: existing sibling repo remote does not match the configured data repository.")
            print(f"Expected: {ns.data_repo_url}")
            print(f"Actual: {remote.strip()}")
            return 1
        print(f"[1/4] Existing data repo validated; not pulling or mutating it.")
    else:
        print(f"[1/4] Cloning {ns.data_repo_url} -> {sibling_dir}")
        ok, out = _run(["git", "clone", ns.data_repo_url, str(sibling_dir)], dry_run=ns.dry_run)
        if not ok:
            print(f"FAILED: {out}")
            return 1

    # --- Step 2: preflight all files before copying any state ---
    # Never overwrite an existing file, especially SQLite databases/WAL/SHM.
    # Conflicts abort before the source data/ is renamed or junctioned.
    files_to_copy: list[tuple[Path, Path]] = []
    conflicts: list[str] = []
    if data_dir.exists() and not _is_junction_or_symlink(data_dir):
        for source in sorted(data_dir.rglob("*")):
            if not source.is_file() or ".git" in source.parts:
                continue
            relative = source.relative_to(data_dir)
            target = sibling_dir / relative
            if target.exists():
                try:
                    same = target.is_file() and source.stat().st_size == target.stat().st_size
                    if same:
                        import hashlib
                        def _sha256(path: Path) -> str:
                            h = hashlib.sha256()
                            with path.open("rb") as handle:
                                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                    h.update(chunk)
                            return h.hexdigest()
                        same = _sha256(source) == _sha256(target)
                except OSError:
                    same = False
                if not same:
                    conflicts.append(str(relative))
            else:
                files_to_copy.append((source, target))
    if conflicts:
        print("[2/4] ERROR: conflicting files already exist in the data repository.")
        for name in conflicts:
            print(f"  CONFLICT (not overwritten): {name}")
        print("No files copied; source data/ has not been renamed or relinked. Back up and reconcile these files manually.")
        return 1
    print(f"[2/4] Preflight complete: {len(files_to_copy)} missing file(s) to copy; matching files remain unchanged.")
    if ns.dry_run:
        for source, target in files_to_copy:
            print(f"  [dry-run] copy {source} -> {target}")
    else:
        try:
            for source, target in files_to_copy:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            print("  copy complete; no existing target file was overwritten.")
        except Exception as exc:
            print(f"ERROR while copying state: {exc}")
            print("No junction switch performed; original data/ remains in place. Any newly copied files are non-overwriting additions.")
            return 1

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
