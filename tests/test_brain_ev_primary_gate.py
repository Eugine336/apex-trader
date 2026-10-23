"""Identity invariant — net-EV actionability is mandatory in the Cognitive Brain.

Constitution §I (direction is a CONSEQUENCE of cognition), §VI Q13/Q16/Q17 (EV is
evaluated; no premature collapse), §VIII (FLAT = "no exploitable opportunity after
EV/risk/execution", NOT "the indicators disagree").

A positive net-expected-value opportunity must not be rejected solely for falling
below an arbitrary confidence floor. Conversely, negative net EV must not become
actionable just because confidence is high. The legacy ``ev_primary_gate=False``
argument is retained for compatibility but is ignored by the Brain.

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
    # Two market-analysis domains plus explicit reasoning/council metadata. Tests
    # should not accidentally rely on missing coverage being treated as healthy.
    ms = MarketState(symbol=symbol)
    ms.add(Evidence(source_module="s", domain="momentum",
                    confidence=0.9, uncertainty=0.1, polarity=polarity))
    ms.add(Evidence(source_module="s2", domain="structure",
                    confidence=0.9, uncertainty=0.1, polarity=polarity))
    ms.add(Evidence(
        source_module="reasoning_engine.test_council", domain="reasoning",
        symbol=symbol, observation="verified test council coverage",
        confidence=0.9, uncertainty=0.1,
        measurements={
            "advisors_responded": 2,
            "advisors_available": 2,
            "advisors_total": 2,
        },
        relevance_horizon_seconds=600.0,
    ))
    ms.add(Evidence(
        source_module="execution_cost.test_quote", domain="execution_quality",
        symbol=symbol, observation="fresh test spread and slippage quote",
        confidence=0.9, uncertainty=0.1,
        measurements={"estimated_total_cost_r": 0.05},
        relevance_horizon_seconds=60.0,
    ))
    return ms


def _brain(confidence, *, ev_primary=True, threshold=0.0, direction="LONG"):
    return CognitiveBrain(
        reasoner=_Reasoner(_Opinion(direction, confidence)),
        ev_primary_gate=ev_primary,
        ev_action_threshold_r=threshold,
    )


# ── Identity invariant: callers cannot restore the confidence-floor shortcut ──

def test_explicit_legacy_gate_request_cannot_veto_positive_net_ev():
    # The caller explicitly requests the old gate. The Brain must ignore it:
    # positive net EV qualifies the active opportunity, even below confidence 0.55.
    out = _brain(0.45, ev_primary=False).reason(_state())
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.decision.expected_value > 0.0
    assert out.campaign is not None


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


# ── Net-EV qualification remains mandatory on every constructor path ───────

def test_legacy_high_confidence_opens():
    assert _brain(0.80).reason(_state()).decision.decision_type == DecisionType.OPEN_CAMPAIGN


def test_negative_net_ev_is_rejected_even_without_a_confidence_floor():
    out = _brain(0.30, ev_primary=False).reason(_state())
    assert out.decision.decision_type == DecisionType.REJECT_OPPORTUNITY
    assert out.decision.expected_value < 0.0
    assert out.campaign is None


def test_flat_rejects_in_both_modes():
    assert _brain(0.90, direction="FLAT").reason(_state()).decision.decision_type \
        == DecisionType.REJECT_OPPORTUNITY
    assert _brain(0.90, ev_primary=True, direction="FLAT").reason(_state()).decision.decision_type \
        == DecisionType.REJECT_OPPORTUNITY


def test_status_exposes_gate_fields():
    st = _brain(0.80, ev_primary=True, threshold=0.1).get_status()
    assert st["ev_primary_gate"] is True
    assert st["ev_action_threshold_r"] == 0.1


def test_positive_ev_does_not_authorize_when_execution_cost_is_unverified():
    state = _state()
    state.evidence = [
        item for item in state.evidence
        if getattr(item.domain, "value", item.domain) != "execution_quality"
    ]
    out = _brain(0.80).reason(state)
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None
    assert "UNVERIFIED" in out.decision.questions_answered["execution_cost_verification"]
