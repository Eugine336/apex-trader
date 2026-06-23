"""APEX TRADER — API request/response schemas (Pydantic v2)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


# ── auth ───────────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class MessageResponse(BaseModel):
    message: str


# ── users ──────────────────────────────────────────────────────────────────
class UserResponse(BaseModel):
    id: int
    email: str
    is_active: bool
    is_admin: bool
    created_at: str


# ── broker credentials ───────────────────────────────────────────────────
class MT5CredentialsRequest(BaseModel):
    broker_type: str = "mt5"
    login: int
    password: str = Field(min_length=1)
    server: str = Field(min_length=1)
    label: str = ""


class DerivCredentialsRequest(BaseModel):
    broker_type: str = "deriv"
    access_token: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    account_type: str = "demo"
    client_id: str = ""
    label: str = ""


class BrokerCredentialResponse(BaseModel):
    broker_type: str
    label: str
    masked: dict[str, Any]
    created_at: str
    updated_at: str


# ── trading config ─────────────────────────────────────────────────────────
class TradingConfig(BaseModel):
    """User-tunable trading preferences applied to their isolated instance."""

    enabled_categories: Optional[list[str]] = None
    enabled_symbols_override: Optional[list[str]] = None
    risk_per_trade_pct: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    max_daily_drawdown_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    max_open_trades: Optional[int] = Field(default=None, ge=1, le=100)
    data_path_fixes_enabled: Optional[bool] = None
    log_level: Optional[str] = None


# ── trading instance ───────────────────────────────────────────────────────
class InstanceStatusResponse(BaseModel):
    user_id: int
    status: str
    pid: Optional[int] = None
    alive: bool = False
    restarts: int = 0
    last_error: str = ""
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None


class InstanceLogResponse(BaseModel):
    lines: list[str]


# ── dashboard ──────────────────────────────────────────────────────────────
class DashboardSummary(BaseModel):
    total_trades: int
    total_pnl: float
    win_rate: float
    wins: int
    losses: int
    best_trade: float
    worst_trade: float
    instance_status: str
    instance_alive: bool


class PositionResponse(BaseModel):
    order_id: str
    symbol: str
    direction: str
    lots: float
    entry_price: float
    sl: float
    tp1: float
    tp2: float
    open_time: str


class TradeResponse(BaseModel):
    id: int
    ticket: str
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pips: float
    exit_reason: str
    opened_at: Optional[str]
    closed_at: str


class EquityPoint(BaseModel):
    closed_at: Optional[str]
    equity: float
