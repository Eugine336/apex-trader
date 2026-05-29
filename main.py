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
        RegimeDetector, VolumeAnalyzer, InducementDetector, WyckoffEngine,
        MTFOrchestrator, TradeJournal, DrawdownGuard, ExecutionMonitor,
        CorrelationEngine, BacktestEngine,
    )
    logger.info("Phase 1 — Brain loaded (17 modules)")

    from scanner import PairScanner, PairRanker, ScanScheduler
    logger.info("Phase 2 — Scanner loaded (pair scanner, ranker, scheduler)")

    from trigger import EntryEngine, EntryPatternDetector, EntryValidator
    logger.info("Phase 3 — Trigger loaded (entry engine, patterns, validator)")

    from management import TradeManager, PartialCloseCalculator, StructureTrailingStop, ReEntryManager
    logger.info("Phase 4 — Management loaded (trade manager, trailing, partial, re-entry)")

    from risk import RiskEngine, PositionSizer, PnLTracker, SpreadMonitor, RiskReporter
    logger.info("Phase 5 — Risk loaded (risk engine, position sizer, P&L tracker, spread, reporter)")

    from ml import MLAdapter, TradeAnalyzer, ScoreOptimizer, RegimeLearner, PairLearner, SessionLearner
    logger.info("Phase 6 — ML loaded (adapter, analyzer, optimizers, learners)")

    from platforms import PlatformManager, TradingLoop
    logger.info("Phase 7 — Platforms loaded (MT5 + Deriv connectors, trading loop)")

    logger.info("Phase 8 — Dashboard available (--dashboard to launch)")

    logger.info("-" * 60)
    logger.info("APEX TRADER IS LIVE")
    logger.info("-" * 60)

    if "--dashboard" in sys.argv:
        import uvicorn
        from dashboard.api import app
        logger.info("Launching dashboard on http://localhost:8000")
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        logger.info("Run with --dashboard to start the web dashboard")


if __name__ == "__main__":
    main()
