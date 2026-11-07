"""APEX TRADER — Per-user trading configuration routes.

Get/update the user's trading preferences. Changes are persisted and take
effect on the next instance (re)start — the route reports whether a restart is
needed so the UI can prompt the user.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api.auth import get_current_user, get_db, get_process_manager
from api.database import Database
from api.instance_config import apply_user_overrides
from api.models import TradingConfig
from api.process_manager import STATUS_RUNNING, STATUS_STARTING, ProcessManager
from config import AppConfig

router = APIRouter(prefix="/api/config", tags=["config"])


def _owner_limits_audit(data: dict[str, Any], effective: AppConfig) -> dict[str, Any]:
    """Describe the effective owner-limit source without exposing internals."""
    nested = data.get("owner_limits")
    nested = nested if isinstance(nested, dict) else {}
    legacy_sources = {
        "risk_per_trade_pct": "risk_per_trade_pct",
        "max_daily_drawdown_pct": "daily_loss_limit_pct",
        "max_open_trades": "max_open_positions",
    }
    sources: dict[str, str] = {}
    for field in effective.owner_limits.to_owner_dict():
        if field in nested:
            sources[field] = "owner_override"
        elif any(
            legacy_key in data and owner_key == field
            for legacy_key, owner_key in legacy_sources.items()
        ):
            sources[field] = "legacy_override"
        else:
            sources[field] = "runtime_default_or_internal_config"
    return {
        "validation_status": "valid",
        "source_by_field": sources,
    }


@router.get("", response_model=TradingConfig)
async def get_trading_config(
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> TradingConfig:
    raw = db.get_user_config(int(user["id"]))
    data = json.loads(raw) if raw else {}
    if data.get("enabled_symbols_override") and data.get("universe_mode") in (None, "all"):
        # Preserve an existing explicit symbol restriction rather than
        # interpreting a stale/legacy all-mode record as permission to expand.
        data["universe_mode"] = "selected"
    try:
        effective = AppConfig()
        apply_user_overrides(effective, data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"stored trading config is invalid: {exc}") from exc

    # Return one canonical effective view. Legacy keys remain in the response
    # for older clients, but they mirror the canonical values rather than
    # exposing competing stored values.
    data["owner_limits"] = effective.owner_limits.to_owner_dict()
    data["owner_limits_audit"] = _owner_limits_audit(data, effective)
    data["risk_per_trade_pct"] = effective.owner_limits.risk_per_trade_pct
    data["max_daily_drawdown_pct"] = effective.owner_limits.daily_loss_limit_pct
    data["max_open_trades"] = effective.owner_limits.max_open_positions
    data["universe_mode"] = effective.universe_mode
    data["enabled_categories"] = effective.enabled_categories
    data["enabled_symbols_override"] = effective.enabled_symbols_override
    data["log_level"] = effective.log_level
    data["data_path_fixes_enabled"] = effective.features.data_path_fixes_enabled
    return TradingConfig(**{k: v for k, v in data.items() if k in TradingConfig.model_fields})


@router.put("")
async def update_trading_config(
    payload: TradingConfig,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    pm: ProcessManager = Depends(get_process_manager),
) -> dict[str, Any]:
    # Persist only explicitly-set fields so partial updates merge cleanly.
    raw = db.get_user_config(int(user["id"]))
    current = json.loads(raw) if raw else {}
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if updates.get("universe_mode") == "all":
        current.pop("enabled_symbols_override", None)

    # Migrate the three historical top-level risk keys into the canonical nested
    # object. A supplied nested object is a full replacement; otherwise explicit
    # legacy updates modify the existing canonical value for compatibility.
    legacy_to_owner = {
        "risk_per_trade_pct": "risk_per_trade_pct",
        "max_daily_drawdown_pct": "daily_loss_limit_pct",
        "max_open_trades": "max_open_positions",
    }
    has_nested_owner_patch = "owner_limits" in updates
    if has_nested_owner_patch:
        owner_updates = dict(updates.pop("owner_limits") or {})
        for legacy_key, owner_key in legacy_to_owner.items():
            if legacy_key in updates and owner_key not in owner_updates:
                owner_updates[owner_key] = updates[legacy_key]
            updates.pop(legacy_key, None)
    else:
        owner_updates = dict(current.get("owner_limits") or {})
        for legacy_key, owner_key in legacy_to_owner.items():
            if legacy_key in current and owner_key not in owner_updates:
                owner_updates[owner_key] = current[legacy_key]
            if legacy_key in updates:
                owner_updates[owner_key] = updates.pop(legacy_key)
    for legacy_key in legacy_to_owner:
        current.pop(legacy_key, None)
    if has_nested_owner_patch or owner_updates:
        updates["owner_limits"] = owner_updates

    # Validate the complete merged owner configuration before writing anything.
    # This catches conflicts between a partial update and already-persisted limits.
    candidate = dict(current)
    candidate.update(updates)
    try:
        validation_config = AppConfig()
        apply_user_overrides(validation_config, candidate)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    current.update(updates)
    db.set_user_config(int(user["id"]), json.dumps(current))

    # Legacy response aliases are computed from the validated canonical object;
    # they are never persisted as competing configuration sources.
    response_config = dict(current)
    response_config["risk_per_trade_pct"] = validation_config.owner_limits.risk_per_trade_pct
    response_config["max_daily_drawdown_pct"] = validation_config.owner_limits.daily_loss_limit_pct
    response_config["max_open_trades"] = validation_config.owner_limits.max_open_positions
    response_config["owner_limits"] = validation_config.owner_limits.to_owner_dict()
    response_config["owner_limits_audit"] = _owner_limits_audit(current, validation_config)

    status_info = pm.instance_status(int(user["id"]))
    restart_required = status_info.get("status") in {STATUS_RUNNING, STATUS_STARTING}
    return {
        "config": response_config,
        "restart_required": restart_required,
        "message": (
            "config saved — restart your instance to apply"
            if restart_required
            else "config saved"
        ),
    }
