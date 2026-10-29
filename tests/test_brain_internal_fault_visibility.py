"""An internal reasoning fault fails closed but is never presented as a market view."""

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType


class _BrokenState:
    symbol = "EURUSD"

    def consolidation(self, now=None):
        raise RuntimeError("evidence store unreadable")


def test_reasoning_fault_is_a_labelled_non_action_not_a_market_conclusion():
    out = CognitiveBrain(reasoner=None).reason(_BrokenState())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.internal_fault is True
    assert out.to_dict()["internal_fault"] is True
    assert "internal fault" in out.decision.questions_answered["should_i_do_nothing"]
    assert out.decision.do_nothing_considered is False
    assert out.decision.confidence == 0.0
    assert out.campaign is None


def test_ordinary_observation_is_not_flagged_as_a_fault():
    brain = CognitiveBrain(reasoner=None)
    out = brain._observe("EURUSD", "no qualifying opportunity", {})
    assert out.internal_fault is False
    assert out.decision.do_nothing_considered is True
