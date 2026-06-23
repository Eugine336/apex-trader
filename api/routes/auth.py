"""APEX TRADER — Authentication routes.

Registration, login, token refresh, and a basic password-reset flow. Auth
endpoints are rate-limited per client IP to blunt credential-stuffing.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError
from loguru import logger

from api.auth import (
    TOKEN_REFRESH,
    TOKEN_RESET,
    RateLimiter,
    create_access_token,
    create_refresh_token,
    create_reset_token,
    decode_token,
    get_config,
    get_db,
    get_rate_limiter,
    hash_password,
    verify_password,
)
from api.config import ApiConfig
from api.database import Database
from api.models import (
    LoginRequest,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request, limiter: RateLimiter) -> None:
    if not limiter.check(f"{_client_key(request)}:{request.url.path}"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts — slow down",
        )


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    db: Database = Depends(get_db),
    config: ApiConfig = Depends(get_config),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> TokenPair:
    _rate_limit(request, limiter)
    if db.get_user_by_email(payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="email already registered"
        )
    # First registered user becomes an admin (operator bootstrap).
    is_admin = db.count_users() == 0
    user_id = db.create_user(
        payload.email, hash_password(payload.password), is_admin=is_admin
    )
    logger.info("[auth] registered user id={} admin={}", user_id, is_admin)
    return TokenPair(
        access_token=create_access_token(user_id, config),
        refresh_token=create_refresh_token(user_id, config),
    )


@router.post("/login", response_model=TokenPair)
async def login(
    payload: LoginRequest,
    request: Request,
    db: Database = Depends(get_db),
    config: ApiConfig = Depends(get_config),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> TokenPair:
    _rate_limit(request, limiter)
    user = db.get_user_by_email(payload.email)
    # Verify even when the user is missing to keep timing uniform.
    stored_hash = user["password_hash"] if user else "$2b$12$" + "x" * 53
    if not verify_password(payload.password, stored_hash) or user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )
    if not user.get("is_active", 1):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="account disabled"
        )
    return TokenPair(
        access_token=create_access_token(int(user["id"]), config),
        refresh_token=create_refresh_token(int(user["id"]), config),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshRequest,
    db: Database = Depends(get_db),
    config: ApiConfig = Depends(get_config),
) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token, TOKEN_REFRESH, config)
        user_id = int(claims["sub"])
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired refresh token",
        ) from exc
    user = db.get_user_by_id(user_id)
    if user is None or not user.get("is_active", 1):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found or inactive"
        )
    return TokenPair(
        access_token=create_access_token(user_id, config),
        refresh_token=create_refresh_token(user_id, config),
    )


@router.post("/password-reset/request", response_model=MessageResponse)
async def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    db: Database = Depends(get_db),
    config: ApiConfig = Depends(get_config),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> MessageResponse:
    _rate_limit(request, limiter)
    user = db.get_user_by_email(payload.email)
    # Always return the same response to avoid leaking which emails exist.
    if user is not None:
        token = create_reset_token(int(user["id"]), config)
        # Persist the token so it can be single-use / revocable.
        claims = decode_token(token, TOKEN_RESET, config)
        expires_at = datetime.fromtimestamp(
            int(claims["exp"]), tz=timezone.utc
        ).isoformat()
        db.create_password_reset(token, int(user["id"]), expires_at)
        # In a real deployment this token is emailed; here it is logged so an
        # operator can complete the flow without an email provider wired up.
        logger.info("[auth] password reset token issued for user id={}", user["id"])
        return MessageResponse(
            message="if the account exists, a reset token has been issued",
        )
    return MessageResponse(
        message="if the account exists, a reset token has been issued",
    )


@router.post("/password-reset/confirm", response_model=MessageResponse)
async def confirm_password_reset(
    payload: PasswordResetConfirm,
    db: Database = Depends(get_db),
    config: ApiConfig = Depends(get_config),
) -> MessageResponse:
    try:
        claims = decode_token(payload.token, TOKEN_RESET, config)
        user_id = int(claims["sub"])
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired reset token",
        ) from exc

    record = db.get_password_reset(payload.token)
    if record is None or record.get("used"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reset token already used or unknown",
        )
    db.set_user_password(user_id, hash_password(payload.new_password))
    db.mark_password_reset_used(payload.token)
    logger.info("[auth] password reset completed for user id={}", user_id)
    return MessageResponse(message="password updated")
