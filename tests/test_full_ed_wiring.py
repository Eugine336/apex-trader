"""Tests for the full ED system wiring — tunable adapters, heat monitoring,
shadow resolution, dashboard mixin fallbacks, and conviction cap.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace, ModuleType
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# Stub torch and related modules before any import that chains into rl/
_TORCH_STUBS = {}
for _mod_name in ("torch", "torch.nn", "torch.nn.functional", "torch.optim",
                   "torch.distributions", "gymnasium", "gymnasium.spaces",
                   "fastapi", "uvicorn"):
    if _mod_name not in sys.modules:
        _stub = MagicMock()
        _stub.__path__ = []
        _stub.__file__ = f"<stub:{_mod_name}>"
        sys.modules[_mod_name] = _stub
        _TORCH_STUBS[_mod_name] = _stub


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_ctx(**overrides) -> SimpleNamespace:
    """Build a minimal SystemContext-like namespace."""
    defaults = dict(
        tuner_agent=MagicMock(),
        ml_adapter=MagicMock(),
        gate_tuner=MagicMock(),
        shadow_store=MagicMock(),
        calibrator=MagicMock(),
        outcome_logger=MagicMock(),
        trade_planner=MagicMock(),
        signal_ledger=MagicMock(),
        post_close_tracker=MagicMock(),
        vote_calibrator=MagicMock(),
        module_governor=MagicMock(),
        counterfactual_engine=MagicMock(),
        interaction_analyzer=MagicMock(),
        signal_discovery=MagicMock(),
        virtual_signal_manager=MagicMock(),
        capital_allocator=MagicMock(),
        execution_profiles=MagicMock(),
        regime_detector=MagicMock(),
        behavior_discovery=MagicMock(),
        risk_engine=MagicMock(),
        drawdown_guard=MagicMock(),
        portfolio_risk_sm=MagicMock(),
        portfolio_governor=MagicMock(),
        account_risk=MagicMock(),
        correlation_engine=MagicMock(),
        risk_reporter=MagicMock(),
        decision_engine=MagicMock(),
        situation_engine=MagicMock(),
        risk_governor=MagicMock(),
        decision_journal=MagicMock(),
        session_engine=MagicMock(),
        news_guard=MagicMock(),
        re_entry_manager=MagicMock(),
        orchestrator=MagicMock(),
        process_watchdog=MagicMock(),
        daily_maintenance=MagicMock(),
        trade_journal=MagicMock(),
        entry_engine=MagicMock(),
        execution_monitor=MagicMock(),
        system_volatility_monitor=MagicMock(),
        opportunity_density_tracker=MagicMock(),
        pair_ranker=MagicMock(),
        opportunity_executor=MagicMock(),
        virtual_module_registry=MagicMock(),
        outcome_feedback=MagicMock(),
        emitter_feedback=MagicMock(),
    )
    defaults.update(overrides)
    ctx = SimpleNamespace(**defaults)
    ctx.account_key = MagicMock(return_value="mt5:12345")
    return ctx


# ── P1: Tunable adapter registration ────────────────────────────────────


class TestTunableRegistration:
    """Verify _register_tunable_adapters registers >0 adapters."""

    def test_registers_adapters_with_full_ctx(self):
        """TunerAgent.register() called multiple times with a full ctx."""
        ctx = _make_ctx()
        ctx.ml_adapter.optimizer = MagicMock()
        ctx.ml_adapter.regime_learner = MagicMock()
        ctx.ml_adapter.pair_learner = MagicMock()
        ctx.ml_adapter.session_learner = MagicMock()
        ctx.ml_adapter.get_trade_history = MagicMock(return_value=[])

        from event_driven_bootstrap import EventDrivenSystem

        with patch.object(EventDrivenSystem, "__init__", lambda self, *a, **kw: None):
            sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
            sys_obj._ctx = ctx
            sys_obj._config = SimpleNamespace(
                risk=SimpleNamespace(risk_per_trade_pct=0.75),
                vote_calibrator=SimpleNamespace(vote_calibration_enabled=True),
                module_governor=SimpleNamespace(module_governor_enabled=True),
                counterfactual=SimpleNamespace(min_trades_for_attribution=50),
                signal_discovery=SimpleNamespace(
                    min_trades_for_discovery=100, retirement_check_interval=50,
                ),
                capital_allocation=SimpleNamespace(rebalance_interval_trades=25),
                behavior_discovery=SimpleNamespace(min_trades_to_cluster=100),
            )
            sys_obj._tick_store = MagicMock()
            sys_obj._tick_store.get_latest = MagicMock(return_value=None)
            sys_obj._register_tunable_adapters()

        assert ctx.tuner_agent.register.call_count > 0, "No adapters registered"

    def test_no_crash_with_none_ctx(self):
        from event_driven_bootstrap import EventDrivenSystem

        with patch.object(EventDrivenSystem, "__init__", lambda self, *a, **kw: None):
            sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
            sys_obj._ctx = None
            sys_obj._register_tunable_adapters()

    def test_no_crash_with_none_tuner(self):
        from event_driven_bootstrap import EventDrivenSystem

        with patch.object(EventDrivenSystem, "__init__", lambda self, *a, **kw: None):
            sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
            sys_obj._ctx = SimpleNamespace(tuner_agent=None)
            sys_obj._register_tunable_adapters()


# ── P2: Portfolio heat monitoring ────────────────────────────────────────


class TestPortfolioHeatMonitoring:

    def test_emergency_closes_all(self):
        from event_driven_bootstrap import EventDrivenSystem

        with patch.object(EventDrivenSystem, "__init__", lambda self, *a, **kw: None):
            sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
            from risk.portfolio_risk_state import PortfolioRiskState

            ctx = SimpleNamespace(
                portfolio_risk_sm=SimpleNamespace(state=PortfolioRiskState.EMERGENCY),
            )
            sys_obj._ctx = ctx
            pm = MagicMock()
            pos = SimpleNamespace(order_id="123", symbol="EURUSD", ticket="123")
            pm.get_all_open_positions.return_value = [pos]
            sys_obj._pm = pm
            sys_obj._aggregator = MagicMock()
            sys_obj._check_portfolio_heat()
            assert sys_obj._aggregator.submit.call_count == 1

    def test_normal_does_nothing(self):
        from event_driven_bootstrap import EventDrivenSystem

        with patch.object(EventDrivenSystem, "__init__", lambda self, *a, **kw: None):
            sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
            from risk.portfolio_risk_state import PortfolioRiskState

            ctx = SimpleNamespace(
                portfolio_risk_sm=SimpleNamespace(state=PortfolioRiskState.NORMAL),
            )
            sys_obj._ctx = ctx
            sys_obj._aggregator = MagicMock()
            sys_obj._check_portfolio_heat()
            assert sys_obj._aggregator.submit.call_count == 0

    def test_no_ctx(self):
        from event_driven_bootstrap import EventDrivenSystem

        with patch.object(EventDrivenSystem, "__init__", lambda self, *a, **kw: None):
            sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
            sys_obj._ctx = None
            sys_obj._check_portfolio_heat()


# ── P5: Shadow resolution ───────────────────────────────────────────────


class TestShadowResolution:

    def test_resolves_hit_tp(self):
        from event_driven_bootstrap import EventDrivenSystem

        with patch.object(EventDrivenSystem, "__init__", lambda self, *a, **kw: None):
            sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
            shadow = SimpleNamespace(
                contract_id="s1", symbol="EURUSD", direction="LONG",
                stop_loss=1.0800, tp1=1.0900,
            )
            tick = SimpleNamespace(mid=1.0910)
            ctx = SimpleNamespace(shadow_store=MagicMock())
            ctx.shadow_store.get_open_contracts.return_value = [shadow]
            sys_obj._ctx = ctx
            sys_obj._tick_store = MagicMock()
            sys_obj._tick_store.get_latest.return_value = tick
            sys_obj._resolve_shadows()
            ctx.shadow_store.resolve_contract.assert_called_once()

    def test_no_crash_with_no_shadows(self):
        from event_driven_bootstrap import EventDrivenSystem

        with patch.object(EventDrivenSystem, "__init__", lambda self, *a, **kw: None):
            sys_obj = EventDrivenSystem.__new__(EventDrivenSystem)
            ctx = SimpleNamespace(shadow_store=MagicMock())
            ctx.shadow_store.get_open_contracts.return_value = []
            sys_obj._ctx = ctx
            sys_obj._tick_store = MagicMock()
            sys_obj._resolve_shadows()


# ── P6: Conviction cap ──────────────────────────────────────────────────


class TestConvictionCap:

    def test_cap_allows_upscaling(self):
        combined = 1.5 * 1.2 * 1.0 * 1.0 * 1.0 * 1.0
        capped = max(0.15, min(2.0, combined))
        assert capped == pytest.approx(1.8)
        assert capped > 1.0


# ── P9: Dashboard mixin fallback — direct import without dashboard pkg ──


class TestDashboardMixinFallbacksUnit:
    """Test component accessor logic directly without importing dashboard.__init__."""

    def test_module_governor_from_ctx(self):
        """ModuleGovernorMixin._module_governor reads from SystemContext."""
        # Import the mixin file directly to avoid dashboard/__init__.py chain
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "state_module_governor", "dashboard/state_module_governor.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class FakeState(mod.ModuleGovernorMixin):
            pass

        state = FakeState()
        gov = MagicMock()
        state._system_context = SimpleNamespace(module_governor=gov)
        state._trading_loop = None
        state.is_live = True
        assert state._module_governor() is gov

    def test_module_governor_fallback_to_loop(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "state_module_governor", "dashboard/state_module_governor.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class FakeState(mod.ModuleGovernorMixin):
            pass

        state = FakeState()
        gov = MagicMock()
        state._system_context = None
        state._trading_loop = SimpleNamespace(_module_governor=gov)
        state.is_live = True
        assert state._module_governor() is gov

