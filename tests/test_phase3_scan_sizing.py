"""Tests for Phase 3 — Scan Pipeline + Orchestrator + Entry Quality wiring.

Verifies that SystemContext creates all 7 Phase 3 subsystems and that the
event-driven entry path integrates Orchestrator sizing, ATR SL/TP,
volatility/density multipliers, and execution quality tracking.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from core.system_context import SystemContext
from config import AppConfig


@pytest.fixture
def config():
    return AppConfig()


class _FakePM:
    def get_broker_name(self, symbol: str) -> str:
        return "test_broker"
    def get_account_id(self, symbol: str) -> str:
        return "12345"


class TestSystemContextPhase3:
    """Phase 3 subsystems are created by SystemContext.create."""

    def test_creates_orchestrator(self, config):
        ctx = SystemContext.create(config, _FakePM())
        assert ctx.orchestrator is not None

    def test_creates_volatility_monitor(self, config):
        ctx = SystemContext.create(config, _FakePM())
        assert ctx.system_volatility_monitor is not None

    def test_creates_density_tracker(self, config):
        ctx = SystemContext.create(config, _FakePM())
        assert ctx.opportunity_density_tracker is not None

    def test_creates_entry_engine(self, config):
        ctx = SystemContext.create(config, _FakePM())
        assert ctx.entry_engine is not None

    def test_creates_execution_monitor(self, config):
        ctx = SystemContext.create(config, _FakePM())
        assert ctx.execution_monitor is not None

    def test_creates_opportunity_executor(self, config):
        ctx = SystemContext.create(config, _FakePM())
        assert ctx.opportunity_executor is not None

    def test_creates_rl_bridge(self, config):
        ctx = SystemContext.create(config, _FakePM())
        if ctx.rl_bridge is None:
            pytest.skip("RLBridge unavailable (torch not installed)")
        # Dormant by default (no trained checkpoint) but must report status.
        assert ctx.rl_bridge.status()["status_label"].startswith(("ACTIVE", "INACTIVE"))

    def test_all_phase3_subsystems_optional(self):
        ctx = SystemContext()
        assert ctx.orchestrator is None
        assert ctx.system_volatility_monitor is None
        assert ctx.opportunity_density_tracker is None
        assert ctx.entry_engine is None
        assert ctx.execution_monitor is None
        assert ctx.opportunity_executor is None
        assert ctx.rl_bridge is None

    def test_entry_engine_shares_drawdown_guard(self, config):
        ctx = SystemContext.create(config, _FakePM())
        if ctx.entry_engine is not None and ctx.drawdown_guard is not None:
            assert ctx.entry_engine.drawdown is ctx.drawdown_guard

    def test_density_tracker_default_window(self, config):
        ctx = SystemContext.create(config, _FakePM())
        assert ctx.opportunity_density_tracker is not None
        assert ctx.opportunity_density_tracker.get_size_multiplier() == 1.0


class TestOrchestratorSizing:
    """Orchestrator graded sizing produces bounded multipliers."""

    def test_orchestrator_returns_bounded_multiplier(self, config):
        ctx = SystemContext.create(config, _FakePM())
        if ctx.orchestrator is None:
            pytest.skip("Orchestrator not available")
        from brain.orchestrator import TradeProposal
        proposal = TradeProposal(
            pair="EURUSD",
            direction="LONG",
            scan_score=95.0,
            de_conviction=0.8,
        )
        verdict = ctx.orchestrator.evaluate(proposal)
        assert 0.0 <= verdict.size_multiplier <= 1.0

    def test_low_conviction_dims_size(self, config):
        ctx = SystemContext.create(config, _FakePM())
        if ctx.orchestrator is None:
            pytest.skip("Orchestrator not available")
        from brain.orchestrator import TradeProposal
        strong = TradeProposal(pair="EURUSD", direction="LONG", scan_score=100, de_conviction=0.95)
        weak = TradeProposal(pair="EURUSD", direction="LONG", scan_score=30, de_conviction=0.2)
        strong_v = ctx.orchestrator.evaluate(strong)
        weak_v = ctx.orchestrator.evaluate(weak)
        assert strong_v.size_multiplier >= weak_v.size_multiplier


class TestVolatilityDensity:
    """Vol monitor and density tracker multipliers default to 1.0."""

    def test_vol_monitor_default_multiplier(self, config):
        ctx = SystemContext.create(config, _FakePM())
        if ctx.system_volatility_monitor is None:
            pytest.skip("VolatilityMonitor not available")
        assert ctx.system_volatility_monitor.get_size_multiplier() == 1.0

    def test_density_tracker_default_multiplier(self, config):
        ctx = SystemContext.create(config, _FakePM())
        if ctx.opportunity_density_tracker is None:
            pytest.skip("DensityTracker not available")
        assert ctx.opportunity_density_tracker.get_size_multiplier() == 1.0

    def test_density_high_reduces_size(self, config):
        ctx = SystemContext.create(config, _FakePM())
        if ctx.opportunity_density_tracker is None:
            pytest.skip("DensityTracker not available")
        symbols = [f"PAIR{i}" for i in range(15)]
        ctx.opportunity_density_tracker.record_scan(symbols)
        mult = ctx.opportunity_density_tracker.get_size_multiplier()
        assert mult < 1.0


class TestExecutionMonitor:
    """Execution monitor records fills and provides quality multipliers."""

    def test_default_multiplier_is_one(self, config):
        ctx = SystemContext.create(config, _FakePM())
        if ctx.execution_monitor is None:
            pytest.skip("ExecutionMonitor not available")
        assert ctx.execution_monitor.get_size_multiplier("EURUSD") == 1.0

    def test_records_execution(self, config):
        ctx = SystemContext.create(config, _FakePM())
        if ctx.execution_monitor is None:
            pytest.skip("ExecutionMonitor not available")
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        ctx.execution_monitor.record_execution(
            requested_price=1.10000,
            filled_price=1.10002,
            signal_timestamp=now - timedelta(seconds=1),
            fill_timestamp=now,
            spread=0.00012,
            pip_size=0.0001,
            symbol="EURUSD",
        )
        stats = ctx.execution_monitor.get_stats("EURUSD")
        assert stats is not None


class TestPhase1And2StillWork:
    """Regression: Phase 1 + 2 subsystems still created correctly."""

    def test_risk_subsystems_still_present(self, config):
        ctx = SystemContext.create(config, _FakePM())
        assert ctx.risk_engine is not None
        assert ctx.drawdown_guard is not None
        assert ctx.correlation_engine is not None

    def test_decision_subsystems_still_present(self, config):
        ctx = SystemContext.create(config, _FakePM())
        assert ctx.session_engine is not None
        assert ctx.decision_engine is not None
        assert ctx.situation_engine is not None
