"""Native Brain reasoning — the Brain as an INDEPENDENT reasoner.

Constitution Articles II / IV / IX / X. Executable assertions that the Brain:

* reads the evidence into its OWN structured analysis and forms its OWN
  hypotheses (with a counter-hypothesis) WITHOUT any LLM (Article IX);
* detects each defined structural pattern deterministically (Article X);
* INTEGRATES advisor opinions with its own hypotheses — agreement boosts,
  disagreement attenuates (Article II — advisors inform, they are not the
  reasoning);
* can INDEPENDENTLY ORIGINATE an opportunity when the advisor is unavailable but
  the native evidence is strong (Article X);
* SELF-CRITICISES its hypotheses, lowering confidence on contradicted ones
  (Article IV);
* records the FULL native reasoning chain in ``questions_answered``.

Pure standard library.
"""

import pytest

from cognition.brain import CognitiveBrain
from cognition.brain_reasoning import (
    LONG,
    SHORT,
    BrainHypothesis,
    BrainReasoner,
    EvidenceAnalysis,
)
from cognition.contracts import DecisionType, Evidence, MarketState
from cognition.pattern_rules import detect_patterns, pattern_name


# ── builders ─────────────────────────────────────────────────────────────────

class _Opinion:
    def __init__(self, direction, confidence, rationale="because"):
        self.direction = direction
        self.confidence = confidence
        self.rationale = rationale
        self.competing_hypotheses = []
        self.missing_information = []
        # V-002 — a directional advisor opinion carries a first-class, ACTIVATED
        # opportunity so origination flows through a structured object (these
        # tests exercise native/advisor integration, not the legacy-scalar path).
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
        return self._opinion


def _ev(domain, measurements, *, conf=0.9, module=None):
    return Evidence(
        source_module=module or f"m_{domain}", domain=domain, confidence=conf,
        uncertainty=1.0 - conf, measurements=dict(measurements),
        relevance_horizon_seconds=900.0,
    )


def _strong_long_state(symbol="EURUSD"):
    """A rich, low-uncertainty state whose evidence structurally leans LONG."""
    ms = MarketState(symbol=symbol)
    ms.add(_ev("structure", {"displacement": 150, "bos": True,
                             "structural_integrity": 0.85, "support": True}))
    ms.add(_ev("volume", {"volume_z": 1.8, "expansion": True}))
    ms.add(_ev("order_flow", {"delta": 0.8, "imbalance": 0.8}))
    ms.add(_ev("momentum", {"acceleration": True, "momentum_state": "accelerating"}))
    ms.add(_ev("multi_timeframe", {"alignment": "aligned", "conflict_score": 0.1}))
    return ms


# ── Article IX: the Brain forms its OWN hypotheses without any LLM ────────────

def test_brain_generates_hypotheses_without_llm():
    brain = CognitiveBrain(reasoner=None)
    ms = _strong_long_state()
    analysis = brain._analyze_evidence(ms, ms.consolidation())
    hyps = brain._generate_hypotheses(analysis, "EURUSD")
    assert len(hyps) >= 2, "must form a primary reading AND a counter-hypothesis"
    lead = brain._native.leading(hyps)
    assert lead is not None and lead.direction_implication == LONG
    # Article IV — a counter-hypothesis is always present.
    assert any("counter" in h.opportunity_implication.lower() for h in hyps)
    # It reasoned purely from evidence — no reasoner was involved.
    assert brain.available is False


def test_analysis_detects_patterns_and_regime():
    brain = CognitiveBrain(reasoner=None)
    ms = _strong_long_state()
    analysis = brain._analyze_evidence(ms, ms.consolidation())
    names = [pattern_name(p) for p in analysis.detected_patterns]
    assert "displacement_confirmed" in names
    # V-001 — the regime carries INDEPENDENT structured dimensions (a signed
    # displacement, an order-flow reading), NOT a pre-collapsed directional lean.
    assert analysis.regime_indicators.get("structure") == LONG
    assert analysis.regime_indicators.get("order_flow") == "buying"
    assert analysis.regime_indicators.get("displacement", 0.0) > 0.0
    assert "directional_lean" not in analysis.regime_indicators
    assert analysis.evidence_uncertainty < 0.5


def test_hypothesis_direction_emerges_from_structured_evidence_short():
    # V-001 — no pre-collapsed lean: a structurally-SHORT picture (negative
    # displacement + selling order flow) yields a SHORT leading hypothesis purely
    # by combining the independent structured dimensions during hypothesis
    # formation, not by reading a directional-lean scalar.
    ms = MarketState(symbol="EURUSD")
    ms.add(_ev("structure", {"displacement": -150, "bos": True,
                             "structural_integrity": 0.85}))
    ms.add(_ev("volume", {"volume_z": 1.8, "expansion": True}))
    ms.add(_ev("order_flow", {"delta": -0.8, "imbalance": 0.8}))
    ms.add(_ev("momentum", {"acceleration": True, "momentum_state": "accelerating"}))
    brain = CognitiveBrain(reasoner=None)
    analysis = brain._analyze_evidence(ms, ms.consolidation())
    assert analysis.regime_indicators.get("structure") == SHORT
    assert analysis.regime_indicators.get("order_flow") == "selling"
    assert "directional_lean" not in analysis.regime_indicators
    lead = brain._native.leading(brain._generate_hypotheses(analysis, "EURUSD"))
    assert lead is not None and lead.direction_implication == SHORT


# ── Article X: every defined pattern rule fires on crafted evidence ───────────

_PATTERN_CASES = [
    ("liquidity_sweep_absorbed", [("liquidity", {"sweep": True, "absorption": True})]),
    ("liquidity_sweep_with_momentum_exhaustion",
     [("liquidity", {"sweep": True}), ("momentum", {"deceleration": True})]),
    ("displacement_confirmed",
     [("structure", {"displacement": 100}), ("volume", {"volume_z": 1.5})]),
    ("momentum_divergence", [("momentum", {"divergence": True})]),
    ("multi_tf_aligned", [("multi_timeframe", {"alignment": "aligned"})]),
    ("multi_tf_conflict", [("multi_timeframe", {"conflict_score": 0.7})]),
    ("volatility_compression", [("volatility", {"compression": True})]),
    ("volume_confirmed_breakout",
     [("volume", {"expansion": True}), ("volatility", {"expansion": True})]),
    ("order_flow_at_structure",
     [("order_flow", {"imbalance": 0.7}), ("structure", {"support": True})]),
    ("fvg_liquidity_confluence",
     [("order_flow", {"fvg": True}), ("liquidity", {"pool": True})]),
    ("structural_break", [("structure", {"bos": True})]),
    ("momentum_acceleration", [("momentum", {"acceleration": True})]),
    ("execution_degraded", [("execution_quality", {"spread_widening": True})]),
    ("portfolio_concentrated", [("portfolio", {"concentration": 0.7})]),
    ("order_flow_imbalance", [("order_flow", {"delta": 0.8})]),
]


@pytest.mark.parametrize("expected,items", _PATTERN_CASES,
                         ids=[c[0] for c in _PATTERN_CASES])
def test_each_pattern_rule_detects(expected, items):
    ms = MarketState(symbol="EURUSD")
    for domain, meas in items:
        ms.add(_ev(domain, meas))
    names = [pattern_name(p) for p in detect_patterns(ms.fresh_evidence())]
    assert expected in names


# ── Article II: agreement boosts, disagreement attenuates ─────────────────────

def test_brain_advisor_agreement_boosts_confidence():
    # Brain reads LONG from the evidence; advisor also says LONG (modest 0.6).
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion(LONG, 0.6)),
                           reward_r_default=2.0, min_expected_value=0.0)
    out = brain.reason(_strong_long_state())
    synth = out.decision.questions_answered.get("brain_advisor_synthesis", "")
    assert "agreement" in synth
    # Agreement lifts the effective conviction above the advisor's own 0.6.
    assert out.decision.confidence > 0.6
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN


def test_brain_advisor_disagreement_attenuates_confidence():
    # Brain reads LONG from the evidence; advisor says SHORT with high conviction.
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion(SHORT, 0.8)),
                           reward_r_default=2.0, min_expected_value=0.0)
    out = brain.reason(_strong_long_state())
    synth = out.decision.questions_answered.get("brain_advisor_synthesis", "")
    assert "disagreement" in synth
    # Disagreement pulls the effective conviction below the advisor's own 0.8.
    assert out.decision.confidence < 0.8


def test_flat_brain_defers_without_changing_confidence():
    # Measurement-thin evidence ⇒ the Brain forms no directional view and defers
    # to the advisor with NO confidence change (zero regression path).
    ms = MarketState(symbol="EURUSD")
    ms.add(_ev("momentum", {}, conf=0.9))
    ms.add(_ev("structure", {}, conf=0.9))
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion(LONG, 0.8)), reward_r_default=2.0)
    out = brain.reason(ms)
    synth = out.decision.questions_answered.get("brain_advisor_synthesis", "")
    assert "defer" in synth
    assert out.decision.confidence == pytest.approx(0.8)
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN


# ── Article X: independent origination when the advisor is unavailable ────────

def test_brain_originates_independently_when_advisor_unavailable():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion(LONG, 0.9), available=False),
                           reward_r_default=2.0)
    out = brain.reason(_strong_long_state())
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.direction == LONG
    assert out.campaign is not None
    assert out.campaign.initial_execution_intent.get("independent_origination") is True
    note = out.decision.questions_answered.get("brain_independent_origination", "")
    assert "without advisor" in note
    assert brain.get_status()["independent_originations"] == 1


def test_weak_native_view_stands_down_when_advisor_unavailable():
    # Thin, non-directional evidence ⇒ no strong native hypothesis ⇒ the Brain
    # stands down (REASONER_UNAVAILABLE), it does NOT invent an opportunity.
    ms = MarketState(symbol="EURUSD")
    ms.add(_ev("momentum", {}, conf=0.5))
    ms.add(_ev("structure", {}, conf=0.5))
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion(LONG, 0.9), available=False))
    out = brain.reason(ms)
    assert out.decision.decision_type == DecisionType.REASONER_UNAVAILABLE
    assert out.campaign is None


# ── Article IV: self-criticism lowers confidence on contradicted hypotheses ───

def test_self_criticism_lowers_confidence_on_contradiction():
    br = BrainReasoner()
    analysis = EvidenceAnalysis(
        domain_coverage={"structure": 0.9, "volume": 0.9, "momentum": 0.9},
        detected_patterns=[
            "displacement_confirmed: structural displacement backed by volume",
            "momentum_divergence: price advancing but momentum weakening",
        ],
        regime_indicators={"structure": LONG, "order_flow": "buying"},
        agreement_score=0.4, evidence_uncertainty=0.2,
    )
    hyps = br.generate_hypotheses(analysis, "EURUSD")
    lead = br.leading(hyps)
    before = lead.confidence
    br.self_criticize(hyps, analysis)
    assert lead.confidence < before
    assert "contradict" in lead.criticism


def test_self_criticism_reinforces_clean_hypothesis():
    br = BrainReasoner()
    analysis = EvidenceAnalysis(
        domain_coverage={"structure": 0.9, "volume": 0.9},
        detected_patterns=[
            "displacement_confirmed: structural displacement backed by volume"],
        regime_indicators={"structure": LONG, "order_flow": "buying"},
        agreement_score=0.85, evidence_uncertainty=0.15,
    )
    hyps = br.generate_hypotheses(analysis, "EURUSD")
    lead = br.leading(hyps)
    before = lead.confidence
    br.self_criticize(hyps, analysis)
    assert lead.confidence >= before
    assert "survived criticism" in lead.criticism


# ── the FULL native reasoning chain is recorded ───────────────────────────────

def test_full_reasoning_chain_recorded():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion(LONG, 0.6)),
                           reward_r_default=2.0, min_expected_value=0.0)
    q = brain.reason(_strong_long_state()).decision.questions_answered
    assert q.get("brain_evidence_analysis")
    assert q.get("brain_hypotheses")
    assert q.get("brain_advisor_synthesis")
    assert q.get("brain_self_criticism")


# ── Article XXVIII: book-wide opportunity comparison ──────────────────────────

def test_compare_opportunities_ranks_against_book():
    br = BrainReasoner()
    opp = BrainHypothesis(statement="long", confidence=0.8, direction_implication=LONG)
    assert br.compare_opportunities(opp, "EURUSD", []) == "superior"
    # Same symbol + same direction already held ⇒ redundant.
    held = [{"symbol": "EURUSD", "direction": "LONG", "confidence": 0.7}]
    assert br.compare_opportunities(opp, "EURUSD", held) == "redundant"
    # A stronger new opportunity beats a weaker existing position ⇒ superior.
    weak = [{"symbol": "USDJPY", "direction": "LONG", "confidence": 0.4}]
    assert br.compare_opportunities(opp, "EURUSD", weak) == "superior"
