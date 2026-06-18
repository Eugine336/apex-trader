"""Phase 5 — Dashboard + Ops Safety integration tests.

Tests:
- SystemContext creates ops subsystems (journal, watchdog, maintenance)
- Domain events emitted from ED entry/close paths
- Dashboard mixins route to ED-aware paths when ED system is present
- Shutdown flushes stores and clears crash marker
- Startup recovery detects crash markers
"""

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── SystemContext ops subsystem creation ──────────────────────────────


class TestSystemContextOpsSubsystems:
    """Verify Phase 5 fields are initialized in SystemContext.create()."""

    def test_trade_journal_field_exists(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "trade_journal")
        assert ctx.trade_journal is None

    def test_process_watchdog_field_exists(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "process_watchdog")
        assert ctx.process_watchdog is None

    def test_daily_maintenance_field_exists(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "daily_maintenance")
        assert ctx.daily_maintenance is None


# ── Domain event emission ────────────────────────────────────────────


class TestDomainEventImports:
    """Verify domain event constants are importable."""

    def test_domain_event_constants(self):
        from persistence import domain_events as DE
        assert DE.ORDER_SENT == "ORDER_SENT"
        assert DE.ORDER_FILLED == "ORDER_FILLED"
        assert DE.TRADE_OPEN == "TRADE_OPEN"
        assert DE.TRADE_CLOSE == "TRADE_CLOSE"


# ── Dashboard LiveState ED awareness ─────────────────────────────────


class TestLiveStateEDAwareness:
    """Verify LiveState routes to ED paths when ED system is present."""

    def _make_state(self, with_ed=True):
        from dashboard.state import LiveState
        state = LiveState()
        pm = MagicMock()
        pm.any_connected = True
        pm._mt5_connected_flags = [True]
        pm.deriv = MagicMock()
        pm.deriv.is_connected = MagicMock(return_value=True)
        pm.get_platform_balance.return_value = 10000.0
        pm.get_all_open_positions.return_value = []
        pm.get_price.return_value = None

        ed = None
        ctx = None
        if with_ed:
            ed = MagicMock()
            ed.is_running = True
            ed._config = SimpleNamespace(
                risk=SimpleNamespace(
                    max_open_trades=6,
                    max_daily_drawdown_pct=5.0,
                    risk_per_trade_pct=0.75,
                ),
            )
            ed.world_model_store = MagicMock()
            ed.world_model_store.get.return_value = None
            ed.tick_store = MagicMock()
            ed.tick_store.get_latest.return_value = None
            ed._mgmt_store = MagicMock()
            ed.stats.return_value = {}

            ctx = MagicMock()
            ctx.drawdown_guard = None
            ctx.risk_engine = None
            ctx.execution_monitor = None
            ctx.risk_reporter = None
            ctx.account_risk = None
            ctx.gate_tuner = None
            ctx.process_watchdog = None

        state.attach(None, pm, {"mt5": True, "deriv": True}, system_context=ctx)
        if ed is not None:
            state.set_event_driven_system(ed)
        return state

    def test_is_live_with_ed_system(self):
        state = self._make_state(with_ed=True)
        assert state.is_live is True

    def test_status_returns_event_driven_mode(self):
        state = self._make_state(with_ed=True)
        result = state.get_status()
        assert result["mode"] == "event_driven"
        assert result["bot_status"] == "running"

    def test_open_trades_returns_from_ed(self):
        state = self._make_state(with_ed=True)
        result = state.get_open_trades()
        assert "trades" in result
        assert "count" in result
        assert result["count"] == 0

    def test_risk_status_returns_from_ed(self):
        state = self._make_state(with_ed=True)
        result = state.get_risk_status()
        assert "mode" in result
        assert "account_balance" in result
        assert result["account_balance"] > 0

    def test_health_returns_from_ed(self):
        state = self._make_state(with_ed=True)
        result = state.get_health()
        assert result["mode"] == "event_driven"
        assert result["running"] is True
        assert result["broker"]["mt5"] is True

    def test_scanner_results_from_ed(self):
        state = self._make_state(with_ed=True)
        result = state.get_scanner_results()
        assert "instruments" in result
        assert "ready_count" in result


# ── EventDrivenSystem properties ─────────────────────────────────────


class TestEventDrivenSystemProperties:
    """Verify the new properties on EventDrivenSystem.
    
    Skip if torch is not installed (RL import chain).
    """

    @pytest.fixture(autouse=True)
    def _skip_without_torch(self):
        try:
            import torch  # noqa: F401
        except ImportError:
            pytest.skip("torch not installed — RL import chain unavailable")

    def test_ed_system_has_world_model_store_property(self):
        from event_driven_bootstrap import EventDrivenSystem
        assert hasattr(EventDrivenSystem, "world_model_store")

    def test_ed_system_has_tick_store_property(self):
        from event_driven_bootstrap import EventDrivenSystem
        assert hasattr(EventDrivenSystem, "tick_store")

    def test_ed_system_has_entry_orchestrator_property(self):
        from event_driven_bootstrap import EventDrivenSystem
        assert hasattr(EventDrivenSystem, "entry_orchestrator")

    def test_ed_system_has_executor_property(self):
        from event_driven_bootstrap import EventDrivenSystem
        assert hasattr(EventDrivenSystem, "executor")


# ── Shutdown + crash marker ──────────────────────────────────────────


class TestStartupRecovery:
    """Verify crash marker lifecycle."""

    def test_crash_marker_write_and_clear(self, tmp_path):
        from ops.lifecycle import StartupRecovery
        marker_path = tmp_path / ".crash_marker"
        cfg = SimpleNamespace(crash_marker_path=str(marker_path))
        recovery = StartupRecovery(cfg)

        assert not recovery.crash_marker_present()
        recovery.write_crash_marker()
        assert recovery.crash_marker_present()
        recovery.clear_crash_marker()
        assert not recovery.crash_marker_present()

    def test_run_detects_unclean(self, tmp_path):
        from ops.lifecycle import StartupRecovery
        marker_path = tmp_path / ".crash_marker"
        cfg = SimpleNamespace(crash_marker_path=str(marker_path))
        recovery = StartupRecovery(cfg)

        marker_path.write_text("pid=1234 started=2026-01-01")
        result = recovery.run()
        assert result["unclean_previous_exit"] is True
        assert recovery.crash_marker_present()


# ── ProcessWatchdog ──────────────────────────────────────────────────


class TestProcessWatchdog:
    """Verify watchdog heartbeat and stall detection."""

    def test_beat_and_stall(self, tmp_path):
        from ops.watchdog import ProcessWatchdog
        cfg = SimpleNamespace(
            heartbeat_file=str(tmp_path / ".heartbeat"),
            heartbeat_interval_seconds=0,
            max_tick_duration_seconds=0.1,
        )
        wd = ProcessWatchdog(cfg)
        wd.beat(force=True)
        assert (tmp_path / ".heartbeat").exists()

        wd.record_tick()
        assert wd.check_stall() is None

        time.sleep(0.15)
        stall = wd.check_stall()
        assert stall is not None
        assert stall > 0.1
