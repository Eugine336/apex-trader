"""Automatic provisioning of Apex's separate runtime-data repository.

The source checkout's data/ path is a stable runtime contract. On a fresh
single-user clone this module provisions the sibling public data repository and
routes that path to it before any database or trading subsystem is initialized.
Existing data is copied without deletion; conflicting remote files are preserved
in a timestamped backup before the local copy becomes active.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from scripts.git_identity import (
    DEFAULT_DATA_REPO_URL,
    git_ceiling_env,
    verify_dedicated_data_repo,
)

class DataRepoBootstrapError(RuntimeError):
    """Automatic data-repository provisioning failed safely."""


def _run_git(args: list[str], *, cwd: Path | None = None, timeout: int = 180) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=git_ceiling_env(cwd) if cwd else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DataRepoBootstrapError(f"git {' '.join(args[:2])} failed: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown git error").strip()
        raise DataRepoBootstrapError(f"git {' '.join(args[:2])} failed: {detail}")
    return result.stdout.strip()


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if path.is_symlink() or (callable(is_junction) and is_junction()):
        return True
    # pathlib.Path.is_junction() was added in Python 3.12. Windows Python
    # 3.11 still needs to recognize directory junction reparse points.
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise DataRepoBootstrapError(
                f"could not create Windows data junction: {(result.stderr or result.stdout).strip()}"
            )
    else:
        link.symlink_to(os.path.relpath(target, link.parent), target_is_directory=True)


def _files_equal(left: Path, right: Path) -> bool:
    with left.open("rb") as a, right.open("rb") as b:
        while True:
            aa, bb = a.read(1024 * 1024), b.read(1024 * 1024)
            if aa != bb:
                return False
            if not aa:
                return True


def _copy_local_data(source: Path, destination: Path, backup_root: Path) -> int:
    """Merge local files, preserving any replaced remote file in backup_root."""
    copied = 0
    for root, dirs, files in os.walk(source, followlinks=False):
        root_path = Path(root)
        dirs[:] = [name for name in dirs if name != ".git"]
        relative_dir = root_path.relative_to(source)
        (destination / relative_dir).mkdir(parents=True, exist_ok=True)
        for name in files:
            src = root_path / name
            if src.name == ".git":
                continue
            relative = src.relative_to(source)
            dst = destination / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() or dst.is_symlink():
                try:
                    same = (
                        src.is_file() and dst.is_file()
                        and not src.is_symlink() and not dst.is_symlink()
                        and src.stat().st_size == dst.stat().st_size
                        and _files_equal(src, dst)
                    )
                except OSError:
                    same = False
                if same:
                    continue
                preserved = backup_root / relative
                preserved.parent.mkdir(parents=True, exist_ok=True)
                if preserved.exists() or preserved.is_symlink():
                    raise DataRepoBootstrapError(f"bootstrap backup collision at {preserved}")
                shutil.move(str(dst), str(preserved))
            if src.is_symlink():
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                dst.symlink_to(os.readlink(src), target_is_directory=src.is_dir())
            elif src.is_file():
                shutil.copy2(src, dst)
            copied += 1
    return copied


def bootstrap_data_repo(
    *,
    source_root: str | Path | None = None,
    data_repo_url: str = DEFAULT_DATA_REPO_URL,
) -> Path:
    """Provision and route source_root/data to sibling apex-trader-data.

    Multi-tenant instances are excluded: APEX_DATA_DIR remains isolated.
    This function never edits .env, resets a repository, or force-pushes.
    """
    if os.getenv("APEX_DATA_DIR", "").strip():
        from runtime_paths import ensure_data_dir
        return ensure_data_dir()

    source = Path(source_root or Path(__file__).resolve().parent.parent).resolve()
    link = source / "data"
    target = source.parent / "apex-trader-data"

    from scripts.git_identity import normalize_repo_identity, DATA_REPO_IDENTITY
    if normalize_repo_identity(data_repo_url) != DATA_REPO_IDENTITY:
        raise DataRepoBootstrapError(
            f"refusing unexpected data repository identity: {data_repo_url}"
        )

    if _is_link(link):
        try:
            resolved = link.resolve(strict=True)
            if resolved != target.resolve():
                raise DataRepoBootstrapError(f"data link points to {resolved}, expected {target}")
        except OSError as exc:
            raise DataRepoBootstrapError(f"data link target is unavailable: {exc}") from exc
        ok, reason = verify_dedicated_data_repo(
            resolved, expected_repo_url=data_repo_url, require_remote=True
        )
        if not ok:
            raise DataRepoBootstrapError(f"linked data repository is unsafe: {reason}")
        return link

    if target.exists():
        if not (target / ".git").exists():
            raise DataRepoBootstrapError(
                f"{target} already exists but is not a Git clone; preserving it"
            )
        ok, reason = verify_dedicated_data_repo(
            target, expected_repo_url=data_repo_url, require_remote=True
        )
        if not ok:
            raise DataRepoBootstrapError(f"existing data repository is unsafe: {reason}")
    else:
        if link.exists() and not link.is_dir():
            raise DataRepoBootstrapError(f"{link} exists and is not a directory")
        _run_git(
            ["clone", "--branch", "main", "--single-branch", data_repo_url, str(target)],
            timeout=600,
        )
        ok, reason = verify_dedicated_data_repo(
            target, expected_repo_url=data_repo_url, require_remote=True
        )
        if not ok:
            raise DataRepoBootstrapError(f"cloned data repository failed identity check: {reason}")

    if link.exists():
        if not link.is_dir():
            raise DataRepoBootstrapError(f"{link} exists and is not a directory")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = source.parent / f"{source.name}-data-local-backup-{stamp}"
        conflict_backup = target / ".apex-bootstrap-preserved" / stamp
        try:
            if any(link.iterdir()):
                _copy_local_data(link, target, conflict_backup)
            link.rename(backup)
            try:
                _make_directory_link(link, target)
            except Exception:
                backup.rename(link)
                raise
        except Exception as exc:
            if backup.exists() and not link.exists():
                backup.rename(link)
            raise DataRepoBootstrapError(
                f"could not safely route existing data; original preserved: {exc}"
            ) from exc
    else:
        _make_directory_link(link, target)

    ok, reason = verify_dedicated_data_repo(
        link, expected_repo_url=data_repo_url, require_remote=True
    )
    if not ok:
        raise DataRepoBootstrapError(f"post-bootstrap verification failed: {reason}")
    return link
