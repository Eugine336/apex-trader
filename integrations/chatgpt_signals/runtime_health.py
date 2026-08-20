from __future__ import annotations

import os

from .binance_execution import BinanceExecutionError, BinanceSignedClient


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def check() -> int:
    mode = os.getenv("BINANCE_EXECUTION_MODE", "PAPER").upper()
    live_enabled = env_bool("BINANCE_LIVE_EXECUTION_ENABLED")
    relay_token = os.getenv("CHATGPT_RELAY_TOKEN", "")
    relay_host = os.getenv("CHATGPT_RELAY_HOST", "127.0.0.1")
    relay_port = os.getenv("CHATGPT_RELAY_PORT", "8790")
    max_notional = float(os.getenv("BINANCE_MAX_NOTIONAL_USDT", "100"))
    timeout = int(os.getenv("CHATGPT_SIGNAL_TIMEOUT_SECONDS", "90"))
    allow_duplicates = env_bool("CHATGPT_ALLOW_DUPLICATES")
    max_positions = int(os.getenv("MAX_SIMULTANEOUS_POSITIONS", "3"))
    max_risk = float(os.getenv("MAX_ACCOUNT_RISK_PERCENT", "1.0"))
    base_url = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com")

    print("=" * 64)
    print("APEX BINANCE / CHATGPT EXECUTION RUNTIME")
    print("=" * 64)
    print(f"Execution mode       : {mode}")
    print(f"Live enabled         : {'YES' if live_enabled else 'NO'}")
    print(f"Relay endpoint       : {relay_host}:{relay_port}")
    print(f"Relay token          : {'SET' if relay_token else 'MISSING'}")
    print(f"Binance endpoint     : {base_url}")
    print(f"Max notional         : {max_notional:g} USDT")
    print(f"Signal timeout       : {timeout}s")
    print(f"Duplicate signals    : {'ALLOWED' if allow_duplicates else 'BLOCKED'}")
    print(f"Max positions       : {max_positions}")
    print(f"Max account risk     : {max_risk:g}%")

    if mode not in {"PAPER", "LIVE"}:
        print("Status               : FAIL (invalid BINANCE_EXECUTION_MODE)")
        return 2
    if not relay_token:
        print("Status               : FAIL (CHATGPT_RELAY_TOKEN is missing)")
        return 2
    if max_notional <= 0 or timeout <= 0 or max_positions < 1 or not 0 < max_risk <= 100:
        print("Status               : FAIL (invalid safety configuration)")
        return 2

    if mode == "PAPER":
        print("Binance account      : NOT REQUIRED (paper mode)")
        print("Live trading         : DISABLED")
        print("Status               : READY FOR PAPER")
        print("=" * 64)
        return 0

    if not live_enabled:
        print("Status               : FAIL (LIVE mode requires explicit live enable)")
        return 2

    if not os.getenv("BINANCE_API_KEY") or not os.getenv("BINANCE_API_SECRET"):
        print("Status               : FAIL (Binance live credentials missing)")
        return 2

    try:
        client = BinanceSignedClient()
        account = client.account()
        can_trade = bool(account.get("canTrade", False))
        if not can_trade:
            print("Binance trading      : DISABLED")
            print("Status               : FAIL (Binance account cannot trade)")
            return 2

        print("Binance connection   : OK")
        print("Futures trading      : ENABLED")
        print("Withdrawals          : NOT USED BY EXECUTOR")
        print("Status               : LIVE-READY")
        print("=" * 64)
        return 0
    except BinanceExecutionError as exc:
        print(f"Status               : FAIL ({exc})")
        return 2


if __name__ == "__main__":
    raise SystemExit(check())
