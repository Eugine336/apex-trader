"""Management multidimensional confidence (Part XXV) — the profile gates
risk-ADDING actions only.

SCALE_IN adds risk, so it now requires the EFFECTIVE conviction (the weakest of
thesis/opportunity/timing/execution): the Brain never adds into weak execution or
timing. Risk-REDUCING actions (EXIT / TIGHTEN / REVERSE) keep using the raw
scalar — a poor execution read must never make it harder to cut a position. A
legacy opinion carrying only the overall scalar is unchanged (zero regression).
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

def test_scale_in_suppressed_when_execution_weak():
    op = LLMOpinion(symbol="EURUSD", direction="LONG", confidence=0.8,
                    execution_confidence=0.3)   # strong thesis, poor execution
    out = _brain(op).manage(_pos("LONG", profit_r=1.0), _state(0.8))
    assert out.decision.decision_type == DecisionType.HOLD
    rr = out.decision.risk_rationale.lower()
    assert "not adding" in rr and "execution" in rr
    assert abs(out.decision.confidence - 0.3) < 1e-9   # honest effective conviction


def test_scale_in_fires_when_every_dimension_is_strong():
    op = LLMOpinion(symbol="EURUSD", direction="LONG", confidence=0.8)  # all dims ⇒ 0.8
    out = _brain(op).manage(_pos("LONG", profit_r=1.0), _state(0.8))
    assert out.decision.decision_type == DecisionType.SCALE_IN


def test_scale_in_zero_regression_for_legacy_opinion():
    # No dimension attributes at all → resolves to the scalar → adds as before.
    out = _brain(_LegacyOpinion("LONG", 0.8)).manage(_pos("LONG", profit_r=1.0), _state(0.8))
    assert out.decision.decision_type == DecisionType.SCALE_IN


# ── risk-REDUCING actions are NOT blocked by a weak execution read ──────────

def test_exit_on_contrary_is_unaffected_by_weak_execution():
    op = LLMOpinion(symbol="EURUSD", direction="SHORT", confidence=0.6,
                    execution_confidence=0.2)   # contrary; poor execution
    out = _brain(op).manage(_pos("LONG"), _state(-0.5))
    assert out.decision.decision_type == DecisionType.EXIT


def test_reverse_on_strong_contrary_is_unaffected_by_weak_execution():
    op = LLMOpinion(symbol="EURUSD", direction="SHORT", confidence=0.85,
                    execution_confidence=0.2)
    out = _brain(op).manage(_pos("LONG"), _state(-0.8))
    assert out.decision.decision_type == DecisionType.REVERSE
    assert out.direction == "SHORT"


# ── the profile is recorded on the management decision (audit) ──────────────

def test_management_records_confidence_profile():
    op = LLMOpinion(symbol="EURUSD", direction="LONG", confidence=0.8,
                    execution_confidence=0.3)
    out = _brain(op).manage(_pos("LONG", profit_r=1.0), _state(0.8))
    qa = out.decision.questions_answered
    assert qa["confidence_raw"] == 0.8
    assert "execution 0.30" in qa["confidence_profile"]
