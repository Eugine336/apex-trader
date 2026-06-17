"""
Tests for the central Tuner Agent (adaptive/tuner_agent.py).

Stdlib + sqlite only — no numpy / torch / pandas. Verifies registration,
dependency resolution (topological order + cycle detection), trigger routing
by frequency, the should_tune gate, dependency-failure skipping, validation →
rollback, exception safety, consecutive-failure disabling, audit-log
persistence, force_tune behaviour, and the disabled (backward-compatible)
no-op path.
"""

import pytest

from adaptive.tunable import TuneContext, TuneFrequency, TuneResult
from adaptive.tuner_agent import TunerAgent


# ── Test doubles ────────────────────────────────────────────────────────


class FakeTunable:
    """Minimal configurable Tunable implementation for agent tests."""

    def __init__(
        self,
        name,
        *,
        frequency=TuneFrequency.ON_TRADE_CLOSE,
        dependencies=None,
        min_trades=0,
        min_interval=0.0,
        should=True,
        raise_on_tune=False,
        invalid=False,
        slow=False,
        changed=True,
    ):
        self._name = name
        self._frequency = frequency
        self._dependencies = list(dependencies or [])
        self._min_trades = min_trades
        self._min_interval = min_interval
        self._should = should
        self._raise = raise_on_tune
        self._invalid = invalid
        self._slow = slow
        self._changed = changed
        self.params = {"value": 1}
        self.tune_calls = 0
        self.rollback_calls = 0

    @property
    def tunable_name(self):
        return self._name

    @property
    def frequency(self):
        return self._frequency

    @property
    def dependencies(self):
        return list(self._dependencies)

    @property
    def min_trades_required(self):
        return self._min_trades

    @property
    def min_interval_seconds(self):
        return self._min_interval

    def should_tune(self, ctx):
        return self._should or ctx.force

    def get_current_params(self):
        return dict(self.params)

    def tune(self, ctx):
        self.tune_calls += 1
        if self._raise:
            raise RuntimeError("boom")
        if self._slow:
            import time as _t
            _t.sleep(0.05)
        self.params = {"value": 2}
        return TuneResult(
            tunable_name=self._name, success=True, changed=self._changed,
            reason="tuned",
        )

    def validate_params(self, params):
        if self._invalid:
            return False, "intentionally invalid"
        return True, "ok"

    def rollback(self):
        self.rollback_calls += 1
        self.params = {"value": 1}
        return True


@pytest.fixture
def agent(tmp_path):
    a = TunerAgent(enabled=True, audit_db_path=str(tmp_path / "audit.db"))
    yield a
    a.close()


def _ctx(**kw):
    return TuneContext(**kw)


# ── Registration ────────────────────────────────────────────────────────


class TestRegistration:
    def test_register_and_list(self, agent):
        agent.register(FakeTunable("a"))
        agent.register(FakeTunable("b"))
        assert set(agent.registered_names) == {"a", "b"}

    def test_reregister_replaces(self, agent):
        t1 = FakeTunable("a")
        t2 = FakeTunable("a")
        agent.register(t1)
        agent.register(t2)
        assert agent.registered_names == ["a"]

    def test_empty_name_rejected(self, agent):
        with pytest.raises(ValueError):
            agent.register(FakeTunable(""))

    def test_unregister(self, agent):
        agent.register(FakeTunable("a"))
        assert agent.unregister("a") is True
        assert agent.unregister("missing") is False


# ── Dependency resolution ───────────────────────────────────────────────


class TestDependencyResolution:
    def test_topological_order(self, agent):
        # c depends on b depends on a → a, b, c
        agent.register(FakeTunable("c", dependencies=["b"]))
        agent.register(FakeTunable("b", dependencies=["a"]))
        agent.register(FakeTunable("a"))
        order = agent._resolve_order(["c", "b", "a"])
        assert order.index("a") < order.index("b") < order.index("c")

    def test_cycle_detection(self, agent):
        agent.register(FakeTunable("a", dependencies=["b"]))
        agent.register(FakeTunable("b", dependencies=["a"]))
        with pytest.raises(ValueError, match="[Cc]ircular"):
            agent._resolve_order(["a", "b"])

    def test_dependency_outside_batch_is_ignored(self, agent):
        # a is registered but not in the batch; b's edge to it imposes no order.
        agent.register(FakeTunable("a"))
        agent.register(FakeTunable("b", dependencies=["a"]))
        order = agent._resolve_order(["b"])
        assert order == ["b"]


# ── Trigger routing ─────────────────────────────────────────────────────


class TestTriggerRouting:
    def test_on_trade_close_runs_correct_frequencies(self, agent):
        tc = FakeTunable("tc", frequency=TuneFrequency.ON_TRADE_CLOSE)
        tb = FakeTunable("tb", frequency=TuneFrequency.ON_TRADE_BATCH)
        per = FakeTunable("per", frequency=TuneFrequency.PERIODIC)
        scan = FakeTunable("scan", frequency=TuneFrequency.PER_SCAN_CYCLE)
        for t in (tc, tb, per, scan):
            agent.register(t)
        agent.on_trade_close(_ctx())
        assert tc.tune_calls == 1
        assert tb.tune_calls == 1
        assert per.tune_calls == 0
        assert scan.tune_calls == 0

    def test_on_scan_cycle_runs_only_per_scan(self, agent):
        scan = FakeTunable("scan", frequency=TuneFrequency.PER_SCAN_CYCLE)
        tc = FakeTunable("tc", frequency=TuneFrequency.ON_TRADE_CLOSE)
        agent.register(scan)
        agent.register(tc)
        agent.on_scan_cycle(_ctx())
        assert scan.tune_calls == 1
        assert tc.tune_calls == 0

    def test_on_periodic_runs_only_periodic(self, agent):
        per = FakeTunable("per", frequency=TuneFrequency.PERIODIC)
        agent.register(per)
        agent.on_periodic_tick(_ctx())
        assert per.tune_calls == 1

    def test_disabled_agent_is_noop(self, tmp_path):
        a = TunerAgent(enabled=False, audit_db_path=str(tmp_path / "a.db"))
        t = FakeTunable("t")
        a.register(t)
        assert a.on_trade_close(_ctx()) == []
        assert t.tune_calls == 0
        a.close()


# ── should_tune gate ────────────────────────────────────────────────────


class TestShouldTuneGate:
    def test_skip_when_not_due(self, agent):
        t = FakeTunable("t", should=False)
        agent.register(t)
        agent.on_trade_close(_ctx())
        assert t.tune_calls == 0

    def test_force_bypasses_should_tune(self, agent):
        t = FakeTunable("t", should=False)
        agent.register(t)
        res = agent.force_tune("t", _ctx())
        assert res is not None and res.success
        assert t.tune_calls == 1

    def test_force_tune_unknown_returns_none(self, agent):
        assert agent.force_tune("nope", _ctx()) is None


# ── Dependency failure handling ─────────────────────────────────────────


class TestDependencyFailure:
    def test_dependent_skipped_when_dependency_fails(self, agent):
        dep = FakeTunable("dep", raise_on_tune=True)
        child = FakeTunable("child", dependencies=["dep"])
        agent.register(dep)
        agent.register(child)
        results = agent.on_trade_close(_ctx())
        by_name = {r.tunable_name: r for r in results}
        assert by_name["dep"].success is False
        assert by_name["child"].skipped is True
        assert child.tune_calls == 0


# ── Validation + rollback ───────────────────────────────────────────────


class TestValidationRollback:
    def test_invalid_params_trigger_rollback(self, agent):
        t = FakeTunable("t", invalid=True)
        agent.register(t)
        results = agent.on_trade_close(_ctx())
        res = results[0]
        assert res.success is False
        assert res.rollback_performed is True
        assert t.rollback_calls == 1
        assert t.params == {"value": 1}  # reverted

    def test_exception_caught_and_rolled_back(self, agent):
        t = FakeTunable("t", raise_on_tune=True)
        agent.register(t)
        results = agent.on_trade_close(_ctx())
        res = results[0]
        assert res.success is False
        assert res.error and "boom" in res.error
        assert res.rollback_performed is True


# ── Consecutive-failure disabling ───────────────────────────────────────


class TestFailureDisabling:
    def test_disable_after_max_failures(self, tmp_path):
        a = TunerAgent(
            enabled=True, audit_db_path=str(tmp_path / "a.db"),
            max_consecutive_failures=2,
        )
        t = FakeTunable("t", raise_on_tune=True)
        a.register(t)
        a.on_trade_close(_ctx())   # failure 1
        a.on_trade_close(_ctx())   # failure 2 → disabled
        calls_at_disable = t.tune_calls
        a.on_trade_close(_ctx())   # should be skipped (disabled)
        assert t.tune_calls == calls_at_disable
        status = a.get_tuner_status()["t"]
        assert status["disabled"] is True
        a.close()

    def test_reset_failure_count_reenables(self, tmp_path):
        a = TunerAgent(
            enabled=True, audit_db_path=str(tmp_path / "a.db"),
            max_consecutive_failures=1,
        )
        t = FakeTunable("t", raise_on_tune=True)
        a.register(t)
        a.on_trade_close(_ctx())   # disabled immediately
        assert a.get_tuner_status()["t"]["disabled"] is True
        assert a.reset_failure_count("t") is True
        assert a.get_tuner_status()["t"]["disabled"] is False
        a.close()

    def test_success_resets_failure_counter(self, agent):
        t = FakeTunable("t")
        agent.register(t)
        # Force a failure then a success.
        t._raise = True
        agent.on_trade_close(_ctx())
        t._raise = False
        agent.on_trade_close(_ctx())
        assert agent.get_tuner_status()["t"]["consecutive_failures"] == 0


# ── Audit log ───────────────────────────────────────────────────────────


class TestAuditLog:
    def test_audit_persists_changes(self, agent):
        agent.register(FakeTunable("t"))
        agent.on_trade_close(_ctx())
        rows = agent.get_audit_log("t")
        assert len(rows) >= 1
        assert rows[0]["tunable_name"] == "t"
        assert rows[0]["success"] == 1
        assert isinstance(rows[0]["params_after"], dict)

    def test_audit_survives_reopen(self, tmp_path):
        path = str(tmp_path / "audit.db")
        a = TunerAgent(enabled=True, audit_db_path=path)
        a.register(FakeTunable("t"))
        a.on_trade_close(_ctx())
        a.close()
        b = TunerAgent(enabled=True, audit_db_path=path)
        rows = b.get_audit_log("t")
        assert len(rows) >= 1
        b.close()

    def test_pure_noop_skip_not_audited(self, agent):
        # should_tune False → never runs → no audit row.
        agent.register(FakeTunable("t", should=False))
        agent.on_trade_close(_ctx())
        assert agent.get_audit_log("t") == []

    def test_last_tune_time_tracked(self, agent):
        agent.register(FakeTunable("t"))
        assert agent.get_last_tune_time("t") is None
        agent.on_trade_close(_ctx())
        assert agent.get_last_tune_time("t") is not None


# ── Status + force-all ──────────────────────────────────────────────────


class TestStatusAndForce:
    def test_status_reports_metadata(self, agent):
        agent.register(FakeTunable(
            "t", frequency=TuneFrequency.ON_TRADE_BATCH,
            dependencies=["x"], min_trades=50, min_interval=3600,
        ))
        st = agent.get_tuner_status()["t"]
        assert st["frequency"] == "on_trade_batch"
        assert st["dependencies"] == ["x"]
        assert st["min_trades_required"] == 50
        assert st["min_interval_seconds"] == 3600

    def test_force_tune_all_runs_everything_in_order(self, agent):
        agent.register(FakeTunable("c", dependencies=["b"], should=False))
        agent.register(FakeTunable("b", dependencies=["a"], should=False))
        agent.register(FakeTunable("a", should=False))
        results = agent.force_tune_all(_ctx())
        ran = [r.tunable_name for r in results if not r.skipped]
        assert set(ran) == {"a", "b", "c"}

    def test_slow_tune_still_completes(self, tmp_path):
        a = TunerAgent(
            enabled=True, audit_db_path=str(tmp_path / "a.db"),
            max_tune_duration_seconds=0.001,  # force overrun warning path
        )
        t = FakeTunable("t", slow=True)
        a.register(t)
        results = a.on_trade_close(_ctx())
        assert results[0].success is True  # overrun is logged, not fatal
        a.close()


class TestRealisticGraph:
    """Mirror the production registration graph and verify ordering / routing."""

    def _build(self, agent):
        F = FakeTunable
        agent.register(F("score_optimizer", frequency=TuneFrequency.ON_TRADE_BATCH))
        agent.register(F("regime_learner", frequency=TuneFrequency.ON_TRADE_BATCH))
        agent.register(F("pair_learner", frequency=TuneFrequency.ON_TRADE_BATCH))
        agent.register(F("session_learner", frequency=TuneFrequency.ON_TRADE_BATCH))
        agent.register(F("ev_estimator", frequency=TuneFrequency.ON_TRADE_CLOSE,
                         dependencies=["pair_learner"]))
        agent.register(F("gate_tuner", frequency=TuneFrequency.PERIODIC,
                         dependencies=["ev_estimator"]))
        agent.register(F("planner_calibrator", frequency=TuneFrequency.ON_TRADE_BATCH,
                         dependencies=["regime_learner"]))
        agent.register(F("signal_ledger", frequency=TuneFrequency.PER_SCAN_CYCLE))

    def test_trade_close_orders_dependencies(self, agent):
        self._build(agent)
        results = agent.on_trade_close(_ctx())
        order = [r.tunable_name for r in results if not r.skipped]
        # ev depends on pair_learner; planner depends on regime_learner.
        assert order.index("pair_learner") < order.index("ev_estimator")
        assert order.index("regime_learner") < order.index("planner_calibrator")
        # gate_tuner is PERIODIC and signal_ledger PER_SCAN — not in this batch.
        assert "gate_tuner" not in order
        assert "signal_ledger" not in order

    def test_periodic_runs_gate_tuner_only(self, agent):
        self._build(agent)
        results = agent.on_periodic_tick(_ctx())
        ran = [r.tunable_name for r in results if not r.skipped]
        # ev_estimator is in a different batch → cross-batch dep is satisfied.
        assert ran == ["gate_tuner"]

    def test_scan_cycle_runs_signal_ledger_only(self, agent):
        self._build(agent)
        results = agent.on_scan_cycle(_ctx())
        ran = [r.tunable_name for r in results if not r.skipped]
        assert ran == ["signal_ledger"]
