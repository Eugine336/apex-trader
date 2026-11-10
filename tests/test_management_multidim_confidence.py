"""Management confidence is diagnostic metadata, not an action threshold.

Management is thesis-based. Confidence dimensions remain visible for diagnosis,
but cannot authorize EXIT, REVERSE, or SCALE_IN. EXIT requires invalidation,
opportunity loss, or negative verified EV. New exposure requires a separately
qualified Brain opportunity with its own geometry, EV, costs and risk permit.
A degraded opinion with no management fields continues to HOLD (V-008).
"""

from cognition.brain import CognitiveBrain, PositionView
from cognition.contracts import DecisionType, Evidence, MarketState
from llm.reasoner import LLMOpinion


class _Reasoner:
    def __init__(self, opinion, available=True):
        self._opinion = opinion
        self._available = available

    @property
    def available(self):
        return self._available

    def reason(self, symbol, evidence, now=None):
        return self._opinion


class _LegacyOpinion:
    """An opinion with NO confidence dimensions (pre-Part-XXV shape)."""
    def __init__(self, direction, confidence):
        self.direction = direction
        self.confidence = confidence
        self.rationale = "r"
        self.competing_hypotheses = []
        self.missing_information = []


def _state(polarity=0.8):
    ms = MarketState(symbol="EURUSD")
    ms.add(Evidence(source_module="s", confidence=0.9, uncertainty=0.1, polarity=polarity))
    return ms


def _pos(direction="LONG", profit_r=0.0):
    return PositionView(symbol="EURUSD", direction=direction, profit_r=profit_r)


def _brain(opinion, **kw):
    kw.setdefault("allow_scale_in", True)
    kw.setdefault("reverse_confidence", 0.7)
    kw.setdefault("min_confidence_to_act", 0.55)
    return CognitiveBrain(reasoner=_Reasoner(opinion), **kw)


# ── SCALE_IN respects the effective conviction ──────────────────────────────

def test_scale_in_requires_new_opportunity_even_when_execution_is_weak():
    op = LLMOpinion(symbol="EURUSD", direction="FLAT", confidence=0.8,
                    execution_confidence=0.3, management_action="SCALE_IN",
                    thesis_state="strengthening")   # strong thesis, poor execution
    out = _brain(op).manage(_pos("LONG", profit_r=1.0), _state(0.8))
    assert out.decision.decision_type == DecisionType.HOLD
    rr = out.decision.risk_rationale.lower()
    assert "separately qualified brain opportunity" in rr
    assert abs(out.decision.confidence - 0.8) < 1e-9   # raw advisor field only


def test_scale_in_requires_separate_qualified_opportunity_even_when_confidence_is_high():
    op = LLMOpinion(symbol="EURUSD", direction="FLAT", confidence=0.99,
                    management_action="SCALE_IN", thesis_state="strengthening")
    out = _brain(op).manage(_pos("LONG", profit_r=1.0), _state(0.8))
    assert out.decision.decision_type == DecisionType.HOLD


def test_legacy_opinion_without_management_fields_holds():
    # V-008 — a direction-only opinion (no management fields) is degraded
    # management data → HOLD (previously this added via the direction fallback).
    out = _brain(_LegacyOpinion("LONG", 0.8)).manage(_pos("LONG", profit_r=1.0), _state(0.8))
    assert out.decision.decision_type == DecisionType.HOLD


# ── EXIT (risk-reducing) is NOT blocked by a weak execution read ────────────

def test_thesis_invalidation_exits_even_when_execution_confidence_is_weak():
    # An EXIT is purely risk-reducing → it uses the raw scalar, so a weak
    # execution dimension must never make it harder to cut the position.
    op = LLMOpinion(symbol="EURUSD", direction="FLAT", confidence=0.01,
                    execution_confidence=0.2, management_action="EXIT",
                    thesis_state="invalidated")
    out = _brain(op).manage(_pos("LONG"), _state(-0.5))
    assert out.decision.decision_type == DecisionType.EXIT


# ── REVERSE requires a separately qualified opportunity ─────────────────────

def test_reverse_is_never_authorized_by_confidence():
    weak = LLMOpinion(symbol="EURUSD", direction="FLAT", confidence=0.01,
                      execution_confidence=0.2, management_action="REVERSE")
    out = _brain(weak).manage(_pos("LONG"), _state(-0.8))
    strong = LLMOpinion(symbol="EURUSD", direction="FLAT", confidence=0.99,
                        management_action="REVERSE")
    out2 = _brain(strong).manage(_pos("LONG"), _state(-0.8))
    assert out.decision.decision_type == DecisionType.TIGHTEN_RISK
    assert out2.decision.decision_type == DecisionType.TIGHTEN_RISK
    assert out.decision.questions_answered["held_direction"] == "LONG"
    assert out2.decision.questions_answered["held_direction"] == "LONG"


# ── the profile is recorded on the management decision (audit) ──────────────

def test_management_records_raw_confidence_without_effective_profile():
    op = LLMOpinion(symbol="EURUSD", direction="FLAT", confidence=0.8,
                    execution_confidence=0.3, management_action="SCALE_IN",
                    thesis_state="strengthening")
    out = _brain(op).manage(_pos("LONG", profit_r=1.0), _state(0.8))
    qa = out.decision.questions_answered
    assert qa["advisor_confidence_raw"] == 0.8
    assert qa["advisor_confidence_dimensions"]["execution"] == 0.3
    assert "confidence_profile" not in qa
    assert "effective_confidence" not in qa
