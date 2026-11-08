"""Tests that contextual observations replace, rather than duplicate, generic gates."""
from cognition.concepts.base import ConceptReading, Implication, FLAT
from cognition.concepts.external_data_gated_readings import ExternalDataGatedReadings
from cognition.contracts import EvidenceDomain


def _reading(concept):
    return ConceptReading(
        concept=concept, timeframe="M5", domain=EvidenceDomain.CORRELATION,
        applicable=True, applicability_reason="test", observation="measured",
        implications=(Implication(
            direction=FLAT, statement="context", strength=None, activated=False,
            activation_reason="measurement only",
        ),),
        facts={"measured": True}, source_bar_epoch=1_800_000_000,
        source_timestamp_epoch=1_800_000_000, source_timestamp_verified=True,
    )


def test_external_gate_does_not_contradict_available_contextual_reading():
    module = ExternalDataGatedReadings()
    readings = module.read_with_context(
        "EURUSD",
        {"M5": [{
            "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0,
            "timestamp_epoch": 1_800_000_000,
            "source_timestamp_verified": True,
        }]},
        {"concept_readings": (_reading("cross_asset"), _reading("relative_value"))},
    )
    concepts = {item.concept for item in readings}
    assert "cross_asset" not in concepts
    assert "relative_value" not in concepts
    assert "macro_rates" in concepts
