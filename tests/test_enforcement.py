"""
Tests for TunerAgent sole-authority enforcement (Part 1).

When the agent is enabled it is the ONLY place tuning may happen: a component's
own tune / calibrate / retrain entry point is blocked (and logged) on any direct
call UNLESS the agent itself is currently driving that very tune. When the agent
is disabled (or not attached) every component behaves exactly as before, so the
change is backward-compatible.

Covered:
  * is_sole_authority reflects enabled
  * TuningGuardMixin blocks/allows direct calls and never raises
  * bypass attempts are logged in-memory AND persisted to the audit DB
  * agent-driven (delegated) calls pass the guard (authorization window)
  * the four real guarded entry points (AdaptiveOptimizer.run_optimization,
    GateTuner.calibrate, Calibrator.calibrate, SignalLedger.run_grading_cycle)
  * detaching the agent restores the legacy behaviour

Stdlib + sqlite only for the core; the real-component cases use the light
learners (no torch).
"""

import pytest

from adaptive.tunable import TuneContext, TuneFrequency, TuneResult, TuningGuardMixin
from adaptive.tuner_agent import TunerAgent


# ── Test doubles ──────────────────────────────────────────────────────────


class GuardedComponent(TuningGuardMixin):
    """A component whose self-tune entry point defers to the agent."""

    def __init__(self):
        self.runs = 0

    def retrain(self):
        if self._tuning_blocked("retrain"):
            return "BLOCKED"
        self.runs += 1
        return "RAN"


class DelegatingAdapter:
    """A Tunable whose tune() delegates to a guarded component — exactly how the
    real adapters call calibrate()/run_grading_cycle() during agent-driven tuning."""

    def __init__(self, component):
        self._component = component
        self.tunable_name = "guarded"
        self.frequency = TuneFrequency.ON_TRADE_CLOSE
        self.dependencies = []
        self.min_trades_required = 0
        self.min_interval_seconds = 0.0

    def should_tune(self, ctx):
        return True

    def get_current_params(self):
        return {"runs": self._component.runs}

    def tune(self, ctx):
        result = self._component.retrain()
        return TuneResult(
            tunable_name="guarded", success=True,
            changed=(result == "RAN"), reason=result,
        )

    def validate_params(self, params):
        return True, "ok"

    def rollback(self):
        return True


@pytest.fixture
def agent(tmp_path):
    a = TunerAgent(enabled=True, audit_db_path=str(tmp_path / "audit.db"))
    yield a
    a.close()


@pytest.fixture
def disabled_agent(tmp_path):
    a = TunerAgent(enabled=False, audit_db_path=str(tmp_path / "audit_off.db"))
    yield a
    a.close()


# ── is_sole_authority ──────────────────────────────────────────────────────


def test_is_sole_authority_true_when_enabled(agent):
    assert agent.is_sole_authority is True


def test_is_sole_authority_false_when_disabled(disabled_agent):
    assert disabled_agent.is_sole_authority is False


def test_is_authorizing_false_outside_tuning(agent):
    assert agent.is_authorizing() is False


# ── Direct-call blocking ────────────────────────────────────────────────────


def test_direct_call_blocked_when_sole_authority(agent):
    c = GuardedComponent()
    c.set_tuner_agent(agent)
    assert c.retrain() == "BLOCKED"
    assert c.runs == 0


def test_direct_call_allowed_when_agent_disabled(disabled_agent):
    c = GuardedComponent()
    c.set_tuner_agent(disabled_agent)
    assert c.retrain() == "RAN"
    assert c.runs == 1


def test_direct_call_allowed_when_no_agent_attached():
    c = GuardedComponent()
    assert c.retrain() == "RAN"
    assert c.runs == 1


def test_detaching_agent_restores_legacy_behaviour(agent):
    c = GuardedComponent()
    c.set_tuner_agent(agent)
    assert c.retrain() == "BLOCKED"
    c.set_tuner_agent(None)
    assert c.retrain() == "RAN"
    assert c.runs == 1


# ── Bypass logging ──────────────────────────────────────────────────────────


def test_bypass_attempt_logged_in_memory(agent):
    c = GuardedComponent()
    c.set_tuner_agent(agent)
    c.retrain()
    c.retrain()
    attempts = agent.get_bypass_attempts()
    assert len(attempts) == 2
    assert attempts[0]["component"] == "GuardedComponent"
    assert attempts[0]["caller"] == "retrain"


def test_bypass_attempt_persisted_to_audit_db(agent):
    c = GuardedComponent()
    c.set_tuner_agent(agent)
    c.retrain()
    rows = agent.get_audit_log()
    bypass_rows = [r for r in rows if r.get("error") == "tuning_bypass_attempt"]
    assert len(bypass_rows) == 1
    assert bypass_rows[0]["tunable_name"] == "GuardedComponent"


def test_bypass_log_is_bounded(agent):
    c = GuardedComponent()
    c.set_tuner_agent(agent)
    for _ in range(agent._max_bypass_log + 50):
        c.retrain()
    assert len(agent._bypass_log) <= agent._max_bypass_log


def test_guard_never_raises_on_faulty_agent():
    class BadAgent:
        is_sole_authority = True

        def is_authorizing(self):
            raise RuntimeError("boom")

    c = GuardedComponent()
    c.set_tuner_agent(BadAgent())
    # A faulty agent must not break the component — it falls through to running.
    assert c.retrain() == "RAN"


# ── Agent-driven (delegated) calls pass the guard ───────────────────────────


def test_agent_driven_delegated_call_passes_guard(agent):
    c = GuardedComponent()
    c.set_tuner_agent(agent)
    agent.register(DelegatingAdapter(c))
    results = agent.on_trade_close(TuneContext(total_trades=100))
    assert c.runs == 1
    assert results and results[0].success and results[0].changed


def test_authorization_window_closes_after_tune(agent):
    c = GuardedComponent()
    c.set_tuner_agent(agent)
    agent.register(DelegatingAdapter(c))
    agent.on_trade_close(TuneContext(total_trades=100))
    assert agent.is_authorizing() is False
    # And a direct call after the agent-driven one is blocked again.
    assert c.retrain() == "BLOCKED"


def test_authorization_window_closes_even_if_tune_raises(agent):
    class RaisingAdapter(DelegatingAdapter):
        def tune(self, ctx):
            raise RuntimeError("kaboom")

    c = GuardedComponent()
    c.set_tuner_agent(agent)
    agent.register(RaisingAdapter(c))
    agent.on_trade_close(TuneContext(total_trades=100))
    # Window must be closed even though tune() raised.
    assert agent.is_authorizing() is False


# ── Real guarded entry points ───────────────────────────────────────────────


def test_adaptive_optimizer_run_optimization_blocked(agent):
    from adaptive.optimizer import AdaptiveOptimizer

    opt = AdaptiveOptimizer()
    opt.set_tuner_agent(agent)
    report = opt.run_optimization([{"pair": "EURUSD", "outcome": "WIN"}])
    # Inert report — no learners trained behind the agent's back.
    assert report.trades_analyzed == 1
    assert report.recommendations == []
    assert agent.get_bypass_attempts()


def test_adaptive_optimizer_runs_when_no_agent():
    from adaptive.optimizer import AdaptiveOptimizer

    opt = AdaptiveOptimizer()
    report = opt.run_optimization([])
    assert report is not None


def test_gate_tuner_calibrate_blocked(agent, tmp_path):
    from adaptive.gate_tuner import GateTuner

    gt = GateTuner(path=str(tmp_path / "gate.json"))
    gt.set_tuner_agent(agent)
    changes = gt.calibrate([{"rejecting_gate": "ev_gate", "outcome": "WIN", "cnt": 50, "avg_r": 1.0}])
    assert changes == []


def test_gate_tuner_calibrate_runs_when_disabled(disabled_agent, tmp_path):
    from adaptive.gate_tuner import GateTuner

    gt = GateTuner(path=str(tmp_path / "gate2.json"))
    gt.set_tuner_agent(disabled_agent)
    # Does not raise and is allowed to evaluate (may or may not change).
    result = gt.calibrate([])
    assert isinstance(result, list)


def test_calibrator_calibrate_blocked(agent):
    from planning.calibrator import Calibrator

    cal = Calibrator()
    cal.set_tuner_agent(agent)
    before = cal.config
    out = cal.calibrate([{"outcome": {"pnl_r": 1.0}} for _ in range(20)])
    # Unchanged config object returned — no calibration happened.
    assert out is before


def test_calibrator_calibrate_runs_when_no_agent():
    from planning.calibrator import Calibrator

    cal = Calibrator()
    out = cal.calibrate([])  # empty -> returns config, but not via the guard
    assert out is cal.config


def test_signal_ledger_grading_blocked(agent, tmp_path):
    from adaptive.signal_ledger import SignalLedger

    ledger = SignalLedger(db_path=tmp_path / "sl.db")
    ledger.set_tuner_agent(agent)
    stats = ledger.run_grading_cycle({"EURUSD": 1.2345})
    assert stats == {"observed": 0, "graded": 0}


def test_signal_ledger_grading_runs_when_disabled(disabled_agent, tmp_path):
    from adaptive.signal_ledger import SignalLedger

    ledger = SignalLedger(db_path=tmp_path / "sl2.db")
    ledger.set_tuner_agent(disabled_agent)
    stats = ledger.run_grading_cycle({"EURUSD": 1.2345})
    # Real (non-blocked) grading runs — returns the observed/graded summary.
    assert "graded" in stats
