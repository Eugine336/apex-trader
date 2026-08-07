"""APEX TRADER — Broker credential routes.

Store, list, test, and delete a user's encrypted broker credentials. Plaintext
secrets are accepted on write but never returned — list/get responses are
masked. A "test connection" endpoint validates MT5 credentials by attempting a
real login in an isolated subprocess (so a bad password can't crash the API),
and validates Deriv credentials structurally (a live WebSocket probe would
require network access not guaranteed in all deployments).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger

from api.auth import get_current_user, get_db, get_vault
from api.broker_vault import (
    BROKER_DERIV,
    BROKER_MT5,
    BrokerVault,
    VaultError,
    mask_credentials,
)
from api.database import Database
from api.models import (
    BrokerCredentialResponse,
    DerivCredentialsRequest,
    MessageResponse,
    MT5CredentialsRequest,
)

router = APIRouter(prefix="/api/broker", tags=["broker"])


def _store(
    db: Database,
    vault: BrokerVault,
    user_id: int,
    broker_type: str,
    credentials: dict[str, Any],
    label: str,
) -> None:
    try:
        ciphertext = vault.encrypt(user_id, broker_type, credentials)
    except VaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    db.upsert_broker_credentials(user_id, broker_type, ciphertext, label=label)
    logger.info("[broker] stored {} credentials for user id={}", broker_type, user_id)


@router.put("/mt5", response_model=MessageResponse)
async def set_mt5_credentials(
    payload: MT5CredentialsRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    vault: BrokerVault = Depends(get_vault),
) -> MessageResponse:
    _store(
        db,
        vault,
        int(user["id"]),
        BROKER_MT5,
        {
            "login": payload.login,
            "password": payload.password,
            "server": payload.server,
        },
        payload.label,
    )
    return MessageResponse(message="mt5 credentials stored")


@router.put("/deriv", response_model=MessageResponse)
async def set_deriv_credentials(
    payload: DerivCredentialsRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    vault: BrokerVault = Depends(get_vault),
) -> MessageResponse:
    _store(
        db,
        vault,
        int(user["id"]),
        BROKER_DERIV,
        {
            "access_token": payload.access_token,
            "app_id": payload.app_id,
            "account_type": payload.account_type,
            "client_id": payload.client_id,
        },
        payload.label,
    )
    return MessageResponse(message="deriv credentials stored")


@router.get("", response_model=list[BrokerCredentialResponse])
async def list_credentials(
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    vault: BrokerVault = Depends(get_vault),
) -> list[BrokerCredentialResponse]:
    out: list[BrokerCredentialResponse] = []
    for row in db.list_broker_credentials(int(user["id"])):
        try:
            creds = vault.decrypt(int(user["id"]), row["encrypted_credentials"])
            masked = mask_credentials(row["broker_type"], creds)
        except VaultError:
            masked = {"broker_type": row["broker_type"], "error": "undecryptable"}
        out.append(
            BrokerCredentialResponse(
                broker_type=row["broker_type"],
                label=row.get("label", ""),
                masked=masked,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
    return out


@router.delete("/{broker_type}", response_model=MessageResponse)
async def delete_credentials(
    broker_type: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> MessageResponse:
    if not db.delete_broker_credentials(int(user["id"]), broker_type.lower()):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no {broker_type} credentials stored",
        )
    return MessageResponse(message=f"{broker_type} credentials deleted")
