"""
APEX TRADER — Clean-Start Helpers

Two best-effort, non-fatal startup steps:

1. ``sync_clean_state_from_remote`` — fetch + hard-reset the data repository so
   the local junction matches remote truth even when local/remote histories have
   diverged.

2. ``purge_stale_learned_data`` — remove stale learned/adaptive artifacts only
   when the local artifact schema version differs from the code schema version.
   Version state is stored in a local file at the code-repo root
   (``.local_schema_version``), outside the ``data/`` junction.

Both functions swallow their own errors and return a short summary string;
callers should log the result and continue regardless.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from loguru import logger

from config import SCHEMA_VERSION

# Learned / adaptive artifacts that encoded the old structure-biased weights.
# These are safe to delete — they are regenerated empty on first write.
_STALE_LEARNED_FILES: tuple[str, ...] = (
    "scoring_weights.json",
    "zone_edge.json",
    "ml_pair_profiles.json",
    "ml_session_profiles.json",
    "ml_regime_strategies.json",
    "gate_tuning.json",
    "planner_config.json",
    "governor_state.json",
    "outcome_feedback.jsonl",
    "plan_journal.jsonl",
    "trade_journal.db",
    "post_close_tracker.db",
    "module_governor.db",
    "signal_ledger.db",
    "tuner_audit.db",
    "counterfactual.db",
    "interaction_discovery.db",
    "param_evolution.db",
    "signal_discovery.db",
    "capital_allocation.db",
    "execution_profiles.db",
    "regime_detection.db",
    "risk_management.db",
    "behavior_discovery.db",
)

_CODE_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_SCHEMA_VERSION_NAME = ".local_schema_version"


def _first_line(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0] if lines else "unknown"


def _is_git_repo(data_dir: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(data_dir.resolve()), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (FileNotFoundError, OSError):
        return False


def _read_local_schema_version(version_file: Path) -> str | None:
    try:
        value = version_file.read_text(encoding="utf-8").strip()
        return value or None
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("[clean-start] schema-version read failed: {}", exc)
        return None


def _write_local_schema_version(version_file: Path, schema_version: str) -> bool:
    try:
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(f"{schema_version}\n", encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("[clean-start] schema-version write failed: {}", exc)
        return False


def sync_clean_state_from_remote(
    data_dir: str = "data",
    *,
    remote: str = "origin",
    branch: str = "main",
) -> str:
    """Fetch + hard-reset the data repo so it matches the remote branch.

    Non-fatal. Returns a short summary string.
    """
    d = Path(data_dir)
    if not d.is_dir():
        return "no data directory"
    if not _is_git_repo(d):
        return "data dir is not a git repo"
    try:
        fetch_out = subprocess.run(
            ["git", "-C", str(d.resolve()), "fetch", remote],
            capture_output=True,
            text=True,
        )
        if fetch_out.returncode != 0:
            reason = _first_line(fetch_out.stderr or fetch_out.stdout or "unknown")
            logger.warning("[clean-start] data-repo fetch failed: {}", reason)
            return f"fetch failed: {reason}"

        candidate_branches: list[str] = [branch]
        if branch == "main":
            candidate_branches.append("master")

        last_reason = "unknown"
        for candidate in candidate_branches:
            reset_out = subprocess.run(
                ["git", "-C", str(d.resolve()), "reset", "--hard", f"{remote}/{candidate}"],
                capture_output=True,
                text=True,
            )
            if reset_out.returncode == 0:
                if candidate != branch:
                    logger.warning(
                        "[clean-start] synced from fallback branch {}/{} (configured branch missing)",
                        remote,
                        candidate,
                    )
                return f"synced remote state ({candidate})"
            last_reason = _first_line(reset_out.stderr or reset_out.stdout or "unknown")

        logger.warning("[clean-start] data-repo hard reset failed: {}", last_reason)
        return f"reset failed: {last_reason}"
    except Exception as exc:
        logger.warning("[clean-start] data-repo sync errored: {}", exc)
        return f"error: {exc}"


def purge_stale_learned_data(
    data_dir: str = "data",
    *,
    schema_version: str = SCHEMA_VERSION,
    local_schema_version_file: str | Path | None = None,
) -> str:
    """Remove stale learned/adaptive artifacts when schema version changes."""
    d = Path(data_dir)
    version_file = Path(local_schema_version_file) if local_schema_version_file else (
        _CODE_REPO_ROOT / _LOCAL_SCHEMA_VERSION_NAME
    )
    expected_version = str(schema_version).strip()
    current_version = _read_local_schema_version(version_file)

    if current_version == expected_version:
        return "already clean"

    if not d.is_dir():
        _write_local_schema_version(version_file, expected_version)
        return "no data directory"

    removed: list[str] = []
    for name in _STALE_LEARNED_FILES:
        target = d / name
        if target.exists():
            try:
                target.unlink()
                removed.append(name)
            except Exception as exc:
                logger.warning("[clean-start] could not remove {}: {}", name, exc)

    _write_local_schema_version(version_file, expected_version)

    if removed:
        logger.warning(
            "[clean-start] purged {} stale learned artifact(s) so the "
            "equal-weight system starts fresh: {}",
            len(removed),
            ", ".join(removed),
        )
        return f"purged {len(removed)}: {', '.join(removed)}"
    return "nothing stale to purge"


def run_startup_clean_start(
    *,
    data_dir: str = "data",
    branch: str = "main",
    schema_version: str = SCHEMA_VERSION,
    local_schema_version_file: str | Path | None = None,
) -> tuple[str, str]:
    """Run startup sync + version-gated purge using path/config values only."""
    sync_result = sync_clean_state_from_remote(data_dir=data_dir, branch=branch)
    purge_result = purge_stale_learned_data(
        data_dir=data_dir,
        schema_version=schema_version,
        local_schema_version_file=local_schema_version_file,
    )
    return sync_result, purge_result
