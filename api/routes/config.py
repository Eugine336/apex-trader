"""APEX TRADER — Per-user trading configuration routes.

Get/update the user's trading preferences. Changes are persisted and take
effect on the next instance (re)start — the route reports whether a restart is
needed so the UI can prompt the user.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends

from api.auth import get_current_user, get_db, get_process_manager
from api.database import Database
from api.models import TradingConfig
from api.process_manager import STATUS_RUNNING, ProcessManager

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("", response_model=TradingConfig)
async def get_trading_config(
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> TradingConfig:
    raw = db.get_user_config(int(user["id"]))
    data = json.loads(raw) if raw else {}
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
