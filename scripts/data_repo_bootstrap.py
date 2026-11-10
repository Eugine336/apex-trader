"""Automatic provisioning of Apex's separate runtime-data repository.

The source checkout's data/ path is a stable runtime contract. On a fresh
single-user clone this module provisions the sibling public data repository and
routes that path to it before any database or trading subsystem is initialized.
Existing sibling data is backed up and adopted in place. Distinct source and
sibling database files are retained independently; conflicts are not merged.
"""
from __future__ import annotations

import hashlib
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


def _remove_directory_link(link: Path) -> None:
    """Remove the link object only; never recurse into its target."""
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


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


def _copy_local_data(
    source: Path,
    destination: Path,
    backup_root: Path,
    *,
    preserve_conflicts: bool = False,
) -> int:
    """Copy local-only files; optionally keep both sides of every conflict.

    With preserve_conflicts=True, destination data remains untouched when the
    same relative path contains different bytes. Bootstrap retains the complete
    source directory separately, so conflicting databases are never merged.
    """
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
                if preserve_conflicts:
                    # The complete source tree is retained as a timestamped
                    # sibling backup after this merge. Keep the active target.
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


def _tree_manifest(root: Path) -> list[tuple[str, str, int]]:
    """Describe a directory tree without following symbolic links."""
    entries: list[tuple[str, str, int]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        traversable_dirs: list[str] = []
        for name in dirs:
            item = current_path / name
            relative = item.relative_to(root).as_posix()
            if _is_link(item):
                raise DataRepoBootstrapError(
                    f"nested directory link cannot be safely snapshotted: {item}"
                )
            entries.append((relative, "directory", 0))
            traversable_dirs.append(name)
        dirs[:] = traversable_dirs
        for name in files:
            item = current_path / name
            relative = item.relative_to(root).as_posix()
            if item.is_symlink():
                entries.append((relative, "link:" + os.readlink(item), 0))
            else:
                entries.append((relative, "file", item.stat().st_size))
    return sorted(entries)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_tree_verified(source: Path, destination: Path) -> None:
    """Create a complete independent backup and verify every copied file."""
    # Preflight before copying so a Windows junction inside the data tree
    # cannot cause copytree to traverse an unrelated external directory.
    source_entries = _tree_manifest(source)
    shutil.copytree(source, destination, symlinks=True)
    copied_entries = _tree_manifest(destination)
    if source_entries != copied_entries:
        raise DataRepoBootstrapError(
            f"pre-adoption backup inventory differs: {destination}"
        )
    for relative, kind, _size in source_entries:
        if kind != "file":
            continue
        if _sha256_file(source / relative) != _sha256_file(destination / relative):
            raise DataRepoBootstrapError(
                f"pre-adoption backup hash mismatch: {relative}"
            )


def _verify_git_lfs_and_tracking(target: Path) -> None:
    """Require main -> origin/main tracking and usable Git LFS filters."""
    branch = _run_git(["branch", "--show-current"], cwd=target)
    upstream = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=target,
    )
    if branch != "main" or upstream != "origin/main":
        raise DataRepoBootstrapError(
            f"data repository branch tracking is invalid: {branch!r} -> {upstream!r}"
        )
    _run_git(["lfs", "version"], cwd=target)
    for key in (
        "filter.lfs.clean", "filter.lfs.smudge",
        "filter.lfs.process", "filter.lfs.required",
    ):
        if not _run_git(["config", "--get", key], cwd=target):
            raise DataRepoBootstrapError(f"Git LFS filter is not configured: {key}")
    attributes = _run_git(
        ["check-attr", "filter", "--", "apex_events.db"], cwd=target
    )
    if "filter: lfs" not in attributes:
        raise DataRepoBootstrapError(
            "Git LFS is installed but the data repository does not assign "
            "the LFS filter to SQLite database files"
        )
    _run_git(["lfs", "env"], cwd=target)


def _adopt_existing_data_repo(target: Path, data_repo_url: str) -> Path:
    """Adopt a populated plain directory without replacing any existing data.

    A byte-for-byte verified sibling backup is created first. Git metadata is
    initialized in place; the remote tree is loaded into the index without
    checking out over existing files. checkout-index writes only missing paths.
    A recovery ref retains the original remote main commit.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = target.parent / f"{target.name}-pre-adoption-{stamp}"
    staging = target.parent / f".{target.name}-pre-adoption-{stamp}.tmp"
    if backup.exists() or staging.exists():
        raise DataRepoBootstrapError(f"pre-adoption backup path collision: {backup}")
    try:
        _copy_tree_verified(target, staging)
        staging.rename(backup)
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise DataRepoBootstrapError(
            f"could not create verified pre-adoption backup; original untouched: {exc}"
        ) from exc

    try:
        _run_git(["init"], cwd=target)
        _run_git(["remote", "add", "origin", data_repo_url], cwd=target)
        _run_git(["fetch", "--no-tags", "origin", "main"], cwd=target, timeout=900)
        _run_git(["lfs", "install", "--local"], cwd=target)
        _run_git(["read-tree", "origin/main"], cwd=target)
        _run_git(["update-ref", "refs/heads/main", "refs/remotes/origin/main"], cwd=target)
        _run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=target)
        _run_git(["config", "branch.main.remote", "origin"], cwd=target)
        _run_git(["config", "branch.main.merge", "refs/heads/main"], cwd=target)
        _run_git(
            ["update-ref", f"refs/apex-bootstrap/original-origin-main/{stamp}",
             "refs/remotes/origin/main"],
            cwd=target,
        )
        # No --force: existing files are never overwritten. Missing LFS-tracked
        # paths are materialized through the installed LFS smudge filter.
        _run_git(["checkout-index", "--all"], cwd=target, timeout=900)
        ok, reason = verify_dedicated_data_repo(
            target, expected_repo_url=data_repo_url, require_remote=True
        )
        if not ok:
            raise DataRepoBootstrapError(
                f"adopted repository identity verification failed: {reason}"
            )
        _verify_git_lfs_and_tracking(target)
    except Exception as exc:
        # Keep the original data tree in place. Move only newly-created Git
        # metadata aside so the next startup can retry adoption from a plain
        # directory; the verified full backup remains independently available.
        git_metadata = target / ".git"
        if git_metadata.exists():
            failed_metadata = target.parent / (
                f"{target.name}-failed-adoption-git-{stamp}"
            )
            if not failed_metadata.exists():
                git_metadata.rename(failed_metadata)
        raise DataRepoBootstrapError(
            f"data repository adoption failed; original data remains at {target}, "
            f"verified backup at {backup}: {exc}"
        ) from exc
    return backup


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
        _verify_git_lfs_and_tracking(resolved)
        return link

    if target.exists():
        if not (target / ".git").exists():
            _adopt_existing_data_repo(target, data_repo_url)
        ok, reason = verify_dedicated_data_repo(
            target, expected_repo_url=data_repo_url, require_remote=True
        )
        if not ok:
            raise DataRepoBootstrapError(f"existing data repository is unsafe: {reason}")
        _verify_git_lfs_and_tracking(target)
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
        _run_git(["lfs", "install", "--local"], cwd=target)
        _verify_git_lfs_and_tracking(target)

    source_backup: Path | None = None
    if link.exists():
        if not link.is_dir():
            raise DataRepoBootstrapError(f"{link} exists and is not a directory")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        source_backup = source.parent / f"{source.name}-data-local-backup-{stamp}"
        conflict_backup = target / ".apex-bootstrap-preserved" / stamp
        try:
            if any(link.iterdir()):
                _copy_local_data(
                    link, target, conflict_backup, preserve_conflicts=True
                )
            link.rename(source_backup)
            try:
                _make_directory_link(link, target)
            except Exception:
                source_backup.rename(link)
                raise
        except Exception as exc:
            if source_backup.exists() and not link.exists():
                source_backup.rename(link)
            raise DataRepoBootstrapError(
                f"could not safely route existing data; original preserved: {exc}"
            ) from exc
    else:
        _make_directory_link(link, target)

    try:
        ok, reason = verify_dedicated_data_repo(
            link, expected_repo_url=data_repo_url, require_remote=True
        )
        if not ok:
            raise DataRepoBootstrapError(
                f"post-bootstrap verification failed: {reason}"
            )
        _verify_git_lfs_and_tracking(target)
        if link.resolve() != target.resolve():
            raise DataRepoBootstrapError(
                f"junction resolution mismatch: {link.resolve()} != {target.resolve()}"
            )
    except Exception as exc:
        if _is_link(link):
            _remove_directory_link(link)
        if source_backup is not None and source_backup.exists() and not link.exists():
            source_backup.rename(link)
        raise DataRepoBootstrapError(
            f"post-bootstrap verification failed; source data restored where "
            f"applicable: {exc}"
        ) from exc
    return link
