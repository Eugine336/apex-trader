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
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

try:
    from scripts.git_identity import (
        SOURCE_REPO_IDENTITY,
        git_ceiling_env,
        normalize_repo_identity,
        verify_dedicated_data_repo,
    )
except ImportError:  # running as a bare script — scripts/ is on sys.path
    from git_identity import (  # type: ignore[no-redef]
        SOURCE_REPO_IDENTITY,
        git_ceiling_env,
        normalize_repo_identity,
        verify_dedicated_data_repo,
    )

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


# ── WAL checkpoint helper ─────────────────────────────────────────────────


def _checkpoint_wal_files(data_dir: Path) -> int:
    """Flush WAL journals into main .db files before a git sync.

    SQLite WAL mode keeps recent writes in a separate -wal file. Git
    excludes -wal/-shm files, so without an explicit checkpoint the
    committed .db files contain only stale data (often just the empty
    schema). Running ``PRAGMA wal_checkpoint(TRUNCATE)`` merges the WAL
    back and truncates it, making the .db file self-contained.

    Returns the number of databases successfully checkpointed.
    """
    checkpointed = 0
    for db_path in sorted(data_dir.glob("*.db")):
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            checkpointed += 1
        except Exception as exc:
            logger.debug("[data-sync] WAL checkpoint skipped for {}: {}", db_path.name, exc)
    if checkpointed:
        logger.info("[data-sync] checkpointed {} WAL files in {}", checkpointed, data_dir)
    return checkpointed


# ── Git helpers ──────────────────────────────────────────────────────────


def _get_remote_url() -> str | None:
    # Resolve the remote from the data directory rather than the current
    # working directory. ``_DATA_DIR`` may be a junction/symlink pointing at a
    # separate data repository, which owns the correct ``origin`` remote.
    #
    # ``GIT_CEILING_DIRECTORIES`` stops git from walking up into an enclosing
    # source checkout: if ``data/`` is a plain directory (no junction, no own
    # ``.git``) this returns ``None`` rather than the SOURCE repo's remote, so
    # the backup can never force-push onto the engine repo.
    data_dir = str(_DATA_DIR.resolve())
    try:
        url = (
            subprocess.check_output(
                ["git", "-C", data_dir, "remote", "get-url", "origin"],
                stderr=subprocess.DEVNULL,
                env=git_ceiling_env(data_dir),
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    if normalize_repo_identity(url) == SOURCE_REPO_IDENTITY:
        logger.critical(
            "[data-backup] REFUSING backup — origin of {} resolves to the SOURCE "
            "repo. Provision the dedicated data repo/junction before backing up.",
            data_dir,
        )
        return None
    return url


def _run_git(args: list[str], cwd: str) -> tuple[bool, str]:
    try:
        out = subprocess.check_output(
            ["git"] + args,
            cwd=cwd,
            stderr=subprocess.STDOUT,
            # Defense in depth: never let git discover a repo above ``cwd`` (the
            # source checkout) via parent-directory walking.
            env=git_ceiling_env(cwd),
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
        _checkpoint_wal_files(_DATA_DIR)
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


def _verify_data_repo_or_refuse(
    work_tree: str,
    *,
    operation: str,
    data_repo_url: str | None,
    require_remote: bool,
) -> str | None:
    """Return a refusal string if *work_tree* is not a safe dedicated data repo.

    ``sync_data_repo``/``compact_repo_history`` are designed for ``data/`` being
    a separate junction/clone with its own ``.git`` and an ``origin`` that points
    at ``Eugine336/apex-trader-data`` — every commit and the squash-compaction
    only ever touch that dedicated repo. If ``data/`` is instead a plain
    subdirectory of the source repo (no ``.git`` of its own), git discovers the
    enclosing source repo by walking up, and operating in place would commit raw
    data onto — or squash and force-push over — the *source* engine repo's real
    history. This guard (backed by :func:`git_identity.verify_dedicated_data_repo`)
    refuses rather than do that. ``None`` means the work tree is safe to operate
    on.
    """
    ok, reason = verify_dedicated_data_repo(
        work_tree,
        expected_repo_url=data_repo_url,
        require_remote=require_remote,
    )
    if ok:
        return None
    logger.critical(
        "[{}] REFUSING to operate on {} — {}. This guard prevents data "
        "maintenance from destroying the SOURCE engine repository. Provision the "
        "dedicated data repo/junction (its own .git + origin → {}) to enable it.",
        operation,
        work_tree,
        reason,
        data_repo_url or "the data repo",
    )
    return f"refused: {reason}"


def sync_data_repo(
    *,
    commit_message: str,
    exclude_patterns: list[str] | None = None,
    remote: str = "origin",
    branch: str = "main",
    push: bool = True,
    data_dir: Path | str | None = None,
    data_repo_url: str | None = None,
) -> str:
    """Commit + push a ``data/`` git work tree to its GitHub remote.

    Unlike :func:`run_backup` (which force-pushes a filtered snapshot to the
    ``data-backup`` orphan branch), this commits the data repository in place on
    its working branch — the everyday auto-sync. Raw market CSVs (and anything
    matching ``exclude_patterns``) are kept out of the commit via git pathspec
    exclusion. Idempotent: returns ``"no changes"`` when the tree is clean.

    *data_dir* selects which work tree to sync. It defaults to the module-level
    single-user junction (``data/``). The multi-tenant sync passes the shared
    data-repo junction here after mirroring per-user instance data into it.

    *data_repo_url*, when supplied, is the expected data-repo clone URL; the
    ``origin`` remote must normalize to its identity or the sync is refused.

    Non-fatal by contract — callers should treat any non-success string as a
    soft warning and keep running.
    """
    if exclude_patterns is None:
        exclude_patterns = list(_DEFAULT_EXCLUDE_PATTERNS)

    base = Path(data_dir) if data_dir is not None else _DATA_DIR
    if not base.is_dir():
        return "no data directory"

    work_tree = str(base.resolve())

    ok, _ = _run_git(["rev-parse", "--is-inside-work-tree"], work_tree)
    if not ok:
        return "data dir is not a git repo"

    # A commit-only sync (push=False, no remote) is safe as long as the tree is
    # its own dedicated repo, so only *require* a remote when we intend to push.
    # A configured remote is always checked: an origin pointing at the source
    # repo is refused even for commit-only.
    refusal = _verify_data_repo_or_refuse(
        work_tree,
        operation="data-sync",
        data_repo_url=data_repo_url,
        require_remote=push,
    )
    if refusal is not None:
        return refusal

    # Flush WAL journals into the main .db files so the committed databases
    # are self-contained — git excludes the -wal/-shm sidecars.
    _checkpoint_wal_files(base)

    # Stage everything except excluded patterns. Default git pathspec matching
    # treats '*' as crossing '/', so ':(exclude)*.csv' drops CSVs at any depth.
    add_args = ["add", "-A", "--force", "."]
    add_args += [f":(exclude){pat}" for pat in exclude_patterns]
    ok, add_out = _run_git(add_args, work_tree)
    if not ok:
        logger.warning("[data-sync] stage failed: {}", add_out)
        return f"stage failed: {add_out.splitlines()[0] if add_out else 'unknown'}"

    ok, diff_out = _run_git(["diff", "--cached", "--stat"], work_tree)
    if ok and not diff_out.strip():
        return "no changes"

    ok, commit_out = _run_git(["commit", "-m", commit_message], work_tree)
    if not ok:
        logger.warning("[data-sync] commit failed: {}", commit_out)
        return f"commit failed: {commit_out.splitlines()[0] if commit_out else 'unknown'}"

    if not push:
        return "committed (push skipped)"

    # Final identity assertion immediately before the push — the single most
    # dangerous operation. Even though the tree was verified up front, re-check
    # right here so the wrong repository can never be reached at push time.
    refusal = _verify_data_repo_or_refuse(
        work_tree,
        operation="data-sync",
        data_repo_url=data_repo_url,
        require_remote=True,
    )
    if refusal is not None:
        return refusal

    ok, push_out = _run_git(["push", remote, branch], work_tree)
    if not ok:
        logger.warning("[data-sync] push failed: {}", push_out)
        return f"push failed: {push_out.splitlines()[0] if push_out else 'unknown'}"

    return f"synced → {remote}/{branch}"


# ── History compaction ─────────────────────────────────────────────────────


def _repo_object_size_mb(work_tree: str) -> float | None:
    """Return the total git object size in MB from ``git count-objects``.

    Sums loose (``size:``) and packed (``size-pack:``) object sizes — both
    reported in KiB by the plain ``-v`` form. Using only ``size-pack`` would
    read 0 on a repo whose recent commits aren't packed yet; summing both gives
    a stable total regardless of pack state. ``None`` when unreadable.
    """
    ok, out = _run_git(["count-objects", "-v"], work_tree)
    if not ok:
        return None
    total_kib = 0.0
    found = False
    for line in out.splitlines():
        for key in ("size:", "size-pack:"):
            if line.startswith(key):
                try:
                    total_kib += float(line.split(":", 1)[1].strip())
                    found = True
                except ValueError:
                    pass
    return (total_kib / 1024.0) if found else None


def _recover_stranded_compaction(
    work_tree: str, branch: str, temp_branch: str
) -> None:
    """Undo a compaction that was interrupted while on the orphan temp branch.

    A previous ``compact_repo_history`` run can be killed after
    ``checkout --orphan _compact_temp`` (and possibly after the real branch was
    deleted) but before the temp branch is promoted back into place. That leaves
    HEAD stranded on ``_compact_temp`` — git then refuses to delete the branch
    HEAD points at, so the next orphan checkout aborts with "a branch named
    '_compact_temp' already exists", and any push of the real branch fails with
    "src refspec <branch> does not match any" because the real ref is gone.

    Best-effort recovery: if HEAD is on the temp branch, return to the real
    branch (restoring it from the temp branch when it was already deleted), then
    drop the stale temp branch. Never raises.
    """
    ok, cur = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], work_tree)
    on_temp = ok and cur.strip() == temp_branch
    if not on_temp:
        # Not stranded — just clear any leftover (non-current) temp branch.
        _run_git(["branch", "-D", temp_branch], work_tree)
        return
    real_ok, _ = _run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], work_tree
    )
    if real_ok:
        # Real branch still exists — switch back to it and drop the orphan.
        _run_git(["checkout", "-f", branch], work_tree)
        _run_git(["branch", "-D", temp_branch], work_tree)
    else:
        # Real branch was already deleted mid-compaction; the temp branch holds
        # the compacted tree — promote it into the real branch name in place.
        _run_git(["branch", "-m", branch], work_tree)


def compact_repo_history(
    data_dir: Path | str,
    *,
    max_repo_size_mb: float = 500.0,
    keep_commits: int = 10,
    branch: str = "main",
    data_repo_url: str | None = None,
) -> str:
    """Squash the data-repo history into a single commit when it grows too big.

    Every auto-sync commits full binary DB snapshots, so months of hourly
    commits accumulate thousands of binary diffs — ``git clone`` slows and the
    on-disk repo balloons. When the pack exceeds ``max_repo_size_mb`` *and*
    there are more than ``keep_commits`` commits, this rewrites history into a
    single commit holding the current tree, then force-pushes it.

    Best-effort by contract — returns a status string and never raises. On any
    mid-operation failure it restores the working branch so the tree is never
    left stranded on a dangling orphan branch.
    """
    base = Path(data_dir)
    if not base.is_dir():
        return "no data directory"
    work_tree = str(base.resolve())

    ok, _ = _run_git(["rev-parse", "--is-inside-work-tree"], work_tree)
    if not ok:
        return "not a git repo"

    # Structural guard up front: refuse before touching anything if the work
    # tree is not its own repo, or a configured origin points at the source
    # repo. A remote is not required here (small repos with no remote still hit
    # the size/keep gates below and short-circuit before any history rewrite).
    refusal = _verify_data_repo_or_refuse(
        work_tree,
        operation="repo-compact",
        data_repo_url=data_repo_url,
        require_remote=False,
    )
    if refusal is not None:
        return refusal

    # Heal a repo left stranded on the orphan temp branch by an interrupted
    # prior run before doing anything else — otherwise the size check may
    # short-circuit and leave the tree unable to push its real branch.
    _recover_stranded_compaction(work_tree, branch, "_compact_temp")

    size_mb = _repo_object_size_mb(work_tree)
    if size_mb is None:
        return "size unknown"
    if size_mb <= max_repo_size_mb:
        return f"ok ({size_mb:.1f} MB <= {max_repo_size_mb:.0f} MB)"

    ok, count_out = _run_git(["rev-list", "--count", "HEAD"], work_tree)
    try:
        commit_count = int(count_out.strip()) if ok else 0
    except ValueError:
        commit_count = 0
    if commit_count <= keep_commits:
        return f"skipped ({commit_count} commits <= keep {keep_commits})"

    # About to rewrite history and force-push. Re-verify the FULL identity now
    # (remote required, must normalize to the expected data repo, never the
    # source) immediately before the first destructive step.
    refusal = _verify_data_repo_or_refuse(
        work_tree,
        operation="repo-compact",
        data_repo_url=data_repo_url,
        require_remote=True,
    )
    if refusal is not None:
        return refusal

    temp_branch = "_compact_temp"
    # Drop any stale temp branch left by a previous interrupted run.
    _run_git(["branch", "-D", temp_branch], work_tree)

    ok, out = _run_git(["checkout", "--orphan", temp_branch], work_tree)
    if not ok:
        return f"orphan checkout failed: {out.splitlines()[0] if out else 'unknown'}"

    # Stage the full current tree, honouring the same CSV exclusion as sync.
    add_args = ["add", "-A", "--force", "."]
    add_args += [f":(exclude){pat}" for pat in _DEFAULT_EXCLUDE_PATTERNS]
    _run_git(add_args, work_tree)

    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ok, out = _run_git(
        [
            "commit",
            "-m",
            f"compacted history {ts} (was {commit_count} commits)",
            "--allow-empty",
        ],
        work_tree,
    )
    if not ok:
        # Restore the original branch so we never strand the tree on the orphan.
        _run_git(["checkout", "-f", branch], work_tree)
        _run_git(["branch", "-D", temp_branch], work_tree)
        return f"commit failed: {out.splitlines()[0] if out else 'unknown'}"

    # Promote the compacted commit onto the real branch WITHOUT deleting it
    # first. Force-moving the ref keeps the real branch valid at every instant,
    # so a crash here can never strand the tree on the orphan branch with no
    # real ref (which would break the next push with "src refspec <branch> does
    # not match any"). We then switch onto it and drop the orphan.
    ok_move, mv_out = _run_git(["branch", "-f", branch, temp_branch], work_tree)
    if not ok_move:
        _run_git(["checkout", "-f", branch], work_tree)
        _run_git(["branch", "-D", temp_branch], work_tree)
        return f"branch update failed: {mv_out.splitlines()[0] if mv_out else 'unknown'}"
    _run_git(["checkout", "-f", branch], work_tree)
    _run_git(["branch", "-D", temp_branch], work_tree)

    _run_git(["gc", "--aggressive", "--prune=now"], work_tree)
    new_size_mb = _repo_object_size_mb(work_tree)

    # Final identity re-check immediately before the force-push — the single
    # most destructive remote action. Refusing here (rather than pushing) is the
    # last guarantee that a squashed history can never overwrite the source repo.
    refusal = _verify_data_repo_or_refuse(
        work_tree,
        operation="repo-compact",
        data_repo_url=data_repo_url,
        require_remote=True,
    )
    if refusal is not None:
        return refusal

    ok, push_out = _run_git(["push", "--force", "origin", branch], work_tree)
    if not ok:
        logger.warning("[repo-compact] push failed: {}", push_out)
        return f"push failed: {push_out.splitlines()[0] if push_out else 'unknown'}"

    if new_size_mb is not None:
        return (
            f"compacted {size_mb:.1f} MB → {new_size_mb:.1f} MB "
            f"({commit_count} commits squashed)"
        )
    return f"compacted (was {size_mb:.1f} MB, {commit_count} commits squashed)"


# ── Multi-tenant: per-user namespaced sync ────────────────────────────────


def mirror_instances(
    instances_dir: Path | str,
    dest_root: Path | str,
    *,
    max_file_size_mb: float = _DEFAULT_MAX_FILE_SIZE_MB,
    exclude_patterns: list[str] | None = None,
) -> tuple[int, int, int]:
    """Mirror each per-user instance's ``data/`` into a per-user namespace.

    Copies ``<instances_dir>/user_<id>/data`` → ``<dest_root>/instances/user_<id>/data``
    for every user. The per-user namespace guarantees one user can never
    overwrite another user's data inside the shared data repo. Oversized /
    excluded files are skipped.

    Returns ``(users, copied, skipped)``.
    """
    if exclude_patterns is None:
        exclude_patterns = list(_DEFAULT_EXCLUDE_PATTERNS)

    src_root = Path(instances_dir)
    dst_base = Path(dest_root)
    if not src_root.is_dir():
        return (0, 0, 0)

    max_bytes = int(max_file_size_mb * 1024 * 1024)
    users = copied = skipped = 0
    for child in sorted(src_root.iterdir()):
        if not child.is_dir() or not child.name.startswith("user_"):
            continue
        src_data = child / "data"
        if not src_data.is_dir():
            continue
        users += 1
        dest_data = dst_base / "instances" / child.name / "data"
        dest_data.mkdir(parents=True, exist_ok=True)
        _checkpoint_wal_files(src_data)
        c, s = _copy_filtered(src_data, dest_data, max_bytes, exclude_patterns)
        copied += c
        skipped += s
    return (users, copied, skipped)


def sync_instances_to_data_repo(
    *,
    instances_dir: Path | str,
    junction_dir: Path | str,
    commit_message: str,
    exclude_patterns: list[str] | None = None,
    remote: str = "origin",
    branch: str = "main",
    push: bool = True,
    max_file_size_mb: float = _DEFAULT_MAX_FILE_SIZE_MB,
    data_repo_url: str | None = None,
) -> str:
    """Mirror every per-user instance's data into the shared data-repo junction
    (under ``instances/user_<id>/``), then commit + push the junction once.

    This is the multi-tenant analogue of the single-user :func:`sync_data_repo`:
    each spawned instance writes to an isolated, non-git working dir, so the
    control plane gathers those dirs into the one git-backed junction without
    any user overwriting another. Non-fatal by contract — returns a status
    string; callers treat any non-success value as a soft warning.
    """
    junction = Path(junction_dir)
    if not junction.is_dir():
        return "no data junction"

    users, copied, skipped = mirror_instances(
        instances_dir,
        junction,
        max_file_size_mb=max_file_size_mb,
        exclude_patterns=exclude_patterns,
    )
    if users == 0:
        return "no instances"

    result = sync_data_repo(
        commit_message=commit_message,
        exclude_patterns=exclude_patterns,
        remote=remote,
        branch=branch,
        push=push,
        data_dir=junction,
        data_repo_url=data_repo_url,
    )
    return f"{result} (users={users}, files={copied}, skipped={skipped})"


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
