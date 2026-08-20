"""Environment-backed configuration for the ChatGPT -> Binance path."""
from __future__ import annotations

import os


def get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if not value or not value.strip() else int(value)


def get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if not value or not value.strip() else float(value)


CHATGPT_RELAY_TOKEN = os.getenv("CHATGPT_RELAY_TOKEN", "")
CHATGPT_RELAY_HOST = os.getenv("CHATGPT_RELAY_HOST", "127.0.0.1")
CHATGPT_RELAY_PORT = get_int("CHATGPT_RELAY_PORT", 8790)

BINANCE_EXECUTION_MODE = os.getenv("BINANCE_EXECUTION_MODE", "PAPER").strip().upper()
BINANCE_LIVE_EXECUTION_ENABLED = get_bool("BINANCE_LIVE_EXECUTION_ENABLED", False)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_MAX_NOTIONAL_USDT = get_float("BINANCE_MAX_NOTIONAL_USDT", 100.0)
BINANCE_FUTURES_BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")

CHATGPT_SIGNAL_TIMEOUT_SECONDS = get_int("CHATGPT_SIGNAL_TIMEOUT_SECONDS", 90)
CHATGPT_ALLOW_DUPLICATES = get_bool("CHATGPT_ALLOW_DUPLICATES", False)
MAX_SIMULTANEOUS_POSITIONS = get_int("MAX_SIMULTANEOUS_POSITIONS", 3)
MAX_ACCOUNT_RISK_PERCENT = get_float("MAX_ACCOUNT_RISK_PERCENT", 1.0)


def validate() -> None:
    if BINANCE_EXECUTION_MODE not in {"PAPER", "LIVE"}:
        raise ValueError("BINANCE_EXECUTION_MODE must be PAPER or LIVE")
    if not CHATGPT_RELAY_TOKEN:
        raise ValueError("CHATGPT_RELAY_TOKEN is required")
    if BINANCE_MAX_NOTIONAL_USDT <= 0:
        raise ValueError("BINANCE_MAX_NOTIONAL_USDT must be positive")
    if MAX_SIMULTANEOUS_POSITIONS < 1:
        raise ValueError("MAX_SIMULTANEOUS_POSITIONS must be >= 1")
    if not 0 < MAX_ACCOUNT_RISK_PERCENT <= 100:
        raise ValueError("MAX_ACCOUNT_RISK_PERCENT must be > 0 and <= 100")
    if CHATGPT_SIGNAL_TIMEOUT_SECONDS <= 0:
        raise ValueError("CHATGPT_SIGNAL_TIMEOUT_SECONDS must be positive")
    if BINANCE_EXECUTION_MODE == "LIVE":
        if not BINANCE_LIVE_EXECUTION_ENABLED:
            raise ValueError("LIVE requires BINANCE_LIVE_EXECUTION_ENABLED=true")
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            raise ValueError("LIVE requires BINANCE_API_KEY and BINANCE_API_SECRET")
