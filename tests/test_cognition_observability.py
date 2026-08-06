"""Tests for cognition observability (Part XII) + validation harness (Part XIII)."""

from types import SimpleNamespace

from cognition.contracts import DecisionPackage, DecisionType, MarketState
from cognition.observability import CognitionObservability
from cognition.validation import ReplayHarness, readiness_verdict


# ── Stubs ────────────────────────────────────────────────────────────────────

def _brain_status(**kw):
    base = dict(available=True, decisions=0, campaigns_opened=0, observed=0,
                managed=0, faults=0)
    base.update(kw)
    return SimpleNamespace(get_status=lambda: base)


def _gate_status(**kw):
    base = dict(mode="shadow", authoritative=False, evaluations=0, would_veto=0, vetoed=0)
    base.update(kw)
    return SimpleNamespace(get_status=lambda: base)


def _cal_status(**kw):
    base = dict(samples=0, mean_confidence=0.0, win_rate=0.0, reliability_gap=0.0, brier=0.0)
    base.update(kw)
    return SimpleNamespace(get_status=lambda: base)


# ── CognitionObservability ──────────────────────────────────────────────────

def test_metrics_flat_view_from_components():
    obs = CognitionObservability(
        brain=_brain_status(decisions=10, campaigns_opened=3, observed=7, faults=0),
        gate=_gate_status(mode="veto", evaluations=20, vetoed=5, would_veto=8),
        calibration=_cal_status(samples=30, reliability_gap=0.1, brier=0.12),
        min_calibration_samples=20,
    )
    m = obs.metrics(now=1000.0)
    assert m["decisions_total"] == 10 and m["campaigns_opened"] == 3
    assert m["gate_mode"] == "veto"
    assert m["veto_rate"] == 0.25 and m["authorise_rate"] == 0.75
    assert m["would_veto_rate"] == 0.4
    assert m["reasoning_quality_measurable"] is True
    assert m["reasoning_quality_score"] == 0.9    # 1 - 0.1


def test_reasoning_quality_not_measurable_below_sample_floor():
    obs = CognitionObservability(
        brain=_brain_status(decisions=5),
        calibration=_cal_status(samples=5, reliability_gap=0.05),
        min_calibration_samples=20,
    )
    m = obs.metrics(now=1000.0)
    assert m["reasoning_quality_measurable"] is False
    assert m["reasoning_quality_score"] is None


def test_decisions_per_min_rolling_rate():
    brain = _brain_status(decisions=0)
    obs = CognitionObservability(brain=brain, window_seconds=600.0)
    # First sample at t=0 (0 decisions), second at t=60 (120 decisions) → 120/min.
    obs.metrics(now=0.0)
    brain.get_status = lambda: dict(available=True, decisions=120, campaigns_opened=0,
                                    observed=0, managed=0, faults=0)
    m = obs.metrics(now=60.0)
    assert m["decisions_per_min"] == 120.0


def test_observability_fail_safe_on_absent_components():
    m = CognitionObservability().metrics(now=1.0)
    assert m["decisions_total"] == 0 and m["gate_mode"] == "unknown"
    assert m["reasoning_quality_score"] is None


def test_observability_fail_safe_on_faulty_component():
    class _Boom:
        def get_status(self):
            raise RuntimeError("down")
    m = CognitionObservability(brain=_Boom(), gate=_Boom()).metrics(now=1.0)
    assert m["decisions_total"] == 0     # fault swallowed → defaults


# ── ReplayHarness ─────────────────────────────────────────────────────────────

class _StubBrain:
    """Emits OPEN on even indices, CONTINUE_OBSERVING on odd; fixed confidence."""

    available = True

    def __init__(self, confidence=0.6, raise_on=None):
        self._c = confidence
        self._raise_on = raise_on
        self._i = 0

    def reason(self, market_state, now=None):
        i = self._i
        self._i += 1
        if self._raise_on is not None and i == self._raise_on:
            raise RuntimeError("boom")
        dtype = DecisionType.OPEN_CAMPAIGN if i % 2 == 0 else DecisionType.CONTINUE_OBSERVING
        decision = DecisionPackage(symbol="EURUSD", decision_type=dtype,
                                   confidence=self._c, uncertainty=0.3)
        return SimpleNamespace(decision=decision)


def _states(n):
    return [MarketState(symbol="EURUSD") for _ in range(n)]


def test_replay_tallies_decision_distribution_and_open_rate():
    report = ReplayHarness(_StubBrain()).run(_states(4))
    assert report.total == 4 and report.faults == 0
    assert report.by_decision_type["open_campaign"] == 2
    assert report.by_decision_type["continue_observing"] == 2
    assert report.open_rate == 0.5
    assert report.ready is True


def test_replay_empty_is_not_ready():
    report = ReplayHarness(_StubBrain()).run([])
    assert report.total == 0 and report.ready is False


def test_replay_fault_makes_not_ready():
    report = ReplayHarness(_StubBrain(raise_on=1)).run(_states(4))
    assert report.faults == 1 and report.ready is False


def test_replay_unavailable_brain_not_ready():
    brain = _StubBrain()
    brain.available = False
    report = ReplayHarness(brain).run(_states(2))
    assert report.ready is False
    assert any("not available" in r for r in report.reasons)


def test_replay_calibration_gates_readiness():
    # Confidence 0.9 but only 50% win → reliability gap 0.4 > 0.2 default → not ready.
    outcomes = [i % 2 == 0 for i in range(30)]
    report = ReplayHarness(_StubBrain(confidence=0.9), min_calibration_samples=20).run(
        _states(30), outcomes=outcomes)
    assert report.calibration is not None and report.calibration["samples"] == 30
    assert report.ready is False
    assert any("reliability gap" in r for r in report.reasons)


# ── readiness_verdict (live status gate) ──────────────────────────────────────

def test_readiness_verdict_ready():
    status = {
        "brain": {"available": True, "faults": 0},
        "calibration": {"samples": 40, "reliability_gap": 0.1},
    }
    ok, reasons = readiness_verdict(status)
    assert ok is True and any("ready" in r for r in reasons)


def test_readiness_verdict_blocks_on_low_samples():
    status = {"brain": {"available": True, "faults": 0},
              "calibration": {"samples": 3, "reliability_gap": 0.0}}
    ok, reasons = readiness_verdict(status, min_calibration_samples=20)
    assert ok is False and any("below floor" in r for r in reasons)


def test_readiness_verdict_blocks_on_faults_and_unavailable():
    ok, reasons = readiness_verdict({"brain": {"available": False, "faults": 5}})
    assert ok is False
    assert any("not available" in r for r in reasons)


def test_readiness_verdict_fail_safe_on_garbage():
    ok, reasons = readiness_verdict(None)
    assert ok is False
