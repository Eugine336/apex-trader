"""Concept-reader failures remain visible without becoming market signals."""

from cognition import concepts


def test_failed_reader_emits_non_directional_runtime_diagnostic_and_continues(monkeypatch):
    calls = []

    class BrokenReader:
        key = "broken_reader"

        def read(self, symbol, bars_by_tf):
            raise ArithmeticError("internal details must not enter evidence")

    class HealthyReader:
        key = "healthy_reader"

        def read(self, symbol, bars_by_tf):
            calls.append((symbol, bars_by_tf))
            return []

    monkeypatch.setattr(
        concepts,
        "CONCEPT_MODULES",
        (BrokenReader(), HealthyReader()),
    )

    evidence = concepts.run_concepts("EURUSD", {"H1": []})

    assert calls == [("EURUSD", {"H1": []})]
    assert len(evidence) == 1
    diagnostic = evidence[0]
    assert diagnostic.source_module == "concept_runtime.broken_reader"
    assert diagnostic.domain.value == "other"
    assert diagnostic.symbol == "EURUSD"
    assert diagnostic.confidence == 0.0
    assert diagnostic.uncertainty == 1.0
    assert diagnostic.polarity == 0.0
    assert diagnostic.source_timestamp_verified is False
    assert diagnostic.source_timestamp_epoch is None
    assert diagnostic.provisional is True
    assert diagnostic.measurements == {
        "source_kind": "concept_runtime_diagnostic",
        "concept_module": "broken_reader",
        "status": "reader_failed",
        "exception_type": "ArithmeticError",
        "reading_unavailable": True,
        "non_market_evidence": True,
    }
    assert "internal details" not in diagnostic.observation



def test_brain_surfaces_reader_failure_as_unknown_not_negative_market_evidence():
    from cognition.brain_reasoning import BrainReasoner
    from cognition.contracts import Evidence, EvidenceDomain, MarketState

    state = MarketState(symbol="EURUSD")
    diagnostic = Evidence(
        source_module="concept_runtime.broken_reader",
        domain=EvidenceDomain.OTHER,
        symbol="EURUSD",
        observation="A registered concept reader failed; its market interpretation is unavailable.",
        confidence=0.0,
        uncertainty=1.0,
        polarity=0.0,
        measurements={
            "source_kind": "concept_runtime_diagnostic",
            "concept_module": "broken_reader",
            "status": "reader_failed",
            "exception_type": "ArithmeticError",
            "reading_unavailable": True,
            "non_market_evidence": True,
        },
        relevance_horizon_seconds=300.0,
        data_basis="runtime_health_diagnostic",
        provisional=True,
        independence_group="concept_runtime_health:broken_reader",
    )
    state.add(diagnostic)

    analysis = BrainReasoner().analyze_evidence(state, state.consolidation())

    assert any(
        "Concept analysis unavailable: broken_reader reader failed (ArithmeticError)" == item
        for item in analysis.missing_information
    )
    assert any(
        "registered concept reader(s) failed" in item
        for item in analysis.observations
    )
    assert analysis.market_context["concept_runtime_failures"] == [{
        "module": "broken_reader",
        "status": "reader_failed",
        "exception_type": "ArithmeticError",
        "evidence_id": diagnostic.evidence_id,
    }]
    assert "other" not in analysis.informative_domains
