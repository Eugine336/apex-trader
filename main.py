"""
APEX TRADER — Entry Point
Sharp. Precise. Always watching.
Two arms, one mind. MT5 for Forex/indices. Deriv for synthetics — 24/7.

Usage:
    python main.py               # Start trading only (headless)
    python main.py --dashboard   # Start trading + dashboard on port 8000
"""

import os
import sys
import threading

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

from config import AppConfig, get_instruments_by_category, INSTRUMENT_REGISTRY


def _start_trading_loop(trading_loop) -> None:
    """Run the TradingLoop cycle in a background thread.

    Uses run_once() directly instead of run() to avoid double-connecting
    platforms that were already connected in main().
    """
    import time as _time

    try:
        while trading_loop.running:
            trading_loop._check_daily_reset()
            trading_loop.run_once()
            interval = trading_loop._get_sleep_interval()
            _time.sleep(interval)
    except Exception as exc:
        logger.error("Trading loop crashed: {}", exc)
        trading_loop.running = False


def main() -> None:
    dashboard_mode = "--dashboard" in sys.argv

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

    trading_loop = TradingLoop(config)
    platform_manager = trading_loop.platforms

    logger.info("Connecting to platforms…")
    connection_status = platform_manager.connect_all()

    logger.info("-" * 60)
    logger.info("APEX TRADER IS LIVE")
    if connection_status.get("mt5"):
        logger.info("  MT5   — ONLINE")
    else:
        logger.info("  MT5   — OFFLINE")
    if connection_status.get("deriv"):
        logger.info("  Deriv — ONLINE")
    else:
        logger.info("  Deriv — OFFLINE")
    logger.info("-" * 60)

    if dashboard_mode:
        import uvicorn
        from dashboard.state import LiveState
        from dashboard.api import create_app

        state = LiveState()
        if platform_manager.any_connected:
            trading_loop.running = True
            state.attach(trading_loop, platform_manager, connection_status)
            trade_thread = threading.Thread(
                target=_start_trading_loop,
                args=(trading_loop,),
                daemon=True,
                name="TradingLoop",
            )
            trade_thread.start()
            logger.info("Trading loop started in background thread")
        else:
            logger.warning(
                "No platforms connected — dashboard will show empty data. "
                "Set DERIV_API_TOKEN and DERIV_APP_ID in .env to connect."
            )
            state.attach(trading_loop, platform_manager, connection_status)

        app = create_app(state)
        logger.info("Launching dashboard on http://0.0.0.0:8000")
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    else:
        if not platform_manager.any_connected:
            logger.error(
                "No platforms connected — cannot trade. "
                "Set DERIV_API_TOKEN and DERIV_APP_ID in .env"
            )
            logger.info("Run with --dashboard to start the web dashboard anyway")
            return
        trading_loop.run()


if __name__ == "__main__":
    main()
