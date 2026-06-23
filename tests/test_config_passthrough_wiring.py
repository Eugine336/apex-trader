"""Regression tests for the nested-config-passthrough bug class.

Several learning/evolution subsystems read their settings off the config object
that ``SystemContext.create`` hands them. The recurring bug was handing the
top-level ``AppConfig`` (or reading a field/key that does not exist on it)
instead of the component's own nested ``*Config``. The component then silently
fell back to its constructor defaults and the operator's configured values were
ignored — the same failure mode that left the ModuleGovernor permanently inert.

These tests build a real ``AppConfig`` with DISTINCTIVE non-default values on the
nested configs and assert the constructed component reflects those values,
proving the nested config actually reaches the component.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

# ── Stub heavy optional deps before importing the system modules ─────
_STUBS = {}
for _mod in ("torch", "torch.nn", "torch.optim", "torch.nn.functional",
             "torch.distributions", "torch.utils", "torch.utils.data",
             "MetaTrader5", "websockets", "websockets.sync",
             "websockets.sync.client", "feedparser"):
    if _mod not in sys.modules:
        _STUBS[_mod] = sys.modules[_mod] = types.ModuleType(_mod)

_nn = sys.modules["torch.nn"]
_nn.Module = type("Module", (), {"__init__": lambda self, *a, **kw: None})
_nn.Linear = type("Linear", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.Conv1d = type("Conv1d", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.Sequential = type("Sequential", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.LayerNorm = type("LayerNorm", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.BatchNorm1d = type("BatchNorm1d", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.ReLU = type("ReLU", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.GELU = type("GELU", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.Dropout = type("Dropout", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.Embedding = type("Embedding", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.ModuleList = type("ModuleList", (_nn.Module,), {"__init__": lambda self, *a, **kw: None})
_nn.Parameter = lambda *a, **kw: MagicMock()

_torch = sys.modules["torch"]
_torch.nn = _nn
_torch.Tensor = type("Tensor", (), {})
_torch.float32 = "float32"
_torch.no_grad = lambda: MagicMock(__enter__=lambda s: s, __exit__=lambda s, *a: None)
_torch.zeros = lambda *a, **kw: MagicMock()
_torch.tensor = lambda *a, **kw: MagicMock()
_torch.load = lambda *a, **kw: {}
_torch.save = lambda *a, **kw: None
_torch.device = lambda *a: "cpu"

_dist = sys.modules["torch.distributions"]
_dist.Categorical = type("Categorical", (), {"__init__": lambda self, *a, **kw: None})

_optim = sys.modules["torch.optim"]
_optim.Adam = type("Adam", (), {"__init__": lambda self, *a, **kw: None})
_optim.AdamW = type("AdamW", (), {"__init__": lambda self, *a, **kw: None})


@pytest.fixture(scope="module")
def built_ctx():
    """Build a SystemContext from a real AppConfig carrying distinctive nested
    values, so every assertion proves the nested config reached the component."""
    from config import AppConfig
    from core.system_context import SystemContext

    cfg = AppConfig()

    # ── distinctive non-default nested values ────────────────────────
    cfg.outcome_feedback.accuracy_lookback = 137

    cfg.post_close_tracker.enabled = True
    cfg.post_close_tracker.check_intervals_minutes = [7, 11, 23]
    cfg.post_close_tracker.max_retries = 9

    cfg.counterfactual.counterfactual_enabled = True
    cfg.counterfactual.attribution_lookback = 321
    cfg.counterfactual.attribution_interval = 47

    cfg.interaction.interaction_discovery_enabled = True
    cfg.interaction.interaction_lookback = 222
    cfg.interaction.interaction_interval = 88

    cfg.tuner_agent.enabled = True
    cfg.tuner_agent.max_tune_duration_seconds = 12.5
    cfg.tuner_agent.max_consecutive_failures = 7

    cfg.regime_detection.lookback_bars = 33
    cfg.regime_detection.hysteresis_bars = 9

    cfg.signal_ledger.signal_grading_delay_minutes = 17.0
    cfg.signal_ledger.signal_min_move_pct = 0.25
    cfg.signal_ledger.accuracy_lookback = 55

    cfg.capital_allocation.short_horizon_trades = 13
    cfg.execution_profiles.max_active_profiles = 4

    # Prove the virtual cluster flag reaches the manager (defaults OFF).
    cfg.signal_discovery.virtual_promotion_enabled = True

    pm = MagicMock()
    pm.get_broker_name.return_value = "test"
    pm.get_account_id.return_value = "123"

    ctx = SystemContext.create(cfg, pm)
    return ctx, cfg


def test_outcome_feedback_uses_nested_config(built_ctx):
    ctx, cfg = built_ctx
    assert ctx.outcome_feedback is not None
    assert ctx.outcome_feedback._lookback == cfg.outcome_feedback.accuracy_lookback


def test_post_close_tracker_uses_nested_config(built_ctx):
    ctx, cfg = built_ctx
    assert ctx.post_close_tracker is not None
    assert ctx.post_close_tracker.enabled is True
    assert ctx.post_close_tracker.intervals == cfg.post_close_tracker.check_intervals_minutes
    assert ctx.post_close_tracker.max_retries == cfg.post_close_tracker.max_retries


def test_counterfactual_uses_real_field_names(built_ctx):
    ctx, cfg = built_ctx
    assert ctx.counterfactual_engine is not None
    assert ctx.counterfactual_engine.enabled is True
    assert ctx.counterfactual_engine._lookback == cfg.counterfactual.attribution_lookback
    assert ctx.counterfactual_engine._interval == cfg.counterfactual.attribution_interval


def test_interaction_uses_real_field_names(built_ctx):
    ctx, cfg = built_ctx
    assert ctx.interaction_analyzer is not None
    assert ctx.interaction_analyzer.enabled is True
    assert ctx.interaction_analyzer._lookback == cfg.interaction.interaction_lookback
    assert ctx.interaction_analyzer._interval == cfg.interaction.interaction_interval


def test_tuner_agent_uses_correct_key(built_ctx):
    ctx, cfg = built_ctx
    assert ctx.tuner_agent is not None
    assert ctx.tuner_agent.enabled is True
    assert ctx.tuner_agent._max_duration == cfg.tuner_agent.max_tune_duration_seconds
    assert ctx.tuner_agent._max_failures == cfg.tuner_agent.max_consecutive_failures


def test_regime_detector_uses_correct_key(built_ctx):
    ctx, cfg = built_ctx
    assert ctx.regime_detector is not None
    assert ctx.regime_detector.lookback_bars == cfg.regime_detection.lookback_bars
    assert ctx.regime_detector.hysteresis_bars == cfg.regime_detection.hysteresis_bars


def test_signal_ledger_uses_nested_config(built_ctx):
    ctx, cfg = built_ctx
    assert ctx.signal_ledger is not None
    assert ctx.signal_ledger._grading_delay_minutes == cfg.signal_ledger.signal_grading_delay_minutes
    assert ctx.signal_ledger._min_move_pct == cfg.signal_ledger.signal_min_move_pct
    assert ctx.signal_ledger._accuracy_lookback == cfg.signal_ledger.accuracy_lookback


def test_capital_allocator_uses_nested_config(built_ctx):
    ctx, cfg = built_ctx
    assert ctx.capital_allocator is not None
    assert ctx.capital_allocator.short_horizon_trades == cfg.capital_allocation.short_horizon_trades


def test_execution_profiles_uses_nested_config(built_ctx):
    ctx, cfg = built_ctx
    assert ctx.execution_profiles is not None
    assert ctx.execution_profiles.max_active_profiles == cfg.execution_profiles.max_active_profiles


def test_virtual_manager_reads_signal_discovery_config(built_ctx):
    ctx, cfg = built_ctx
    # The manager reads virtual_promotion_enabled off the nested
    # SignalDiscoveryConfig; with the passthrough fixed, setting it True is
    # reflected on the constructed manager (proving the wiring).
    assert ctx.virtual_signal_manager is not None
    assert ctx.virtual_signal_manager.enabled is True


def test_signal_discovery_cluster_defaults_off():
    """Dormant-by-default: the live consumer reads the cluster flags off the
    nested config, and those defaults are OFF (documented intent)."""
    from config import AppConfig
    cfg = AppConfig()
    assert cfg.signal_discovery.signal_discovery_enabled is False
    assert cfg.signal_discovery.virtual_promotion_enabled is False


def test_post_close_tracker_default_enabled():
    """PostCloseTracker is observational and now runs by default (preserves the
    pre-fix runtime behaviour while becoming config-authoritative)."""
    from config import AppConfig
    cfg = AppConfig()
    assert cfg.post_close_tracker.enabled is True
