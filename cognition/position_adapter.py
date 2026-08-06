"""APEX TRADER — live-book adapter for Brain position management.

Constitution Part VI: the AI Cognitive Brain must manage the ACTUAL open broker
positions — the live book — not an internal campaign ledger. Brain-originated
entries reach the broker through the execution plane and are never registered as
"campaigns", so sourcing manageable positions from the campaign registry left
the Brain with nothing to manage (open-and-forget). These pure helpers convert a
broker position's fields into the normalized view the Brain and the management
sink agree on.

All functions are pure and side-effect free (no broker I/O), so the mapping is
fully offline-testable.
"""

from __future__ import annotations

import time
from typing import Optional


def canonical_side(direction: str) -> str:
    """Normalize any side label to ``LONG``/``SHORT`` (``""`` if unknown).

    Broker positions report ``BUY``/``SELL``; the Brain and campaign layer speak
    ``LONG``/``SHORT``. Both sides of a management match MUST be compared on the
    same canonical axis — otherwise the management sink can never map a Brain
    verdict (``LONG``) onto its live broker position (``BUY``) and silently drops
    every EXIT/PARTIAL/PROTECT/TIGHTEN.
    """
    s = str(direction or "").strip().upper()
    if s in ("BUY", "LONG"):
        return "LONG"
    if s in ("SELL", "SHORT"):
        return "SHORT"
    return ""


def broker_profit_r(
    direction: str,
    entry_price: Optional[float],
    current_price: Optional[float],
    sl: Optional[float],
) -> Optional[float]:
    """Open profit in R multiples from broker prices; ``None`` if not computable.

    ``R`` is the initial risk (``|entry - sl|``). Returns a signed multiple:
    positive when the position is in profit, negative when underwater. Returns
    ``None`` when any price is missing/invalid or the stop distance is zero
    (so the Brain treats profit as simply unknown rather than fabricated).
    """
    try:
        entry = float(entry_price or 0.0)
        price = float(current_price or 0.0)
        stop = float(sl or 0.0)
    except (TypeError, ValueError):
        return None
    if entry <= 0.0 or price <= 0.0 or stop <= 0.0:
        return None
    risk = abs(entry - stop)
    if risk < 1e-12:
        return None
    side = canonical_side(direction)
    if side == "LONG":
        return (price - entry) / risk
    if side == "SHORT":
        return (entry - price) / risk
    return None


def hold_seconds_from(open_time: object) -> float:
    """Seconds a position has been open, from a tz-aware datetime or epoch.

    Returns ``0.0`` when the open time is unknown or in the future. Uses
    wall-clock time (broker ``open_time`` is wall-clock, never monotonic).
    """
    if open_time is None:
        return 0.0
    try:
        ts = (
            open_time.timestamp()
            if hasattr(open_time, "timestamp")
            else float(open_time)  # type: ignore[arg-type]
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return 0.0
    delta = time.time() - ts
    return delta if delta > 0.0 else 0.0


__all__ = ["canonical_side", "broker_profit_r", "hold_seconds_from"]
