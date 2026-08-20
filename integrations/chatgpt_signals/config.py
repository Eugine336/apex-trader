"""Centralized environment configuration for ChatGPT -> Binance execution."""
from __future__ import annotations

import os


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or not value.strip() else int(value)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None or not value.strip() else float(value)


CHATGPT_RELAY_TOKEN = os.getenv("CHATGPT_RELAY_TOKEN", "")
CHATGPT_RELAY_HOST = os.getenv("CHATGPT_RELAY_HOST", "127.0.0.1")
CHATGPT_RELAY_PORT = env_int("CHATGPT_RELAY_PORT", 8790)

BINANCE_EXECUTION_MODE = os.getenv("BINANCE_EXECUTION_MODE", "PAPER").strip().upper()
BINANCE_LIVE_EXECUTION_ENABLED = env_bool("BINANCE_LIVE_EXECUTION_ENABLED", False)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_MAX_NOTIONAL_USDT = env_float("BINANCE_MAX_NOTIONAL_USDT", 100.0)
BINANCE_FUTURES_BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")

CHATGPT_SIGNAL_TIMEOUT_SECONDS = env_int("CHATGPT_SIGNAL_TIMEOUT_SECONDS", 90)
CHATGPT_ALLOW_DUPLICATES = env_bool("CHATGPT_ALLOW_DUPLICATES", False)
MAX_SIMULTANEOUS_POSITIONS = env_int("MAX_SIMULTANEOUS_POSITIONS", 3)
MAX_ACCOUNT_RISK_PERCENT = env_float("MAX_ACCOUNT_RISK_PERCENT", 1.0)


def validate_config(*, live: bool | None = None) -> None:
    """Fail fast on unsafe/invalid execution configuration."""
    mode = BINANCE_EXECUTION_MODE
    if mode not in {"PAPER", "LIVE"}:
        raise ValueError("BINANCE_EXECUTION_MODE must be PAPER or LIVE")
    if BINANCE_MAX_NOTIONAL_USDT <= 0:
        raise ValueError("BINANCE_MAX_NOTIONAL_USDT must be positive")
    if MAX_SIMULTANEOUS_POSITIONS < 1:
        raise ValueError("MAX_SIMULTANEOUS_POSITIONS must be >= 1")
    if not 0 < MAX_ACCOUNT_RISK_PERCENT <= 100:
        raise ValueError("MAX_ACCOUNT_RISK_PERCENT must be > 0 and <= 100")
    if CHATGPT_SIGNAL_TIMEOUT_SECONDS <= 0:
        raise ValueError("CHATGPT_SIGNAL_TIMEOUT_SECONDS must be positive")
    if not CHATGPT_RELAY_TOKEN:
        raise ValueError("CHATGPT_RELAY_TOKEN is required")
    if mode == "LIVE" or live:
        if not BINANCE_LIVE_EXECUTION_ENABLED:
            raise ValueError("LIVE execution requires BINANCE_LIVE_EXECUTION_ENABLED=true")
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            raise ValueError("LIVE execution requires BINANCE_API_KEY and BINANCE_API_SECRET")
