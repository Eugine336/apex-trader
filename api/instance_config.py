"""APEX TRADER — Per-user instance configuration.

Two responsibilities, both dependency-light (no FastAPI/pydantic imports) so the
trading engine's ``main.py`` can import :func:`apply_user_overrides` cheaply:

1. :func:`apply_user_overrides` — mutate a freshly-built ``AppConfig`` with a
   user's saved trading preferences. Called from ``main.py`` when the
   ``APEX_USER_CONFIG`` environment variable points at a per-user JSON file.

2. :func:`build_instance_environment` — assemble the environment + working-dir
   layout used by :mod:`api.process_manager` to launch an isolated instance.
   Broker credentials are injected as the same environment variables the
   untouched ``platforms.platform_manager`` already reads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from runtime_paths import repo_root as _repo_root

# Keys accepted in a user's saved trading config (subset of AppConfig surface).
# Each maps to a setter applied to the constructed AppConfig instance.
_KNOWN_KEYS = {
    "enabled_categories",
    "enabled_symbols_override",
    "risk_per_trade_pct",
    "max_daily_drawdown_pct",
    "max_open_trades",
    "data_path_fixes_enabled",
    "log_level",
}


def apply_user_overrides(config: Any, overrides: dict[str, Any]) -> Any:
    """Apply a user's saved trading preferences onto *config* in place.

    *config* is an ``AppConfig`` instance (already constructed, so its dataclass
    ``__post_init__`` validation has run). Unknown keys are ignored. Returns the
    same config object for convenience.
    """
    if not overrides:
        return config

    if isinstance(overrides.get("enabled_categories"), list):
        config.enabled_categories = [str(c) for c in overrides["enabled_categories"]]

    if isinstance(overrides.get("enabled_symbols_override"), list):
        config.enabled_symbols_override = [
            str(s) for s in overrides["enabled_symbols_override"]
        ]

    risk = getattr(config, "risk", None)
    if risk is not None:
        if overrides.get("risk_per_trade_pct") is not None:
            risk.risk_per_trade_pct = float(overrides["risk_per_trade_pct"])
        if overrides.get("max_daily_drawdown_pct") is not None:
            risk.max_daily_drawdown_pct = float(overrides["max_daily_drawdown_pct"])
            # Keep the weekly cap consistent with the (post-init validated)
            # invariant weekly >= daily — bump it if the user lowered weekly
            # below daily implicitly.
            if getattr(risk, "max_weekly_drawdown_pct", 0.0) < risk.max_daily_drawdown_pct:
                risk.max_weekly_drawdown_pct = risk.max_daily_drawdown_pct
        if overrides.get("max_open_trades") is not None:
            risk.max_open_trades = int(overrides["max_open_trades"])

    features = getattr(config, "features", None)
    if features is not None and overrides.get("data_path_fixes_enabled") is not None:
        features.data_path_fixes_enabled = bool(overrides["data_path_fixes_enabled"])

    if overrides.get("log_level"):
        config.log_level = str(overrides["log_level"]).upper()

    # Per-user instances run from an isolated, non-git working directory, so the
    # data-junction git sync / clean-start machinery must not run.
    backup = getattr(config, "data_backup", None)
    if backup is not None:
        backup.enabled = False
        backup.auto_sync_data_repo = False
        # main.py reads this via getattr(default=True); force-disable clean-start.
        backup.clean_start_on_first_boot = False

    return config


def load_user_overrides_from_env() -> dict[str, Any]:
    """Load the per-user override dict from ``APEX_USER_CONFIG`` (if set).

    Returns an empty dict when the variable is unset or the file is missing or
    unparseable. Best-effort — never raises.
    """
    path = os.getenv("APEX_USER_CONFIG", "").strip()
    if not path:
        return {}
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _mt5_brokers_env(creds: dict[str, Any]) -> str:
    """Serialize MT5 credentials into the MT5_BROKERS JSON the engine reads."""
    return json.dumps(
        [
            {
                "login": int(str(creds["login"]).strip()),
                "password": str(creds["password"]).strip(),
                "server": str(creds["server"]).strip(),
            }
        ]
    )


def build_instance_environment(
    *,
    user_id: int,
    workdir: Path,
    config_path: Path,
    api_db_path: Path,
    broker_credentials: dict[str, dict[str, Any]],
    base_env: Optional[dict[str, str]] = None,
    dashboard_port: Optional[int] = None,
) -> dict[str, str]:
    """Build the environment for a user's isolated trading subprocess.

    *broker_credentials* maps broker_type ("mt5"/"deriv") → decrypted creds.
    The returned env isolates data/log directories, injects broker creds via the
    exact variables ``platforms.platform_manager`` consumes, and enables trade
    reporting back to the API database.
    """
    env: dict[str, str] = dict(base_env if base_env is not None else os.environ)

    data_dir = workdir / "data"
    log_dir = workdir / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # ── Isolation: per-user state + logs ──────────────────────────────────
    env["APEX_USER_ID"] = str(user_id)
    env["APEX_DATA_DIR"] = str(data_dir.resolve())
    env["APEX_LOG_DIR"] = str(log_dir.resolve())
    env["APEX_USER_CONFIG"] = str(config_path.resolve())

    # ── Shared read-only resources (anchored to the code repo) ────────────
    # The subprocess runs with cwd = per-user workdir, so relative paths like
    # ``checkpoints/...`` would resolve under the workdir and miss the shared,
    # repo-shipped assets (trained RL models, reference configs). Pin the repo
    # root explicitly so ``runtime_paths.repo_root()`` / ``checkpoints_dir()``
    # resolve to the shared location for every instance.
    env["APEX_REPO_DIR"] = str(_repo_root().resolve())

    # ── Trade reporting bridge → API database ─────────────────────────────
    env["APEX_TRADE_REPORT_DB"] = str(api_db_path.resolve())

    # ── Event-driven engine on; dashboard bind managed by the proc manager ─
    env["USE_EVENT_DRIVEN"] = "true"
    # Each instance runs its read-only dashboard API on a private loopback port
    # (set via DD_DASHBOARD_PORT below) so the control plane can proxy live
    # engine panels. Drop any inherited public bind host here; the process
    # manager pins it back to 127.0.0.1 so the dashboard is never public.
    env.pop("DD_DASHBOARD_BIND_HOST", None)

    # ── Broker credentials (consumed by platforms.platform_manager) ───────
    # Strip on consumption as defense-in-depth: credentials stored before the
    # vault began trimming the password (or any stray whitespace) are cleaned
    # here too, since decrypt() does not re-validate.
    mt5 = broker_credentials.get("mt5")
    if mt5:
        env["MT5_BROKERS"] = _mt5_brokers_env(mt5)
        # Also set legacy single-broker vars for any code path that reads them.
        env["MT5_LOGIN"] = str(int(str(mt5["login"]).strip()))
        env["MT5_PASSWORD"] = str(mt5["password"]).strip()
        env["MT5_SERVER"] = str(mt5["server"]).strip()

    deriv = broker_credentials.get("deriv")
    if deriv:
        env["DERIV_ACCESS_TOKEN"] = str(deriv["access_token"]).strip()
        env["DERIV_APP_ID"] = str(deriv["app_id"]).strip()
        env["DERIV_ACCOUNT_TYPE"] = str(deriv.get("account_type", "demo")).strip()
        if str(deriv.get("client_id", "")).strip():
            env["DERIV_CLIENT_ID"] = str(deriv["client_id"]).strip()

    if dashboard_port is not None:
        env["DD_DASHBOARD_PORT"] = str(dashboard_port)

    return env


def write_user_config(config_path: Path, overrides: dict[str, Any]) -> None:
    """Persist a user's trading overrides to *config_path* as JSON."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    filtered = {k: v for k, v in overrides.items() if k in _KNOWN_KEYS}
    config_path.write_text(json.dumps(filtered, indent=2), encoding="utf-8")
