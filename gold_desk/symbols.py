"""Instrument-agnostic symbol policy for market observation.

Apex observes every enabled instrument; no symbol is privileged. Evidence is keyed
by the exact configured symbol (case-insensitive), so the Brain always asks for
the same name the broker route uses.
"""
from __future__ import annotations


class SymbolPolicy:
    """Canonical (upper-case) symbol keys; any non-empty symbol is observable."""

    def canonical_symbol(self, raw: str) -> str | None:
        value = str(raw or "").strip().upper()
        return value or None

    def is_observable(self, raw: str) -> bool:
        return self.canonical_symbol(raw) is not None
