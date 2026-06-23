"""APEX TRADER — Admin routes.

Operator-only control plane: user management, global instance supervision,
cross-user trade visibility, and a system overview. Every endpoint requires an
authenticated admin (see :func:`api.auth.get_current_admin`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.auth import get_current_admin, get_db, get_process_manager, get_vault
from api.broker_vault import BrokerVault
from api.credentials import load_decrypted_credentials
from api.database import Database
from api.models import (
    AdminInstanceResponse,
    AdminStatsResponse,
    AdminTradeResponse,
    AdminUserUpdate,
    InstanceStatusResponse,
    UserResponse,
)
from api.process_manager import ProcessManager

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _user_response(row: dict[str, Any]) -> UserResponse:
    return UserResponse(
        id=int(row["id"]),
        email=row["email"],
        is_active=bool(row.get("is_active", 1)),
        is_admin=bool(row.get("is_admin", 0)),
        created_at=row["created_at"],
    )


# ── users ──────────────────────────────────────────────────────────────────
@router.get("/users", response_model=list[UserResponse])
async def list_users(
    _admin: dict[str, Any] = Depends(get_current_admin),
    db: Database = Depends(get_db),
) -> list[UserResponse]:
    return [_user_response(row) for row in db.list_users()]


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    _admin: dict[str, Any] = Depends(get_current_admin),
    db: Database = Depends(get_db),
) -> UserResponse:
    row = db.get_user_by_id(user_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
        )
    return _user_response(row)


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    admin: dict[str, Any] = Depends(get_current_admin),
    db: Database = Depends(get_db),
) -> UserResponse:
    row = db.get_user_by_id(user_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
        )
    # Guard the operator against locking themselves out.
    if user_id == int(admin["id"]):
        if payload.is_admin is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cannot remove your own admin privileges",
            )
        if payload.is_active is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cannot disable your own account",
            )
    if payload.is_active is not None:
        db.set_user_active(user_id, payload.is_active)
    if payload.is_admin is not None:
        db.set_user_admin(user_id, payload.is_admin)
    updated = db.get_user_by_id(user_id)
    return _user_response(updated or row)


# ── instances ───────────────────────────────────────────────────────────────
@router.get("/instances", response_model=list[AdminInstanceResponse])
async def list_instances(
    _admin: dict[str, Any] = Depends(get_current_admin),
    db: Database = Depends(get_db),
    pm: ProcessManager = Depends(get_process_manager),
) -> list[AdminInstanceResponse]:
    emails = {int(u["id"]): u["email"] for u in db.list_users()}
    out: list[AdminInstanceResponse] = []
    for st in pm.all_statuses():
        uid = int(st["user_id"])
        out.append(
            AdminInstanceResponse(
                user_id=uid,
                email=emails.get(uid, ""),
                status=st.get("status", "STOPPED"),
                pid=st.get("pid"),
                alive=bool(st.get("alive", False)),
                restarts=int(st.get("restarts", 0) or 0),
                last_error=st.get("last_error", ""),
                started_at=st.get("started_at"),
                stopped_at=st.get("stopped_at"),
                uptime_seconds=float(st.get("uptime_seconds", 0.0) or 0.0),
            )
        )
    return out


@router.post("/instances/{user_id}/stop", response_model=InstanceStatusResponse)
async def stop_instance(
    user_id: int,
    _admin: dict[str, Any] = Depends(get_current_admin),
    db: Database = Depends(get_db),
    pm: ProcessManager = Depends(get_process_manager),
) -> InstanceStatusResponse:
    if db.get_user_by_id(user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
        )
    result = pm.stop_instance(user_id, user_initiated=True)
    return InstanceStatusResponse(**result)


@router.post("/instances/{user_id}/start", response_model=InstanceStatusResponse)
async def start_instance(
    user_id: int,
    _admin: dict[str, Any] = Depends(get_current_admin),
    db: Database = Depends(get_db),
    vault: BrokerVault = Depends(get_vault),
    pm: ProcessManager = Depends(get_process_manager),
) -> InstanceStatusResponse:
    if db.get_user_by_id(user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="user not found"
        )
    creds = load_decrypted_credentials(db, vault, user_id)
    if not creds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user has no broker credentials configured",
        )
    try:
        result = pm.start_instance(user_id, creds)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return InstanceStatusResponse(**result)


# ── trades ───────────────────────────────────────────────────────────────────
@router.get("/trades", response_model=list[AdminTradeResponse])
async def list_trades(
    user_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _admin: dict[str, Any] = Depends(get_current_admin),
    db: Database = Depends(get_db),
) -> list[AdminTradeResponse]:
    rows = db.list_all_trades(user_id=user_id, limit=limit, offset=offset)
    return [
        AdminTradeResponse(
            id=int(r["id"]),
            user_id=int(r["user_id"]),
            ticket=r["ticket"],
            symbol=r["symbol"],
            direction=r["direction"],
            entry_price=r["entry_price"],
            exit_price=r["exit_price"],
            pnl=r["pnl"],
            pnl_pips=r["pnl_pips"],
            exit_reason=r["exit_reason"],
            opened_at=r["opened_at"],
            closed_at=r["closed_at"],
        )
        for r in rows
    ]


# ── system overview ───────────────────────────────────────────────────────
@router.get("/stats", response_model=AdminStatsResponse)
async def system_stats(
    _admin: dict[str, Any] = Depends(get_current_admin),
    db: Database = Depends(get_db),
    pm: ProcessManager = Depends(get_process_manager),
) -> AdminStatsResponse:
    users = db.list_users()
    totals = db.global_trade_totals()
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return AdminStatsResponse(
        total_users=len(users),
        active_users=sum(1 for u in users if u.get("is_active", 1)),
        admin_users=sum(1 for u in users if u.get("is_admin", 0)),
        active_instances=pm.active_count(),
        total_instances=len(db.list_instances()),
        total_trades=int(totals.get("total", 0)),
        trades_today=db.count_trades_since(midnight.isoformat()),
        total_pnl=float(totals.get("total_pnl", 0.0)),
        system_uptime_seconds=pm.system_uptime_seconds(),
    )
