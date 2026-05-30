"""
APEX TRADER — Spread Bootstrap
Fetches live spreads from MT5 and Deriv at startup and patches
the INSTRUMENT_REGISTRY so every symbol has a real typical_spread_pips
instead of a hardcoded guess.

Called once by main.py after platforms connect.
Results are cached in-process — no file I/O needed.
"""

from dataclasses import replace
from loguru import logger

import config as _cfg


def bootstrap_spreads(
    mt5_connector=None,
    deriv_connector=None,
    sample_retries: int = 3,
) -> None:
    """
    Query live bid/ask for every registered symbol and update
    INSTRUMENT_REGISTRY with the observed spread as typical_spread_pips.

    Only updates symbols that can actually be priced right now.
    Symbols that fail (market closed, not available on connector)
    keep their hardcoded fallback — they won't be traded anyway.
    """
    updated = 0
    failed = 0

    for symbol, info in list(_cfg.INSTRUMENT_REGISTRY.items()):
        # Pick the right connector
        if info.platform == _cfg.Platform.DERIV:
            connector = deriv_connector
        elif info.platform == _cfg.Platform.MT5:
            connector = mt5_connector
        else:
            # Platform.BOTH — prefer MT5 for spread reference
            connector = mt5_connector or deriv_connector

        if connector is None:
            continue

        spread = _sample_spread(connector, symbol, sample_retries)
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


def _sample_spread(connector, symbol: str, retries: int) -> float | None:
    """
    Try up to `retries` times to get a clean spread reading.
    Returns the median of successful samples, or None on total failure.
    """
    samples: list[float] = []
    for _ in range(retries):
        try:
            tick = connector.get_tick(symbol)
            if tick and tick.spread > 0:
                samples.append(tick.spread)
        except Exception as exc:
            logger.debug("Spread sample failed for {}: {}", symbol, exc)

    if not samples:
        return None

    samples.sort()
    # Use median to avoid outliers from momentary widening
    return round(samples[len(samples) // 2], 2)
