"""Tests for the opportunity-SET reasoning architecture (Part XXV, items 1-9).

The APEX opportunity-harvesting philosophy says the market contains MULTIPLE
opportunities at once, each with its own lifecycle state and activation /
invalidation conditions. Previously the decision/execution architecture
collapsed that ranked SET back to a single direction+confidence before deciding,
recording the set for audit only. These tests cover the corrected end-to-end
behaviour:

* the Brain reasons over first-class :class:`Opportunity` objects (the SET),
* execution requires ACTIVATION — a FORMING opportunity WAITS
  (``OPPORTUNITY_FORMING``) instead of firing immediately,
* NO_OPPORTUNITY (nothing to harvest) is DISTINCT from OPPORTUNITY_FORMING
  (something real, tracked, not yet activated),
* several opportunities coexist (a SHORT correction AND a LONG reversal) without
  being averaged into a weak FLAT,
* campaign formation depends on opportunity quality, not directional consensus,
* the full set (including FORMING ideas) is carried on the DecisionPackage,
* management can compare the live campaign against the tracked opportunity set,
* legacy single-direction replies are entirely unaffected (backward compat).
"""

import json

import pytest

from cognition.brain import CognitiveBrain, PositionView
from cognition.contracts import (
    DecisionType,
    Evidence,
    MarketState,
    Opportunity,
    OpportunityState,
    opportunity_state_from,
)
from llm.reasoner import LLMOpinion, LLMReasoner


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _StubClient:
    usable = True
    model = "stub"

    def __init__(self, reply):
        self._reply = reply

    def complete(self, system, user):
        return self._reply

    def describe(self):
        return {"provider": "stub", "model": self.model, "has_api_key": False}


class _PassThroughReasoner:
    """Wraps a real LLMReasoner so the Brain drives it with a fixed reply."""

    def __init__(self, reply):
        self._inner = LLMReasoner(
            client=_StubClient(reply), enabled=True, min_interval_seconds=0)

    @property
    def available(self):
        return True

    def reason(self, symbol, evidence, now=None):
        return self._inner.reason(symbol, evidence, now=now)


def _opp(**kw):
    base = {
        "id": "opp_1", "horizon": "MTF", "direction": "LONG", "state": "ACTIVE",
        "thesis": "pullback into demand", "why_now": "reclaim",
        "entry_conditions": ["reclaim M15"], "confirmation_conditions": ["HL"],
        "invalidation_conditions": ["lose demand"], "target_logic": "range high",
        "quality": 0.7, "asymmetry": 0.7, "urgency": 0.7, "evidence_strength": 0.7,
    }
    base.update(kw)
    return base


def _new_reply(opps, preferred="opp_1", untradeable=False, regime="transitional"):
    return json.dumps({
        "market_state": {
            "regime": regime, "context": "HTF up",
            "dominant_pressure": "transitioning", "key_location": "demand",
            "volatility_state": "expanding",
        },
        "opportunities": opps,
        "preferred_opportunity": preferred,
        "market_is_untradeable": untradeable,
        "reasoning_summary": "tie it together",
    })


def _confident_state(symbol="ETHUSD", polarity=0.8):
    ms = MarketState(symbol=symbol)
    ms.add(Evidence(source_module="s", domain="momentum", confidence=0.9,
                    uncertainty=0.1, polarity=polarity))
    ms.add(Evidence(source_module="s2", domain="structure", confidence=0.9,
                    uncertainty=0.1, polarity=polarity))
    return ms


def _brain(reply):
    return CognitiveBrain(
        reasoner=_PassThroughReasoner(reply),
        min_confidence_to_act=0.5, min_advisors_for_action=0, min_evidence_domains=0,
    )


# ── Opportunity dataclass ───────────────────────────────────────────────────

def test_opportunity_from_reply_parses_all_fields():
    o = Opportunity.from_reply(_opp(id="opp_x", direction="SHORT", state="CONFIRMED"),
                               preferred_id="opp_x")
    assert o.opportunity_id == "opp_x"
    assert o.direction == "SHORT"
    assert o.state is OpportunityState.CONFIRMED
    assert o.horizon == "MTF"
    assert o.entry_conditions == ["reclaim M15"]
    assert o.invalidation_conditions == ["lose demand"]
    assert o.is_preferred is True
    assert o.is_directional is True


def test_opportunity_actionability_by_state():
    # ACTIVE / CONFIRMED / STRENGTHENING are eligible; FORMING must wait.
    for st in ("active", "confirmed", "strengthening"):
        assert Opportunity(direction="LONG", state=st).is_actionable is True
    forming = Opportunity(direction="LONG", state="forming")
    assert forming.is_actionable is False
    assert forming.is_forming is True
    # Fading states are neither actionable nor "forming".
    for st in ("weakening", "exhausted"):
        o = Opportunity(direction="LONG", state=st)
        assert o.is_actionable is False and o.is_forming is False


def test_opportunity_unknown_state_is_not_actionable():
    # V-003 — an opportunity with no explicit lifecycle state (UNKNOWN) has not
    # been shown to have ACTIVATED, so it is NOT actionable: direction alone is
    # not an activated opportunity.
    assert Opportunity(direction="LONG").state is OpportunityState.UNKNOWN
    assert Opportunity(direction="LONG").is_actionable is False
    # A non-directional opportunity is never actionable regardless of state.
    assert Opportunity(direction="FLAT", state="active").is_actionable is False


def test_opportunity_rank_score_quality_fallback():
    # Missing asymmetry/evidence fall back to quality (monotonic, non-zero).
    assert Opportunity(direction="LONG", quality=0.8).rank_score == pytest.approx(0.8 ** 3)
    full = Opportunity(direction="LONG", quality=0.5, asymmetry=0.5, evidence_strength=0.5)
    assert full.rank_score == pytest.approx(0.125)


def test_opportunity_state_alias_normalisation():
    assert opportunity_state_from("triggered") is OpportunityState.ACTIVE
    assert opportunity_state_from("fading") is OpportunityState.WEAKENING
    assert opportunity_state_from("setting up") is OpportunityState.FORMING
    assert opportunity_state_from("") is OpportunityState.UNKNOWN
    assert opportunity_state_from("nonsense") is OpportunityState.UNKNOWN


# ── Activation gate (item 7): FORMING waits, ACTIVE/CONFIRMED open ──────────

def test_forming_opportunity_waits_instead_of_opening():
    reply = _new_reply([_opp(id="opp_1", direction="SHORT", state="FORMING",
                             quality=0.8, asymmetry=0.8, evidence_strength=0.8,
                             urgency=0.8)])
    out = _brain(reply).reason(_confident_state(polarity=-0.6))
    assert out.decision.decision_type == DecisionType.OPPORTUNITY_FORMING
    assert out.campaign is None
    assert out.direction == "FLAT"
    # A FORMING opportunity must never authorise execution.
    assert out.decision.authorises_action is False


def test_forming_gate_blocks_even_at_maximum_conviction():
    # Every sub-score maxed out — the ONLY reason not to fire is that the
    # opportunity has not activated. It must still wait.
    reply = _new_reply([_opp(id="opp_1", direction="LONG", state="FORMING",
                             quality=1.0, asymmetry=0.9, evidence_strength=1.0,
                             urgency=1.0)])
    out = _brain(reply).reason(_confident_state(polarity=0.8))
    assert out.decision.decision_type == DecisionType.OPPORTUNITY_FORMING
    assert out.campaign is None


@pytest.mark.parametrize("state", ["ACTIVE", "CONFIRMED", "STRENGTHENING"])
def test_activated_opportunity_opens_campaign(state):
    reply = _new_reply([_opp(id="opp_1", direction="SHORT", state=state,
                             quality=0.8, asymmetry=0.8, evidence_strength=0.8)])
    out = _brain(reply).reason(_confident_state(polarity=-0.6))
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.campaign is not None and out.campaign.direction == "SHORT"
    # The campaign records WHICH opportunity (and its state) activated it.
    assert out.campaign.opportunity_id == "opp_1"
    assert out.campaign.opportunity_state == state.lower()


# ── NO_OPPORTUNITY vs OPPORTUNITY_FORMING are distinct (item 4) ─────────────

def test_untradeable_is_no_opportunity_not_forming():
    out = _brain(_new_reply([], preferred="", untradeable=True)).reason(_confident_state(0.0))
    assert out.decision.decision_type == DecisionType.NO_OPPORTUNITY
    assert out.campaign is None


def test_all_nondirectional_is_no_opportunity():
    out = _brain(_new_reply([_opp(id="opp_1", direction="")])).reason(_confident_state(0.0))
    assert out.decision.decision_type == DecisionType.NO_OPPORTUNITY


def test_forming_and_no_opportunity_are_different_decisions():
    forming = _brain(_new_reply([_opp(direction="LONG", state="FORMING")])
                     ).reason(_confident_state()).decision.decision_type
    nothing = _brain(_new_reply([], preferred="", untradeable=True)
                     ).reason(_confident_state(0.0)).decision.decision_type
    assert forming == DecisionType.OPPORTUNITY_FORMING
    assert nothing == DecisionType.NO_OPPORTUNITY
    assert forming != nothing


# ── The full opportunity SET is first-class on the decision (items 1, 2, 9) ──

def test_decision_carries_full_first_class_opportunity_set():
    opps = [
        _opp(id="opp_1", direction="SHORT", state="ACTIVE", quality=0.75),
        _opp(id="opp_2", direction="LONG", state="FORMING", quality=0.6,
             thesis="reversal later"),
    ]
    out = _brain(_new_reply(opps, preferred="opp_1")).reason(_confident_state(polarity=-0.6))
    dec = out.decision
    assert len(dec.opportunities) == 2
    assert all(isinstance(o, Opportunity) for o in dec.opportunities)
    assert dec.preferred_opportunity_id == "opp_1"
    # The FORMING alternative is tracked (not discarded, not collapsed to FLAT).
    assert [o.opportunity_id for o in dec.forming_opportunities] == ["opp_2"]
    assert [o.opportunity_id for o in dec.actionable_opportunities] == ["opp_1"]


def test_multiple_coexisting_opportunities_short_active_long_forming():
    # HTF LONG reversal (forming) + LTF SHORT correction (active) coexist. The
    # activated SHORT drives the campaign; the LONG is retained as a tracked
    # forming opportunity — the market is NOT averaged to FLAT (item 5).
    opps = [
        _opp(id="opp_1", direction="SHORT", state="ACTIVE", quality=0.75,
             thesis="LTF correction"),
        _opp(id="opp_2", direction="LONG", state="FORMING", quality=0.6,
             thesis="HTF resumes"),
    ]
    out = _brain(_new_reply(opps, preferred="opp_1")).reason(_confident_state(polarity=-0.6))
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.direction == "SHORT"
    directions = {o.opportunity_id: o.direction for o in out.decision.opportunities}
    assert directions == {"opp_1": "SHORT", "opp_2": "LONG"}


def test_campaign_forms_on_quality_not_directional_consensus():
    # A single high-quality SHORT correction with clear activation is actionable
    # even though a competing LONG opportunity is present (advisors "disagree").
    # Campaign formation depends on the opportunity, not a directional vote (item 6).
    opps = [
        _opp(id="opp_1", direction="SHORT", state="ACTIVE",
             quality=0.85, asymmetry=0.8, evidence_strength=0.8),
        _opp(id="opp_2", direction="LONG", state="FORMING", quality=0.55),
    ]
    out = _brain(_new_reply(opps, preferred="opp_1")).reason(_confident_state(polarity=-0.6))
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.direction == "SHORT"


# ── Backward compatibility: legacy single-direction replies unaffected ──────

# ── V-002: a legacy single-direction reply is observational, not authority ──

def test_legacy_reply_is_observational_not_execution_authority():
    # V-002 — a legacy direction+confidence reply carries no first-class,
    # activated opportunity, so it is OBSERVATIONAL input only and must NOT open a
    # campaign from a pre-collapsed directional scalar.
    reply = json.dumps({
        "direction": "LONG", "confidence": 0.72, "rationale": "HTF trend up",
    })
    out = _brain(reply).reason(_confident_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None
    assert out.direction == "FLAT"
    # No opportunity-set metadata on a legacy decision.
    assert out.decision.opportunities == []
    assert out.decision.preferred_opportunity_id == ""
    assert "observational" in out.decision.risk_rationale


def test_legacy_flat_reply_still_continue_observing():
    # A legacy FLAT reply keeps its historical decision type — the new
    # NO_OPPORTUNITY / OPPORTUNITY_FORMING states apply only to harvesting replies.
    reply = json.dumps({"direction": "FLAT", "confidence": 0.1})
    out = _brain(reply).reason(_confident_state(0.0))
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.decision.opportunities == []


# ── New DecisionTypes are non-authorising ───────────────────────────────────

def test_new_decision_types_do_not_authorise_action():
    from cognition.contracts import DecisionPackage
    for dt in (DecisionType.NO_OPPORTUNITY, DecisionType.OPPORTUNITY_FORMING):
        assert DecisionPackage(symbol="X", decision_type=dt).authorises_action is False


# ── Management compares the live campaign against the tracked set (item 8) ───

class _ManageCapture:
    """Origination via a harvesting reply; management captures the payload."""

    def __init__(self, reply):
        self._orig = LLMReasoner(
            client=_StubClient(reply), enabled=True, min_interval_seconds=0)
        self.captured_payload = None

    @property
    def available(self):
        return True

    def reason(self, symbol, evidence, now=None):
        return self._orig.reason(symbol, evidence, now=now)

    def reason_management(self, symbol, payload, now=None, **kwargs):
        self.captured_payload = payload
        return LLMOpinion(
            symbol=symbol, direction="FLAT", confidence=0.6,
            management_action="HOLD", thesis_state="intact",
            rationale="thesis intact",
        )


def test_management_payload_surfaces_competing_opposing_opportunity():
    # A held LONG campaign with a newly tracked, superior SHORT opportunity: the
    # management payload must expose the opportunity SET and flag the opposing
    # rotation candidate (previously no set was tracked to compare against).
    opps = [
        _opp(id="opp_1", direction="LONG", state="ACTIVE", quality=0.7),
        _opp(id="opp_2", direction="SHORT", state="ACTIVE", quality=0.85,
             asymmetry=0.8, evidence_strength=0.8, thesis="correction now"),
    ]
    reasoner = _ManageCapture(_new_reply(opps, preferred="opp_1"))
    brain = CognitiveBrain(
        reasoner=reasoner, min_confidence_to_act=0.5,
        min_advisors_for_action=0, min_evidence_domains=0,
    )
    # 1) Originate so the Brain records the visible opportunity set for the symbol.
    brain.reason(_confident_state(symbol="ETHUSD", polarity=0.6))
    # 2) Manage a held LONG position on the same symbol.
    pos = PositionView(symbol="ETHUSD", direction="LONG", profit_r=0.2, hold_seconds=120.0)
    brain.manage(pos, _confident_state(symbol="ETHUSD", polarity=-0.4))

    payload = reasoner.captured_payload
    assert payload is not None
    assert "current_opportunities" in payload
    ids = {o["id"] for o in payload["current_opportunities"]}
    assert ids == {"opp_1", "opp_2"}
    opposing = payload["campaign"]["competing_opposing_opportunity"]
    assert opposing["id"] == "opp_2"
    assert opposing["direction"] == "SHORT"
