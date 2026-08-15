"""V-01 — EV-primary actionability gate (opt-in) for the Cognitive Brain.

Constitution §I (direction is a CONSEQUENCE of cognition), §VI Q13/Q16/Q17 (EV is
evaluated; no premature collapse), §VIII (FLAT = "no exploitable opportunity after
EV/risk/execution", NOT "the indicators disagree").

The legacy gate opens a campaign only when effective confidence clears a floor
(0.55) and uncertainty is under a ceiling (0.6). Because mixed/conflicting evidence
pulls effective confidence down, a genuine POSITIVE-expected-value opportunity is
vetoed purely for being "not confident enough" — the forbidden "conflicting
evidence ⇒ FLAT" shortcut. The opt-in ``ev_primary_gate`` makes a positive EXPECTED
VALUE (net of cost, over flat) the actionability criterion instead; confidence
still feeds the EV and the position sizing.

These tests drive the real ``CognitiveBrain.reason`` path (only the reasoner +
market state are faked) and are pure stdlib (no pytest/numpy).
"""

from __future__ import annotations

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, Evidence, MarketState


class _Opinion:
    def __init__(self, direction: str, confidence: float):
        self.direction = direction
        self.confidence = confidence
        self.rationale = "because"
        self.competing_hypotheses = []
        self.missing_information = []
        # V-002 — a directional advisor opinion carries a first-class, ACTIVATED
        # opportunity so origination flows through a structured object; these
        # tests exercise the EV / act gate, not the legacy-scalar path.
        self.opportunities = (
            [{"id": "auto", "direction": direction, "state": "ACTIVE",
              "quality": confidence, "asymmetry": confidence,
              "evidence_strength": confidence}]
            if str(direction).upper() in ("LONG", "SHORT") else []
        )
        self.preferred_opportunity_id = "auto"


class _Reasoner:
    def __init__(self, opinion):
        self._opinion = opinion

    @property
    def available(self):
        return True

    def reason(self, symbol, evidence, now=None):
        return self._opinion


def _state(symbol: str = "EURUSD", polarity: float = 0.8) -> MarketState:
    # Two evidence domains so the minimum-coverage gate (default 2) is satisfied.
    ms = MarketState(symbol=symbol)
    ms.add(Evidence(source_module="s", domain="momentum",
                    confidence=0.9, uncertainty=0.1, polarity=polarity))
    ms.add(Evidence(source_module="s2", domain="structure",
                    confidence=0.9, uncertainty=0.1, polarity=polarity))
    return ms


def _brain(confidence, *, ev_primary=False, threshold=0.0, direction="LONG"):
    return CognitiveBrain(
        reasoner=_Reasoner(_Opinion(direction, confidence)),
        ev_primary_gate=ev_primary,
        ev_action_threshold_r=threshold,
    )


# ── The V-01 fix: a positive-EV, sub-floor-confidence opportunity ─────────────

def test_legacy_confidence_floor_vetoes_positive_ev():
    # conf 0.45 → EV +0.35R (positive) but below the 0.55 floor → legacy FLAT.
    out = _brain(0.45).reason(_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.decision.expected_value > 0.0   # positive EV was vetoed by the floor


def test_ev_primary_acts_on_positive_ev_below_floor():
    out = _brain(0.45, ev_primary=True).reason(_state())
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.direction == "LONG"
    assert out.decision.expected_value > 0.0
    assert out.campaign is not None


def test_ev_primary_rejects_negative_ev_as_genuine_ev_verdict():
    # conf 0.30 → EV negative → REJECT_OPPORTUNITY (a real EV verdict, not a
    # confidence-floor "observe").
    out = _brain(0.30, ev_primary=True).reason(_state())
    assert out.decision.decision_type == DecisionType.REJECT_OPPORTUNITY
    assert out.decision.expected_value < 0.0
    assert out.campaign is None


def test_ev_primary_threshold_declines_thin_edge():
    # EV +0.35R at conf 0.45; a 0.4R action threshold declines it …
    thin = _brain(0.45, ev_primary=True, threshold=0.4).reason(_state())
    assert thin.decision.decision_type == DecisionType.REJECT_OPPORTUNITY
    # … while a fatter +0.50R edge (conf 0.50) clears it.
    fat = _brain(0.50, ev_primary=True, threshold=0.4).reason(_state())
    assert fat.decision.decision_type == DecisionType.OPEN_CAMPAIGN


# ── Legacy behaviour is unchanged when the gate is off (default) ──────────────

def test_legacy_high_confidence_opens():
    assert _brain(0.80).reason(_state()).decision.decision_type == DecisionType.OPEN_CAMPAIGN


def test_legacy_low_confidence_observes():
    assert _brain(0.30).reason(_state()).decision.decision_type == DecisionType.CONTINUE_OBSERVING


def test_flat_rejects_in_both_modes():
    assert _brain(0.90, direction="FLAT").reason(_state()).decision.decision_type \
        == DecisionType.REJECT_OPPORTUNITY
    assert _brain(0.90, ev_primary=True, direction="FLAT").reason(_state()).decision.decision_type \
        == DecisionType.REJECT_OPPORTUNITY


def test_status_exposes_gate_fields():
    st = _brain(0.80, ev_primary=True, threshold=0.1).get_status()
    assert st["ev_primary_gate"] is True
    assert st["ev_action_threshold_r"] == 0.1
