"""Owner-controlled instrument-universe selection.

This module separates the owner's requested universe from the symbols a
connected broker actually exposes. Discovery is not permission to trade:
downstream broker-capability and risk admission remain mandatory.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class UniverseMode(str, Enum):
    ALL = "all"
    SELECTED = "selected"
    SINGLE = "single"


@dataclass(frozen=True)
class UniverseSelection:
    mode: UniverseMode = UniverseMode.ALL
    symbols: tuple[str, ...] = ()

    @classmethod
    def build(cls, mode: str | UniverseMode, symbols: Iterable[str] = ()) -> "UniverseSelection":
        raw_mode = mode.value if isinstance(mode, UniverseMode) else str(mode).strip().lower()
        raw_mode = {"full": "all", "custom": "selected", "one": "single"}.get(raw_mode, raw_mode)
        try:
            normalized_mode = UniverseMode(raw_mode)
        except ValueError as exc:
            raise ValueError("universe mode must be one of: all, selected, single") from exc

        normalized_symbols = tuple(dict.fromkeys(
            str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
        ))
        if normalized_mode is UniverseMode.ALL and normalized_symbols:
            raise ValueError("all mode cannot contain a symbol restriction")
        if normalized_mode is UniverseMode.SELECTED and not normalized_symbols:
            raise ValueError("selected mode requires at least one symbol")
        if normalized_mode is UniverseMode.SINGLE and len(normalized_symbols) != 1:
            raise ValueError("single mode requires exactly one symbol")
        return cls(mode=normalized_mode, symbols=normalized_symbols)


@dataclass(frozen=True)
class UniverseResolution:
    mode: UniverseMode
    requested_symbols: tuple[str, ...]
    available_symbols: tuple[str, ...]
    selected_symbols: tuple[str, ...]
    unavailable_symbols: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return bool(self.selected_symbols) and not self.unavailable_symbols

    @property
    def trading_enabled(self) -> bool:
        return self.valid


def resolve_universe(selection: UniverseSelection, broker_symbols: Iterable[str]) -> UniverseResolution:
    """Resolve an owner selection against live broker symbol discovery.

    Broker-native spelling is preserved. Missing requested symbols invalidate
    the resolution; they are never silently substituted or partially accepted.
    """
    available = tuple(dict.fromkeys(
        str(symbol).strip() for symbol in broker_symbols if str(symbol).strip()
    ))
    by_normalized = {symbol.upper(): symbol for symbol in available}

    if selection.mode is UniverseMode.ALL:
        requested: tuple[str, ...] = ()
        selected = available
        unavailable: tuple[str, ...] = ()
    else:
        requested = selection.symbols
        selected = tuple(by_normalized[symbol] for symbol in requested if symbol in by_normalized)
        unavailable = tuple(symbol for symbol in requested if symbol not in by_normalized)

    if selection.mode is UniverseMode.SINGLE and len(selected) != 1:
        unavailable = unavailable or selection.symbols
        selected = ()

    return UniverseResolution(
        mode=selection.mode,
        requested_symbols=requested,
        available_symbols=available,
        selected_symbols=selected,
        unavailable_symbols=unavailable,
    )
