"""Tests for the Governance aggregate-health thermostat.

Covers:

* :class:`HealthAssessor` verdicts — HEALTHY (positive EV), DEGRADED (negative
  EV), CRITICAL (negative EV + accelerating participation, or + heavy
  learner-enabled losses).
* Behaviour-neutral small-sample guard (HEALTHY until the window fills).
* Fail-safe: an internal fault in ``assess`` / ``record_trade_close`` never
  raises and never escalates past HEALTHY.
* Bounded memory: the outcome deque never grows past ``max_history``.
* Thread safety: concurrent record + assess does not crash or corrupt state.
* GovernanceDivision wiring — auto-freeze on CRITICAL fires once, auto-release
  on recovery to HEALTHY fires once, and the freeze is edge-triggered (not
  re-issued on every close).
"""

from __future__ import annotations

import threading
import time

from governance.division import GovernanceDivision
from governance.health_assessor import HealthAssessor, _TradeOutcome
from governance.health_models import HealthAssessment, HealthStatus


# ── Stub TunerAgent (just enough surface for freeze/release) ──────────────────


class _StubTuner:
    def __init__(self, names):
        self._names = list(names)
        self.frozen: set[str] = set()
        self.released: list[str] = []

    @property
    def registered_names(self):
        return list(self._names)

    def freeze_tunable(self, name: str, *, reason: str = "") -> bool:
        self.frozen.add(name)
        return True

    def reset_failure_count(self, name: str) -> bool:
        self.released.append(name)
        return True


def _record(assessor: HealthAssessor, n: int, r: float, learner: bool = False) -> None:
    for _ in range(n):
        assessor.record_trade_close(realized_r=r, entry_path="zone", learner_enabled=learner)


# ── HealthAssessor verdicts ──────────────────────────────────────────────────


def test_healthy_when_all_trades_positive():
    a = HealthAssessor(window_size=10)
    _record(a, 10, r=+1.0)
    res = a.assess()
    assert isinstance(res, HealthAssessment)
    assert res.health_status == HealthStatus.HEALTHY
    assert res.rolling_ev > 0
    assert res.rolling_win_rate == 1.0
    assert res.sample_size == 10


def test_degraded_when_rolling_ev_negative():
    a = HealthAssessor(window_size=10)
    # Negative EV, no acceleration (single window → entry_rate neutral 1.0),
    # no learner-enabled losses → DEGRADED, not CRITICAL.
    _record(a, 10, r=-0.2)
    res = a.assess()
    assert res.health_status == HealthStatus.DEGRADED
    assert res.rolling_ev < 0


def test_critical_when_ev_negative_and_entry_rate_accelerating():
    a = HealthAssessor(window_size=10, critical_entry_rate_threshold=1.3)
    now = time.time()
    day = 86_400.0
    # Prior 7-day window (7–14 days ago): 5 trades.
    for _ in range(5):
        a._trades.append(
            _TradeOutcome(realized_r=+0.1, entry_path="zone",
                          learner_enabled=False, ts=now - 10 * day)
        )
    # Recent 7-day window (within last day): 12 losing trades — these are the
    # most-recent items, so they dominate the rolling-EV window too.
    for _ in range(12):
        a._trades.append(
            _TradeOutcome(realized_r=-0.5, entry_path="zone",
                          learner_enabled=False, ts=now - 0.5 * day)
        )
    res = a.assess()
    assert res.rolling_ev < 0
    assert res.entry_rate_trend > 1.3
    assert res.health_status == HealthStatus.CRITICAL


def test_critical_when_ev_negative_and_learner_losses_heavy():
    a = HealthAssessor(window_size=10, critical_learner_loss_rate=0.65)
    _record(a, 10, r=-1.0, learner=True)
    res = a.assess()
    assert res.rolling_ev < 0
    assert res.learner_enabled_loss_rate > 0.65
    assert res.health_status == HealthStatus.CRITICAL


def test_small_sample_is_healthy_behaviour_neutral():
    a = HealthAssessor(window_size=20)
    # Below the internal min-sample floor → HEALTHY regardless of the numbers.
    _record(a, 3, r=-5.0)
    res = a.assess()
    assert res.health_status == HealthStatus.HEALTHY
    assert res.sample_size == 3


def test_disabled_assessor_is_always_healthy():
    a = HealthAssessor(enabled=False, window_size=5)
    _record(a, 20, r=-1.0, learner=True)
    res = a.assess()
    assert res.health_status == HealthStatus.HEALTHY
    # Disabled assessor records nothing.
    assert len(a._trades) == 0


# ── Fail-safe ────────────────────────────────────────────────────────────────


def test_assess_internal_error_returns_healthy():
    a = HealthAssessor(window_size=10)
    _record(a, 10, r=-1.0, learner=True)  # would be CRITICAL

    def _boom():
        raise RuntimeError("synthetic fault")

    a._assess_locked = _boom  # type: ignore[assignment]
    res = a.assess()
    assert res.health_status == HealthStatus.HEALTHY


def test_record_trade_close_swallows_bad_input():
    a = HealthAssessor(window_size=10)
    before = len(a._trades)
    # Non-numeric realized_r raises inside float() and must be swallowed.
    a.record_trade_close(realized_r="not-a-number")  # type: ignore[arg-type]
    assert len(a._trades) == before  # nothing recorded, no raise


def test_nan_realized_r_is_neutralised():
    a = HealthAssessor(window_size=10)
    _record(a, 9, r=+1.0)
    a.record_trade_close(realized_r=float("nan"))
    res = a.assess()
    # NaN coerced to 0.0 — no NaN leaks into the rolling mean.
    assert res.rolling_ev == res.rolling_ev  # not NaN
    assert res.sample_size == 10


# ── Bounded memory ───────────────────────────────────────────────────────────


def test_bounded_memory_deque_does_not_grow_past_max_history():
    a = HealthAssessor(window_size=20, max_history=50)
    _record(a, 500, r=+0.3)
    assert len(a._trades) == 50


# ── Thread safety ────────────────────────────────────────────────────────────


def test_concurrent_record_and_assess():
    a = HealthAssessor(window_size=20, max_history=500)
    stop = threading.Event()
    errors: list[Exception] = []

    def writer():
        try:
            for i in range(2000):
                a.record_trade_close(realized_r=(0.5 if i % 2 else -0.5),
                                     learner_enabled=bool(i % 3))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                a.assess()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    threads.append(threading.Thread(target=reader))
    for t in threads:
        t.start()
    for t in threads[:-1]:
        t.join()
    stop.set()
    threads[-1].join()

    assert not errors
    assert len(a._trades) <= 500


# ── GovernanceDivision wiring ────────────────────────────────────────────────


def test_governance_auto_freeze_on_critical_then_release_on_recovery():
    assessor = HealthAssessor(window_size=10, critical_learner_loss_rate=0.65)
    tuner = _StubTuner(["gate_tuner", "pair_learner", "score_optimizer"])
    gov = GovernanceDivision(
        tuner_agent=tuner,
        health_assessor=assessor,
        health_auto_freeze=True,
        health_auto_release=True,
    )

    # Drive the assessor into CRITICAL via heavy learner-enabled losses.
    last = None
    for _ in range(10):
        last = gov.check_health_after_close(realized_r=-1.0, entry_path="zone",
                                            learner_enabled=True)
    assert last is not None and last.health_status == HealthStatus.CRITICAL
    assert gov.learning_frozen_for_health is True
    # Every registered tunable frozen.
    assert tuner.frozen == {"gate_tuner", "pair_learner", "score_optimizer"}

    # Recover: a full window of clean winners flips back to HEALTHY → release.
    for _ in range(10):
        last = gov.check_health_after_close(realized_r=+1.0, entry_path="zone",
                                            learner_enabled=False)
    assert last is not None and last.health_status == HealthStatus.HEALTHY
    assert gov.learning_frozen_for_health is False
    assert set(tuner.released) == {"gate_tuner", "pair_learner", "score_optimizer"}


def test_governance_freeze_is_edge_triggered_not_per_close():
    assessor = HealthAssessor(window_size=10, critical_learner_loss_rate=0.65)

    class _CountingTuner(_StubTuner):
        def __init__(self, names):
            super().__init__(names)
            self.freeze_calls = 0

        def freeze_tunable(self, name: str, *, reason: str = "") -> bool:
            self.freeze_calls += 1
            return super().freeze_tunable(name, reason=reason)

    tuner = _CountingTuner(["gate_tuner", "pair_learner"])
    gov = GovernanceDivision(tuner_agent=tuner, health_assessor=assessor)

    for _ in range(20):  # stays CRITICAL the whole time
        gov.check_health_after_close(realized_r=-1.0, learner_enabled=True)

    # Freeze issued exactly once per tunable (2 names), not once per close.
    assert tuner.freeze_calls == 2
    assert gov.learning_frozen_for_health is True


def test_governance_no_assessor_is_noop():
    gov = GovernanceDivision(tuner_agent=_StubTuner(["x"]))
    assert gov.check_health() is None
    assert gov.check_health_after_close(realized_r=-1.0) is None
    assert gov.learning_frozen_for_health is False


def test_governance_status_includes_health_block():
    assessor = HealthAssessor(window_size=10)
    gov = GovernanceDivision(tuner_agent=_StubTuner([]), health_assessor=assessor)
    status = gov.get_status()
    assert "health" in status
    assert status["health"]["has_assessor"] is True
    assert status["health"]["learning_frozen_for_health"] is False
