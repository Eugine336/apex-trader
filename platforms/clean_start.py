"""
APEX TRADER — Clean-Start Helpers

Two best-effort, non-fatal startup steps that keep the data junction in sync
with the cleared remote after the equal-weight migration:

1. ``sync_clean_state_from_remote`` — ``git pull`` the data repo so the local
   working tree matches the (cleared) remote. Only touches tracked files; live
   runtime DBs written since the last commit are untracked and survive.

2. ``purge_stale_learned_data`` — a ONE-TIME safety net. On the first boot after
   this build is deployed it removes any residual learned/adaptive artifacts
   (scoring weights, ML profiles, edge trackers, tuner/discovery DBs) so the
   equal-weight system learns from scratch. Operational stores that carry live
   position/recovery state (positions, events, shadow, deriv, management) are
   deliberately left untouched. Guarded by a sentinel file so it never runs
   twice.

Both functions swallow their own errors and return a short summary string;
callers should log the result and continue regardless.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from loguru import logger

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

_SENTINEL_NAME = ".clean_start_done"


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


def sync_clean_state_from_remote(
    data_dir: str = "data",
    *,
    remote: str = "origin",
    branch: str = "main",
) -> str:
    """``git pull`` the data junction so it matches the cleared remote.

    Non-fatal. Returns a short summary string.
    """
    d = Path(data_dir)
    if not d.is_dir():
        return "no data directory"
    if not _is_git_repo(d):
        return "data dir is not a git repo"
    try:
        out = subprocess.run(
            ["git", "-C", str(d.resolve()), "pull", "--ff-only", remote, branch],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            msg = (out.stderr or out.stdout or "unknown").strip().splitlines()
            reason = msg[0] if msg else "unknown"
            logger.warning("[clean-start] data-repo pull failed: {}", reason)
            return f"pull failed: {reason}"
        return "pulled remote state"
    except Exception as exc:
        logger.warning("[clean-start] data-repo pull errored: {}", exc)
        return f"error: {exc}"


def purge_stale_learned_data(data_dir: str = "data") -> str:
    """One-time removal of residual learned/adaptive artifacts.

    Idempotent via a sentinel file — after the first successful run it returns
    ``"already clean"`` and deletes nothing. Non-fatal.
    """
    d = Path(data_dir)
    sentinel = d / _SENTINEL_NAME
    if sentinel.exists():
        return "already clean"

    if not d.is_dir():
        # Nothing to purge yet, but still mark done so we don't re-scan forever
        # once the directory appears with freshly-learned (valid) data.
        try:
            d.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("clean-start: no data dir at first boot\n")
        except Exception as exc:
            logger.warning("[clean-start] sentinel write failed: {}", exc)
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

    try:
        sentinel.write_text(
            f"clean-start complete — purged {len(removed)} learned artifact(s)\n"
        )
    except Exception as exc:
        logger.warning("[clean-start] sentinel write failed: {}", exc)

    if removed:
        logger.warning(
            "[clean-start] purged {} stale learned artifact(s) so the "
            "equal-weight system starts fresh: {}",
            len(removed),
            ", ".join(removed),
        )
        return f"purged {len(removed)}: {', '.join(removed)}"
    return "nothing stale to purge"
