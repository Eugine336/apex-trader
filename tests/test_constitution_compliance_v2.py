"""Constitutional compliance — Session 2 fixes.

Executable assertions for the six violations addressed in this session:

* V3  — Advisor quorum (Article XX): a campaign needs at least ``min_advisors``.
* V4  — Degraded-mode observation (Article XXI): a partial council is recorded
  and logged as DEGRADED COGNITION for visibility, but is observation-only and
  does NOT attenuate the Brain's effective confidence.
* V8  — Silent-failure integrity Evidence (Article XXXIV): a failed evidence
  source surfaces a zero-confidence integrity Evidence rather than degrading
  silently.
* V10 — Stale-data horizon (Article XXXV): evidence with no horizon expires
  after ``DEFAULT_EVIDENCE_HORIZON_SECONDS``.
* V12 — Minimum evidence-domain coverage (Article XXXIV).
* V17 — Provider backoff (Article XX): a persistently failing provider is not
  retried every cycle.
"""

import json
from types import SimpleNamespace

from cognition.brain import CognitiveBrain
from cognition.contracts import (
    DEFAULT_EVIDENCE_HORIZON_SECONDS,
    DecisionType,
    Evidence,
    EvidenceDomain,
    MarketState,
)
from cognition.evidence_adapters import evidence_from_reasoning
from cognition.loop import EvidenceConsolidator
from llm.reasoner import LLMReasoner
from llm.reasoning_orchestrator import (
    EngineOpinion,
    ReasoningEngine,
    ReasoningOrchestrator,
)


# ── reasoner / opinion stubs ────────────────────────────────────────────────

class _Opinion:
    def __init__(self, direction="LONG", confidence=0.8):
        self.direction = direction
        self.confidence = confidence
        self.rationale = "scripted"


class _Reasoner:
    available = True

    def __init__(self, opinion):
        self._op = opinion

    def reason(self, symbol, evidence, now=None):
        return self._op


def _state_with_advisors(responded, available, total, *, symbol="XAUUSD",
                         extra_domains=("structure", "momentum")):
    """A MarketState carrying reasoning Evidence with council-coverage counts
    plus enough distinct domains to clear the minimum-coverage gate."""
    ms = MarketState(symbol=symbol)
    for dom in extra_domains:
        ms.add(Evidence(source_module=f"m_{dom}", domain=dom, symbol=symbol,
                        observation="reading", confidence=0.9, uncertainty=0.1,
                        relevance_horizon_seconds=900.0))
    ms.add(Evidence(
        source_module="reasoning_engine.a", domain=EvidenceDomain.REASONING,
        symbol=symbol, observation="advisor a reasons", confidence=0.8,
        uncertainty=0.2,
        measurements={
            "advisors_responded": responded,
            "advisors_available": available,
            "advisors_total": total,
        },
        relevance_horizon_seconds=600.0,
    ))
    return ms


# ── V3 — advisor quorum ─────────────────────────────────────────────────────

def test_v3_brain_declines_when_only_one_advisor_responded():
    # 1 of 1 responded, quorum default = 2 → cognitive coverage insufficient.
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)),
                           min_advisors_for_action=2)
    out = brain.reason(_state_with_advisors(1, 1, 3))
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None
    assert "advisor quorum not met" in out.decision.risk_rationale
    quorum = out.decision.questions_answered.get("advisor_quorum", "")
    assert quorum.startswith("NOT MET")


def test_v3_brain_acts_when_two_advisors_responded():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)),
                           min_advisors_for_action=2)
    out = brain.reason(_state_with_advisors(2, 2, 3))
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.campaign is not None and out.campaign.direction == "LONG"
    assert out.decision.questions_answered.get("advisor_quorum", "").startswith("MET")


def test_v3_quorum_unenforced_without_council_metadata():
    # No reasoning Evidence carrying counts ⇒ gate not enforced (backward compat).
    ms = MarketState(symbol="XAUUSD")
    for dom in ("structure", "momentum"):
        ms.add(Evidence(source_module=f"m_{dom}", domain=dom, confidence=0.9,
                        uncertainty=0.1, relevance_horizon_seconds=900.0))
    out = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8))).reason(ms)
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN


# ── V4 — degraded-mode observation (observation-only, no penalty) ───────────

def test_v4_degradation_observed_but_not_penalised_when_less_than_half_responded():
    # 1 of 4 available responded (< 50%) ⇒ degraded; quorum lowered to 1 so the
    # degradation is what we observe (not a quorum block). Degradation is now
    # observation-only: it is recorded/logged but does NOT attenuate confidence.
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.9)),
                           min_advisors_for_action=1,
                           degraded_confidence_multiplier=0.7)
    out = brain.reason(_state_with_advisors(1, 4, 15))
    deg = out.decision.questions_answered.get("cognitive_degradation", "")
    assert deg.startswith("OBSERVED")
    # Confidence is NOT penalised — 0.9 is preserved (above the 0.55 act bar).
    assert abs(out.decision.confidence - 0.9) < 1e-6
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN


def test_v4_no_degradation_when_majority_responded():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.9)),
                           min_advisors_for_action=1,
                           degraded_confidence_multiplier=0.7)
    out = brain.reason(_state_with_advisors(3, 4, 15))
    deg = out.decision.questions_answered.get("cognitive_degradation", "")
    assert deg.startswith("NONE")
    assert abs(out.decision.confidence - 0.9) < 1e-6


# ── V8 — silent-failure integrity Evidence ─────────────────────────────────

def _boom(*_a, **_k):
    raise RuntimeError("source down")


def test_v8_price_failure_adds_integrity_evidence():
    con = EvidenceConsolidator()
    con.set_price_source(_boom)
    ms = con.build("EURUSD")
    integrity = [e for e in ms.evidence if e.source_module == "consolidator.integrity"]
    assert any("PRICE DATA UNAVAILABLE" in e.observation for e in integrity)
    e0 = next(e for e in integrity if "PRICE DATA UNAVAILABLE" in e.observation)
    assert e0.confidence == 0.0 and e0.uncertainty == 1.0


def test_v8_many_source_failures_add_meta_integrity_evidence():
    con = EvidenceConsolidator(vote_source=_boom)
    con.set_price_source(_boom)
    con.set_portfolio_source(_boom)
    ms = con.build("EURUSD")
    obs = [e.observation for e in ms.evidence
           if e.source_module == "consolidator.integrity"]
    assert any("EVIDENCE INTEGRITY DEGRADED" in o for o in obs)


# ── V10 — stale-data horizon ────────────────────────────────────────────────

def test_v10_evidence_with_no_horizon_expires_after_default():
    e = Evidence(source_module="x", timestamp_epoch=1000.0)
    assert e.relevance_horizon_seconds is None
    assert e.is_fresh(now=1000.0 + DEFAULT_EVIDENCE_HORIZON_SECONDS - 1.0) is True
    assert e.is_fresh(now=1000.0 + DEFAULT_EVIDENCE_HORIZON_SECONDS + 1.0) is False


# ── V12 — minimum evidence-domain coverage ──────────────────────────────────

def _state_domains(n, *, symbol="XAUUSD"):
    domains = ("structure", "momentum", "liquidity", "volume")[:max(1, n)]
    ms = MarketState(symbol=symbol)
    for dom in domains:
        ms.add(Evidence(source_module=f"m_{dom}", domain=dom, symbol=symbol,
                        observation="reading", confidence=0.9, uncertainty=0.1,
                        relevance_horizon_seconds=900.0))
    return ms


def test_v12_brain_declines_when_too_few_domains():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)),
                           min_evidence_domains=2)
    out = brain.reason(_state_domains(1))
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert "insufficient evidence coverage" in out.decision.risk_rationale
    cov = out.decision.questions_answered.get("evidence_coverage", "")
    assert cov.startswith("INSUFFICIENT")


def test_v12_brain_acts_with_adequate_domains():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)),
                           min_evidence_domains=2)
    out = brain.reason(_state_domains(2))
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.decision.questions_answered.get("evidence_coverage", "").startswith("ADEQUATE")


# ── V17 — provider backoff ──────────────────────────────────────────────────

class _StubClient:
    usable = True
    model = "stub"

    def __init__(self, reply):
        self._reply = reply

    def complete(self, system, user):
        return self._reply


def test_v17_reasoner_backs_off_after_consecutive_failures():
    r = LLMReasoner(client=_StubClient(None), enabled=True, min_interval_seconds=0)
    # First call fails (no reply) → arms the backoff window.
    assert r.reason("EURUSD", {}, now=1000.0) is None
    st = r.get_status()
    assert st["consecutive_failures"] == 1
    # A call within the backoff window is skipped outright (not another failure).
    assert r.reason("EURUSD", {}, now=1000.5) is None
    assert r.get_status()["consecutive_failures"] == 1
    # Once the window elapses and the provider recovers, it is retried + resets.
    r._client = _StubClient(json.dumps({"direction": "LONG", "confidence": 0.8}))
    assert r.reason("EURUSD", {}, now=2000.0) is not None
    assert r.get_status()["consecutive_failures"] == 0


def test_v17_backoff_grows_with_more_failures():
    r = LLMReasoner(client=_StubClient(None), enabled=True, min_interval_seconds=0)
    r.reason("EURUSD", {}, now=1000.0)          # failure 1 → backoff 2s (until 1002)
    # Skipped (within backoff) — does not count as a new failure.
    assert r.reason("EURUSD", {}, now=1001.0) is None
    assert r.get_status()["consecutive_failures"] == 1
    # Past the 2s window → retried, fails again → failure 2 → backoff 4s.
    assert r.reason("EURUSD", {}, now=1003.0) is None
    assert r.get_status()["consecutive_failures"] == 2
    # Within the new 4s window → skipped.
    assert r.reason("EURUSD", {}, now=1005.0) is None
    assert r.get_status()["consecutive_failures"] == 2


# ── V3 orchestrator carries advisor counts end-to-end ───────────────────────

class _FakeReasoner:
    available = True

    def __init__(self, none=False):
        self._none = none

    def reason(self, symbol, evidence, now=None):
        if self._none:
            return None
        return SimpleNamespace(direction="LONG", confidence=0.7, rationale="r")


def test_v3_consultation_reports_advisor_coverage():
    orch = ReasoningOrchestrator([
        ReasoningEngine("a", _FakeReasoner()),
        ReasoningEngine("b", _FakeReasoner(none=True)),   # asked, no reply
        ReasoningEngine("c", _FakeReasoner()),
    ], max_engines=5)
    con = orch.consult("EURUSD", {})
    assert con.advisors_total == 3
    assert con.advisors_available == 3           # all three asked
    assert con.advisors_responded == 2           # 'b' did not reply
    # The counts reach the Brain via the reasoning Evidence measurements.
    ev = evidence_from_reasoning("EURUSD", con)
    assert ev and ev[0].measurements["advisors_responded"] == 2
    assert ev[0].measurements["advisors_available"] == 3
    assert ev[0].measurements["advisors_total"] == 3
