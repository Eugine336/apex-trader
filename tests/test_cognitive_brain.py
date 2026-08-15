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
        # V-002 — a directional advisor opinion carries a first-class, ACTIVATED
        # opportunity so origination flows through a structured object; these
        # tests exercise sizing/EV/observability, not the legacy-scalar path.
        self.opportunities = (
            [{"id": "auto", "direction": direction, "state": "ACTIVE",
              "quality": confidence, "asymmetry": confidence,
              "evidence_strength": confidence}]
            if str(direction).upper() in ("LONG", "SHORT") else []
        )
        self.preferred_opportunity_id = "auto"


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
    # Two distinct evidence domains so the Brain's minimum-coverage gate
    # (Article XXXIV, default 2 domains) is satisfied for the act path.
    ms.add(Evidence(source_module="s", domain="momentum", confidence=0.9,
                    uncertainty=0.1, polarity=polarity))
    ms.add(Evidence(source_module="s2", domain="structure", confidence=0.9,
                    uncertainty=0.1, polarity=polarity))
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


# ── V15: EV-proportional sizing (Part XXVIII — capital is competitive) ─────────

class _RichOpinion:
    """Opinion carrying payoff-geometry excursions so EV is meaningful."""

    def __init__(self, direction, confidence, efe="", eae=""):
        self.direction = direction
        self.confidence = confidence
        self.rationale = "because"
        self.competing_hypotheses = []
        self.missing_information = []
        self.expected_favorable_excursion = efe
        self.expected_adverse_excursion = eae
        # V-002 — carry a first-class, ACTIVATED opportunity so origination flows
        # through a structured object (EV/sizing still derive from the excursions).
        self.opportunities = (
            [{"id": "auto", "direction": direction, "state": "ACTIVE",
              "quality": confidence, "asymmetry": confidence,
              "evidence_strength": confidence}]
            if str(direction).upper() in ("LONG", "SHORT") else []
        )
        self.preferred_opportunity_id = "auto"


def test_sizing_is_lesser_of_confidence_and_ev_normalized():
    # confidence 0.8, default reward 2.0R → EV = 0.8*2 - 0.2*1 = 1.4R;
    # ev_normalized = 1.4/2.0 = 0.7. Sizing takes the lesser of ev_normalized and
    # the reasoning-quality-weighted confidence (Art XIX): a bare opinion (quality
    # 0.1) weights to 0.8*0.1 + 0.9*0.55 = 0.575, so exposure = min(0.575, 0.7).
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)), reward_r_default=2.0)
    out = brain.reason(_confident_state(polarity=0.8))
    assert out.campaign is not None
    assert out.campaign.desired_exposure == pytest.approx(0.575)
    assert out.campaign.desired_exposure < out.campaign.confidence
    assert "sizing_rationale" in out.decision.questions_answered


def test_high_confidence_poor_payoff_is_sized_down():
    # Same 0.8 confidence but poor payoff geometry (0.5R favorable vs 1.0R adverse)
    # → reward 0.5R → EV = 0.8*0.5 - 0.2*1 = 0.2R → ev_normalized = 0.2/2.0 = 0.1
    # → exposure = min(0.8, 0.1) = 0.1 — sized far below confidence.
    op = _RichOpinion("LONG", 0.8, efe="0.5R", eae="1.0R")
    brain = CognitiveBrain(reasoner=_Reasoner(op), reward_r_default=2.0)
    out = brain.reason(_confident_state(polarity=0.8))
    assert out.campaign is not None
    assert out.campaign.desired_exposure == 0.1


def test_campaign_carries_expected_value_field():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)), reward_r_default=2.0)
    out = brain.reason(_confident_state(polarity=0.8))
    assert out.campaign is not None
    assert out.campaign.expected_value == pytest.approx(1.4)
    assert out.campaign.to_dict()["expected_value"] == pytest.approx(1.4)


# ── V13: Brain applies calibration correction to effective confidence ──────────

class _StubCalibration:
    def __init__(self, factor):
        self._factor = factor

    def calibration_adjustment(self):
        return self._factor

    def metrics(self):
        return {"samples": 100, "mean_confidence": 0.9,
                "win_rate": 0.63, "reliability_gap": 0.27, "brier": 0.24}


def test_calibration_attenuates_effective_confidence():
    # Over-predicting Brain: factor 0.7 attenuates eff_conf 0.8 → 0.56.
    brain = CognitiveBrain(
        reasoner=_Reasoner(_Opinion("LONG", 0.8)),
        calibration=_StubCalibration(0.7),
    )
    out = brain.reason(_confident_state(polarity=0.8))
    assert out.decision.confidence == pytest.approx(0.56)
    note = out.decision.questions_answered.get("calibration_correction", "")
    assert note.startswith("applied: factor 0.700")


def test_calibration_not_wired_records_inactive():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)))
    out = brain.reason(_confident_state(polarity=0.8))
    assert out.decision.questions_answered.get("calibration_correction") == (
        "not active (insufficient samples or not wired)"
    )


def test_calibration_neutral_factor_leaves_confidence_unchanged():
    brain = CognitiveBrain(
        reasoner=_Reasoner(_Opinion("LONG", 0.8)),
        calibration=_StubCalibration(1.0),
    )
    out = brain.reason(_confident_state(polarity=0.8))
    assert out.decision.confidence == pytest.approx(0.8)
