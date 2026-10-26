"""Phase 6 — Evolution Engines + Unify + Single Execution Path.

Tests:
- SystemContext creates evolution engine subsystems
- SystemContext creates planning/shadow subsystems
- main.py no longer imports TradingLoop or checks USE_EVENT_DRIVEN
- EventDrivenSystem uses evolution engines in entry/close paths
- Shutdown closes evolution engine DB connections
"""


import pytest


# ── SystemContext evolution subsystem fields ──────────────────────────


class TestSystemContextEvolutionFields:
    """Verify Phase 6 fields exist on SystemContext."""

    def test_capital_allocator_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "capital_allocator")
        assert ctx.capital_allocator is None

    def test_execution_profiles_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "execution_profiles")
        assert ctx.execution_profiles is None

    def test_regime_detector_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "regime_detector")
        assert ctx.regime_detector is None

    def test_behavior_discovery_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "behavior_discovery")
        assert ctx.behavior_discovery is None

    def test_signal_discovery_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "signal_discovery")
        assert ctx.signal_discovery is None

    def test_virtual_module_registry_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "virtual_module_registry")
        assert ctx.virtual_module_registry is None

    def test_virtual_signal_manager_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "virtual_signal_manager")
        assert ctx.virtual_signal_manager is None


class TestSystemContextPlanningFields:
    """Verify Phase 6 planning/shadow fields exist on SystemContext."""

    def test_trade_planner_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "trade_planner")
        assert ctx.trade_planner is None

    def test_outcome_logger_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "outcome_logger")
        assert ctx.outcome_logger is None

    def test_calibrator_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "calibrator")
        assert ctx.calibrator is None

    def test_re_entry_manager_field(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        assert hasattr(ctx, "re_entry_manager")
        assert ctx.re_entry_manager is None


# ── main.py single path ─────────────────────────────────────────────


class TestMainSinglePath:
    """Verify main.py has no TradingLoop or USE_EVENT_DRIVEN references."""

    def test_no_trading_loop_import_in_main(self):
        import inspect
        import main
        source = inspect.getsource(main)
        assert "TradingLoop" not in source, "main.py should not reference TradingLoop"

    def test_no_use_event_driven_in_main(self):
        import inspect
        import main
        source = inspect.getsource(main)
        assert "USE_EVENT_DRIVEN" not in source, "main.py should not reference USE_EVENT_DRIVEN"
        assert "is_event_driven_enabled" not in source, "main.py should not reference is_event_driven_enabled"

    def test_no_start_trading_loop_in_main(self):
        import inspect
        import main
        source = inspect.getsource(main)
        assert "_start_trading_loop" not in source, "main.py should not have _start_trading_loop helper"


# ── is_event_driven_enabled removed ──────────────────────────────────


class TestKillSwitchRemoved:
    """Verify the kill switch function is removed from the bootstrap."""

    def test_no_is_event_driven_enabled(self):
        from pathlib import Path
        source = Path("event_driven_bootstrap.py").read_text()
        assert "def is_event_driven_enabled" not in source, \
            "is_event_driven_enabled should be removed from event_driven_bootstrap"


# ── Evolution engine hooks in ED system ──────────────────────────────


class TestEvolutionHooksInBootstrap:
    """Verify evolution engine hooks exist in the bootstrap source."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        from pathlib import Path
        self.source = Path("event_driven_bootstrap.py").read_text()

    def test_brain_origination_uses_portfolio_division(self):
        # The current architecture sizes Brain-authored exposure through the
        # shared PortfolioDivision, not the retired inline cap_mult fold.
        start = self.source.index("    def _size_brain_origination_with_portfolio(")
        end = self.source.index("    def _make_origination_sink(", start)
        helper = self.source[start:end]
        assert "PortfolioDivision" in helper
        assert "division.evaluate(" in helper
        assert "SizingFactors(base_risk_pct=base_risk_pct)" in helper
        assert "cap_mult" not in helper

    def test_execution_profiles_in_entry_path(self):
        assert "execution_profiles" in self.source
        assert "exec_profile" in self.source

    def test_capital_allocator_in_close_path(self):
        assert "capital_allocator" in self.source

    def test_behavior_discovery_in_close_path(self):
        assert "behavior_discovery" in self.source

    def test_outcome_logger_in_close_path(self):
        assert "outcome_logger" in self.source

    def test_calibrator_in_close_path(self):
        assert "calibrator" in self.source

    def test_execution_profiles_in_close_path(self):
        assert "execution_profiles" in self.source


# ── Shutdown closes evolution DBs ────────────────────────────────────


class TestShutdownClosesEvolutionDBs:
    """Verify shutdown path includes evolution engine DB closures."""

    def test_shutdown_includes_evolution_engines(self):
        from pathlib import Path
        source = Path("event_driven_bootstrap.py").read_text()
        for name in ("capital_allocator", "execution_profiles",
                     "regime_detector", "behavior_discovery",
                     "signal_discovery", "virtual_module_registry"):
            assert name in source, f"Shutdown should close {name}"


# ── Brain entry sizing contract ──────────────────────────────────────


class TestBrainOriginationSizing:
    """New exposure must pass through one portfolio sizing/admission boundary."""

    def test_origination_sink_has_no_inline_sizer_fallback(self):
        from pathlib import Path
        source = Path("event_driven_bootstrap.py").read_text()
        start = source.index("    def _make_origination_sink(")
        end = source.index("    def _derive_origination_levels(", start)
        sink = source[start:end]
        assert "_size_brain_origination_with_portfolio(" in sink
        assert "self._origination_lot_size(" not in sink
        assert "if sized is None:" in sink


# ── EventDrivenSystem bootstrap docstring updated ────────────────────


class TestBootstrapDocstring:
    """Verify the module docstring no longer references kill switch."""

    def test_no_kill_switch_in_docstring(self):
        from pathlib import Path
        source = Path("event_driven_bootstrap.py").read_text()
        lines = source.split('"""')
        if len(lines) >= 2:
            docstring = lines[1]
            assert "Kill switch" not in docstring
            assert "USE_EVENT_DRIVEN" not in docstring
