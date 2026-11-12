"""Exact broker-position identity helpers shared by the executor and risk gate."""

from __future__ import annotations

from typing import Any


def position_identity_key(platform: str, ticket: str) -> str:
    """Return a composite key; preserve ticket-only keys for legacy singletons."""
    clean_ticket = str(ticket or "").strip()
    clean_platform = str(platform or "").strip().lower()
    return f"{clean_platform}::{clean_ticket}" if clean_platform else clean_ticket


def resolve_position_record(
    positions: dict[Any, dict],
    ticket: str,
    platform: str = "",
) -> dict | None:
    """Resolve one position by exact platform+ticket, never by an ambiguous ticket."""
    clean_ticket = str(ticket or "").strip()
    clean_platform = str(platform or "").strip().lower()
    if not clean_ticket:
        return None

    if clean_platform:
        exact = positions.get(position_identity_key(clean_platform, clean_ticket))
        if isinstance(exact, dict):
            return exact
        direct = positions.get(clean_ticket)
        if isinstance(direct, dict):
            record_platform = str(direct.get("platform") or "").strip().lower()
            if record_platform == clean_platform:
                return direct
            if clean_platform == "mt5" and record_platform.startswith("mt5_"):
                return direct
        matches = [
            value for value in positions.values()
            if isinstance(value, dict)
            and str(value.get("ticket") or "").strip() == clean_ticket
            and (
                str(value.get("platform") or "").strip().lower() == clean_platform
                or (
                    clean_platform == "mt5"
                    and str(value.get("platform") or "").strip().lower().startswith("mt5_")
                )
            )
        ]
    else:
        direct = positions.get(clean_ticket)
        if isinstance(direct, dict):
            return direct
        matches = [
            value for value in positions.values()
            if isinstance(value, dict)
            and str(value.get("ticket") or "").strip() == clean_ticket
        ]

    return matches[0] if len(matches) == 1 else None


def remove_position_record(
    positions: dict[Any, dict],
    ticket: str,
    platform: str = "",
) -> bool:
    """Remove the exact position and any unique ticket alias pointing to it."""
    record = resolve_position_record(positions, ticket, platform)
    if record is None:
        return False
    for key, value in list(positions.items()):
        if value is record:
            positions.pop(key, None)
    return True
