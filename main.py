"""
APEX TRADER — Entry Point
Sharp. Precise. Always watching.
Two arms, one mind. MT5 for Forex/indices. Deriv for synthetics — 24/7.

Usage:
    python main.py               # Start trading only (headless)
    python main.py --dashboard   # Start trading + dashboard on port 8000
    python main.py --rediscover  # Force broker symbol re-discovery then trade
"""

import bootstrap.datadog_init  # noqa: F401
import atexit
import os
import sys
import threading

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

from persistence.event_sink import event_store_sink, install_stdlib_intercept
from persistence.event_store import get_event_store, shutdown_event_store

logger.add(event_store_sink, level="DEBUG")
install_stdlib_intercept()
atexit.register(shutdown_event_store)

from config import AppConfig, get_instruments_by_category, INSTRUMENT_REGISTRY


def _apply_log_level(level: str) -> None:
    """Apply the configured console log level.

    The console (stderr) sink is re-added at ``config.log_level`` while the
    event-store sink is preserved at DEBUG so the dashboard/persistence keep
    full fidelity regardless of the console verbosity. The stdlib intercept is
    independent of the loguru sinks and does not need re-installing.
    """
    lvl = str(level or "INFO").upper()
    try:
        logger.remove()
    except Exception:  # noqa: BLE001
        pass
    logger.add(sys.stderr, level=lvl)
    logger.add(event_store_sink, level="DEBUG")


def _start_trading_loop(trading_loop) -> None:
    """Run the TradingLoop cycle in a background thread.

    Uses _run_supervised_cycle() so a single bad cycle cannot kill
    the thread — the supervisor absorbs per-cycle exceptions and only
    halts after max_consecutive_cycle_failures back-to-back failures.
    """
    import time as _time

    try:
        while trading_loop.running:
            trading_loop._run_supervised_cycle()
            _time.sleep(trading_loop._get_sleep_interval())
    except KeyboardInterrupt:
        trading_loop.running = False
    except Exception as exc:
        logger.error("Trading loop thread unexpected escape: {}", exc)
        trading_loop.running = False


def main() -> None:
    dashboard_mode   = "--dashboard"   in sys.argv
    force_rediscover = "--rediscover"  in sys.argv  # force broker symbol re-discovery and overwrite JSONs

    logger.info("=" * 60)
    logger.info("  APEX TRADER — Institutional-Grade Trading System")
    logger.info("=" * 60)

    config = AppConfig()
    _apply_log_level(config.log_level)

    # Ops: optional structured JSON logging + trade/risk audit streams. Additive
    # — composes with the console + event-store sinks above. Never fatal.
    try:
        from ops.logging_config import configure_structured_logging
        configure_structured_logging(getattr(config, "ops", None))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Structured logging setup skipped: {}", exc)

    logger.info(f"Instrument registry loaded — {len(INSTRUMENT_REGISTRY)} instruments")
    for cat in config.enabled_categories:
        instruments = get_instruments_by_category(cat)
        logger.info(f"  {cat.upper()}: {len(instruments)} instruments enabled")
    logger.info(f"Total enabled instruments: {config.total_instruments}")

    # ── Auto-discover broker symbols — runs silently if config is fresh ──
    logger.info("Running broker symbol auto-discovery ...")
    try:
        from brain.broker_autodiscovery import run_autodiscovery
        run_autodiscovery(mt5_broker_name="auto", force=force_rediscover)
        logger.info("Broker auto-discovery complete")
    except Exception as exc:
        logger.warning("Auto-discovery skipped: {}", exc)

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

    from adaptive import AdaptiveOptimizer, TradeAnalyzer, ScoreOptimizer, RegimeLearner, PairLearner, SessionLearner
    MLAdapter = AdaptiveOptimizer
    logger.info("Phase 6 — Adaptive Optimizer loaded (analyzer, optimizers, learners)")

    from platforms import PlatformManager, TradingLoop
    logger.info("Phase 7 — Platforms loaded (MT5 + Deriv connectors, trading loop)")

    logger.info("Phase 8 — Dashboard available (--dashboard to launch)")

    trading_loop = TradingLoop(config)
    platform_manager = trading_loop.platforms

    logger.info("Connecting to platforms…")
    connection_status = platform_manager.connect_all()

    # Bootstrap real spreads from live brokers — patches INSTRUMENT_REGISTRY
    # so every symbol uses actual observed spreads, not hardcoded guesses.
    try:
        from risk.spread_bootstrap import bootstrap_spreads
        bootstrap_spreads(
            mt5_connectors=[
                c for i, c in enumerate(platform_manager.mt5_connectors)
                if platform_manager._mt5_connected_flags[i]
            ] if connection_status.get("mt5") else [],
            deriv_connector=platform_manager.deriv if connection_status.get("deriv") else None,
        )
    except Exception as exc:
        logger.warning("Spread bootstrap failed (hardcoded values used): {}", exc)

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
        from platforms.startup_check import StartupCheck

        state = LiveState()
        state.attach(trading_loop, platform_manager, connection_status)

        if platform_manager.any_connected:
            passed, results = StartupCheck().run_all()
            for r in results:
                lvl = "INFO" if r.passed else "ERROR"
                logger.log(lvl, "  [{}] {} — {} ({:.0f}ms)", "✅" if r.passed else "❌", r.name, r.message, r.duration_ms)
            if not passed:
                logger.error("Startup self-test FAILED — refusing to start dashboard trading to protect capital")
                return

            trading_loop.running = True
            trading_loop._perform_startup_recovery()
            t = threading.Thread(target=_start_trading_loop, args=(trading_loop,), daemon=True)
            t.start()
            logger.info("Trading loop started in background thread")
        else:
            logger.warning("No platforms connected — dashboard will show empty data")

        app = create_app(state)
        bind_host = os.getenv("DD_DASHBOARD_BIND_HOST", "127.0.0.1")
        bind_port = int(os.getenv("DD_DASHBOARD_PORT", "8000"))

        if bind_host != "127.0.0.1" and not os.getenv("DD_DASHBOARD_API_KEY"):
            logger.critical(
                "REFUSING to bind dashboard on {} without DD_DASHBOARD_API_KEY — "
                "set an API key or use the default 127.0.0.1 bind host",
                bind_host,
            )
            return

        logger.info("Launching dashboard on http://{}:{}", bind_host, bind_port)
        try:
            uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
        finally:
            trading_loop.running = False
    else:
        if not platform_manager.any_connected:
            logger.error("No platforms connected — cannot trade. Set DERIV_CLIENT_ID and DERIV_ACCESS_TOKEN in .env")
            return
        trading_loop.run()


if __name__ == "__main__":
    main()
