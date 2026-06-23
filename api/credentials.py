"""APEX TRADER — Credential loading helper.

Decrypts a user's stored broker credentials into the ``{broker_type: creds}``
shape that :mod:`api.process_manager` injects into a trading subprocess. Shared
by the trading routes and the process manager's auto-restart path so there is a
single decryption code path.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from api.broker_vault import BrokerVault, VaultError
from api.database import Database


def load_decrypted_credentials(
    db: Database, vault: BrokerVault, user_id: int
) -> dict[str, dict[str, Any]]:
    """Return ``{broker_type: decrypted_credentials}`` for *user_id*.

    Undecryptable rows are skipped (logged) rather than aborting the whole load.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in db.list_broker_credentials(user_id):
        try:
            out[row["broker_type"]] = vault.decrypt(
                user_id, row["encrypted_credentials"]
            )
        except VaultError as exc:
            logger.warning(
                "[credentials] skip undecryptable {} creds for user {}: {}",
                row["broker_type"],
                user_id,
                exc,
            )
    return out
