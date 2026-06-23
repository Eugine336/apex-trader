"""APEX TRADER — Authentication & authorization.

JWT (access + refresh) auth with bcrypt password hashing, plus a lightweight
in-memory sliding-window rate limiter for the auth endpoints and FastAPI
dependencies for protected routes.

Tokens are signed with the symmetric key from :class:`api.config.ApiConfig`.
The ``type`` claim distinguishes ``access`` / ``refresh`` / ``reset`` tokens so
a refresh token can never be used to authorize a normal request and vice versa.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from api.config import ApiConfig, get_api_config
from api.database import Database

# bcrypt rejects inputs longer than 72 bytes; truncate to stay within the limit.
_BCRYPT_MAX_BYTES = 72

_bearer = HTTPBearer(auto_error=False)

TOKEN_ACCESS = "access"
TOKEN_REFRESH = "refresh"
TOKEN_RESET = "reset"


# ── password hashing ─────────────────────────────────────────────────────
def _encode_secret(password: str) -> bytes:
    """Encode *password* to UTF-8, truncated to bcrypt's 72-byte limit."""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Return a bcrypt hash for *password*."""
    return bcrypt.hashpw(_encode_secret(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verify of *password* against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(_encode_secret(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ── token creation / decoding ──────────────────────────────────────────────
def _create_token(
    subject: str,
    token_type: str,
    expires_delta: timedelta,
    config: ApiConfig,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, config.jwt_secret, algorithm=config.jwt_algorithm)


def create_access_token(user_id: int, config: Optional[ApiConfig] = None) -> str:
    config = config or get_api_config()
    return _create_token(
        str(user_id),
        TOKEN_ACCESS,
        timedelta(minutes=config.access_token_ttl_minutes),
        config,
    )


def create_refresh_token(user_id: int, config: Optional[ApiConfig] = None) -> str:
    config = config or get_api_config()
    return _create_token(
        str(user_id),
        TOKEN_REFRESH,
        timedelta(days=config.refresh_token_ttl_days),
        config,
    )


def create_reset_token(user_id: int, config: Optional[ApiConfig] = None) -> str:
    config = config or get_api_config()
    return _create_token(
        str(user_id),
        TOKEN_RESET,
        timedelta(minutes=config.password_reset_ttl_minutes),
        config,
    )


def decode_token(
    token: str, expected_type: str, config: Optional[ApiConfig] = None
) -> dict[str, Any]:
    """Decode + validate a JWT, asserting the ``type`` claim.

    Raises :class:`jose.JWTError` on any failure (bad signature, expired, or
    wrong token type).
    """
    config = config or get_api_config()
    payload = jwt.decode(token, config.jwt_secret, algorithms=[config.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise JWTError(f"expected {expected_type} token, got {payload.get('type')!r}")
    return payload


# ── rate limiting ────────────────────────────────────────────────────────
class RateLimiter:
    """In-memory sliding-window rate limiter keyed by an arbitrary string.

    Suitable for a single-process API server (the intended Option-A topology).
    For a horizontally-scaled deployment this would be backed by Redis instead.
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """Record an attempt for *key*; return True if still under the limit."""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            cutoff = now - self._window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            return True


# ── FastAPI dependencies ─────────────────────────────────────────────────
def get_db(request: Request) -> Database:
    """Return the shared :class:`Database` from app state."""
    db = getattr(request.app.state, "db", None)
    if db is None:  # pragma: no cover — app always sets this
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database not initialized",
        )
    return db


def get_config(request: Request) -> ApiConfig:
    cfg = getattr(request.app.state, "config", None)
    return cfg or get_api_config()


def get_vault(request: Request):
    """Return the shared :class:`api.broker_vault.BrokerVault` from app state."""
    vault = getattr(request.app.state, "vault", None)
    if vault is None:  # pragma: no cover — app always sets this
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="vault not initialized",
        )
    return vault


def get_process_manager(request: Request):
    """Return the shared :class:`api.process_manager.ProcessManager`."""
    pm = getattr(request.app.state, "process_manager", None)
    if pm is None:  # pragma: no cover — app always sets this
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="process manager not initialized",
        )
    return pm


def get_rate_limiter(request: Request) -> "RateLimiter":
    """Return the shared auth :class:`RateLimiter` from app state."""
    rl = getattr(request.app.state, "rate_limiter", None)
    if rl is None:  # pragma: no cover
        rl = RateLimiter(10, 60)
        request.app.state.rate_limiter = rl
    return rl


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Database = Depends(get_db),
    config: ApiConfig = Depends(get_config),
) -> dict[str, Any]:
    """Resolve the authenticated user from a bearer access token.

    Raises 401 if the token is missing, invalid, or the user is inactive.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(credentials.credentials, TOKEN_ACCESS, config)
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get_user_by_id(user_id)
    if user is None or not user.get("is_active", 1):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not found or inactive",
        )
    return user


async def get_current_admin(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Require the authenticated user to be an admin."""
    if not user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin privileges required",
        )
    return user
