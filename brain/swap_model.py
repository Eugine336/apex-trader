"""
APEX TRADER — Swap / Rollover Financing Model (Phase 1: Observability Only)

Deterministic, network-free model that estimates overnight financing costs
from a user-supplied rate table.  Phase 1 captures and journals the modeled
cost alongside each closed trade — it does NOT subtract financing from
reported PnL, alter sizing, or influence any trade decision.

Rate table JSON schema (data/swap_rates.json):
    {
        "EURUSD": {
            "long_per_lot_per_night":  -6.50,
            "short_per_lot_per_night":  1.20
        },
        ...
    }

    Keys   — uppercase symbol names matching the instrument registry.
    Values — account-currency cost per 1.0 standard lot per overnight
             rollover.  Positive = credit, negative = cost.  A symbol
             may legitimately have 0.0 for one or both directions.

If the rate file is absent or a symbol is not listed, the model returns
(None, "unavailable") — never a fabricated number.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


def load_swap_rates(path: str) -> dict[str, dict[str, float]]:
    """Load per-symbol swap rates from a JSON file.

    Returns an empty dict when the file does not exist or contains
    invalid JSON — making every symbol "unavailable" rather than
    raising or inventing rates.
    """
    p = Path(path)
    if not p.exists():
        logger.debug("[swap_model] Rate file not found: {} — all symbols unavailable", path)
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning("[swap_model] Rate file root is not a JSON object — treating as empty")
            return {}
        return data
    except json.JSONDecodeError as exc:
        logger.warning("[swap_model] Invalid JSON in rate file {}: {} — treating as empty", path, exc)
        return {}


def _count_rollover_nights(
    open_time: datetime,
    close_time: datetime,
    rollover_hour_utc: int,
    triple_weekday: int,
) -> int:
    """Count the number of rollover-night equivalents between two timestamps.

    A rollover boundary occurs at ``rollover_hour_utc`` (hour-of-day, UTC)
    on each calendar day.  Any boundary that falls on ``triple_weekday``
    (Python weekday, Monday=0) counts as 3 nights instead of 1.
    """
    if open_time.tzinfo is None:
        open_time = open_time.replace(tzinfo=timezone.utc)
    if close_time.tzinfo is None:
        close_time = close_time.replace(tzinfo=timezone.utc)

    if close_time <= open_time:
        return 0

    open_utc = open_time.astimezone(timezone.utc)
    close_utc = close_time.astimezone(timezone.utc)

    start_date = open_utc.date()
    end_date = close_utc.date()

    nights = 0
    current = start_date
    while current <= end_date:
        boundary = datetime(current.year, current.month, current.day, rollover_hour_utc, 0, 0, tzinfo=timezone.utc)
        if open_utc < boundary <= close_utc:
            nights += 3 if boundary.weekday() == triple_weekday else 1
        current = datetime(current.year, current.month, current.day, tzinfo=timezone.utc).date()
        from datetime import timedelta

        current = current + timedelta(days=1)

    return nights


def estimate_swap(
    symbol: str,
    direction: str,
    lots: float,
    open_time: datetime,
    close_time: datetime,
    *,
    rates: dict[str, dict[str, float]],
    rollover_hour_utc: int = 21,
    triple_weekday: int = 2,
) -> tuple[Optional[float], str]:
    """Estimate the overnight financing cost for a closed position.

    Returns
    -------
    (value, status)
        value : modeled swap cost in account currency, or None if the
                symbol is not in the rate table.
        status : ``"modeled"`` when a rate was found (value may be 0.0
                 for zero-night holds or a genuinely-zero rate),
                 ``"unavailable"`` when the symbol has no rate entry.
    """
    key = symbol.upper()
    if key not in rates:
        return (None, "unavailable")

    entry = rates[key]
    is_long = direction.upper() in ("BUY", "LONG")
    rate_key = "long_per_lot_per_night" if is_long else "short_per_lot_per_night"
    rate = entry.get(rate_key, 0.0)

    nights = _count_rollover_nights(open_time, close_time, rollover_hour_utc, triple_weekday)

    value = round(rate * lots * nights, 4)
    return (value, "modeled")
