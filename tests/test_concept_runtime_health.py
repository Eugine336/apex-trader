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
