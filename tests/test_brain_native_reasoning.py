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
        source_module=module or f"m_{domain}",
        domain=domain,
        symbol="EURUSD",
        observation=f"{domain} observer measurement: {dict(measurements)}",
        confidence=conf,
        uncertainty=1.0 - conf,
        measurements=dict(measurements),
        relevance_horizon_seconds=900.0,
    )



def _add_market_geometry(ms, *, target_r=2.0, direction="LONG"):
    """Add fresh, completed market-lens geometry for entry-admission tests."""
    import time

    entry = 1.1000
    risk = 0.0050
    side = str(direction).upper()
    if side == "LONG":
        high = entry + risk * float(target_r)
        low = entry - risk
        trend = "uptrend"
        breakout = "up"
        change = 0.0010
    else:
        high = entry + risk
        low = entry - risk * float(target_r)
        trend = "downtrend"
        breakout = "down"
        change = -0.0010
    stamp = time.time()
    ms.add(Evidence(
        source_module="market_lenses.m5.structure",
        domain="structure", symbol=ms.symbol,
        observation="fresh completed-bar structural range for test geometry",
        confidence=0.9, uncertainty=0.1,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "timeframe": "M5",
            "current_close": entry,
            "prior_range_high": high,
            "prior_range_low": low,
            "range_position": 0.5,
            "breakout_direction": breakout,
            "trend_state": trend,
            "trend_strength": 0.4,
            "upper_wick": 0.0,
            "lower_wick": 0.0,
        },
        timestamp_epoch=stamp,
        relevance_horizon_seconds=900.0,
    ))
    ms.add(Evidence(
        source_module="market_lenses.m5.momentum",
        domain="momentum", symbol=ms.symbol,
        observation="fresh completed-bar momentum for test geometry",
        confidence=0.9, uncertainty=0.1,
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "timeframe": "M5",
            "close_change": change,
        },
        timestamp_epoch=stamp,
        relevance_horizon_seconds=900.0,
    ))
    return ms


def _with_verified_council(ms, *, responded=2, available=2, total=2):
    ms.add(Evidence(
        source_module="execution_cost.test_quote", domain="execution_quality",
        symbol=ms.symbol, observation="fresh test cost estimate",
        confidence=0.9, uncertainty=0.1,
        measurements={
            "estimated_total_cost_r": 0.05,
            "cost_verified": True,
            "cost_source": "simulated live quote for test",
        },
        relevance_horizon_seconds=60.0,
    ))
    ms.add(Evidence(
        source_module="reasoning_engine.test_council",
        domain="reasoning",
        symbol=ms.symbol,
        observation="test council coverage is explicitly known",
        confidence=0.9,
        uncertainty=0.1,
        measurements={
            "advisors_responded": responded,
            "advisors_available": available,
            "advisors_total": total,
        },
        relevance_horizon_seconds=600.0,
    ))
    return ms


def _strong_long_state(symbol="EURUSD"):
    """A rich, low-uncertainty state whose evidence structurally leans LONG."""
    ms = MarketState(symbol=symbol)
    ms.add(_ev("structure", {"displacement": 150, "bos": True,
                             "structural_integrity": 0.85, "support": True},
                   module="structure.observer"))
    ms.add(_ev("volume", {"volume_z": 1.8, "expansion": True},
                   module="volume.observer"))
    ms.add(_ev("order_flow", {"delta": 0.8, "imbalance": 0.8},
                   module="order_flow.observer"))
    ms.add(_ev("momentum", {"acceleration": True, "momentum_state": "accelerating"},
                   module="momentum.observer"))
    ms.add(_ev("multi_timeframe", {"alignment": "aligned", "conflict_score": 0.1},
                   module="multi_timeframe.observer"))
    _add_market_geometry(ms, target_r=2.0, direction="LONG")
    ms.add(_ev("execution_quality", {
        "estimated_total_cost_r": 0.05,
        "cost_verified": True,
        "cost_source": "simulated live quote for test",
    }, module="execution_cost.observer"))
    return _with_verified_council(ms)


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


def test_statistical_displacement_becomes_a_competing_native_mean_reversion_hypothesis():
    ms = MarketState(symbol="XAUUSD")
    ms.add(_ev("multi_timeframe", {"clarity": 0.9}, conf=0.9))
    ms.add(_ev("momentum", {
        "rolling_zscore_30": 2.4,
        "ema_spread_atr": 0.8,
        "last_close": 2085.0,
    }, conf=0.9, module="gold_market_desk.pattern.statistical_displacement"))
    brain = CognitiveBrain(reasoner=None)
    analysis = brain._analyze_evidence(ms, ms.consolidation())
    names = [pattern_name(p) for p in analysis.detected_patterns]
    assert "statistical_displacement" in names

    hypotheses = brain._generate_hypotheses(analysis, "XAUUSD")
    mean_reversion = next(
        h for h in hypotheses if "statistical_displacement" in h.supporting_patterns
    )
    assert mean_reversion.direction_implication == SHORT
    assert "rolling mean" in mean_reversion.statement
    # The detector's z-score is context; only the Brain creates the hypothesis.
    assert mean_reversion.is_directional



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


def test_regime_derived_multitimeframe_trend_hypothesis_survives_empty_pattern_list():
    # A quiet named-pattern detector is not proof that no directional market
    # state exists when multiple completed timeframes independently agree.
    analysis = EvidenceAnalysis(
        domain_coverage={"structure": 0.85, "multi_timeframe": 0.8},
        evidence_uncertainty=0.25,
        regime_indicators={
            "trend_context": "uptrend",
            "trend_timeframes": 3,
            "momentum": "decelerating",
            "structure": LONG,
            "order_flow": "balanced",
        },
        detected_patterns=[],
    )
    hypotheses = BrainReasoner().generate_hypotheses(analysis, "EURUSD")
    trend = next(h for h in hypotheses if "trend-context hypothesis" in h.opportunity_implication)
    assert trend.direction_implication == LONG
    assert "pullback" in trend.statement
    assert any("counter" in h.opportunity_implication.lower() for h in hypotheses)


def test_multitimeframe_range_state_is_hypothesized_without_named_range_pattern():
    analysis = EvidenceAnalysis(
        domain_coverage={"structure": 0.8, "volatility": 0.75},
        evidence_uncertainty=0.3,
        regime_indicators={
            "trend_context": "range",
            "range_timeframes": 2,
            "trend_timeframes": 0,
            "volatility": "compressing",
        },
        detected_patterns=[],
    )
    hypotheses = BrainReasoner().generate_hypotheses(analysis, "EURUSD")
    range_hypothesis = next(h for h in hypotheses if h.direction_implication == "FLAT")
    assert "competing possibilities" in range_hypothesis.statement
    assert "do not trade" not in range_hypothesis.opportunity_implication.lower()


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
    ms.add(_ev("liquidity", {}, conf=0.9))
    _with_verified_council(ms)
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion(LONG, 0.8)), reward_r_default=2.0)
    out = brain.reason(ms)
    synth = out.decision.questions_answered.get("brain_advisor_synthesis", "")
    assert "defer" in synth
    assert out.decision.confidence == pytest.approx(0.8)
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None




def test_native_fallback_qualifies_sub_070_confidence_without_named_pattern():
    """Native reasoning discovers from context/evidence, not pattern-name gates."""
    ms = _strong_long_state()
    brain = CognitiveBrain(reasoner=None, reward_r_default=3.0, min_expected_value=0.0)
    # This scenario is intended to exercise a directional hypothesis whose
    # confidence is below the former fixed 0.70 cutoff and whose pattern names
    # are not a qualification allowlist.
    analysis = EvidenceAnalysis(
        domain_coverage={
            "structure": 0.85, "volume": 0.8, "order_flow": 0.82,
            "momentum": 0.78, "multi_timeframe": 0.8,
        },
        coverage_gap=0.2,
        observations=["completed timeframe structure and order flow align"],
        detected_patterns=[],
        agreement_score=0.7,
        contradictions=[],
        regime_indicators={
            "structure": LONG, "order_flow": "buying",
            "trend_context": "uptrend", "trend_timeframes": 3,
            "momentum": "decelerating",
        },
        evidence_uncertainty=0.25,
    )
    hypothesis = BrainHypothesis(
        statement="Completed higher-timeframe structure supports continuation with a pullback risk",
        confidence=0.60,
        direction_implication=LONG,
        supporting_patterns=[],
        contradicting_patterns=[],
        invalidation="A completed structural close breaks below the continuation origin",
        opportunity_implication="continuation after pullback",
    )
    brain._native_reasoning = lambda state, consolidation, symbol: (analysis, [hypothesis])
    out = brain.reason(ms)
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.direction == LONG
    assert out.decision.confidence < 0.70
    assert out.campaign is not None
    assert out.campaign.initial_execution_intent.get("independent_origination") is True


def test_native_fallback_requires_explicit_verified_execution_cost():
    state = _strong_long_state()
    state.evidence = [
        item for item in state.evidence
        if getattr(item.domain, "value", item.domain) != "execution_quality"
    ]
    # An unverified numeric cost cannot satisfy the execution-cost requirement.
    state.add(_ev("execution_quality", {"estimated_total_cost_r": 0.01}))
    out = CognitiveBrain(reasoner=None, reward_r_default=3.0).reason(state)
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None

# ── Article X: independent origination when the advisor is unavailable ────────


def test_native_campaign_uses_the_observed_entry_stop_and_target_geometry():
    brain = CognitiveBrain(reasoner=None, reward_r_default=99.0)
    out = brain.reason(_strong_long_state())

    assert out.campaign is not None
    intent = out.campaign.initial_execution_intent
    geometry = intent["payoff_geometry"]
    assert intent["entry_price"] == geometry["entry_price"]
    assert intent["sl"] == geometry["stop_price"]
    assert intent["tp"] == geometry["target_price"]
    assert intent["sl"] < intent["entry_price"] < intent["tp"]
    assert intent["geometry_evidence_ids"] == geometry["evidence_ids"]
    # A huge configured fallback is intentionally irrelevant when real geometry
    # exists; the target is based on observed structure, not reward_r_default.
    assert out.decision.preferred_opportunity.reward_r == geometry["reward_r"]
    assert out.decision.questions_answered["payoff_geometry_status"] == (
        "qualified_from_fresh_completed_market_evidence"
    )


def test_brain_originates_natively_when_no_external_reasoner_is_configured():
    # No AI provider object exists at all. A strong, low-uncertainty structured
    # market read must still be reasoned over by the native Brain.
    brain = CognitiveBrain(reasoner=None, reward_r_default=2.0)
    out = brain.reason(_strong_long_state())
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.campaign is not None
    assert out.campaign.direction == LONG
    assert out.campaign.initial_execution_intent.get("independent_origination") is True
    preferred = out.decision.preferred_opportunity
    assert preferred is not None and preferred.is_actionable
    assert preferred.is_preferred
    assert out.decision.actionable_opportunities == [preferred]
    assert out.campaign.opportunity_id == preferred.opportunity_id
    assert out.campaign.opportunity_state == preferred.state.value
    assert out.decision.supporting_evidence_ids
    assert "no external advisor configured" in out.decision.questions_answered["reasoner_state"]
    assert out.decision.questions_answered["reasoning_mode"] == "native_only"
    assert out.decision.questions_answered["external_advisor_status"] == "not_configured"
    assert brain.get_status()["independent_originations"] == 1



def test_native_fallback_does_not_activate_from_simple_directional_context_alone():
    ms = MarketState(symbol="EURUSD")
    ms.add(_ev("structure", {"displacement": 150, "bos": True}, conf=0.95))
    ms.add(_ev("volume", {"volume_z": 0.2}, conf=0.95))
    ms.add(_ev("order_flow", {"delta": 0.8}, conf=0.95))
    ms.add(_ev("momentum", {"acceleration": True, "momentum_state": "accelerating"}, conf=0.95))
    ms.add(_ev("multi_timeframe", {"alignment": "aligned"}, conf=0.95))
    ms.add(_ev("execution_quality", {"estimated_total_cost_r": 0.05}, conf=0.95))
    out = CognitiveBrain(reasoner=None, reward_r_default=2.0).reason(ms)
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None


def test_native_fallback_stands_down_without_execution_cost_evidence():
    state = _strong_long_state()
    state.evidence = [
        item for item in state.evidence
        if getattr(item.domain, "value", item.domain) != "execution_quality"
    ]
    brain = CognitiveBrain(reasoner=None, reward_r_default=2.0)
    out = brain.reason(state)
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None
    assert out.provider_status == "not_configured"
    assert out.native_verdict == DecisionType.CONTINUE_OBSERVING.value


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
    assert out.decision.questions_answered["reasoning_mode"] == "native_only"
    assert out.decision.questions_answered["external_advisor_status"] == "unavailable"
    assert brain.get_status()["independent_originations"] == 1


def test_native_reasoning_precedes_advisor_availability_check():
    class _OrderCheckingReasoner:
        def __init__(self):
            self.assert_runtime_order = False
            self.native_started = False

        @property
        def available(self):
            if self.assert_runtime_order:
                assert self.native_started, (
                    "native reasoning must start before advisor availability is checked"
                )
            return False

        def reason(self, symbol, evidence, now=None):
            raise AssertionError("unavailable advisor must not be called")

    reasoner = _OrderCheckingReasoner()
    brain = CognitiveBrain(reasoner=reasoner)
    original_native_reasoning = brain._native_reasoning

    def _record_native_start(market_state, consolidation, symbol):
        reasoner.native_started = True
        return original_native_reasoning(market_state, consolidation, symbol)

    brain._native_reasoning = _record_native_start
    reasoner.assert_runtime_order = True
    ms = MarketState(symbol="EURUSD")
    ms.add(_ev("momentum", {}, conf=0.5))
    ms.add(_ev("structure", {}, conf=0.5))

    out = brain.reason(ms)
    assert reasoner.native_started is True
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.provider_status == "unavailable"
    assert out.native_verdict == DecisionType.CONTINUE_OBSERVING.value


def test_qualified_native_campaign_survives_healthy_advisor_no_opinion():
    # An advisor that is healthy but throttled/no-opinion must not become a
    # hidden prerequisite. Native qualification still gets the opportunity to act.
    brain = CognitiveBrain(reasoner=_Reasoner(None), reward_r_default=2.0)
    out = brain.reason(_strong_long_state())
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.campaign is not None
    assert out.campaign.initial_execution_intent.get("independent_origination") is True
    assert out.provider_status == "available"
    assert out.native_verdict == DecisionType.OPEN_CAMPAIGN.value


def test_weak_native_view_stands_down_when_advisor_unavailable():
    # Thin, non-directional evidence ⇒ no strong native hypothesis ⇒ the Brain
    # preserves its native observe verdict and records provider outage separately.
    ms = MarketState(symbol="EURUSD")
    ms.add(_ev("momentum", {}, conf=0.5))
    ms.add(_ev("structure", {}, conf=0.5))
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion(LONG, 0.9), available=False))
    out = brain.reason(ms)
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None
    assert out.direction == FLAT
    assert out.provider_status == "unavailable"
    assert out.native_verdict == DecisionType.CONTINUE_OBSERVING.value
    questions = out.decision.questions_answered
    assert questions["reasoning_mode"] == "native_fallback"
    assert questions["external_advisor_status"] == "unavailable"
    assert "no qualified" in out.decision.thesis
    assert questions["native_analysis_summary"]
    assert questions["native_fallback_status"]
    assert questions["native_hypotheses"]


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
