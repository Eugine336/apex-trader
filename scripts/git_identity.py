"""
APEX TRADER — Git repository identity verification.

Canonical, defense-in-depth guard that keeps every data-maintenance git
operation (auto-sync, history compaction, clean-start reset, orphan-branch
backup) confined to the *dedicated data repository*
(``Eugine336/apex-trader-data``) and structurally incapable of touching the
*source* repository (``Eugine336/apex-trader``).

Three independent layers protect against the production incident where a plain
``data/`` directory inside the source checkout let ``git -C data/ …`` walk up to
the source repo's ``.git`` and force-push away 1800+ commits of real history:

1. :func:`git_ceiling_env` — sets ``GIT_CEILING_DIRECTORIES`` so git cannot walk
   out of the work tree to discover an enclosing repository at all.
2. :func:`resolve_toplevel` — confirms the work tree is its own repository root,
   not a subdirectory of a repo reached by parent-directory discovery.
3. :func:`normalize_repo_identity` + :func:`verify_dedicated_data_repo` — confirm
   the ``origin`` remote resolves to the expected data-repo identity and, above
   all, is NOT the source repo.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# Canonical owner/repo identities (lowercase, no ``.git`` suffix).
SOURCE_REPO_IDENTITY = "eugine336/apex-trader"
DATA_REPO_IDENTITY = "eugine336/apex-trader-data"

# Default clone URL for the dedicated data repository. Mirrored by
# ``config.DataBackupConfig.data_repo_url`` and ``deploy/setup-vps.sh``.
DEFAULT_DATA_REPO_URL = "https://github.com/Eugine336/apex-trader-data.git"


# ── Pure helpers (no subprocess) ───────────────────────────────────────────


def normalize_repo_identity(url: str | None) -> str | None:
    """Return the lowercase ``owner/repo`` for a git remote *url*, else ``None``.

    Collapses every equivalent remote form to one comparable identity, e.g.

    * ``https://github.com/Eugine336/apex-trader-data.git``
    * ``https://github.com/Eugine336/apex-trader-data``
    * ``git@github.com:Eugine336/apex-trader-data.git``
    * ``ssh://git@github.com/Eugine336/apex-trader-data.git``

    all normalize to ``eugine336/apex-trader-data``. A trailing ``.git`` and any
    trailing slashes are stripped and the result is lowercased so comparisons are
    case- and suffix-insensitive.
    """
    if not url:
        return None
    text = url.strip()
    if not text:
        return None
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    # Split on both '/' and ':' so scp-style ``host:owner/repo`` is handled the
    # same as URL forms; the last two non-empty components are owner + repo.
    parts = [p for p in re.split(r"[/:]", text) if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[-2], parts[-1]
    if not owner or not repo:
        return None
    return f"{owner.lower()}/{repo.lower()}"


def is_source_repo_identity(identity: str | None) -> bool:
    """True when *identity* is the source engine repo (``eugine336/apex-trader``)."""
    return identity is not None and identity == SOURCE_REPO_IDENTITY


def git_ceiling_env(
    work_tree: str | Path, base_env: dict[str, str] | None = None
) -> dict[str, str]:
    """Return an environment dict that stops git parent-directory discovery.

    ``GIT_CEILING_DIRECTORIES`` is set to the *parent* of *work_tree* (resolved),
    so a git command run from inside *work_tree* finds a repository only if
    ``work_tree/.git`` exists — it can never climb into an enclosing source
    checkout. This is the cheapest, first line of defense; the identity checks
    below are the second.
    """
    env = dict(base_env if base_env is not None else os.environ)
    try:
        parent = str(Path(work_tree).resolve().parent)
    except OSError:
        parent = str(Path(work_tree).parent)
    existing = env.get("GIT_CEILING_DIRECTORIES", "")
    env["GIT_CEILING_DIRECTORIES"] = (
        f"{parent}{os.pathsep}{existing}" if existing else parent
    )
    return env


# ── Subprocess-backed resolution ───────────────────────────────────────────


def _git_capture(args: list[str], work_tree: str | Path) -> str | None:
    """Run ``git -C <work_tree> <args>`` with the ceiling env; stdout or ``None``."""
    try:
        out = subprocess.run(
            ["git", "-C", str(work_tree), *args],
            capture_output=True,
            text=True,
            env=git_ceiling_env(work_tree),
        )
    except (FileNotFoundError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def resolve_toplevel(work_tree: str | Path) -> str | None:
    """Return ``git rev-parse --show-toplevel`` for *work_tree*, else ``None``."""
    return _git_capture(["rev-parse", "--show-toplevel"], work_tree)


def resolve_remote_identity(
    work_tree: str | Path, remote: str = "origin"
) -> str | None:
    """Return the normalized ``owner/repo`` of *work_tree*'s *remote*, else ``None``."""
    return normalize_repo_identity(
        _git_capture(["remote", "get-url", remote], work_tree)
    )


def resolve_toplevel_unrestricted(work_tree: str | Path) -> str | None:
    """Like :func:`resolve_toplevel` but WITHOUT the ceiling env.

    Reveals the repository git would *actually* discover by walking parent
    directories — used only for diagnostics/boot reporting so the topology
    banner can state explicitly that ``data/`` resolves into the SOURCE repo.
    Never gate a destructive operation on this; the ceiling-restricted variants
    are the safety-critical ones.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(work_tree), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def verify_dedicated_data_repo(
    work_tree: str | Path,
    *,
    remote: str = "origin",
    expected_repo_url: str | None = None,
    require_remote: bool = True,
) -> tuple[bool, str]:
    """Verify *work_tree* is a dedicated data repo safe for destructive git ops.

    Returns ``(ok, reason)``. ``ok`` is ``True`` only when every enforced layer
    passes:

    * *work_tree* exists and is the **root** of its own repository — its
      ``rev-parse --show-toplevel`` resolves to *work_tree* itself, not an
      enclosing repo reached by parent-directory discovery.
    * If a *remote* is configured (always required when ``require_remote``), its
      URL must resolve to an ``owner/repo`` identity that is **not** the source
      repo. When *expected_repo_url* is given, the identity must additionally
      match it exactly.

    The source-repo rejection is unconditional and is the invariant that makes
    data maintenance structurally incapable of operating on
    ``Eugine336/apex-trader``.
    """
    wt = Path(work_tree)
    if not wt.is_dir():
        return False, f"work tree does not exist: {wt}"

    top = resolve_toplevel(wt)
    if top is None:
        return False, "not a git repository"
    try:
        if Path(top).resolve() != wt.resolve():
            return False, (
                f"not a dedicated repo — git resolved {wt} into enclosing "
                f"repository {top}"
            )
    except OSError:
        return False, "could not resolve repository top-level"

    identity = resolve_remote_identity(wt, remote)
    if identity is None:
        if require_remote:
            return False, f"no '{remote}' remote configured"
        return True, "dedicated repo (no remote to verify)"

    if identity == SOURCE_REPO_IDENTITY:
        return False, (
            f"remote '{remote}' resolves to the SOURCE repo '{identity}' — refusing"
        )

    expected_identity = normalize_repo_identity(expected_repo_url)
    if expected_identity is not None and identity != expected_identity:
        return False, (
            f"remote '{remote}' is '{identity}', expected '{expected_identity}'"
        )
    return True, identity


def data_repo_ready(
    work_tree: str | Path,
    *,
    remote: str = "origin",
    data_repo_url: str | None = None,
) -> tuple[bool, str]:
    """Convenience wrapper: is *work_tree* a valid, remote-backed data repo?"""
    return verify_dedicated_data_repo(
        work_tree,
        remote=remote,
        expected_repo_url=data_repo_url,
        require_remote=True,
    )


def describe_repo_topology(
    source_root: str | Path,
    data_dir: str | Path,
    *,
    remote: str = "origin",
) -> dict[str, str]:
    """Return the source/data repo roots and remote identities for boot logging."""
    src = Path(source_root)
    dd = Path(data_dir)
    return {
        "SOURCE_REPO_ROOT": str(resolve_toplevel(src) or src),
        "SOURCE_REMOTE": resolve_remote_identity(src, remote) or "?",
        "DATA_REPO_ROOT": str(resolve_toplevel(dd) or dd),
        "DATA_REMOTE": resolve_remote_identity(dd, remote) or "?",
    }


def evaluate_topology(
    source_root: str | Path,
    data_dir: str | Path,
    *,
    remote: str = "origin",
    data_repo_url: str | None = None,
) -> dict[str, object]:
    """Assess the source/data repo topology for the boot-time safety gate.

    Returns a structured dict. ``auto_sync_ok`` is ``True`` only when the data
    dir is a valid dedicated data repo (own root + remote resolves to the data
    repo, never the source) AND it does not, when discovered without the ceiling
    guard, resolve into the source repository.

    ``data_resolves_to_source`` uses the *unrestricted* probe to surface the
    exact dangerous condition from the incident (``data/`` → source ``.git``) so
    the banner can report expected-vs-actual, independent of the ceiling guard
    that otherwise hides it.
    """
    src = Path(source_root)
    dd = Path(data_dir)

    source_root_actual = resolve_toplevel_unrestricted(src)
    data_root_ceilinged = resolve_toplevel(dd)  # safe view (None if plain-in-source)
    data_root_actual = resolve_toplevel_unrestricted(dd)  # what git would discover

    data_resolves_to_source = False
    if data_root_actual is not None and source_root_actual is not None:
        try:
            data_resolves_to_source = (
                Path(data_root_actual).resolve() == Path(source_root_actual).resolve()
            )
        except OSError:
            data_resolves_to_source = False

    ready, reason = data_repo_ready(dd, remote=remote, data_repo_url=data_repo_url)
    auto_sync_ok = bool(ready) and not data_resolves_to_source
    if data_resolves_to_source:
        reason = "data/ resolves to the source repository"

    return {
        "source_root": str(source_root_actual or src),
        "source_remote": resolve_remote_identity(src, remote) or "?",
        "data_path": str(dd),
        "data_git_root": str(data_root_ceilinged or data_root_actual or "none"),
        "data_remote": resolve_remote_identity(dd, remote) or "?",
        "expected_data_identity": normalize_repo_identity(data_repo_url)
        or DATA_REPO_IDENTITY,
        "source_git_safe": not data_resolves_to_source,
        "data_git_safe": bool(ready),
        "data_resolves_to_source": data_resolves_to_source,
        "auto_sync_ok": auto_sync_ok,
        "reason": reason,
    }


def render_topology_banner(
    source_root: str | Path,
    data_dir: str | Path,
    *,
    remote: str = "origin",
    data_repo_url: str | None = None,
) -> tuple[bool, str]:
    """Return ``(auto_sync_ok, banner_text)`` — an unambiguous boot report.

    The banner makes the topology and its verdict explicit so an operator never
    has to infer safety from a stack trace. On failure it prints expected-vs-
    actual and states that auto-sync is disabled and no git mutation is allowed.
    """
    t = evaluate_topology(
        source_root, data_dir, remote=remote, data_repo_url=data_repo_url
    )
    expected = t["expected_data_identity"]

    if t["auto_sync_ok"]:
        lines = [
            "REPOSITORY TOPOLOGY",
            "────────────────────────────────",
            f"Source repository: {t['source_remote']}",
            f"Data repository:   {t['data_remote']}",
            f"Data path:         {t['data_path']}",
            f"Data Git root:     {t['data_git_root']}",
            f"Source Git:        {'SAFE' if t['source_git_safe'] else 'UNSAFE'}",
            f"Data Git:          {'SAFE' if t['data_git_safe'] else 'UNSAFE'}",
            "Auto-sync:         ENABLED",
            "────────────────────────────────",
        ]
        return True, "\n".join(lines)

    actual = (
        "the SOURCE repository"
        if t["data_resolves_to_source"]
        else t["data_git_root"]
    )
    lines = [
        "CRITICAL: UNSAFE REPOSITORY TOPOLOGY",
        "",
        f"{t['reason']}.",
        "",
        "Expected:",
        f"  {t['data_path']}",
        f"      -> {expected}",
        "",
        "Actual:",
        f"  {t['data_path']}",
        f"      -> {actual}",
        "",
        "AUTO-SYNC DISABLED.",
        "No Git mutation permitted.",
    ]
    return False, "\n".join(lines)
