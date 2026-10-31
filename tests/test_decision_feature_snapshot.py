"""System 2 decision-time feature snapshot tests.

These tests verify provenance capture only; they do not claim predictive ability.
"""
import time
from types import SimpleNamespace

from cognition.brain import CognitiveBrain
from cognition.contracts import CampaignSpecification, Evidence, EvidenceDomain, MarketState
from cognition.campaign_translator import translate
from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION


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
        timestamp_epoch=time.time(),
        source_timestamp_epoch=time.time(),
    )
    state.add(evidence)
    decision = SimpleNamespace(decision_id="decision-test-1", symbol="XAUUSD", created_epoch=time.time())

    snapshot = CognitiveBrain._decision_feature_snapshot(state, decision)

    assert snapshot["schema"] == "apex.decision_feature_snapshot.v1"
    assert snapshot["captured_at_decision"] is True
    assert snapshot["snapshot_complete"] is True
    assert snapshot["decision_id"] == "decision-test-1"
    assert snapshot["evidence_ids"] == [evidence.evidence_id]
    feature = snapshot["features"][0]
    assert feature["source_timestamp_epoch"] == evidence.source_timestamp_epoch
    assert feature["source_timestamp_verified"] is True
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
    decision = SimpleNamespace(decision_id="decision-test-stale", symbol="XAUUSD", created_epoch=time.time())

    snapshot = CognitiveBrain._decision_feature_snapshot(state, decision)

    assert snapshot["features"] == []
    assert snapshot["evidence_ids"] == []
    assert snapshot["snapshot_complete"] is False
    assert snapshot["predictive_ability_demonstrated"] is False



def test_campaign_translation_carries_the_exact_decision_snapshot():
    snapshot = {
        "schema": "apex.decision_feature_snapshot.v1",
        "snapshot_id": "decision-test-translation:123.0",
        "decision_id": "decision-test-translation",
        "symbol": "XAUUSD",
        "decision_timestamp_epoch": 123.0,
        "captured_at_decision": True,
        "snapshot_complete": True,
        "evidence_count": 1,
        "evidence_ids": ["evidence-test"],
        "features": [{"evidence_id": "evidence-test", "source_timestamp_epoch": 122.0}],
        "validation_status": "unvalidated",
        "predictive_ability_demonstrated": False,
    }
    geometry = {
        "identity_version": IDENTITY_VERSION,
        "identity": IDENTITY_STATEMENT,
        "activated": True,
        "symbol": "XAUUSD",
        "direction": "LONG",
        "evidence_ids": ["evidence-test"],
        "entry_price": 100.0,
        "stop_price": 99.0,
        "target_price": 102.0,
        "risk_distance": 1.0,
        "reward_distance": 2.0,
        "reward_r": 2.0,
    }
    spec = CampaignSpecification(
        symbol="XAUUSD",
        direction="LONG",
        desired_exposure=0.2,
        initial_execution_intent={
            "entry_price": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "payoff_geometry": geometry,
            "decision_feature_snapshot": snapshot,
        },
        opportunity_id="opportunity-test",
        opportunity_state="ACTIVE",
        decision_id="decision-test-translation",
    )

    translated = translate(spec)

    assert translated is not None
    assert translated.meta["decision_feature_snapshot"] == snapshot

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
    decision = SimpleNamespace(decision_id="decision-test-future", symbol="XAUUSD", created_epoch=time.time())

    snapshot = CognitiveBrain._decision_feature_snapshot(state, decision)

    assert snapshot["features"] == []
    assert snapshot["snapshot_complete"] is False

def test_snapshot_uses_precise_decision_creation_epoch_as_cutoff():
    state = MarketState(symbol="XAUUSD")
    decision_epoch = time.time()
    evidence = Evidence(
        source_module="test.market",
        domain=EvidenceDomain.STRUCTURE,
        symbol="XAUUSD",
        observation="post-decision synthetic observation",
        confidence=0.8,
        uncertainty=0.2,
        measurements={"range": 12.5},
        timestamp_epoch=decision_epoch + 1.0,
    )
    state.add(evidence)
    decision = SimpleNamespace(
        decision_id="decision-test-cutoff",
        symbol="XAUUSD",
        created_epoch=decision_epoch,
    )

    snapshot = CognitiveBrain._decision_feature_snapshot(state, decision)

    assert snapshot["decision_timestamp_epoch"] == decision_epoch
    assert snapshot["captured_at_decision"] is True
    assert snapshot["features"] == []
    assert snapshot["snapshot_complete"] is False


def test_snapshot_without_decision_creation_epoch_fails_closed():
    state = MarketState(symbol="XAUUSD")
    evidence = Evidence(
        source_module="test.market",
        domain=EvidenceDomain.STRUCTURE,
        symbol="XAUUSD",
        observation="synthetic observation",
        confidence=0.8,
        uncertainty=0.2,
        measurements={"range": 12.5},
    )
    state.add(evidence)
    decision = SimpleNamespace(decision_id="decision-test-no-time", symbol="XAUUSD")

    snapshot = CognitiveBrain._decision_feature_snapshot(state, decision)

    assert snapshot["decision_timestamp_epoch"] == 0.0
    assert snapshot["captured_at_decision"] is False
    assert snapshot["snapshot_complete"] is False
    assert snapshot["features"] == []

