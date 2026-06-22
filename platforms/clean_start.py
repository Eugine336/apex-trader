"""
APEX TRADER — Clean-Start Helpers

Two best-effort, non-fatal startup steps:

1. ``sync_clean_state_from_remote`` — ``git fetch`` + ``git reset --hard`` the
   data repo so the local working tree matches the remote branch exactly.

2. ``purge_stale_learned_data`` — schema-version-gated cleanup. If the local
   schema version file does not match ``config.SCHEMA_VERSION``, remove residual
   learned/adaptive artifacts (scoring weights, ML profiles, edge trackers,
   tuner/discovery DBs) so the equal-weight system learns from scratch.

Both functions swallow their own errors and return a short summary string;
callers should log the result and continue regardless.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from config import SCHEMA_VERSION
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
    """Fetch + hard-reset the data junction to the remote state."""
    d = Path(data_dir)
    if not d.is_dir():
        return "no data directory"
    if not _is_git_repo(d):
        return "data dir is not a git repo"
    try:
        fetch = subprocess.run(
            ["git", "-C", str(d.resolve()), "fetch", remote],
            capture_output=True,
            text=True,
        )
        if fetch.returncode != 0:
            msg = (fetch.stderr or fetch.stdout or "unknown").strip().splitlines()
            reason = msg[0] if msg else "unknown"
            logger.warning("[clean-start] data-repo fetch failed: {}", reason)
            return f"fetch failed: {reason}"

        targets = [branch]
        if branch == "main":
            targets.append("master")

        last_reason = "unknown"
        for idx, target in enumerate(targets):
            reset = subprocess.run(
                ["git", "-C", str(d.resolve()), "reset", "--hard", f"{remote}/{target}"],
                capture_output=True,
                text=True,
            )
            if reset.returncode == 0:
                if idx == 0:
                    return f"reset to {remote}/{target}"
                logger.warning(
                    "[clean-start] data-repo branch '{}' missing, fell back to '{}/{}'",
                    branch,
                    remote,
                    target,
                )
                return f"reset to {remote}/{target} (fallback)"

            msg = (reset.stderr or reset.stdout or "unknown").strip().splitlines()
            last_reason = msg[0] if msg else "unknown"
            if idx == 0 and branch == "main":
                logger.warning(
                    "[clean-start] data-repo reset failed for {}/{}: {} — trying {}/master fallback",
                    remote,
                    target,
                    last_reason,
                    remote,
                )
            else:
                logger.warning(
                    "[clean-start] data-repo reset failed for {}/{}: {}",
                    remote,
                    target,
                    last_reason,
                )
        return f"reset failed: {last_reason}"
    except Exception as exc:
        logger.warning("[clean-start] data-repo sync errored: {}", exc)
        return f"error: {exc}"


def _resolve_local_schema_version_file(local_schema_version_file: str | Path | None) -> Path:
    if local_schema_version_file is None:
        return _CODE_REPO_ROOT / _LOCAL_SCHEMA_VERSION_NAME
    return Path(local_schema_version_file)


def purge_stale_learned_data(
    data_dir: str = "data",
    *,
    schema_version: str = SCHEMA_VERSION,
    local_schema_version_file: str | Path | None = None,
) -> str:
    """Schema-version-gated removal of residual learned/adaptive artifacts."""
    version_file = _resolve_local_schema_version_file(local_schema_version_file)
    target_schema = str(schema_version)
    try:
        current_schema = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
    except Exception as exc:
        logger.warning("[clean-start] local schema version read failed: {}", exc)
        current_schema = ""

    if current_schema == target_schema:
        return "already clean"

    d = Path(data_dir)
    if not d.is_dir():
        logger.warning("[clean-start] data directory missing during purge: {}", d)
        removed = []
    else:
        removed: list[str] = []
        for name in _STALE_LEARNED_FILES:
            target = d / name
            if target.exists():
                try:
                    target.unlink()
                    removed.append(name)
                except Exception as exc:
                    logger.warning("[clean-start] could not remove {}: {}", name, exc)

    try:
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(target_schema, encoding="utf-8")
    except Exception as exc:
        logger.warning("[clean-start] local schema version write failed: {}", exc)

    if removed:
        logger.warning(
            "[clean-start] purged {} stale learned artifact(s) so the "
            "equal-weight system starts fresh: {}",
            len(removed),
            ", ".join(removed),
        )
        return f"purged {len(removed)}: {', '.join(removed)}"
    if not d.is_dir():
        return "no data directory"
    return "nothing stale to purge"


def run_startup_clean_start(
    *,
    data_dir: str = "data",
    branch: str = "main",
    schema_version: str = SCHEMA_VERSION,
    local_schema_version_file: str | Path | None = None,
) -> tuple[str, str]:
    """Convenience wrapper: sync remote state, then run schema-gated purge."""
    pull_res = sync_clean_state_from_remote(data_dir=data_dir, branch=branch)
    purge_res = purge_stale_learned_data(
        data_dir=data_dir,
        schema_version=schema_version,
        local_schema_version_file=local_schema_version_file,
    )
    return pull_res, purge_res
