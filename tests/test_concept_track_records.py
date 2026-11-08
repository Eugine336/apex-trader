from __future__ import annotations

import json

from cognition.concept_track_records import ConceptTrackRecordProducer
from cognition.contracts import Evidence, EvidenceDomain, MarketState
from cognition.loop import CognitionLoop


def _snapshot():
    return {
        "candidate_id": "decision-1:opportunity:opp-1",
        "candidate_kind": "opportunity",
        "candidate_state_at_decision": "active",
        "decision_id": "decision-1",
        "symbol": "EURUSD",
        "decision_epoch": 1000.0,
        "point_in_time_complete": True,
        "selection_population": "ALL_RECORDED_BRAIN_CONSIDERED_CANDIDATES",
        "candidate": {"direction": "LONG", "market_state": "trend_up"},
        "evidence_inventory": [{
            "evidence_id": "concept-evidence-1",
            "symbol": "EURUSD",
            "source_timestamp_epoch": 990.0,
            "source_timestamp_verified": True,
            "provisional": False,
            "measurements": {
                "source_kind": "concept_reading",
                "concept": "crt",
                "applicable": True,
                "implications": [{
                    "direction": "LONG",
                    "activated": True,
                }],
            },
        }],
    }


def _outcome():
    return {
        "candidate_id": "decision-1:opportunity:opp-1",
        "decision_id": "decision-1",
        "symbol": "EURUSD",
        "direction": "LONG",
        "horizon_seconds": 300,
        "observed_at_epoch": 1300.0,
        "outcome_source_timestamp_verified": True,
        "observation_status": "OBSERVED",
        "outcome_label": "FAVOURABLE",
        "directional_return_r": 0.5,
        "outcome_quote": {
            "symbol": "EURUSD",
            "source_timestamp_epoch": 1300.0,
            "source_timestamp_verified": True,
        },
    }


def _event(payload, seq=1):
    return {"payload_json": json.dumps(payload), "seq": seq}


class _Store:
    def __init__(self, snapshots=None, outcomes=None, dispositions=None):
        self.events = {
            "COGNITIVE_CANDIDATE_SNAPSHOT": list(snapshots or []),
            "COGNITIVE_CANDIDATE_HORIZON_OUTCOME": list(outcomes or []),
            "COGNITIVE_CANDIDATE_DISPOSITION": list(dispositions or []),
        }

    def query_events(self, *, event_types, **kwargs):
        return self.events[event_types[0]]


def _producer():
    store = _Store(
        snapshots=[_event(_snapshot())],
        outcomes=[_event(_outcome())],
        dispositions=[_event({
            "candidate_id": "decision-1:opportunity:opp-1",
            "decision_id": "decision-1",
            "symbol": "EURUSD",
            "disposition": "PASSED_PORTFOLIO_REVIEW_PENDING_EXECUTION_ADMISSION",
        }, seq=10)],
    )
    return ConceptTrackRecordProducer(store)


def test_concept_track_records_are_producer_owned_and_descriptive_only():
    (evidence,) = _producer().evidence_for("EURUSD", now=1400.0)

    assert isinstance(evidence, Evidence)
    assert evidence.domain is EvidenceDomain.HISTORICAL_ANALOGUE
    assert evidence.polarity == 0.0
    assert evidence.source_timestamp_verified is True
    assert evidence.source_timestamp_epoch == 1300.0
    assert evidence.measurements["source_kind"] == "producer_concept_track_records"
    assert evidence.measurements["concept_track_records"]["crt"]["sample_count"] == 1
    assert evidence.measurements["concept_outcome_history"][0]["outcome"] == 1
    assert evidence.measurements["predictive_probability"] is False
    assert evidence.measurements["trade_win_rate"] is False
    assert evidence.measurements["entry_authorization_emitted"] is False


def test_concept_track_records_reject_unverified_or_future_quote_outcomes():
    outcome = _outcome()
    outcome["outcome_quote"]["source_timestamp_verified"] = False
    producer = ConceptTrackRecordProducer(_Store(
        snapshots=[_event(_snapshot())],
        outcomes=[_event(outcome)],
        dispositions=[_event({
            "candidate_id": "decision-1:opportunity:opp-1",
            "decision_id": "decision-1",
            "symbol": "EURUSD",
            "disposition": "OBSERVE",
        }, seq=10)],
    ))
    assert producer.evidence_for("EURUSD", now=1400.0) == []


def test_concept_track_records_reject_duplicate_candidate_horizon_outcomes():
    producer = _producer()
    producer._store.events["COGNITIVE_CANDIDATE_HORIZON_OUTCOME"].append(
        _event(_outcome(), seq=2)
    )
    assert producer.evidence_for("EURUSD", now=1400.0) == []


def test_cognition_loop_injects_concept_track_records_as_neutral_context():
    class _Provider:
        def evidence_for(self, symbol, *, now=None):
            return [Evidence(
                source_module="cognition.concept_track_record_producer",
                domain=EvidenceDomain.HISTORICAL_ANALOGUE,
                symbol=symbol,
                observation="historical concept outcomes",
                confidence=0.0,
                uncertainty=1.0,
                polarity=0.0,
                measurements={"source_kind": "producer_concept_track_records"},
            )]

    loop = CognitionLoop(
        brain=object(),
        consolidator=object(),
        symbols_provider=lambda: [],
        calibration_provider=object(),
        concept_track_record_provider=_Provider(),
    )
    state = MarketState(symbol="EURUSD")
    loop._inject_calibration_evidence(state)
    assert any(
        item.source_module == "cognition.concept_track_record_producer"
        and item.polarity == 0.0
        for item in state.evidence
    )
