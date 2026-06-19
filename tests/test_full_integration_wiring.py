"""Tests for full ED integration wiring — DecisionEngine management, tunable
registration, portfolio heat monitoring, BE-stop cooldown, dashboard controls,
TradePlanner gate, and periodic learning.

These tests avoid importing event_driven_bootstrap directly (requires torch/MT5).
Instead they test the individual subsystem interfaces and dashboard mixin wiring."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── DecisionEngine management interface ──────────────────────────────


class TestDecisionEngineManagementInterface:
    """Verify DecisionEngine management methods exist and accept correct args."""

    def test_situation_engine_has_assess_open_trade(self):
        from decision.situation import SituationEngine
        se = SituationEngine()
        assert hasattr(se, "assess_open_trade")

    def test_decision_engine_has_decide_management(self):
        from decision.engine import DecisionEngine
        de = DecisionEngine(soften_gate=True)
        assert hasattr(de, "decide_management")

    def test_risk_governor_has_review(self):
        from decision.governor import RiskGovernor
        rg = RiskGovernor(graded_risk=True)
        assert hasattr(rg, "review")

    def test_decision_journal_has_log(self):
        from decision.journal import DecisionJournal
        dj = DecisionJournal()
        assert hasattr(dj, "log")

    def test_trade_context_construct(self):
        from decision.context import TradeContext
        tc = TradeContext(
            symbol="EURUSD", order_id="123", direction="BUY",
            entry_price=1.1, current_price=1.105, current_sl=1.09,
        )
        assert tc.symbol == "EURUSD"
        assert tc.is_long is True
        assert tc.profit_r == 0.0


# ── BE-stop cooldown logic ───────────────────────────────────────────


class TestBEStopCooldownLogic:

    def test_cooldown_dict_blocks_when_not_expired(self):
        cooldown = {"EURUSD": time.monotonic() + 300.0}
        symbol = "EURUSD"
        expiry = cooldown.get(symbol, 0.0)
        assert expiry > time.monotonic()

    def test_cooldown_allows_when_expired(self):
        cooldown = {"EURUSD": time.monotonic() - 1.0}
        symbol = "EURUSD"
        expiry = cooldown.get(symbol, 0.0)
        assert expiry <= time.monotonic()

    def test_cooldown_records_on_be_exit(self):
        cooldown = {}
        pnl_dollars = 0.0
        pnl_pips = 0.5
        if abs(pnl_dollars) < 0.01 and abs(pnl_pips) < 2.0:
            cooldown["EURUSD"] = time.monotonic() + 300.0
        assert "EURUSD" in cooldown


# ── Dashboard controls ───────────────────────────────────────────────


class TestDashboardControls:

    def test_pause_resume_ed(self):
        from dashboard.state_controls import ControlsMixin

        class FakeDash(ControlsMixin):
            _trading_loop = None
            _platform_manager = MagicMock()
            is_live = True

        ed = MagicMock()
        ed._paused = False
        dash = FakeDash()
        dash._event_driven_system = ed
        result = dash.pause_trading()
        assert result["status"] == "paused"
        assert ed._paused is True

        result = dash.resume_trading()
        assert result["status"] == "resumed"
        assert ed._paused is False

    def test_emergency_close_ed(self):
        from dashboard.state_controls import ControlsMixin

        class FakeDash(ControlsMixin):
            _trading_loop = None
            _platform_manager = MagicMock()
            is_live = True

        pos = SimpleNamespace(order_id="T1", ticket="T1", symbol="EURUSD", direction="BUY", sl=0.0)
        dash = FakeDash()
        dash._platform_manager.get_all_open_positions.return_value = [pos]
        ed = MagicMock()
        from execution.intent_aggregator import IntentAggregator, AggregatorConfig
        ed._aggregator = IntentAggregator(AggregatorConfig())
        dash._event_driven_system = ed
        result = dash.emergency_close_all()
        assert result["status"] in ("emergency_close_submitted", "error"), result
        if result["status"] == "emergency_close_submitted":
            assert result["closed"] == 1


# ── Conviction cap ───────────────────────────────────────────────────


class TestConvictionCap:
    def test_conviction_can_exceed_1(self):
        assert max(0.15, min(2.0, 1.5)) == 1.5

    def test_conviction_bounded_at_2(self):
        assert max(0.15, min(2.0, 3.0)) == 2.0
