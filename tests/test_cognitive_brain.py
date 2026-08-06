"""Tests for the AI Cognitive Brain (Single Reasoner) — Constitution Parts I/II/IV."""

import pytest

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, Evidence, MarketState


class _Opinion:
    def __init__(self, direction, confidence, rationale="because", competing=None, missing=None):
        self.direction = direction
        self.confidence = confidence
        self.rationale = rationale
        self.competing_hypotheses = competing or []
        self.missing_information = missing or []


class _Reasoner:
    def __init__(self, opinion, available=True):
        self._opinion = opinion
        self._available = available

    @property
    def available(self):
        return self._available

    def reason(self, symbol, evidence, now=None):
        if isinstance(self._opinion, Exception):
            raise self._opinion
        return self._opinion


def _confident_state(symbol="EURUSD", polarity=0.8):
    ms = MarketState(symbol=symbol)
    ms.add(Evidence(source_module="s", confidence=0.9, uncertainty=0.1, polarity=polarity))
    return ms


def test_no_reasoner_defaults_to_observe():
    brain = CognitiveBrain(reasoner=None)
    out = brain.reason(_confident_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.decision.do_nothing_considered is True
    assert out.campaign is None


def test_none_opinion_defaults_to_observe():
    brain = CognitiveBrain(reasoner=_Reasoner(None))
    out = brain.reason(_confident_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING


def test_confident_long_opens_campaign():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)))
    out = brain.reason(_confident_state(polarity=0.8))
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.direction == "LONG"
    assert out.campaign is not None
    assert out.campaign.direction == "LONG"
    assert out.campaign.decision_id == out.decision.decision_id


def test_low_confidence_does_not_open():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.3)))
    out = brain.reason(_confident_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None


def test_high_uncertainty_blocks_action():
    # Empty market state ⇒ maximal uncertainty ⇒ no campaign even with a strong opinion.
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.95)))
    out = brain.reason(MarketState(symbol="EURUSD"))
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING


def test_flat_high_confidence_rejects_opportunity():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("FLAT", 0.9)))
    out = brain.reason(_confident_state())
    assert out.decision.decision_type == DecisionType.REJECT_OPPORTUNITY
    assert out.campaign is None


def test_reasoner_fault_is_fail_safe():
    brain = CognitiveBrain(reasoner=_Reasoner(RuntimeError("boom")))
    out = brain.reason(_confident_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert brain.get_status()["faults"] == 1


def test_status_and_latest():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)))
    brain.reason(_confident_state(symbol="EURUSD"))
    st = brain.get_status()
    assert st["decisions"] == 1 and st["campaigns_opened"] == 1
    assert brain.latest("EURUSD") is not None


def test_required_questions_are_recorded():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8, missing=["news"])))
    out = brain.reason(_confident_state())
    assert out.decision.questions_answered.get("should_i_do_nothing")
    assert "news" in out.decision.questions_answered.get("information_missing", "")
