"""
APEX TRADER — Spread Bootstrap
Fetches live spreads at startup (through the PlatformManager gateway) and
patches the INSTRUMENT_REGISTRY so every symbol has a real
typical_spread_pips instead of a hardcoded guess.

Called once by main.py after platforms connect.
Results are cached in-process — no file I/O needed.
"""

from dataclasses import replace
from loguru import logger

import config as _cfg


def bootstrap_spreads(
    platform_manager=None,
    sample_retries: int = 3,
) -> None:
    """
    Query live bid/ask for every registered symbol and update
    INSTRUMENT_REGISTRY with the observed spread as typical_spread_pips.

    All broker access goes through the Execution Division gateway
    (PlatformManager), which routes each symbol to the correct connector
    (multi-broker MT5 / Deriv) — this module never touches a connector
    directly.

    Only updates symbols that can actually be priced right now.
    Symbols that fail (market closed, not available) keep their hardcoded
    fallback — they won't be traded anyway.
    """
    if platform_manager is None:
        logger.warning("Spread bootstrap skipped — no platform manager provided")
        return

    updated = 0
    failed = 0

    for symbol, info in list(_cfg.INSTRUMENT_REGISTRY.items()):
        spread = _sample_spread(platform_manager, symbol, sample_retries)

        if spread is None:
            failed += 1
            continue

        # Replace the frozen dataclass with an updated one
        _cfg.INSTRUMENT_REGISTRY[symbol] = replace(
            info, typical_spread_pips=spread
        )
        updated += 1
        logger.debug(
            "Spread bootstrap — {} typical_spread updated: {:.2f} pips (was {:.2f})",
            symbol, spread, info.typical_spread_pips,
        )

    logger.info(
        "Spread bootstrap complete — {} updated, {} kept hardcoded fallback",
        updated, failed,
    )


def _sample_spread(platform_manager, symbol: str, retries: int) -> float | None:
    """
    Try up to `retries` times to get a clean spread reading via the
    PlatformManager gateway. Returns the median of successful samples, or
    None on total failure. Bails immediately on 'not found' errors to avoid
    log spam.
    """
    samples: list[float] = []
    for attempt in range(retries):
        try:
            tick = platform_manager.get_price(symbol)
            if tick and tick.spread > 0:
                samples.append(tick.spread)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "terminal:" in msg or "invalid" in msg:
                if attempt == 0:
                    logger.debug("Spread sample failed for {}: {}", symbol, exc)
                return None
            logger.debug("Spread sample failed for {}: {}", symbol, exc)

    if not samples:
        return None

    samples.sort()
    # Use median to avoid outliers from momentary widening
    return round(samples[len(samples) // 2], 2)
