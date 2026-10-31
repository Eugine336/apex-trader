from types import SimpleNamespace

from cognition.analytical_knowledge import (
    ANALYTICAL_KNOWLEDGE,
    assess_analytical_knowledge,
    knowledge_coverage,
)
from cognition.contracts import (
    DecisionPackage,
    DecisionType,
    Evidence,
    EvidenceDomain,
    MarketState,
    Opportunity,
    OpportunityState,
)
from cognition.loop import CognitionLoop, EvidenceConsolidator


def test_analytical_knowledge_catalogue_is_broad_unique_and_non_authoritative():
    keys = [item.key for item in ANALYTICAL_KNOWLEDGE]
    assert len(keys) >= 60
    assert len(keys) == len(set(keys))
    assert all(item.domains and item.measurements and item.limitations for item in ANALYTICAL_KNOWLEDGE)


def test_knowledge_assessment_requires_actual_measurements_and_domain():
    evidence = Evidence(
        source_module="test.rsi",
        domain=EvidenceDomain.MOMENTUM,
        symbol="EURUSD",
        observation="RSI measured",
        confidence=0.8,
        uncertainty=0.2,
        measurements={"rsi": 64.0},
    )
    assessed = assess_analytical_knowledge([evidence])
    by_key = {item["key"]: item for item in assessed}
    assert by_key["rsi"]["status"] == "relevant"
    assert evidence.evidence_id in by_key["rsi"]["evidence_ids"]
    assert by_key["rsi"]["authority"] == "non_authoritative_analysis_only"
    assert by_key["volume_profile"]["status"] in {"watch", "not_assessable"}
    summary = knowledge_coverage(assessed)
    assert summary["catalogue_size"] == len(ANALYTICAL_KNOWLEDGE)
    assert summary["semantics"] == "coverage_only_not_signal_not_probability_not_order_authority"


def test_source_timestamp_controls_freshness_not_ingestion_time():
    now = 2_000_000_000.0
    evidence = Evidence(
        source_module="test.cached_feed",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        confidence=1.0,
        uncertainty=0.0,
        timestamp_epoch=now,
        source_timestamp_epoch=now - 3600,
        relevance_horizon_seconds=60,
        data_basis="completed_ohlc",
        measurement_version="test-v1",
    )
    assert evidence.is_fresh(now) is False
    payload = evidence.to_dict()
    assert payload["source_timestamp_epoch"] == now - 3600
    assert payload["data_basis"] == "completed_ohlc"
    assert payload["measurement_version"] == "test-v1"


def test_candidate_snapshot_durably_records_opportunity_and_point_in_time_evidence():
    class Store:
        def __init__(self):
            self.events = []

        def emit(self, event_type, severity, **kwargs):
            self.events.append((event_type, severity, kwargs))
            return "event-1"

    store = Store()
    loop = CognitionLoop(
        brain=object(),
        consolidator=EvidenceConsolidator(),
        symbols_provider=lambda: ["EURUSD"],
        candidate_event_store=store,
    )
    market = MarketState(symbol="EURUSD")
    market.add(Evidence(
        source_module="test.market_lens",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        observation="completed-bar structure",
        confidence=0.8,
        uncertainty=0.2,
        measurements={"source_kind": "market_lens", "timeframe": "H1", "trend_state": "uptrend"},
        source_timestamp_epoch=1_900_000_000.0,
        data_basis="completed_ohlc",
        independence_group="price_structure:H1",
    ))
    opportunity = Opportunity(
        opportunity_id="opp-1",
        direction="LONG",
        state=OpportunityState.FORMING,
        thesis="conditional continuation",
        entry_price=1.1,
        stop_price=1.09,
        target_price=1.13,
        geometry_evidence_ids=["evidence-1"],
    )
    decision = DecisionPackage(
        symbol="EURUSD",
        decision_type=DecisionType.OPPORTUNITY_FORMING,
        thesis="monitor conditional continuation",
        opportunities=[opportunity],
        preferred_opportunity_id="opp-1",
    )
    loop._record_candidate_snapshot(SimpleNamespace(decision=decision), market)

    assert len(store.events) == 1
    event_type, severity, kwargs = store.events[0]
    assert event_type == "COGNITIVE_CANDIDATE_SNAPSHOT"
    assert severity == "DEBUG"
    assert kwargs["durable"] is True
    assert kwargs["parent_id"] == decision.decision_id
    payload = kwargs["payload"]
    assert payload["candidate_id"] == f"{decision.decision_id}:opportunity:opp-1"
    assert payload["candidate_state_at_decision"] == "forming"
    assert payload["predictive_label_status"] == "UNLABELLED"
    assert payload["predictive_validity"] == "NOT_ESTABLISHED"
    assert payload["point_in_time_evidence"][0]["data_basis"] == "completed_ohlc"


def test_no_opportunity_decision_is_also_recorded_in_candidate_universe():
    class Store:
        def __init__(self):
            self.events = []

        def emit(self, event_type, severity, **kwargs):
            self.events.append(kwargs)
            return "event-2"

    store = Store()
    loop = CognitionLoop(
        brain=object(),
        consolidator=EvidenceConsolidator(),
        symbols_provider=lambda: [],
        candidate_event_store=store,
    )
    market = MarketState(symbol="XAUUSD")
    decision = DecisionPackage(
        symbol="XAUUSD",
        decision_type=DecisionType.NO_OPPORTUNITY,
        thesis="no defensible opportunity",
        do_nothing_considered=True,
    )
    loop._record_candidate_snapshot(SimpleNamespace(decision=decision), market)
    assert len(store.events) == 1
    payload = store.events[0]["payload"]
    assert payload["candidate_kind"] == "decision_outcome"
    assert payload["decision_type"] == DecisionType.NO_OPPORTUNITY.value
    assert payload["predictive_validity"] == "NOT_ESTABLISHED"
