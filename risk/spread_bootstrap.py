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
    mt5_connector=None,       # legacy single-connector path (still accepted)
    deriv_connector=None,
    mt5_connectors=None,      # preferred: list of MT5Connector for multi-broker support
    sample_retries: int = 3,
) -> None:
    """
    Query live bid/ask for every registered symbol and update
    INSTRUMENT_REGISTRY with the observed spread as typical_spread_pips.

    Accepts either mt5_connectors (list) or the legacy mt5_connector (single).
    For BOTH-platform symbols, tries each MT5 broker in turn until one
    returns a valid spread, then falls back to Deriv.

    Only updates symbols that can actually be priced right now.
    Symbols that fail (market closed, not available on connector)
    keep their hardcoded fallback — they won't be traded anyway.
    """
    # Normalise to a list regardless of which arg was passed
    if mt5_connectors is None:
        mt5_connectors = [mt5_connector] if mt5_connector is not None else []

    updated = 0
    failed = 0

    for symbol, info in list(_cfg.INSTRUMENT_REGISTRY.items()):
        spread = None

        if info.platform == _cfg.Platform.DERIV:
            if deriv_connector is not None:
                spread = _sample_spread(deriv_connector, symbol, sample_retries)

        elif info.platform == _cfg.Platform.MT5:
            for conn in mt5_connectors:
                spread = _sample_spread(conn, symbol, sample_retries)
                if spread is not None:
                    break

        else:
            # Platform.BOTH — try each MT5 broker first, then Deriv
            for conn in mt5_connectors:
                spread = _sample_spread(conn, symbol, sample_retries)
                if spread is not None:
                    break
            if spread is None and deriv_connector is not None:
                spread = _sample_spread(deriv_connector, symbol, sample_retries)

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
    Bails immediately on 'not found' errors to avoid log spam.
    """
    samples: list[float] = []
    for attempt in range(retries):
        try:
            tick = connector.get_tick(symbol)
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
