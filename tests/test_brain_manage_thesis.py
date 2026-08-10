"""Thesis-based campaign management (the cognitive-management refactor).

These tests pin the NEW management contract (Constitution Part XXV Art 11 and the
system architect's constitutional rule): management is THESIS-based, never a
direction comparison, and uncertainty resolves toward HOLD — never liquidation.

Covered:
  * Flaw 1 — a FLAT / uncertain / HOLD read never auto-EXITs.
  * Flaw 2/4 — the management payload carries the campaign context (held side,
    P&L, duration, original thesis) and prefers ``reason_management``.
  * Flaw 3 — a management "HOLD" / "intact" means KEEP, not FLAT→EXIT.
  * Flaw 4 — a degraded council cannot authorise a state change (EXIT / REVERSE
    / SCALE_IN) but never blocks de-risking (TIGHTEN_RISK / PROTECT_PROFIT).
  * Flaw 5 — LONG and SHORT are handled with perfect symmetry.
"""

from cognition.brain import CognitiveBrain, PositionView
from cognition.contracts import DecisionType, Evidence, EvidenceDomain, MarketState
from llm.reasoner import LLMOpinion


# ── stubs ────────────────────────────────────────────────────────────────────

class _MgmtReasoner:
    """A reasoner exposing the management entrypoint (new thesis-based path)."""
    available = True

    def __init__(self, opinion):
        self._op = opinion
        self.reason_payload = None
        self.manage_payload = None

    def reason(self, symbol, evidence, *, now=None, min_interval=None, throttle_key=None):
        self.reason_payload = evidence
        return self._op

    def reason_management(self, symbol, evidence, *, now=None,
                          min_interval=None, throttle_key=None):
        self.manage_payload = evidence
        return self._op


def _mgmt_op(action="", thesis="", conf=0.8, **kw):
    return LLMOpinion(
        symbol="EURUSD", direction="FLAT", confidence=conf,
        management_action=action, thesis_state=thesis, rationale="r", **kw,
    )


def _state(symbol="EURUSD", polarity=0.0):
    ms = MarketState(symbol=symbol)
    ms.add(Evidence(source_module="s", confidence=0.9, uncertainty=0.1, polarity=polarity))
    return ms


def _state_with_advisors(responded, available, total, *, symbol="EURUSD"):
    ms = MarketState(symbol=symbol)
    ms.add(Evidence(source_module="m_structure", domain="structure", symbol=symbol,
                    observation="reading", confidence=0.9, uncertainty=0.1,
                    relevance_horizon_seconds=900.0))
    ms.add(Evidence(
        source_module="reasoning_engine.a", domain=EvidenceDomain.REASONING,
        symbol=symbol, observation="advisor a reasons", confidence=0.8, uncertainty=0.2,
        measurements={"advisors_responded": responded,
                      "advisors_available": available,
                      "advisors_total": total},
        relevance_horizon_seconds=600.0,
    ))
    return ms


def _pos(direction="LONG", profit_r=0.0):
    return PositionView(symbol="EURUSD", direction=direction, profit_r=profit_r)


def _brain(opinion, **kw):
    kw.setdefault("min_confidence_to_act", 0.55)
    kw.setdefault("reverse_confidence", 0.7)
    return CognitiveBrain(reasoner=_MgmtReasoner(opinion), **kw)


# ── Flaw 3 + Flaw 1 — HOLD / intact / uncertain never exit ───────────────────

def test_action_hold_is_hold_not_exit():
    out = _brain(_mgmt_op(action="HOLD", thesis="intact")).manage(_pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.HOLD


def test_thesis_intact_holds():
    out = _brain(_mgmt_op(thesis="intact", conf=0.9)).manage(_pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.HOLD


def test_uncertain_management_opinion_holds():
    # thesis_state present but obscured/unknown, no action → HOLD, never EXIT.
    out = _brain(_mgmt_op(thesis="", action="", conf=0.1,
                          opportunity_status="temporarily_obscured")).manage(
        _pos("SHORT"), _state())
    # opportunity_status alone still marks this a management opinion.
    assert out.decision.decision_type == DecisionType.HOLD


# ── EXIT only on reasoned invalidation ───────────────────────────────────────

def test_thesis_invalidated_exits():
    out = _brain(_mgmt_op(thesis="invalidated", conf=0.8)).manage(_pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.EXIT


def test_action_exit_with_high_conf_exits():
    out = _brain(_mgmt_op(action="EXIT", thesis="weakening", conf=0.8)).manage(
        _pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.EXIT


def test_action_exit_without_justification_downgrades_to_tighten():
    # Model says EXIT but low confidence, no EV/opportunity signal → de-risk, not liquidate.
    out = _brain(_mgmt_op(action="EXIT", thesis="weakening", conf=0.2)).manage(
        _pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.TIGHTEN_RISK


def test_negative_ev_exits():
    out = _brain(_mgmt_op(thesis="weakening", conf=0.4,
                          expected_value="negative net of costs")).manage(
        _pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.EXIT


# ── weakening → tighten; reverse gating ──────────────────────────────────────

def test_thesis_weakening_tightens():
    out = _brain(_mgmt_op(thesis="weakening", conf=0.5)).manage(_pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.TIGHTEN_RISK


def test_reverse_requires_very_high_confidence():
    low = _brain(_mgmt_op(action="REVERSE", conf=0.55)).manage(_pos("LONG"), _state())
    assert low.decision.decision_type == DecisionType.TIGHTEN_RISK
    high = _brain(_mgmt_op(action="REVERSE", conf=0.9),
                  reverse_confidence=0.7).manage(_pos("LONG"), _state())
    assert high.decision.decision_type == DecisionType.REVERSE
    assert high.direction == "SHORT"


def test_scale_in_only_when_strengthening_and_in_profit():
    op = _mgmt_op(action="SCALE_IN", thesis="strengthening", conf=0.9)
    out = _brain(op, allow_scale_in=True).manage(_pos("LONG", profit_r=1.0), _state())
    assert out.decision.decision_type == DecisionType.SCALE_IN
    # Not in profit → conditions not met → HOLD (never a risky add on a loser).
    out2 = _brain(_mgmt_op(action="SCALE_IN", thesis="strengthening", conf=0.9),
                  allow_scale_in=True).manage(_pos("LONG", profit_r=-0.5), _state())
    assert out2.decision.decision_type == DecisionType.HOLD


def test_scale_out_and_protect_pass_through():
    assert _brain(_mgmt_op(action="SCALE_OUT")).manage(
        _pos("LONG"), _state()).decision.decision_type == DecisionType.SCALE_OUT
    assert _brain(_mgmt_op(action="PROTECT_PROFIT")).manage(
        _pos("LONG"), _state()).decision.decision_type == DecisionType.PROTECT_PROFIT


# ── Flaw 4 — council quorum guard ────────────────────────────────────────────

def test_degraded_council_blocks_exit_holds_instead():
    # 1 of 2 responded (< quorum 2) → EXIT must degrade to HOLD.
    out = _brain(_mgmt_op(thesis="invalidated", conf=0.9),
                 min_advisors_for_action=2).manage(
        _pos("LONG"), _state_with_advisors(1, 2, 3))
    assert out.decision.decision_type == DecisionType.HOLD
    assert "degraded council" in out.decision.risk_rationale


def test_degraded_council_blocks_reverse_and_scale_in():
    rev = _brain(_mgmt_op(action="REVERSE", conf=0.95), min_advisors_for_action=2).manage(
        _pos("LONG"), _state_with_advisors(1, 2, 3))
    assert rev.decision.decision_type == DecisionType.HOLD
    sin = _brain(_mgmt_op(action="SCALE_IN", thesis="strengthening", conf=0.9),
                 allow_scale_in=True, min_advisors_for_action=2).manage(
        _pos("LONG", profit_r=1.0), _state_with_advisors(1, 2, 3))
    assert sin.decision.decision_type == DecisionType.HOLD


def test_degraded_council_still_allows_derisking():
    # TIGHTEN_RISK is risk-reducing → always permitted even below quorum.
    out = _brain(_mgmt_op(thesis="weakening", conf=0.5), min_advisors_for_action=2).manage(
        _pos("LONG"), _state_with_advisors(1, 2, 3))
    assert out.decision.decision_type == DecisionType.TIGHTEN_RISK


def test_quorum_met_allows_exit():
    out = _brain(_mgmt_op(thesis="invalidated", conf=0.9),
                 min_advisors_for_action=2).manage(
        _pos("LONG"), _state_with_advisors(2, 2, 3))
    assert out.decision.decision_type == DecisionType.EXIT


# ── Flaw 5 — directional symmetry ────────────────────────────────────────────

def test_exit_is_symmetric_long_and_short():
    for side in ("LONG", "SHORT"):
        out = _brain(_mgmt_op(thesis="invalidated", conf=0.9)).manage(_pos(side), _state())
        assert out.decision.decision_type == DecisionType.EXIT


def test_hold_is_symmetric_long_and_short():
    for side in ("LONG", "SHORT"):
        out = _brain(_mgmt_op(thesis="intact", conf=0.9)).manage(_pos(side), _state())
        assert out.decision.decision_type == DecisionType.HOLD


def test_reverse_flips_symmetrically():
    long_out = _brain(_mgmt_op(action="REVERSE", conf=0.9)).manage(_pos("LONG"), _state())
    short_out = _brain(_mgmt_op(action="REVERSE", conf=0.9)).manage(_pos("SHORT"), _state())
    assert long_out.decision.decision_type == DecisionType.REVERSE
    assert short_out.decision.decision_type == DecisionType.REVERSE
    assert long_out.direction == "SHORT" and short_out.direction == "LONG"


# ── Flaw 2/4 — management payload carries campaign context & prefers mgmt path ─

def test_management_prefers_reason_management_entrypoint():
    r = _MgmtReasoner(_mgmt_op(thesis="intact", conf=0.8))
    CognitiveBrain(reasoner=r).manage(_pos("LONG"), _state())
    assert r.manage_payload is not None      # management entrypoint used
    assert r.reason_payload is None          # NOT the origination entrypoint


def test_management_payload_includes_campaign_context():
    brain = CognitiveBrain(reasoner=_MgmtReasoner(_mgmt_op(thesis="intact")))
    pos = PositionView(symbol="EURUSD", direction="SHORT", profit_r=1.4,
                       hold_seconds=3600.0, size=0.5, campaign_id="c1",
                       entry_confidence=0.7)
    ms = _state()
    payload = brain._management_evidence_payload("EURUSD", ms, ms.consolidation(), pos)
    camp = payload["campaign"]
    assert camp["held_direction"] == "SHORT"
    assert camp["profit_r"] == 1.4
    assert camp["hold_seconds"] == 3600.0
    assert camp["campaign_id"] == "c1"
    assert payload["management"] is True


# ── fail-safe → HOLD, never EXIT on error ────────────────────────────────────

class _BoomReasoner:
    available = True

    def reason_management(self, symbol, evidence, *, now=None,
                          min_interval=None, throttle_key=None):
        raise RuntimeError("provider blew up")


def test_management_fault_holds_never_exits():
    brain = CognitiveBrain(reasoner=_BoomReasoner())
    out = brain.manage(_pos("LONG"), _state())
    assert out.decision.decision_type == DecisionType.HOLD
