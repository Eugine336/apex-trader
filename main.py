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

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

from config import AppConfig, get_instruments_by_category, INSTRUMENT_REGISTRY

_EVENT_STORE_LOGGING_INITIALIZED = False


def _apply_log_level(level: str) -> None:
    """Apply the configured console log level."""
    lvl = str(level or "INFO").upper()
    try:
        logger.remove()
    except Exception:  # noqa: BLE001
        pass
    logger.add(sys.stderr, level=lvl)


def _initialize_event_store_logging() -> None:
    """Enable EventStore log sinks only after clean-start has completed."""
    global _EVENT_STORE_LOGGING_INITIALIZED
    if _EVENT_STORE_LOGGING_INITIALIZED:
        return

    from persistence.event_sink import event_store_sink, install_stdlib_intercept
    from persistence.event_store import shutdown_event_store

    logger.add(event_store_sink, level="DEBUG")
    install_stdlib_intercept()
    atexit.register(shutdown_event_store)
    _EVENT_STORE_LOGGING_INITIALIZED = True


def main() -> None:
    dashboard_mode   = "--dashboard"   in sys.argv
    force_rediscover = "--rediscover"  in sys.argv

    logger.info("=" * 60)
    logger.info("  APEX TRADER — Institutional-Grade Trading System")
    logger.info("=" * 60)

    config = AppConfig()
    _apply_log_level(config.log_level)

    # ── Multi-tenant: apply per-user trading overrides when this instance was
    # launched by the API control plane (APEX_USER_CONFIG points at a JSON file
    # of saved preferences). No-op for standalone single-user runs.
    try:
        from api.instance_config import (
            apply_user_overrides,
            load_user_overrides_from_env,
        )
        _user_overrides = load_user_overrides_from_env()
        if _user_overrides:
            apply_user_overrides(config, _user_overrides)
            _apply_log_level(config.log_level)
            logger.info(
                "[multi-tenant] applied per-user config overrides ({} keys)",
                len(_user_overrides),
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[multi-tenant] per-user override step skipped: {}", exc)

    try:
        from ops.logging_config import configure_structured_logging
        configure_structured_logging(getattr(config, "ops", None))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Structured logging setup skipped: {}", exc)

    logger.info(f"Instrument registry loaded — {len(INSTRUMENT_REGISTRY)} instruments")
    logger.info(f"Trading mode: {getattr(config, 'trading_mode', 'gold').upper()}")
    for cat in config.enabled_categories:
        instruments = get_instruments_by_category(cat)
        logger.info(f"  {cat.upper()}: {len(instruments)} instruments enabled")
    logger.info(f"Total enabled instruments: {config.total_instruments}")

    logger.info("Running broker symbol auto-discovery ...")
    try:
        from brain.broker_autodiscovery import run_autodiscovery
        run_autodiscovery(mt5_broker_name="auto", force=force_rediscover)
        logger.info("Broker auto-discovery complete")
    except Exception as exc:
        logger.warning("Auto-discovery skipped: {}", exc)

    logger.info("Phase 1 — Brain loaded (17 modules)")

    logger.info("Phase 3 — Trigger loaded (entry engine, patterns, validator)")

    logger.info("Phase 4 — Management loaded (trade manager, trailing, partial, re-entry)")

    logger.info("Phase 5 — Risk loaded (risk engine, position sizer, P&L tracker, spread, reporter)")

    logger.info("Phase 6 — Adaptive Optimizer loaded (analyzer, optimizers, learners)")

    from platforms import PlatformManager
    logger.info("Phase 7 — Platforms loaded (MT5 + Deriv connectors)")

    logger.info("Phase 8 — Dashboard available (--dashboard to launch)")

    # ── Clean-start: sync data junction + one-time learned-data purge ──
    # Best-effort and non-fatal. Logs before EventStore init stay on stderr,
    # which avoids opening data/apex_events.db before git sync/reset runs.
    try:
        db_cfg = getattr(config, "data_backup", None)
        if db_cfg is None or getattr(db_cfg, "clean_start_on_first_boot", True):
            from platforms.clean_start import (
                discard_corrupt_event_store,
                purge_stale_learned_data,
                sync_clean_state_from_remote,
            )

            branch = getattr(db_cfg, "sync_branch", "main") if db_cfg else "main"
            pull_res = sync_clean_state_from_remote(branch=branch)
            # The data-junction reset above restores whatever apex_events.db is
            # committed in the data repo. The event store is operational,
            # machine-local state — a corrupt committed copy would be restored
            # every boot, fail its integrity check, and rotate endlessly. Drop
            # a corrupt restored DB now, before the store opens, so it starts
            # fresh cleanly. A healthy DB is always left untouched.
            event_store_res = discard_corrupt_event_store()
            # The learned-data purge is opt-in (default OFF) so a normal restart
            # never wipes adaptive state — only the data-junction sync runs.
            if db_cfg is not None and getattr(db_cfg, "startup_purge_enabled", False):
                purge_res = purge_stale_learned_data()
            else:
                purge_res = "skipped (disabled)"
            logger.info(
                "[event-driven] clean-start — pull: {} | event-store: {} | purge: {}",
                pull_res,
                event_store_res,
                purge_res,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[startup] clean-start step skipped: {}", exc)

    _initialize_event_store_logging()

    # ── Build SystemContext (all subsystems) ─────────────────────────
    from core.system_context import SystemContext
    platform_manager = PlatformManager(config)
    sys_ctx = SystemContext.create(config, platform_manager)

    def _connect_and_bootstrap() -> dict:
        """Connect brokers + bootstrap spreads + log the LIVE banner.

        Shared by headless and dashboard modes. Returns the connection status.
        """
        logger.info("Connecting to platforms…")
        connection_status = platform_manager.connect_all()

        try:
            from risk.spread_bootstrap import bootstrap_spreads
            if connection_status.get("mt5") or connection_status.get("deriv"):
                bootstrap_spreads(platform_manager=platform_manager)
        except Exception as exc:
            logger.warning("Spread bootstrap failed (hardcoded values used): {}", exc)

        logger.info("-" * 60)
        logger.info("APEX TRADER IS LIVE")
        logger.info("  MT5   — {}", "ONLINE" if connection_status.get("mt5") else "OFFLINE")
        logger.info("  Deriv — {}", "ONLINE" if connection_status.get("deriv") else "OFFLINE")
        logger.info("-" * 60)
        return connection_status

    def _run_startup_self_test() -> bool:
        """Run the pre-trade self-test, logging each check. Returns pass/fail."""
        from platforms.startup_check import StartupCheck
        passed, results = StartupCheck().run_all()
        for r in results:
            lvl = "INFO" if r.passed else "ERROR"
            logger.log(lvl, "  [{}] {} — {} ({:.0f}ms)", "✅" if r.passed else "❌", r.name, r.message, r.duration_ms)
        return passed

    if dashboard_mode:
        import threading
        import uvicorn
        from dashboard.state import LiveState
        from dashboard.api import create_app

        # Bind the read-only dashboard server FIRST, then boot the trading engine
        # in the background. The control-plane proxy (api/routes/engine_proxy.py)
        # can only serve the live panels once this port is listening, so binding
        # must not wait on — or be blocked forever by — a slow broker connect or
        # a failed startup self-test. LiveState serves stable fallback shapes
        # until the engine attaches via set_event_driven_system().
        state = LiveState()
        state.attach(None, platform_manager, {}, system_context=sys_ctx)

        engine_holder: dict = {"ed_system": None}

        def _boot_engine() -> None:
            try:
                connection_status = _connect_and_bootstrap()
                state.attach(
                    None, platform_manager, connection_status, system_context=sys_ctx
                )
                if not platform_manager.any_connected:
                    logger.warning(
                        "No platforms connected — dashboard will show empty data"
                    )
                    return
                if not _run_startup_self_test():
                    logger.error(
                        "Startup self-test FAILED — engine not started; dashboard "
                        "stays up for diagnostics"
                    )
                    return
                from event_driven_bootstrap import EventDrivenSystem
                ed_system = EventDrivenSystem(config, platform_manager, ctx=sys_ctx)
                ed_system.start()
                engine_holder["ed_system"] = ed_system
                state.set_event_driven_system(ed_system)
                logger.info("Event-driven system started in dashboard mode")
            except Exception as exc:  # noqa: BLE001
                logger.exception("[dashboard] trading-engine boot failed: {}", exc)

        threading.Thread(
            target=_boot_engine, name="apex-engine-boot", daemon=True
        ).start()

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
            ed_system = engine_holder.get("ed_system")
            if ed_system is not None:
                ed_system.stop()
    else:
        _connect_and_bootstrap()
        if not platform_manager.any_connected:
            logger.error("No platforms connected — cannot trade. Set DERIV_CLIENT_ID and DERIV_ACCESS_TOKEN in .env")
            return
        if not _run_startup_self_test():
            logger.error("Startup self-test FAILED — refusing to start trading to protect capital")
            return
        from event_driven_bootstrap import EventDrivenSystem
        ed_system = EventDrivenSystem(config, platform_manager, ctx=sys_ctx)
        ed_system.run_forever()


if __name__ == "__main__":
    main()
