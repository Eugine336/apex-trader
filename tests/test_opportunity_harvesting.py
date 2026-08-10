"""Tests for the opportunity-harvesting reasoning architecture.

APEX moved from single-direction voting to multi-opportunity hypothesis-ranking.
The reasoner now accepts a SET of ranked opportunities plus a market-state read
and collapses the PREFERRED opportunity to the legacy direction/confidence fields
the whole downstream pipeline already consumes — while carrying the full set for
auditability. The OLD single-direction format must still parse identically
(backward compatibility is required). These tests cover both formats, the
untradeable/FLAT mapping, preferred selection across competing directions, the
prompt contents, and the Brain integration (quality/asymmetry → confidence/EV/
sizing).
"""

import json

import pytest

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, Evidence, MarketState
from llm.reasoner import (
    _MANAGEMENT_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    LLMOpinion,
    LLMReasoner,
    _select_preferred_opportunity,
)


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _StubClient:
    usable = True
    model = "stub"

    def __init__(self, reply):
        self._reply = reply

    def complete(self, system, user):
        self._last_system = system
        self._last_user = user
        return self._reply

    def describe(self):
        return {"provider": "stub", "model": self.model, "has_api_key": False}


def _reasoner(reply):
    return LLMReasoner(client=_StubClient(reply), enabled=True, min_interval_seconds=0)


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


# ── New-format parsing → preferred collapses to direction/confidence ───────────

def test_new_format_extracts_preferred_direction_and_quality():
    reply = _new_reply([_opp(id="opp_1", direction="SHORT", quality=0.72)])
    op = _reasoner(reply).reason("ETHUSD", {"x": 1})
    assert isinstance(op, LLMOpinion)
    assert op.direction == "SHORT"
    assert op.confidence == pytest.approx(0.72)
    assert op.preferred_opportunity_id == "opp_1"
    assert op.market_is_untradeable is False
    assert len(op.opportunities) == 1
    assert op.market_state.get("regime") == "transitional"
    assert op.regime == "transitional"


def test_new_format_maps_scores_to_confidence_dimensions():
    reply = _new_reply([_opp(quality=0.8, evidence_strength=0.6, urgency=0.5)])
    op = _reasoner(reply).reason("ETHUSD", {})
    assert op.thesis_confidence == pytest.approx(0.8)      # quality
    assert op.opportunity_confidence == pytest.approx(0.6)  # evidence_strength
    assert op.timing_confidence == pytest.approx(0.5)       # urgency
    # execution not provided by the opportunity schema → resolves downstream.
    assert op.execution_confidence is None


def test_new_format_asymmetry_becomes_reward_risk_excursions():
    # asymmetry 0.75 → favourable 0.75 / adverse 0.25 → reward multiple 3.0.
    reply = _new_reply([_opp(asymmetry=0.75)])
    op = _reasoner(reply).reason("ETHUSD", {})
    assert float(op.expected_favorable_excursion) == pytest.approx(0.75)
    assert float(op.expected_adverse_excursion) == pytest.approx(0.25)


def test_new_format_invalidation_and_alternatives_backfilled():
    opps = [
        _opp(id="opp_1", direction="SHORT", quality=0.72,
             invalidation_conditions=["reclaim VWAP", "close above M15 high"]),
        _opp(id="opp_2", direction="LONG", state="FORMING", quality=0.6,
             thesis="reversal later"),
    ]
    op = _reasoner(_new_reply(opps, preferred="opp_1")).reason("ETHUSD", {})
    assert op.invalidation == "reclaim VWAP; close above M15 high"
    assert op.what_would_change_my_mind == ["reclaim VWAP", "close above M15 high"]
    # The non-preferred opportunity survives as a conditional alternative.
    assert any("LONG" in a and "reversal later" in a for a in op.alternative_hypotheses)


# ── Old-format backward compatibility (CRITICAL) ───────────────────────────────

def test_old_format_direction_confidence_unchanged():
    reply = json.dumps({
        "direction": "LONG", "confidence": 0.72, "rationale": "HTF trend up",
        "competing_hypotheses": ["range"], "missing_information": ["news"],
    })
    op = _reasoner(reply).reason("EURUSD", {})
    assert op.direction == "LONG"
    assert op.confidence == pytest.approx(0.72)
    assert op.competing_hypotheses == ["range"]
    # No opportunity-harvesting metadata on a legacy reply.
    assert op.opportunities == []
    assert op.preferred_opportunity_id == ""
    assert op.market_is_untradeable is False
    assert op.market_state == {}


def test_old_format_flat_still_flat():
    reply = json.dumps({"direction": "FLAT", "confidence": 0.1})
    op = _reasoner(reply).reason("EURUSD", {})
    assert op.direction == "FLAT"
    assert op.opportunities == []


# ── Untradeable / empty opportunities → FLAT ───────────────────────────────────

def test_untradeable_maps_to_flat():
    reply = _new_reply([], preferred="", untradeable=True, regime="uncertain")
    op = _reasoner(reply).reason("BTCUSD", {})
    assert op.direction == "FLAT"
    assert op.confidence == 0.0
    assert op.market_is_untradeable is True
    assert op.opportunity == "none"


def test_empty_opportunities_without_flag_still_flat():
    reply = _new_reply([], preferred="", untradeable=False)
    op = _reasoner(reply).reason("BTCUSD", {})
    assert op.direction == "FLAT"


def test_all_nondirectional_opportunities_flat():
    # An opportunity with no LONG/SHORT direction cannot drive a campaign.
    reply = _new_reply([_opp(id="opp_1", direction="")], preferred="opp_1")
    op = _reasoner(reply).reason("BTCUSD", {})
    assert op.direction == "FLAT"


# ── Preferred selection across competing directions ────────────────────────────

def test_preferred_id_wins_even_over_higher_ranked():
    # opp_2 ranks higher, but the model explicitly prefers opp_1 (a SHORT).
    opps = [
        _opp(id="opp_1", direction="SHORT", quality=0.6, asymmetry=0.6, evidence_strength=0.6),
        _opp(id="opp_2", direction="LONG", quality=0.9, asymmetry=0.9, evidence_strength=0.9),
    ]
    op = _reasoner(_new_reply(opps, preferred="opp_1")).reason("ETHUSD", {})
    assert op.direction == "SHORT"
    assert op.preferred_opportunity_id == "opp_1"


def test_highest_ranked_selected_when_preference_absent():
    opps = [
        _opp(id="opp_1", direction="SHORT", quality=0.5, asymmetry=0.5, evidence_strength=0.5),
        _opp(id="opp_2", direction="LONG", quality=0.9, asymmetry=0.9, evidence_strength=0.9),
    ]
    op = _reasoner(_new_reply(opps, preferred="")).reason("ETHUSD", {})
    assert op.direction == "LONG"
    assert op.preferred_opportunity_id == "opp_2"


def test_select_preferred_opportunity_helper_ranks_by_product():
    opps = [
        {"id": "a", "direction": "LONG", "quality": 0.9, "asymmetry": 0.2, "evidence_strength": 0.2},
        {"id": "b", "direction": "SHORT", "quality": 0.7, "asymmetry": 0.7, "evidence_strength": 0.7},
    ]
    # b: 0.343 vs a: 0.036 → b preferred despite a's higher raw quality.
    assert _select_preferred_opportunity(opps, None)["id"] == "b"
    assert _select_preferred_opportunity(opps, "missing")["id"] == "b"
    assert _select_preferred_opportunity([], None) is None


# ── Prompt contents ────────────────────────────────────────────────────────────

def test_system_prompt_is_opportunity_harvester():
    p = _SYSTEM_PROMPT
    assert "OPPORTUNITY HARVESTER" in p
    assert "market_is_untradeable" in p
    assert "preferred_opportunity" in p
    assert "opportunities" in p
    # HTF = context, never a veto of a lower-timeframe opportunity.
    assert "context" in p.lower() and "veto" in p.lower()


def test_management_prompt_asks_about_competing_opportunities():
    p = _MANAGEMENT_SYSTEM_PROMPT.lower()
    assert "competing" in p
    assert "superior" in p
    assert "rotation" in p  # capital rotation


# ── Brain integration — quality/asymmetry inform confidence, EV and sizing ─────

class _PassThroughReasoner:
    """Wraps a real LLMReasoner so the Brain drives it with a fixed reply."""

    def __init__(self, reply):
        self._inner = _reasoner(reply)

    @property
    def available(self):
        return True

    def reason(self, symbol, evidence, now=None):
        return self._inner.reason(symbol, evidence, now=now)


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


def test_brain_opens_campaign_from_preferred_opportunity():
    opps = [
        _opp(id="opp_1", direction="LONG", quality=0.8, asymmetry=0.7),
        _opp(id="opp_2", direction="SHORT", state="FORMING", quality=0.4, thesis="micro fade"),
    ]
    out = _brain(_new_reply(opps, preferred="opp_1")).reason(_confident_state())
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.direction == "LONG"
    assert out.campaign is not None and out.campaign.direction == "LONG"
    qa = out.decision.questions_answered
    assert len(qa["opportunity_set"]) == 2
    assert qa["preferred_opportunity"] == "opp_1"
    assert "micro fade" in qa["conditional_alternatives"]
    assert qa["market_state"]["regime"] == "transitional"


def test_brain_multiple_directions_preferred_drives_campaign():
    # HTF LONG + LTF SHORT on the same symbol — preferred SHORT drives it, the
    # LONG remains a recorded conditional alternative (NOT collapsed to FLAT).
    opps = [
        _opp(id="opp_1", direction="SHORT", quality=0.75, asymmetry=0.7, thesis="LTF correction"),
        _opp(id="opp_2", direction="LONG", state="FORMING", quality=0.6, thesis="HTF resumes"),
    ]
    out = _brain(_new_reply(opps, preferred="opp_1")).reason(_confident_state(polarity=-0.6))
    assert out.direction == "SHORT"
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert "HTF resumes" in out.decision.questions_answered["conditional_alternatives"]


def test_brain_sizing_scales_with_asymmetry():
    # Holding quality fixed, higher asymmetry ⇒ better payoff geometry ⇒ higher
    # EV ⇒ larger desired exposure. Demonstrates asymmetry informs sizing.
    lo = _brain(_new_reply([_opp(direction="LONG", quality=0.8, asymmetry=0.55)])
                ).reason(_confident_state())
    hi = _brain(_new_reply([_opp(direction="LONG", quality=0.8, asymmetry=0.8)])
                ).reason(_confident_state())
    assert lo.campaign is not None and hi.campaign is not None
    assert hi.campaign.expected_value > lo.campaign.expected_value
    assert hi.campaign.desired_exposure >= lo.campaign.desired_exposure


def test_brain_confidence_tracks_quality():
    # Holding other scores fixed, higher quality ⇒ higher effective confidence.
    lo = _brain(_new_reply([_opp(direction="LONG", quality=0.55, asymmetry=0.7,
                                 evidence_strength=0.7, urgency=0.7)])
                ).reason(_confident_state())
    hi = _brain(_new_reply([_opp(direction="LONG", quality=0.85, asymmetry=0.7,
                                 evidence_strength=0.7, urgency=0.7)])
                ).reason(_confident_state())
    assert hi.decision.confidence > lo.decision.confidence
