"""Phase 4 — Learning + Feedback loop integration tests.

Verifies that:
- SystemContext creates all learning subsystems
- Trade-close triggers the full learning feedback chain
- Entry-fill records attribution for OutcomeFeedback + CounterfactualEngine
- Rejected entries record shadow contracts
- Shutdown closes DB-backed subsystems
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


# ── Stub heavy deps before importing main modules ───────────────────

_STUBS = {}
for _mod in ("torch", "torch.nn", "torch.optim", "torch.nn.functional",
             "torch.distributions", "torch.utils", "torch.utils.data",
             "MetaTrader5", "websockets", "websockets.sync",
             "websockets.sync.client", "feedparser"):
    if _mod not in sys.modules:
        _STUBS[_mod] = sys.modules[_mod] = types.ModuleType(_mod)

# torch.nn needs Module and other classes
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


@dataclass
class _StubConfig:
    risk: MagicMock = field(default_factory=lambda: MagicMock(
        risk_per_trade_pct=0.75,
        max_open_trades=10,
        max_correlated_trades=3,
        drawdown_rolling_window_days=30,
        max_cluster_same_direction=2,
        allow_intentional_hedge=False,
        portfolio_risk_engine_enabled=False,
        portfolio_heat_block_pct=2.0,
        daily_loss_flatten_pct=5.0,
        volatility_stop_mode="atr",
        atr_stop_period=14,
        atr_stop_mult=1.5,
        sl_buffer_pips=2.0,
        tp1_rr=1.5,
        tp2_rr=3.0,
    ))
    governor: MagicMock = field(default_factory=lambda: MagicMock(
        enabled=False,
        daily_loss_cap_pct=3.0,
        daily_loss_recovery_pct=1.5,
    ))
    log_level: str = "WARNING"
    enabled_categories: list = field(default_factory=list)
    total_instruments: int = 0


class TestSystemContextPhase4Fields:
    """SystemContext has Phase 4 fields."""

    def test_learning_fields_exist(self):
        from core.system_context import SystemContext
        ctx = SystemContext()
        for attr in (
            "outcome_feedback", "signal_ledger", "emitter_feedback",
            "vote_calibrator", "module_governor", "post_close_tracker",
            "gate_tuner", "counterfactual_engine", "interaction_analyzer",
            "shadow_store", "tuner_agent", "ml_adapter",
        ):
            assert hasattr(ctx, attr), f"Missing field: {attr}"
            assert getattr(ctx, attr) is None

    def test_create_initializes_learning_subsystems(self):
        from core.system_context import SystemContext
        pm = MagicMock()
        pm.get_broker_name.return_value = "test"
        pm.get_account_id.return_value = "123"
        cfg = _StubConfig()

        ctx = SystemContext.create(cfg, pm)

        initialized = []
        for attr in (
            "outcome_feedback", "signal_ledger", "gate_tuner",
            "shadow_store", "ml_adapter",
        ):
            if getattr(ctx, attr, None) is not None:
                initialized.append(attr)

        assert len(initialized) >= 3, (
            f"Expected at least 3 learning subsystems, got {len(initialized)}: {initialized}"
        )


class TestTradeCloseLearningChain:
    """Verify _on_trade_closed fires learning callbacks after risk updates."""

    def _make_system(self):
        from event_driven_bootstrap import EventDrivenSystem
        cfg = _StubConfig()

        pm = MagicMock()
        pm.any_connected = True
        pm.get_platform_balance.return_value = 10_000.0
        pm.get_all_open_positions.return_value = []
        pm.get_price.return_value = None
        pm.get_spread.return_value = 1.0
        pm.fetch_market_data.return_value = {}

        ctx = MagicMock()
        ctx.drawdown_guard = None
        ctx.risk_engine = None
        ctx.portfolio_governor = None
        ctx.account_risk = None
        ctx.portfolio_risk_sm = None

        ctx.outcome_feedback = MagicMock()
        ctx.outcome_feedback.enabled = True
        ctx.counterfactual_engine = MagicMock()
        ctx.signal_ledger = MagicMock()
        ctx.ml_adapter = MagicMock()
        ctx.tuner_agent = MagicMock()
        ctx.post_close_tracker = MagicMock()

        sys = EventDrivenSystem.__new__(EventDrivenSystem)
        sys._config = cfg
        sys._pm = pm
        sys._ctx = ctx
        sys._running = False
        sys._tick_store = MagicMock()
        sys._tick_store.get_latest.return_value = MagicMock(mid=1.1000)
        sys._wm_store = MagicMock()

        return sys, ctx

    def test_outcome_feedback_called_on_close(self):
        sys, ctx = self._make_system()
        sys._on_trade_closed("EURUSD", "LONG", 50.0, 10.0, "T123")
        ctx.outcome_feedback.record_outcome.assert_called_once()
        args = ctx.outcome_feedback.record_outcome.call_args
        assert args[0][0] == "T123"
        assert args[0][1]["won"] is True

    def test_counterfactual_completed_on_close(self):
        sys, ctx = self._make_system()
        sys._on_trade_closed("EURUSD", "SHORT", -30.0, -5.0, "T456")
        ctx.counterfactual_engine.complete.assert_called_once()
        args = ctx.counterfactual_engine.complete.call_args
        assert args[0][0] == "T456"
        assert args[0][1]["won"] is False

    def test_signal_ledger_attach_on_close(self):
        sys, ctx = self._make_system()
        sys._on_trade_closed("GBPUSD", "LONG", 20.0, 8.0, "T789")
        ctx.signal_ledger.attach_trade_outcome.assert_called_once()
        args = ctx.signal_ledger.attach_trade_outcome.call_args
        assert args[0][0] == "T789"
        assert args[0][1]["won"] is True

    def test_ml_adapter_register_on_close(self):
        sys, ctx = self._make_system()
        sys._on_trade_closed("USDJPY", "SHORT", -10.0, -3.0, "T000")
        ctx.ml_adapter.register_new_trade.assert_called_once()

    def test_tuner_agent_on_trade_close(self):
        sys, ctx = self._make_system()
        sys._on_trade_closed("EURUSD", "LONG", 100.0, 20.0, "T111")
        ctx.tuner_agent.on_trade_close.assert_called_once()

    def test_post_close_tracker_on_close(self):
        sys, ctx = self._make_system()
        sys._on_trade_closed("AUDUSD", "LONG", 5.0, 1.0, "T222")
        ctx.post_close_tracker.record_close.assert_called_once()

    def test_missing_subsystem_does_not_crash(self):
        sys, ctx = self._make_system()
        ctx.outcome_feedback = None
        ctx.counterfactual_engine = None
        ctx.signal_ledger = None
        ctx.ml_adapter = None
        ctx.tuner_agent = None
        ctx.post_close_tracker = None
        sys._on_trade_closed("EURUSD", "LONG", 10.0, 2.0, "T333")


class TestEntryFillAttribution:
    """Verify _on_order_filled records entry-time attribution."""

    def _make_system(self):
        from event_driven_bootstrap import EventDrivenSystem
        cfg = _StubConfig()
        pm = MagicMock()
        pm.get_platform_balance.return_value = 10_000.0
        pm.get_spread.return_value = 1.0

        ctx = MagicMock()
        ctx.account_risk = None
        ctx.execution_monitor = None
        ctx.outcome_feedback = MagicMock()
        ctx.outcome_feedback.enabled = True
        ctx.counterfactual_engine = MagicMock()
        ctx.signal_ledger = MagicMock()

        sys = EventDrivenSystem.__new__(EventDrivenSystem)
        sys._config = cfg
        sys._pm = pm
        sys._ctx = ctx
        sys._running = False
        sys._tick_store = MagicMock()
        sys._wm_store = MagicMock()
        sys._wm_store.get.return_value = None

        return sys, ctx

    def test_outcome_feedback_entry_recorded(self):
        sys, ctx = self._make_system()
        result = MagicMock()
        result.order_id = "T500"
        sys._on_order_filled("EURUSD", "LONG", result, 10000.0, 1.1000)
        ctx.outcome_feedback.record_entry.assert_called_once()
        args = ctx.outcome_feedback.record_entry.call_args
        assert args[0][0] == "T500"

    def test_counterfactual_open_recorded(self):
        sys, ctx = self._make_system()
        result = MagicMock()
        result.order_id = "T600"
        sys._on_order_filled("GBPUSD", "SHORT", result, 10000.0, 1.3200)
        ctx.counterfactual_engine.record_open.assert_called_once()

    def test_signal_ledger_trade_open_recorded(self):
        sys, ctx = self._make_system()
        result = MagicMock()
        result.order_id = "T700"
        sys._on_order_filled("USDJPY", "LONG", result, 10000.0, 150.0)
        ctx.signal_ledger.record_trade_opened_for_pair.assert_called_once_with(
            "USDJPY", "T700", "LONG",
        )


class TestShadowRejectionRecording:
    """Verify _record_shadow_rejection stores to ShadowStore."""

    def _make_system(self):
        from event_driven_bootstrap import EventDrivenSystem
        ctx = MagicMock()
        ctx.shadow_store = MagicMock()

        sys = EventDrivenSystem.__new__(EventDrivenSystem)
        sys._ctx = ctx
        return sys, ctx

    def test_shadow_store_receives_rejection(self):
        sys, ctx = self._make_system()
        sys._record_shadow_rejection(
            "EURUSD", "LONG", 1.1000, 1.0950, 1.1100,
            "decision_engine", 75.0,
        )
        ctx.shadow_store.insert_contract.assert_called_once()

    def test_no_crash_without_shadow_store(self):
        sys, ctx = self._make_system()
        ctx.shadow_store = None
        sys._record_shadow_rejection(
            "EURUSD", "LONG", 1.1000, 1.0950, 1.1100,
            "risk_governor", 80.0,
        )


class TestShutdownClosesLearning:
    """Verify stop() closes DB-backed learning subsystems."""

    def test_stop_closes_subsystems(self):
        from event_driven_bootstrap import EventDrivenSystem

        ctx = MagicMock()
        ctx.signal_ledger = MagicMock()
        ctx.counterfactual_engine = MagicMock()
        ctx.interaction_analyzer = MagicMock()
        ctx.module_governor = MagicMock()

        sys = EventDrivenSystem.__new__(EventDrivenSystem)
        sys._running = True
        sys._ctx = ctx
        sys._tick_eval_loop = MagicMock()
        sys._flush_loop = MagicMock()
        sys._mt5_poller = MagicMock()
        sys._deriv_adapter = MagicMock()
        sys._tick_router = MagicMock()
        sys._candle_handler = MagicMock()
        sys._event_bus = MagicMock()
        sys._mgmt_store = MagicMock()

        sys.stop()

        ctx.signal_ledger.close.assert_called_once()
        ctx.counterfactual_engine.close.assert_called_once()
        ctx.interaction_analyzer.close.assert_called_once()
        ctx.module_governor.close.assert_called_once()
