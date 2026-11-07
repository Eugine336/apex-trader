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
from api.models import TradingConfig
from api.process_manager import STATUS_RUNNING, ProcessManager
from api.instance_config import apply_user_overrides
from config import AppConfig

router = APIRouter(prefix="/api/config", tags=["config"])


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

    # Validate the complete merged owner configuration before writing anything.
    # This catches conflicts between a partial update and already-persisted limits
    # (for example, increasing per-trade risk above the existing per-trade cap).
    candidate = dict(current)
    candidate.update(updates)
    try:
        validation_config = AppConfig()
        apply_user_overrides(validation_config, candidate)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    current.update(updates)
    db.set_user_config(int(user["id"]), json.dumps(current))

    status_info = pm.instance_status(int(user["id"]))
    restart_required = status_info.get("status") == STATUS_RUNNING
    return {
        "config": current,
        "restart_required": restart_required,
        "message": (
            "config saved — restart your instance to apply"
            if restart_required
            else "config saved"
        ),
    }
