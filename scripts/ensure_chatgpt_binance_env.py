"""Append missing ChatGPT/Binance execution settings to the local .env.

This helper never replaces existing values. It is intended for a VPS checkout
where the repository already has an operator-managed .env with other credentials.
"""
from __future__ import annotations

from pathlib import Path


BLOCK = """
# ── ChatGPT signal relay ─────────────────────────────────────────────────────
CHATGPT_RELAY_TOKEN=
CHATGPT_RELAY_HOST=127.0.0.1
CHATGPT_RELAY_PORT=8790

# ── Binance Futures execution ────────────────────────────────────────────────
BINANCE_EXECUTION_MODE=PAPER
BINANCE_LIVE_EXECUTION_ENABLED=false
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_MAX_NOTIONAL_USDT=100
BINANCE_FUTURES_BASE_URL=https://fapi.binance.com

# ── Signal safety ────────────────────────────────────────────────────────────
CHATGPT_SIGNAL_TIMEOUT_SECONDS=90
CHATGPT_ALLOW_DUPLICATES=false
MAX_SIMULTANEOUS_POSITIONS=3
MAX_ACCOUNT_RISK_PERCENT=1.0
""".lstrip()

KEYS = (
    "CHATGPT_RELAY_TOKEN",
    "CHATGPT_RELAY_HOST",
    "CHATGPT_RELAY_PORT",
    "BINANCE_EXECUTION_MODE",
    "BINANCE_LIVE_EXECUTION_ENABLED",
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_MAX_NOTIONAL_USDT",
    "BINANCE_FUTURES_BASE_URL",
    "CHATGPT_SIGNAL_TIMEOUT_SECONDS",
    "CHATGPT_ALLOW_DUPLICATES",
    "MAX_SIMULTANEOUS_POSITIONS",
    "MAX_ACCOUNT_RISK_PERCENT",
)


def main() -> None:
    path = Path(".env")
    if not path.exists():
        raise SystemExit(".env does not exist; create it from the existing APEX configuration first.")

    text = path.read_text(encoding="utf-8")
    missing = [key for key in KEYS if not any(line.startswith(key + "=") for line in text.splitlines())]
    if not missing:
        print("ChatGPT/Binance environment is already configured.")
        return

    path.write_text(text.rstrip() + "\n\n" + BLOCK, encoding="utf-8")
    print("Added missing variables:")
    for key in missing:
        print(f"  {key}")
    print("Existing .env values were not changed.")


if __name__ == "__main__":
    main()
