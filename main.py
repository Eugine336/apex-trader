"""
APEX TRADER — Entry Point
Sharp. Precise. Always watching.
"""

from loguru import logger
from config import AppConfig, get_instruments_by_category, INSTRUMENT_REGISTRY

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

logger.info("-" * 60)
logger.info("System ready. Waiting for market data feed…")
