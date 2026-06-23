"""APEX TRADER — User profile routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.auth import get_current_user
from api.models import UserResponse

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/me", response_model=UserResponse)
async def get_me(user: dict[str, Any] = Depends(get_current_user)) -> UserResponse:
    """Return the authenticated user's profile."""
    return UserResponse(
        id=int(user["id"]),
        email=user["email"],
        is_active=bool(user.get("is_active", 1)),
        is_admin=bool(user.get("is_admin", 0)),
        created_at=user["created_at"],
    )
