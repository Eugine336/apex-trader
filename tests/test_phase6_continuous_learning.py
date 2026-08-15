"""APEX TRADER — Phase 6 continuous-learning closure tests.

Covers the pieces that close the learning loop end to end:

  * :class:`AdaptiveWeightProvider` — bounds (sum 1.0, per-TF floor/ceiling,
    max shift per cycle), cold-start fallback to static defaults, the
    min-trades gate, accuracy-driven adaptation direction, persistence
    round-trip, and ``reset_to_defaults``.
  * ``decision_core`` evidence-weight hook — provider used when registered,
    static defaults when cleared or when the provider raises.
  * :class:`RecommendationApplier` — bounded application (±max% + absolute
    band), unregistered/disabled/wrong-type no-ops, and ``reset_target``.
  * :class:`RecommendationGateway` applier hook — fires only on APPROVED.
  * :class:`AdaptiveSchedulerLoop` — drives periodic/scan tuning + recompute on
    cadence, is a no-op when disabled, and is exception-safe with a faulty agent.
"""

import time

import pytest

import brain.decision_core as dc
from adaptive.adaptive_scheduler import AdaptiveSchedulerLoop
from adaptive.adaptive_weight_provider import AdaptiveWeightProvider
from adaptive.recommendation_applier import RecommendationApplier
from adaptive.recommendations import (
    LearningRecommendation,
    RecommendationGateway,
    RecommendationStatus,
    RecommendationType,
)


# ── AdaptiveWeightProvider ───────────────────────────────────────────────


def test_cold_start_returns_static_defaults():
    p = AdaptiveWeightProvider(enabled=True, min_trades=30)
    # No trades recorded yet → defaults verbatim.
    assert p.get_weights() == p._defaults
    assert abs(sum(p.get_weights().values()) - 1.0) < 1e-6


def test_disabled_provider_always_returns_defaults():
    p = AdaptiveWeightProvider(enabled=False, min_trades=1)
    for _ in range(50):
        p.record_observation_outcome({"H1": "BULLISH"}, True)
    p.recompute()
    assert p.get_weights() == p._defaults


def test_min_trades_gate_blocks_adaptation():
    p = AdaptiveWeightProvider(enabled=True, min_trades=10)
    for _ in range(5):
        p.record_observation_outcome({"H1": "BULLISH"}, True)
    assert p.recompute() is False
    assert p.get_weights() == p._defaults


def test_weights_stay_bounded_and_normalized():
    p = AdaptiveWeightProvider(
        enabled=True, min_trades=5, min_weight=0.05, max_weight=0.40,
        max_shift_per_cycle=0.03,
    )
    # H1 perfect, D1 always loses — strongly skewed signal.
    for _ in range(40):
        p.record_observation_outcome({"H1": "BULLISH"}, True)
        p.record_observation_outcome({"D1": "BULLISH"}, False)
    for _ in range(50):  # many cycles to push toward the envelope edges
        p.recompute()
    w = p.get_weights()
    assert abs(sum(w.values()) - 1.0) < 1e-9
    for tf, v in w.items():
        assert 0.05 - 1e-9 <= v <= 0.40 + 1e-9, (tf, v)


def test_max_shift_per_cycle_is_respected():
    p = AdaptiveWeightProvider(
        enabled=True, min_trades=5, max_shift_per_cycle=0.03,
    )
    for _ in range(40):
        p.record_observation_outcome({"H1": "BULLISH"}, True)
        p.record_observation_outcome({"D1": "BULLISH"}, False)
    before = dict(p.get_weights())
    p.recompute()
    after = p.get_weights()
    for tf in after:
        assert abs(after[tf] - before[tf]) <= 0.03 + 1e-9, tf


def test_accuracy_drives_direction_of_adaptation():
    p = AdaptiveWeightProvider(enabled=True, min_trades=5, max_shift_per_cycle=0.03)
    for _ in range(40):
        p.record_observation_outcome({"H1": "BULLISH"}, True)   # winner
        p.record_observation_outcome({"D1": "BULLISH"}, False)  # loser
    p.recompute()
    w = p.get_weights()
    assert w["H1"] >= p._defaults["H1"] - 1e-9
    assert w["D1"] <= p._defaults["D1"] + 1e-9


def test_observation_scored_regardless_of_direction():
    p = AdaptiveWeightProvider(enabled=True, min_trades=1)
    # A definite observation is scored on the opportunity's OUTCOME, never on
    # whether its direction matched the taken trade (Constitution §XXIX). H1
    # produced a confident structural read, so it IS scored even though the
    # trade lost / the direction differed.
    p.record_observation_outcome({"H1": "BEARISH"}, False)
    st = p.status()
    assert st["samples"]["H1"] == 1


def test_silent_timeframe_is_not_scored():
    p = AdaptiveWeightProvider(enabled=True, min_trades=1)
    # A timeframe that produced NO observation (empty / neutral read) contributes
    # nothing that cycle — only observed timeframes are scored.
    p.record_observation_outcome({"H1": "", "H4": "NEUTRAL"}, True)
    st = p.status()
    assert st["samples"]["H1"] == 0
    assert st["samples"]["H4"] == 0


def test_reset_to_defaults():
    p = AdaptiveWeightProvider(enabled=True, min_trades=5)
    for _ in range(40):
        p.record_observation_outcome({"H1": "BULLISH"}, True)
    p.recompute()
    p.reset_to_defaults()
    assert p.get_weights() == p._defaults
    assert p.status()["total_trades"] == 0


def test_persistence_round_trip(tmp_path):
    path = str(tmp_path / "weights.json")
    p1 = AdaptiveWeightProvider(enabled=True, min_trades=5, state_path=path)
    for _ in range(40):
        p1.record_observation_outcome({"H1": "BULLISH"}, True)
        p1.record_observation_outcome({"D1": "BULLISH"}, False)
    p1.recompute()
    saved = p1.get_weights()
    # New instance loads the persisted vector + trade count.
    p2 = AdaptiveWeightProvider(enabled=True, min_trades=5, state_path=path)
    assert p2.status()["total_trades"] >= 80
    loaded = p2.get_weights()
    for tf in saved:
        assert abs(loaded[tf] - saved[tf]) < 1e-6


def test_get_weights_fallback_on_provider_fault():
    p = AdaptiveWeightProvider(enabled=True, min_trades=1)
    # Open the min-trades gate so get_weights reaches the live-vector branch,
    # then corrupt the vector so the copy raises — must fall back, never raise.
    p.record_observation_outcome({"H1": "BULLISH"}, True)
    p._weights = None  # type: ignore[assignment]
    out = p.get_weights()
    assert out == p._defaults  # transparent fallback to static defaults


# ── decision_core hook ───────────────────────────────────────────────────


def test_decision_core_uses_registered_provider():
    class _StubProvider:
        def get_weights(self):
            return {"H1": 1.0}

    try:
        dc.set_evidence_weight_provider(_StubProvider())
        assert dc._active_weights() == {"H1": 1.0}
    finally:
        dc.set_evidence_weight_provider(None)
    # Cleared → static defaults.
    assert dc._active_weights() is dc._EVIDENCE_WEIGHTS


def test_decision_core_falls_back_when_provider_raises():
    class _Boom:
        def get_weights(self):
            raise RuntimeError("nope")

    try:
        dc.set_evidence_weight_provider(_Boom())
        assert dc._active_weights() is dc._EVIDENCE_WEIGHTS
    finally:
        dc.set_evidence_weight_provider(None)


# ── RecommendationApplier ────────────────────────────────────────────────


class _Box:
    """A tiny mutable config stand-in."""

    def __init__(self, value):
        self.value = value


def _make_applier(box, *, max_pct=0.20, lo=0.5, hi=5.0, enabled=True):
    a = RecommendationApplier(enabled=enabled, max_change_pct=max_pct)
    a.register_target(
        "min_net_score",
        lambda: box.value,
        lambda v: setattr(box, "value", v),
        lo=lo, hi=hi,
    )
    return a


def _promote(value, name="min_net_score"):
    return LearningRecommendation(
        source="param_evolver",
        recommendation_type=RecommendationType.PARAM_PROMOTE,
        payload={"param_name": name, "proposed_value": value},
    )


def test_applier_clamps_to_max_change_pct():
    box = _Box(1.0)
    a = _make_applier(box, max_pct=0.20)
    a.apply(_promote(10.0))  # proposed way above; capped to +20%
    assert box.value == pytest.approx(1.2)


def test_applier_clamps_to_absolute_band():
    box = _Box(4.9)
    a = _make_applier(box, max_pct=1.0, lo=0.5, hi=5.0)
    a.apply(_promote(100.0))  # +100% allowed by pct, but abs ceiling is 5.0
    assert box.value == pytest.approx(5.0)


def test_applier_skips_unregistered_target():
    box = _Box(1.0)
    a = _make_applier(box)
    a.apply(_promote(2.0, name="not_registered"))
    assert box.value == 1.0


def test_applier_disabled_is_noop():
    box = _Box(1.0)
    a = _make_applier(box, enabled=False)
    a.apply(_promote(2.0))
    assert box.value == 1.0


def test_applier_ignores_non_param_promote():
    box = _Box(1.0)
    a = _make_applier(box)
    rec = LearningRecommendation(
        source="x", recommendation_type=RecommendationType.SIZE_ADJUST,
        payload={"param_name": "min_net_score", "proposed_value": 2.0},
    )
    a.apply(rec)
    assert box.value == 1.0


def test_applier_reset_target_restores_baseline():
    box = _Box(1.0)
    a = _make_applier(box, max_pct=0.20)
    a.apply(_promote(1.2))
    assert box.value == pytest.approx(1.2)
    assert a.reset_target("min_net_score") is True
    assert box.value == pytest.approx(1.0)


# ── Gateway applier hook ─────────────────────────────────────────────────


def test_gateway_invokes_applier_only_on_approval():
    applied = []
    gw = RecommendationGateway(governance_required=False)  # auto-approve
    gw.set_applier(lambda rec: applied.append(rec.payload.get("proposed_value")))
    d = gw.submit(_promote(2.0))
    assert d.status == RecommendationStatus.APPROVED
    assert applied == [2.0]


def test_gateway_skips_applier_on_rejection():
    applied = []
    gw = RecommendationGateway(
        governance_required=True,
        authorizer=lambda rec: (False, "denied"),
    )
    gw.set_applier(lambda rec: applied.append(rec))
    d = gw.submit(_promote(2.0))
    assert d.status == RecommendationStatus.REJECTED
    assert applied == []


def test_gateway_applier_fault_does_not_flip_decision():
    def _boom(rec):
        raise RuntimeError("apply failed")

    gw = RecommendationGateway(governance_required=False)
    gw.set_applier(_boom)
    d = gw.submit(_promote(2.0))
    # Applier raised, but the decision is still recorded as approved.
    assert d.status == RecommendationStatus.APPROVED


# ── AdaptiveSchedulerLoop ────────────────────────────────────────────────


class _FakeAgent:
    def __init__(self, *, boom=False):
        self.periodic = 0
        self.scan = 0
        self._boom = boom

    def on_periodic_tick(self, ctx):
        if self._boom:
            raise RuntimeError("agent fault")
        self.periodic += 1

    def on_scan_cycle(self, ctx):
        if self._boom:
            raise RuntimeError("agent fault")
        self.scan += 1


class _FakeWeights:
    def __init__(self):
        self.calls = 0

    def recompute(self):
        self.calls += 1
        return True


def test_scheduler_disabled_does_not_start():
    sched = AdaptiveSchedulerLoop(tuner_agent=_FakeAgent(), enabled=False)
    sched.start()
    assert sched.stats()["running"] is False


def test_scheduler_drives_periodic_and_recompute():
    agent = _FakeAgent()
    weights = _FakeWeights()
    sched = AdaptiveSchedulerLoop(
        tuner_agent=agent, weight_provider=weights,
        periodic_interval_seconds=1.0, scan_interval_seconds=1.0,
        trade_count_provider=lambda: 42,
    )
    # Force the cadence open and tick directly (no sleep dependency).
    sched._last_periodic = time.monotonic() - 5.0
    sched._last_scan = time.monotonic() - 5.0
    sched._tick()
    assert agent.periodic == 1
    assert agent.scan == 1
    assert weights.calls == 1


def test_scheduler_is_exception_safe_with_faulty_agent():
    agent = _FakeAgent(boom=True)
    sched = AdaptiveSchedulerLoop(tuner_agent=agent)
    sched._last_periodic = time.monotonic() - 9999
    sched._last_scan = time.monotonic() - 9999
    # Must not raise despite the agent faulting.
    sched._tick()
    assert sched.stats()["periodic_runs"] == 0
