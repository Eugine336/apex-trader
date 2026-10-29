"""System 2 decision-time feature snapshot tests.

These tests verify provenance capture only; they do not claim predictive ability.
"""
from types import SimpleNamespace

from cognition.brain import CognitiveBrain
from cognition.contracts import Evidence, EvidenceDomain, MarketState


def test_decision_feature_snapshot_preserves_source_timestamps_and_ids():
    state = MarketState(symbol="XAUUSD")
    evidence = Evidence(
        source_module="test.market",
        domain=EvidenceDomain.STRUCTURE,
        symbol="XAUUSD",
        observation="synthetic structural observation",
        confidence=0.8,
        uncertainty=0.2,
        measurements={"range": 12.5, "nested": {"confirmed": True}},
    )
    state.add(evidence)
    decision = SimpleNamespace(decision_id="decision-test-1", symbol="XAUUSD")

    snapshot = CognitiveBrain._decision_feature_snapshot(state, decision)

    assert snapshot["schema"] == "apex.decision_feature_snapshot.v1"
    assert snapshot["captured_at_decision"] is True
    assert snapshot["snapshot_complete"] is True
    assert snapshot["decision_id"] == "decision-test-1"
    assert snapshot["evidence_ids"] == [evidence.evidence_id]
    feature = snapshot["features"][0]
    assert feature["source_timestamp_epoch"] == evidence.timestamp_epoch
    assert feature["measurements"]["range"] == 12.5
    assert feature["measurements"]["nested"]["confirmed"] is True
    assert snapshot["predictive_ability_demonstrated"] is False
    assert snapshot["validation_status"] == "unvalidated"


def test_decision_feature_snapshot_does_not_backfill_stale_evidence():
    state = MarketState(symbol="XAUUSD")
    evidence = Evidence(
        source_module="test.market",
        domain=EvidenceDomain.STRUCTURE,
        symbol="XAUUSD",
        observation="stale synthetic observation",
        confidence=0.8,
        uncertainty=0.2,
        measurements={"range": 12.5},
        timestamp_epoch=1.0,
    )
    state.add(evidence)
    decision = SimpleNamespace(decision_id="decision-test-stale", symbol="XAUUSD")

    snapshot = CognitiveBrain._decision_feature_snapshot(state, decision)

    assert snapshot["features"] == []
    assert snapshot["evidence_ids"] == []
    assert snapshot["snapshot_complete"] is False
    assert snapshot["predictive_ability_demonstrated"] is False


def test_decision_feature_snapshot_rejects_future_dated_evidence():
    state = MarketState(symbol="XAUUSD")
    evidence = Evidence(
        source_module="test.market",
        domain=EvidenceDomain.STRUCTURE,
        symbol="XAUUSD",
        observation="future-dated synthetic observation",
        confidence=0.8,
        uncertainty=0.2,
        measurements={"range": 12.5},
        timestamp_epoch=9_999_999_999.0,
    )
    state.add(evidence)
    decision = SimpleNamespace(decision_id="decision-test-future", symbol="XAUUSD")

    snapshot = CognitiveBrain._decision_feature_snapshot(state, decision)

    assert snapshot["features"] == []
    assert snapshot["snapshot_complete"] is False
