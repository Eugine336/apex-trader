"""Tests for core.system_context — Phase 1 SystemContext creation and risk wiring."""

from __future__ import annotations

import pytest

from core.system_context import SystemContext
from config import AppConfig


@pytest.fixture
def config():
    return AppConfig()


class _FakePM:
    """Minimal PlatformManager stub for tests."""
    def get_broker_name(self, symbol: str) -> str:
        return "test_broker"
    def get_account_id(self, symbol: str) -> str:
        return "12345"


class TestSystemContextCreate:
    """Test that SystemContext.create builds all risk subsystems."""

    def test_creates_all_risk_subsystems(self, config):
        pm = _FakePM()
        ctx = SystemContext.create(config, pm)

        assert ctx.risk_engine is not None
        assert ctx.drawdown_guard is not None
        assert ctx.correlation_engine is not None
        assert ctx.account_risk is not None
        assert ctx.risk_reporter is not None

    def test_account_key_caching(self, config):
        pm = _FakePM()
        ctx = SystemContext.create(config, pm)

        key1 = ctx.account_key("EURUSD", pm)
        assert key1 == "test_broker:12345"

        key2 = ctx.account_key("EURUSD", pm)
        assert key2 == key1

    def test_missing_subsystem_does_not_crash(self):
        ctx = SystemContext()
        assert ctx.risk_engine is None
        assert ctx.drawdown_guard is None
        assert ctx.portfolio_governor is None

    def test_drawdown_guard_has_correct_params(self, config):
        pm = _FakePM()
        ctx = SystemContext.create(config, pm)

        guard = ctx.drawdown_guard
        assert guard is not None

    def test_correlation_engine_has_correct_params(self, config):
        pm = _FakePM()
        ctx = SystemContext.create(config, pm)

        engine = ctx.correlation_engine
        assert engine is not None
        assert engine.max_correlated_trades == config.risk.max_correlated_trades


class TestManagementStateStoreSQLite:
    """Test SQLite persistence for ManagementStateStore."""

    def test_create_with_db(self, tmp_path):
        from execution.management_state import ManagementStateStore

        db_path = str(tmp_path / "test_mgmt.db")
        store = ManagementStateStore(db_path=db_path)
        assert len(store) == 0

        state = store.get_or_create("T001", stop_loss=1.1000, tp1=1.1200)
        assert state.ticket == "T001"
        assert state.stop_loss == 1.1000
        store.close()

    def test_persistence_across_restarts(self, tmp_path):
        from execution.management_state import ManagementStateStore

        db_path = str(tmp_path / "test_mgmt2.db")

        store1 = ManagementStateStore(db_path=db_path)
        store1.get_or_create(
            "T100",
            stop_loss=1.0500,
            tp1=1.0800,
            tp1_hit=True,
            at_breakeven=True,
        )
        store1.close()

        store2 = ManagementStateStore(db_path=db_path)
        state = store2.get("T100")
        assert state is not None
        assert state.stop_loss == 1.0500
        assert state.tp1 == 1.0800
        assert state.tp1_hit is True
        assert state.at_breakeven is True
        store2.close()

    def test_cleanup_removes_stale(self, tmp_path):
        from execution.management_state import ManagementStateStore

        db_path = str(tmp_path / "test_mgmt3.db")
        store = ManagementStateStore(db_path=db_path)
        store.get_or_create("T1")
        store.get_or_create("T2")
        store.get_or_create("T3")
        assert len(store) == 3

        removed = store.cleanup({"T2"})
        assert removed == 2
        assert len(store) == 1
        assert store.get("T2") is not None
        assert store.get("T1") is None
        store.close()

    def test_remove_deletes_from_db(self, tmp_path):
        from execution.management_state import ManagementStateStore

        db_path = str(tmp_path / "test_mgmt4.db")
        store1 = ManagementStateStore(db_path=db_path)
        store1.get_or_create("TX", stop_loss=1.0)
        store1.remove("TX")
        assert store1.get("TX") is None
        store1.close()

        store2 = ManagementStateStore(db_path=db_path)
        assert store2.get("TX") is None
        store2.close()

    def test_in_memory_only_fallback(self):
        from execution.management_state import ManagementStateStore

        store = ManagementStateStore()
        store.get_or_create("T1", stop_loss=1.0)
        assert len(store) == 1
        assert store.get("T1").stop_loss == 1.0

    def test_update_persists(self, tmp_path):
        from execution.management_state import ManagementStateStore

        db_path = str(tmp_path / "test_mgmt5.db")
        store1 = ManagementStateStore(db_path=db_path)
        store1.get_or_create("T5", stop_loss=1.0, trailing=False)
        store1.update("T5", trailing=True, stop_loss=1.05)
        store1.close()

        store2 = ManagementStateStore(db_path=db_path)
        state = store2.get("T5")
        assert state is not None
        assert state.trailing is True
        assert state.stop_loss == 1.05
        store2.close()
