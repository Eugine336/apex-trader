"""APEX TRADER — Trading instance control routes.

Start, stop, restart, inspect status, and tail logs for the authenticated
user's isolated APEX trading process.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.auth import get_current_user, get_db, get_process_manager, get_vault
from api.broker_vault import BrokerVault
from api.credentials import load_decrypted_credentials
from api.database import Database
from api.models import InstanceLogResponse, InstanceStatusResponse
from api.process_manager import ProcessManager

router = APIRouter(prefix="/api/trading", tags=["trading"])


@router.post("/start", response_model=InstanceStatusResponse)
async def start_instance(
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    vault: BrokerVault = Depends(get_vault),
    pm: ProcessManager = Depends(get_process_manager),
) -> InstanceStatusResponse:
    creds = load_decrypted_credentials(db, vault, int(user["id"]))
    if not creds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no broker credentials configured — add MT5 or Deriv first",
        )
    try:
        result = pm.start_instance(int(user["id"]), creds)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return InstanceStatusResponse(**result)


@router.post("/stop", response_model=InstanceStatusResponse)
async def stop_instance(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> InstanceStatusResponse:
    result = pm.stop_instance(int(user["id"]), user_initiated=True)
    return InstanceStatusResponse(**result)


@router.post("/restart", response_model=InstanceStatusResponse)
async def restart_instance(
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    vault: BrokerVault = Depends(get_vault),
    pm: ProcessManager = Depends(get_process_manager),
) -> InstanceStatusResponse:
    creds = load_decrypted_credentials(db, vault, int(user["id"]))
    if not creds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no broker credentials configured",
        )
    try:
        result = pm.restart_instance(int(user["id"]), creds)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return InstanceStatusResponse(**result)


@router.get("/status", response_model=InstanceStatusResponse)
async def instance_status(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> InstanceStatusResponse:
    return InstanceStatusResponse(**pm.instance_status(int(user["id"])))


@router.get("/logs", response_model=InstanceLogResponse)
async def instance_logs(
    lines: int = Query(default=200, ge=1, le=2000),
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> InstanceLogResponse:
    return InstanceLogResponse(lines=pm.tail_log(int(user["id"]), lines=lines))
