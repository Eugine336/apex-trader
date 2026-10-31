"""Constitutional compliance fixes — Session 1.

Executable assertions for the six violations fixed in this session:

* V2 — a rich LLM reply WITHOUT ``direction`` is parsed (direction ⇒ FLAT), not
  discarded.
* V5 — the WorldModel no longer carries a pre-computed directional bias; only a
  non-directional ``multi_tf_alignment`` measurement snapshot.
* V7 — ``should_i_do_nothing`` records the ACTUAL do-nothing reasoning, never a
  static "considered".
* V9 — the Brain's EV is net of execution cost when EXECUTION_QUALITY evidence
  is present.
* V1 — the Brain cross-checks the advisor against the evidence and attenuates an
  overconfident opinion.

Pure standard library (no third-party deps) so these run in a minimal sandbox.
"""

import time
from types import SimpleNamespace

import pytest

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, Evidence, EvidenceDomain, MarketState
from cognition.evidence_adapters import evidence_from_execution_cost
from cognition.expected_value import expected_value_r
from llm.reasoner import FLAT, LLMReasoner


# ── shared builders ──────────────────────────────────────────────────────────

class _StubClient:
    usable = True
    model = "stub"

    def __init__(self, reply):
        self._reply = reply

    def complete(self, system, user):
        return self._reply


class _Opinion:
    def __init__(self, direction="LONG", confidence=0.8, **kw):
        self.direction = direction
        self.confidence = confidence
        self.rationale = kw.get("rationale", "because")
        for k, v in kw.items():
            setattr(self, k, v)
        # V-002 — origination requires a first-class, ACTIVATED opportunity. A
        # directional advisor fixture carries one (matching its direction) unless
        # the test supplied its own set, so these non-V-002 tests still originate.
        if "opportunities" not in kw:
            self.opportunities = (
                [{"id": "auto", "direction": direction, "state": "ACTIVE",
              "candidate_family": "continuation",
                  "horizon": "M5",
                  "quality": confidence, "asymmetry": confidence,
                  "evidence_strength": confidence}]
                if str(direction).upper() in ("LONG", "SHORT") else []
            )
        if "preferred_opportunity_id" not in kw:
            self.preferred_opportunity_id = "auto"

    def __getattr__(self, _name):  # rich fields default empty/None
        return None


class _Reasoner:
    available = True

    def __init__(self, opinion):
        self._op = opinion

    def reason(self, symbol, evidence, now=None):
        return self._op


def _evidence_state(symbol="EURUSD", conf=0.9, n=6, extra=None, target_high=1.110, expected_return_r=0.8):
    ms = MarketState(symbol=symbol)
    stamp = time.time()
    domains = [EvidenceDomain.STRUCTURE, EvidenceDomain.LIQUIDITY,
               EvidenceDomain.MOMENTUM, EvidenceDomain.VOLUME,
               EvidenceDomain.ORDER_FLOW, EvidenceDomain.VOLATILITY]
    for i in range(n):
        ms.add(Evidence(source_module=f"m{i}", domain=domains[i % len(domains)],
                        symbol=symbol, confidence=conf, uncertainty=1.0 - conf,
                        timestamp_epoch=stamp, source_timestamp_epoch=stamp,
                        relevance_horizon_seconds=900.0))
    # These fixtures exercise reasoning/EV behavior, not missing-quorum behavior.
    ms.add(Evidence(
        source_module="reasoning_engine.test_panel",
        domain=EvidenceDomain.REASONING,
        symbol=symbol,
        observation="two independent advisors returned opinions",
        confidence=conf,
        uncertainty=1.0 - conf,
        measurements={"advisors_responded": 2, "advisors_available": 2, "advisors_total": 2},
        timestamp_epoch=stamp, source_timestamp_epoch=stamp,
        relevance_horizon_seconds=900.0,
    ))
    extras = list(extra or [])
    has_cost_evidence = any(
        getattr(e, "domain", None) == EvidenceDomain.EXECUTION_QUALITY for e in extras
    )
    if not has_cost_evidence:
        ms.add(Evidence(
            source_module="execution_cost.test_quote",
            domain=EvidenceDomain.EXECUTION_QUALITY,
            symbol=symbol,
            observation="fresh test spread and slippage quote",
            confidence=conf,
            uncertainty=1.0 - conf,
            measurements={"estimated_total_cost_r": 0.05, "cost_verified": True, "cost_source": "test live quote"},
            timestamp_epoch=stamp, source_timestamp_epoch=stamp,
            relevance_horizon_seconds=60.0,
        ))
    for e in extras:
        if (
            getattr(e, "domain", None) == EvidenceDomain.EXECUTION_QUALITY
            and getattr(e, "source_timestamp_epoch", None) is None
        ):
            # Test-only cost fixtures model a quote whose source time is known.
            e.timestamp_epoch = stamp
            e.source_timestamp_epoch = stamp
            e.source_timestamp_verified = True
        ms.add(e)
    # EV/origination tests use activated long opportunities, so provide the
    # fresh completed-bar structure required to derive and validate payoff geometry.
    ms.add(Evidence(
        source_module="market_lenses.m5.structure",
        domain=EvidenceDomain.STRUCTURE, symbol=symbol,
        observation="completed-bar structural range", confidence=conf,
        uncertainty=1.0 - conf,
        measurements={
            "source_kind": "market_lens", "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": time.time(),
            "timeframe": "M5", "current_close": 1.100,
            "prior_range_high": target_high, "prior_range_low": 1.095,
            "range_position": 0.5, "breakout_direction": "up",
            "trend_state": "uptrend", "range_rotation": False, "trend_strength": 0.4,
        },
        relevance_horizon_seconds=900.0,
    ))
    ms.add(Evidence(
        source_module="market_lenses.m5.momentum",
        domain=EvidenceDomain.MOMENTUM, symbol=symbol,
        observation="completed-bar momentum", confidence=conf,
        uncertainty=1.0 - conf,
        measurements={
            "source_kind": "market_lens", "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": time.time(),
            "timeframe": "M5", "close_change": 0.001,
        },
        relevance_horizon_seconds=900.0,
    ))
    # Test-only calibration record; production evidence must come from durable outcomes.
    from test_support.calibration import register_test_calibration
    artifact = register_test_calibration(
        symbol, "LONG", horizon_seconds=300, probability=0.7, expected_return_r=expected_return_r,
        holdout_expected_return_r=expected_return_r,
    )
    ms.add(Evidence(
        source_module="calibration_producer.test_fixture",
        domain=EvidenceDomain.HISTORICAL_ANALOGUE, symbol=symbol,
        observation="producer-registered test calibration artifact",
        confidence=0.0, uncertainty=0.0,
        measurements=artifact.to_measurements(),
        timestamp_epoch=artifact.issued_at_epoch,
        source_timestamp_epoch=artifact.issued_at_epoch,
        relevance_horizon_seconds=3600.0,
    ))
    return ms


# ── V2 — a directionless-but-rich reply is parsed (direction ⇒ FLAT) ──────────

def test_v2_missing_direction_keeps_rich_reasoning():
    reply = (
        '{"regime": "ranging", "primary_hypothesis": "balanced auction",'
        ' "key_uncertainty": "sweep timing", "invalidation": "range break",'
        ' "opportunity": "none", "confidence": 0.4}'
    )
    r = LLMReasoner(client=_StubClient(reply), enabled=True, min_interval_seconds=0)
    op = r.reason("EURUSD", {"x": 1})
    assert op is not None, "a rich reply without direction must NOT be discarded"
    assert op.direction == FLAT
    assert op.regime == "ranging"
    assert op.primary_hypothesis == "balanced auction"
    assert op.invalidation == "range break"


def test_v2_truly_unparseable_still_returns_none():
    r = LLMReasoner(client=_StubClient("no json, no fields at all"),
                    enabled=True, min_interval_seconds=0)
    assert r.reason("EURUSD", {}) is None


# ── V5 — WorldModel carries no directional bias ──────────────────────────────

def test_v5_world_model_has_no_directional_bias():
    from brain.world_model import WorldModel, build_world_model

    wm = build_world_model(
        symbol="EURUSD", version=1,
        multi_tf_alignment={
            # A caller that still passes directional keys has them scrubbed.
            "direction": "LONG", "score": 80, "long_probability": 0.7,
            "short_probability": 0.3, "dominant": "LONG",
            # Non-directional structural measurements survive.
            "trend_strength": 0.6, "alignment_degree": 0.7, "conflict_score": 0.2,
        },
    )
    # The former directional accessor/field is gone.
    assert not hasattr(wm, "bias_dict")
    assert not hasattr(WorldModel, "bias")
    align = wm.multi_tf_alignment_dict()
    for k in ("direction", "score", "long_probability", "short_probability", "dominant"):
        assert k not in align, f"directional key {k} must be scrubbed from the world model"
    assert align["trend_strength"] == 0.6
    assert align["alignment_degree"] == 0.7
    assert align["conflict_score"] == 0.2


# ── V7 — should_i_do_nothing is actual reasoning, never "considered" ─────────

def test_v7_do_nothing_reasoning_on_open():
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)),
                         reward_r_default=2.0).reason(_evidence_state())
    txt = out.decision.questions_answered["should_i_do_nothing"]
    assert txt != "considered"
    assert txt.startswith("evaluated")
    assert "forfeits EV" in txt


def test_v7_do_nothing_reasoning_on_reject():
    # A confident FLAT rejects the opportunity — the do-nothing evaluation must
    # explain WHY, not say "considered".
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("FLAT", 0.9))).reason(_evidence_state())
    txt = out.decision.questions_answered["should_i_do_nothing"]
    assert txt != "considered"
    assert txt.startswith("evaluated: doing nothing is correct")


def test_v7_do_nothing_reasoning_on_negative_expected_return():
    # Low reasoning confidence is not itself a probability; supply a matching
    # low calibrated probability to test the no-action explanation.
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.3))).reason(
        _evidence_state(expected_return_r=-0.1)
    )
    txt = out.decision.questions_answered["should_i_do_nothing"]
    assert txt.startswith("evaluated: doing nothing is correct")
    assert "ev" in txt.lower() or "expected value" in txt.lower()


# ── V9 — EV is net of execution cost ─────────────────────────────────────────

def test_v9_execution_cost_reduces_ev():
    # p_win 0.8, reward 2R, risk 1R ⇒ EV 1.4R before cost; a 0.3R cost ⇒ 1.1R.
    assert expected_value_r(0.8, 2.0, 1.0, cost_r=0.3) == pytest.approx(1.1)

    cost_ev = Evidence(
        source_module="execution.cost_model", domain=EvidenceDomain.EXECUTION_QUALITY,
        symbol="EURUSD", confidence=0.8, uncertainty=0.2,
        measurements={"estimated_total_cost_r": 0.3, "cost_verified": True, "cost_source": "test live quote"}, relevance_horizon_seconds=300.0,
    )
    ms = _evidence_state(extra=[cost_ev], expected_return_r=1.4)
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)),
                         reward_r_default=2.0, min_expected_value=0.0).reason(ms)
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.decision.expected_value == pytest.approx(1.1)


def test_v9_execution_cost_can_flip_ev_negative():
    # A thin reward with a heavy cost turns an otherwise-confident thesis into a
    # declined opportunity (movement is not opportunity once cost is priced in).
    cost_ev = Evidence(
        source_module="execution.cost_model", domain=EvidenceDomain.EXECUTION_QUALITY,
        symbol="EURUSD", confidence=0.8, uncertainty=0.2,
        measurements={"estimated_total_cost_r": 0.5, "cost_verified": True, "cost_source": "test live quote"}, relevance_horizon_seconds=300.0,
    )
    ms = _evidence_state(extra=[cost_ev], target_high=1.1025, expected_return_r=0.4)
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.6)),
                         reward_r_default=0.6, min_expected_value=0.0).reason(ms)
    # EV = 0.6*0.6 - 0.4*1 - 0.5 = 0.36 - 0.4 - 0.5 = -0.54 < 0 ⇒ declined.
    assert out.decision.expected_value < 0.0
    assert out.decision.decision_type == DecisionType.REJECT_OPPORTUNITY


def test_v9_execution_cost_adapter_shapes_measurements():
    ev = evidence_from_execution_cost("EURUSD", {
        "estimated_spread_r": 0.1,
        "estimated_slippage_r": 0.05,
        "cost_verified": True,
        "cost_source": "test live quote",
    })
    assert len(ev) == 1
    m = ev[0].measurements
    assert m["estimated_total_cost_r"] == pytest.approx(0.15)
    assert m["cost_verified"] is True
    assert m["cost_source"] == "test live quote"
    assert ev[0].domain == EvidenceDomain.EXECUTION_QUALITY
    assert ev[0].polarity == 0.0


def test_v9_execution_cost_adapter_defaults_to_unverified():
    ev = evidence_from_execution_cost("EURUSD", {
        "estimated_spread_r": 0.1,
        "estimated_slippage_r": 0.05,
    })
    assert len(ev) == 1
    assert ev[0].measurements["estimated_total_cost_r"] == pytest.approx(0.15)
    assert ev[0].measurements["cost_verified"] is False


# ── V1 — the Brain attenuates an overconfident advisor ───────────────────────

def test_v1_attenuates_overconfident_advisor():
    # Advisor is very confident (0.9) but the evidence mean confidence is low
    # (0.4) — the Brain must attenuate and record the synthesis.
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.9)),
                         reward_r_default=2.0, min_expected_value=0.0).reason(
        _evidence_state(conf=0.4))
    synth = out.decision.questions_answered.get("brain_synthesis", "")
    assert "attenuated effective confidence" in synth
    # The recorded effective confidence is pulled below the advisor's 0.9.
    assert out.decision.confidence < 0.9


def test_v1_consistent_advisor_not_attenuated():
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)),
                         reward_r_default=2.0, min_expected_value=0.0).reason(
        _evidence_state(conf=0.9))
    synth = out.decision.questions_answered.get("brain_synthesis", "")
    assert "consistent with" in synth
    assert out.decision.confidence == pytest.approx(0.8)
