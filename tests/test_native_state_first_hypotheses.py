"""State-first hypothesis generation contracts for the native Brain."""

from cognition.brain_reasoning import BrainReasoner, EvidenceAnalysis, FLAT, LONG, SHORT


def test_competing_hypotheses_exist_without_named_patterns():
    analysis = EvidenceAnalysis(
        domain_coverage={
            "structure": 0.8,
            "liquidity": 0.65,
            "volatility": 0.7,
            "order_flow": 0.75,
        },
        coverage_gap=1.0 - 4.0 / 6.0,
        detected_patterns=[],
        agreement_score=0.7,
        regime_indicators={
            "structure": LONG,
            "order_flow": "buying",
            "trend_context": "uptrend",
            "trend_timeframes": 3,
            "range_timeframes": 0,
        },
        missing_information=["completed-bar breakout confirmation"],
        evidence_uncertainty=0.25,
    )

    hypotheses = BrainReasoner().generate_hypotheses(analysis, "SYNTHETIC")

    descriptions = [
        f"{hypothesis.statement} {hypothesis.opportunity_implication}".lower()
        for hypothesis in hypotheses
    ]
    combined = "\n".join(descriptions)

    for kind in (
        "continuation",
        "pullback",
        "reversal",
        "range rotation",
        "failed breakout",
        "no-trade",
    ):
        assert kind in combined, f"Missing state-first competitor: {kind}"

    assert len(hypotheses) >= 6
    assert any(h.direction_implication == LONG for h in hypotheses)
    assert any(h.direction_implication == SHORT for h in hypotheses)
    assert any(h.direction_implication == FLAT for h in hypotheses)


def test_unresolved_market_state_does_not_force_a_direction():
    analysis = EvidenceAnalysis(
        domain_coverage={"structure": 0.2, "volatility": 0.1},
        coverage_gap=2.0 / 3.0,
        detected_patterns=[],
        agreement_score=0.1,
        regime_indicators={},
        missing_information=["structure", "liquidity", "execution costs"],
        evidence_uncertainty=0.9,
    )

    hypotheses = BrainReasoner().generate_hypotheses(analysis, "SYNTHETIC")

    assert len(hypotheses) >= 6
    assert any(h.direction_implication == FLAT for h in hypotheses)
    # Without a directional state or observed directional pattern, a directional
    # conclusion must not be manufactured just to ensure a non-empty hypothesis.
    assert all(
        h.direction_implication not in (LONG, SHORT)
        for h in hypotheses
        if not h.supporting_patterns
    )
