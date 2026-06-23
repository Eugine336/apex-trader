"""APEX TRADER — Broker credential vault.

Encrypts broker credentials (MT5 login/password/server, Deriv API token/app id)
at rest using Fernet (AES-128-CBC + HMAC-SHA256 authenticated encryption).

A unique encryption key is derived per user from the vault master key using
HKDF-SHA256 with the user id as the info/salt, so a leak of one user's
ciphertext does not help decrypt another's, and the master key never directly
encrypts anything.

Plaintext secrets (passwords, tokens) are NEVER returned through the API —
:func:`mask_credentials` produces a display-safe view, and the full plaintext
is only ever handed to :mod:`api.process_manager` to inject into a user's
trading subprocess environment.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.fernet import Fernet, InvalidToken

# Recognised broker types and the credential fields they require.
BROKER_MT5 = "mt5"
BROKER_DERIV = "deriv"

_MT5_REQUIRED = ("login", "password", "server")
_DERIV_REQUIRED = ("access_token", "app_id")
# Fields whose values must never be echoed back in plaintext.
_SECRET_FIELDS = {"password", "access_token"}


class VaultError(Exception):
    """Raised on encryption/decryption or validation failures."""


def _derive_fernet_key(master_key: str, user_id: int) -> bytes:
    """Derive a per-user Fernet key from the master key via HKDF-SHA256."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=f"apex-vault-user-{user_id}".encode("utf-8"),
        info=b"apex-broker-credentials",
    )
    derived = hkdf.derive(master_key.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


class BrokerVault:
    """Encrypt/decrypt per-user broker credentials with a derived Fernet key."""

    def __init__(self, master_key: str) -> None:
        if not master_key:
            raise VaultError("vault master key is empty")
        self._master_key = master_key

    def _fernet(self, user_id: int) -> Fernet:
        return Fernet(_derive_fernet_key(self._master_key, user_id))

    # ── validation ───────────────────────────────────────────────────────
    @staticmethod
    def validate(broker_type: str, credentials: dict[str, Any]) -> dict[str, Any]:
        """Validate + normalise a credentials dict for *broker_type*.

        Returns the cleaned dict, or raises :class:`VaultError`.
        """
        bt = (broker_type or "").lower()
        if bt == BROKER_MT5:
            required = _MT5_REQUIRED
        elif bt == BROKER_DERIV:
            required = _DERIV_REQUIRED
        else:
            raise VaultError(f"unsupported broker type: {broker_type!r}")

        missing = [f for f in required if not str(credentials.get(f, "")).strip()]
        if missing:
            raise VaultError(
                f"{bt} credentials missing required field(s): {', '.join(missing)}"
            )

        cleaned: dict[str, Any] = {}
        if bt == BROKER_MT5:
            try:
                cleaned["login"] = int(str(credentials["login"]).strip())
            except (TypeError, ValueError) as exc:
                raise VaultError("mt5 login must be an integer") from exc
            cleaned["password"] = str(credentials["password"])
            cleaned["server"] = str(credentials["server"]).strip()
        else:  # deriv
            cleaned["access_token"] = str(credentials["access_token"]).strip()
            cleaned["app_id"] = str(credentials["app_id"]).strip()
            cleaned["account_type"] = str(
                credentials.get("account_type", "demo")
            ).strip() or "demo"
            cleaned["client_id"] = str(credentials.get("client_id", "")).strip()
        return cleaned

    # ── encryption ───────────────────────────────────────────────────────
    def encrypt(
        self, user_id: int, broker_type: str, credentials: dict[str, Any]
    ) -> str:
        cleaned = self.validate(broker_type, credentials)
        token = self._fernet(user_id).encrypt(json.dumps(cleaned).encode("utf-8"))
        return token.decode("utf-8")

    def decrypt(self, user_id: int, ciphertext: str) -> dict[str, Any]:
        try:
            raw = self._fernet(user_id).decrypt(ciphertext.encode("utf-8"))
        except InvalidToken as exc:
            raise VaultError(
                "could not decrypt credentials (wrong master key or corrupted data)"
            ) from exc
        return json.loads(raw.decode("utf-8"))


def mask_credentials(broker_type: str, credentials: dict[str, Any]) -> dict[str, Any]:
    """Return a display-safe view with secrets masked.

    Non-secret fields (login, server, app_id, account_type) are shown in full;
    secret fields (password, access_token) are reduced to a masked stub so the
    UI can confirm a credential is stored without exposing it.
    """
    masked: dict[str, Any] = {}
    for key, value in credentials.items():
        if key in _SECRET_FIELDS:
            text = str(value or "")
            if len(text) <= 4:
                masked[key] = "****"
            else:
                masked[key] = f"****{text[-4:]}"
        else:
            masked[key] = value
    masked["broker_type"] = broker_type
    return masked
