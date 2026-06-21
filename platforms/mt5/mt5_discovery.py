"""APEX TRADER — MT5 raw discovery helpers (Execution Division gateway).

The Execution Division is the **sole** owner of broker APIs.  Broker
auto-discovery needs raw MetaTrader5 access (full symbol list, terminal
company name, per-symbol economics) *before* a configured ``MT5Connector``
session exists, so these thin helpers own the ``MetaTrader5`` import and its
``initialize()/shutdown()`` lifecycle here — inside ``platforms/mt5`` — instead
of leaking the broker import into ``brain/``.

All functions are best-effort: they return empty/None when MT5 is not
installed or no terminal is attached, and always shut the session down.
"""

from __future__ import annotations

import re
from typing import Optional

from loguru import logger

_MT5_AVAILABLE = False
try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]


def mt5_available() -> bool:
    """True when the MetaTrader5 package is importable."""
    return _MT5_AVAILABLE and mt5 is not None


def detect_broker_slug() -> Optional[str]:
    """Attach to the running MT5 terminal and return a filename-safe broker slug.

    Returns ``None`` when MT5 is unavailable, no terminal is attached, or the
    company name cannot be read.  Always shuts the session down.
    """
    if not mt5_available():
        return None
    try:
        if not mt5.initialize():
            return None
        try:
            info = mt5.terminal_info()
            if info is None:
                return None
            raw = getattr(info, "company", "unknown")
            slug = re.sub(r"[^a-z0-9]", "_", str(raw).lower()).strip("_")
            slug = re.sub(r"_+", "_", slug)
            if slug:
                logger.info("MT5 broker detected: '{}' → slug: '{}'", raw, slug)
            return slug or None
        finally:
            mt5.shutdown()
    except Exception as exc:
        logger.warning("[mt5-discovery] broker detection failed: {}", exc)
        return None


def list_broker_symbols() -> list[str]:
    """Return every symbol name the attached MT5 broker offers (raw names).

    Returns ``[]`` when MT5 is unavailable / not connected / has no symbols.
    Always shuts the session down.
    """
    if not mt5_available():
        logger.warning("MetaTrader5 not installed — skipping symbol discovery")
        return []
    try:
        if not mt5.initialize():
            logger.warning("MT5 not connected — skipping symbol discovery")
            return []
        try:
            all_symbols = mt5.symbols_get()
            if not all_symbols:
                logger.warning("MT5 returned no symbols")
                return []
            return [s.name for s in all_symbols]
        finally:
            mt5.shutdown()
    except Exception as exc:
        logger.warning("[mt5-discovery] symbol listing failed: {}", exc)
        return []


def discover_symbol_constraints(broker_symbols: list[str]) -> dict[str, dict]:
    """Return per-symbol broker-truth constraints for the given raw symbols.

    For each broker symbol, forces it visible in Market Watch
    (``symbol_select``) then reads ``symbol_info`` for volume/stops/digits.
    Returns ``{}`` when MT5 is unavailable.  Always shuts the session down.
    """
    if not broker_symbols or not mt5_available():
        return {}
    constraints: dict[str, dict] = {}
    try:
        if not mt5.initialize():
            return {}
        try:
            for broker_symbol in broker_symbols:
                # symbol_info() returns None if the symbol isn't selected in
                # Market Watch — call symbol_select() first to force it visible.
                mt5.symbol_select(broker_symbol, True)
                info = mt5.symbol_info(broker_symbol)
                if info is not None:
                    constraints[broker_symbol] = {
                        "volume_min": round(info.volume_min, 8),
                        "volume_max": round(info.volume_max, 2),
                        "volume_step": round(info.volume_step, 8),
                        "stops_level": int(info.trade_stops_level),
                        "digits": int(info.digits),
                        "point": float(info.point),
                        "contract_size": float(info.trade_contract_size),
                    }
        finally:
            mt5.shutdown()
    except Exception as exc:
        logger.warning("[mt5-discovery] constraint discovery failed: {}", exc)
    return constraints
