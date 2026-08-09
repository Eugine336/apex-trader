"""Constitutional compliance fixes — Session 1.

Executable assertions for the six violations fixed in this session:

* V2 — a rich LLM reply WITHOUT ``direction`` is parsed (direction ⇒ FLAT), not
  discarded.
* V5 — the WorldModel no longer carries a pre-computed directional bias; only a
  non-directional ``multi_tf_alignment`` measurement snapshot.
* V6 — ``evidence_from_votes`` reports the module's raw observation and its
  observation-certainty, not a directional conviction.
* V7 — ``should_i_do_nothing`` records the ACTUAL do-nothing reasoning, never a
  static "considered".
* V9 — the Brain's EV is net of execution cost when EXECUTION_QUALITY evidence
  is present.
* V1 — the Brain cross-checks the advisor against the evidence and attenuates an
  overconfident opinion.

Pure standard library (no third-party deps) so these run in a minimal sandbox.
"""

from types import SimpleNamespace

import pytest

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, Evidence, EvidenceDomain, MarketState
from cognition.evidence_adapters import evidence_from_execution_cost, evidence_from_votes
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

    def __getattr__(self, _name):  # rich fields default empty/None
        return None


class _Reasoner:
    available = True

    def __init__(self, opinion):
        self._op = opinion

    def reason(self, symbol, evidence, now=None):
        return self._op


def _evidence_state(symbol="EURUSD", conf=0.9, n=6, extra=None):
    ms = MarketState(symbol=symbol)
    domains = [EvidenceDomain.STRUCTURE, EvidenceDomain.LIQUIDITY,
               EvidenceDomain.MOMENTUM, EvidenceDomain.VOLUME,
               EvidenceDomain.ORDER_FLOW, EvidenceDomain.VOLATILITY]
    for i in range(n):
        ms.add(Evidence(source_module=f"m{i}", domain=domains[i % len(domains)],
                        symbol=symbol, confidence=conf, uncertainty=1.0 - conf,
                        relevance_horizon_seconds=900.0))
    for e in (extra or []):
        ms.add(e)
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


# ── V6 — vote evidence carries raw observation + observation-certainty ────────

def test_v6_vote_evidence_reports_observation_not_conviction():
    vote = SimpleNamespace(
        module="structure", direction="LONG", confidence=0.7, weight=1.0,
        timeframe="H1",
        evidence={
            "recent_swing_high": 65300, "displacement": 200,
            "structural_integrity": 0.92, "bos": True,
        },
    )
    ev = evidence_from_votes("XAUUSD", [vote])
    assert len(ev) == 1
    e = ev[0]
    # The observation describes what the module SAW, not "instrument reading".
    assert "instrument reading" not in e.observation
    assert "structure observed" in e.observation
    assert "swing high at 65300" in e.observation
    # Confidence is the module's certainty in its OWN observation (structural
    # integrity 0.92), NOT the vote's directional conviction (0.7).
    assert e.confidence == pytest.approx(0.92)
    # Raw secondary reads survive as measurements; direction never leaks.
    assert e.measurements.get("recent_swing_high") == 65300
    assert e.polarity == 0.0
    assert "direction" not in e.measurements


def test_v6_falls_back_to_vote_confidence_without_quality_read():
    vote = SimpleNamespace(module="momentum", direction="SHORT", confidence=0.6,
                           weight=1.0, timeframe="M15", evidence={"rsi": 71})
    e = evidence_from_votes("EURUSD", [vote])[0]
    assert e.confidence == pytest.approx(0.6)
    assert "RSI 71" in e.observation


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


def test_v7_do_nothing_reasoning_on_low_confidence():
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.3))).reason(_evidence_state())
    txt = out.decision.questions_answered["should_i_do_nothing"]
    assert txt.startswith("evaluated: doing nothing is correct")
    assert "confidence" in txt


# ── V9 — EV is net of execution cost ─────────────────────────────────────────

def test_v9_execution_cost_reduces_ev():
    # p_win 0.8, reward 2R, risk 1R ⇒ EV 1.4R before cost; a 0.3R cost ⇒ 1.1R.
    assert expected_value_r(0.8, 2.0, 1.0, cost_r=0.3) == pytest.approx(1.1)

    cost_ev = Evidence(
        source_module="execution.cost_model", domain=EvidenceDomain.EXECUTION_QUALITY,
        symbol="EURUSD", confidence=0.8, uncertainty=0.2,
        measurements={"estimated_total_cost_r": 0.3}, relevance_horizon_seconds=300.0,
    )
    ms = _evidence_state(extra=[cost_ev])
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
        measurements={"estimated_total_cost_r": 0.5}, relevance_horizon_seconds=300.0,
    )
    ms = _evidence_state(extra=[cost_ev])
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.6)),
                         reward_r_default=0.6, min_expected_value=0.0).reason(ms)
    # EV = 0.6*0.6 - 0.4*1 - 0.5 = 0.36 - 0.4 - 0.5 = -0.54 < 0 ⇒ declined.
    assert out.decision.expected_value < 0.0
    assert out.decision.decision_type == DecisionType.REJECT_OPPORTUNITY


def test_v9_execution_cost_adapter_shapes_measurements():
    ev = evidence_from_execution_cost("EURUSD", {"estimated_spread_r": 0.1,
                                                 "estimated_slippage_r": 0.05})
    assert len(ev) == 1
    m = ev[0].measurements
    assert m["estimated_total_cost_r"] == pytest.approx(0.15)
    assert ev[0].domain == EvidenceDomain.EXECUTION_QUALITY
    assert ev[0].polarity == 0.0


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
