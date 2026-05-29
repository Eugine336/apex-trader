"""
APEX TRADER — Entry Point
Sharp. Precise. Always watching.
Two arms, one mind. MT5 for Forex/indices. Deriv for synthetics — 24/7.
"""

import sys

from loguru import logger
from config import AppConfig, get_instruments_by_category, INSTRUMENT_REGISTRY


def main() -> None:
    logger.info("=" * 60)
    logger.info("  APEX TRADER — Institutional-Grade Trading System")
    logger.info("=" * 60)

    config = AppConfig()

    logger.info(f"Instrument registry loaded — {len(INSTRUMENT_REGISTRY)} instruments")
    for cat in config.enabled_categories:
        instruments = get_instruments_by_category(cat)
        logger.info(f"  {cat.upper()}: {len(instruments)} instruments enabled")

    logger.info(f"Total enabled instruments: {config.total_instruments}")

    from brain import (
        StructureEngine, LiquidityMapper, FVGDetector,
        OrderBlockDetector, CurrencyStrengthMeter, SessionEngine, NewsGuard,
    )
    logger.info("Brain modules loaded — market reading engine online")

    from scanner import PairScanner, PairRanker, ScanScheduler
    logger.info("Scanner module loaded — watching all instruments")

    from platforms import PlatformManager, TradingLoop
    logger.info("Platform connectors loaded — MT5 + Deriv ready")

logger.info("-" * 60)

if "--dashboard" in sys.argv:
    import uvicorn
    from dashboard.api import app  # noqa: F811
    logger.info("Launching dashboard on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
else:
    logger.info("APEX TRADER IS LIVE")
    logger.info("Run with --dashboard to start the web dashboard")
