"""Canonical symbol policy for the Gold-only specialist."""
from __future__ import annotations

from typing import Sequence


class GoldOnlyGuard:
    """Exact allowlist for broker-specific XAUUSD aliases; no prefix matching."""

    DEFAULT_ALIASES = (
        "XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.M", "XAUUSD.PRO", "XAUUSD.A",
    )

    def __init__(self, allowed_symbols: Sequence[str] | None = None) -> None:
        aliases = allowed_symbols or self.DEFAULT_ALIASES
        self._aliases = {
            str(value).strip().upper(): "XAUUSD"
            for value in aliases if str(value).strip()
        }
        self._aliases["XAUUSD"] = "XAUUSD"

    def canonical_symbol(self, raw: str) -> str | None:
        return self._aliases.get(str(raw or "").strip().upper())

    def is_gold(self, raw: str) -> bool:
        return self.canonical_symbol(raw) == "XAUUSD"
