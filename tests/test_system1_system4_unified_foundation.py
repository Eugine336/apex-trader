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



def test_fixed_horizon_outcomes_label_unselected_candidates_without_claiming_calibration():
    import json

    class Store:
        def __init__(self):
            self.events = []

        def emit(self, event_type, severity, **kwargs):
            self.events.append({
                "event_type": event_type,
                "severity": severity,
                "payload_json": json.dumps(kwargs["payload"]),
                **kwargs,
            })
            return f"event-{len(self.events)}"

        def query(self, *, event_type, symbol, limit=200):
            return [
                row for row in self.events
                if row["event_type"] == event_type
                and row.get("symbol") == symbol
            ][:limit]

    store = Store()
    loop = CognitionLoop(
        brain=object(),
        consolidator=EvidenceConsolidator(),
        symbols_provider=lambda: [],
        candidate_event_store=store,
    )
    initial_market = MarketState(symbol="EURUSD", created_epoch=1000.0)
    initial_market.add(Evidence(
        source_module="market.price_action",
        domain=EvidenceDomain.MULTI_TIMEFRAME,
        symbol="EURUSD",
        timestamp_epoch=1000.0,
        source_timestamp_epoch=1000.0,
        confidence=0.9,
        uncertainty=0.1,
        measurements={"price": {"bid": 1.099, "ask": 1.101, "mid": 1.1}},
    ))
    opportunity = Opportunity(
        opportunity_id="opp-horizon",
        direction="LONG",
        state=OpportunityState.FORMING,
        thesis="forming candidate",
        entry_price=1.1,
        stop_price=1.09,
        target_price=1.12,
        risk_distance=0.01,
    )
    decision = DecisionPackage(
        symbol="EURUSD",
        decision_type=DecisionType.OPPORTUNITY_FORMING,
        created_epoch=1000.0,
        opportunities=[opportunity],
    )
    output = SimpleNamespace(decision=decision)
    loop._record_candidate_snapshot(output, initial_market)

    later_market = MarketState(symbol="EURUSD", created_epoch=1301.0)
    later_market.add(Evidence(
        source_module="market.price_action",
        domain=EvidenceDomain.MULTI_TIMEFRAME,
        symbol="EURUSD",
        timestamp_epoch=1301.0,
        source_timestamp_epoch=1301.0,
        confidence=0.9,
        uncertainty=0.1,
        measurements={"price": {"bid": 1.109, "ask": 1.111, "mid": 1.11}},
    ))
    loop._label_due_candidate_horizons(later_market)

    labelled = [
        row for row in store.events
        if row["event_type"] == "COGNITIVE_CANDIDATE_HORIZON_OUTCOME"
    ]
    assert len(labelled) == 1
    payload = json.loads(labelled[0]["payload_json"])
    assert payload["horizon_seconds"] == 300
    assert payload["observation_status"] == "OBSERVED"
    assert payload["outcome_label"] == "FAVOURABLE"
    assert payload["predictive_validity"] == "DESCRIPTIVE_ONLY_UNVALIDATED"
    assert payload["calibration_authority"] is False
    assert payload["directional_return_r"] > 0



def test_market_lenses_emit_causal_indicator_families_with_volume_provenance():
    from cognition.market_lenses import analyze_market_frames

    bars = []
    for index in range(240):
        close = 1.0 + index * 0.001 + (0.0004 if index % 3 == 0 else 0.0)
        bars.append({
            "timestamp_epoch": 1_700_000_000.0 + index * 300,
            "open": close - 0.0002,
            "high": close + 0.0008,
            "low": close - 0.0007,
            "close": close,
            "volume": 1000.0 + index * 2,
            "volume_source": "tick_volume_proxy",
        })

    evidence = analyze_market_frames(
        "EURUSD", {"M5": bars}, source_includes_forming_bar=False,
    )
    technical = {
        item.source_module.rsplit(".", 1)[-1]: item
        for item in evidence
        if item.source_module.startswith("market_lenses.m5.technical_")
    }
    assert {"technical_momentum", "technical_volatility", "technical_volume"} <= set(technical)

    momentum = technical["technical_momentum"].measurements
    assert {"rsi", "macd", "macd_signal", "macd_histogram", "adx",
            "plus_di", "minus_di", "sma_20", "ema_20", "ema_50",
            "stochastic_k", "cci", "williams_r", "roc_10"} <= set(momentum)
    volatility = technical["technical_volatility"].measurements
    assert {"atr", "atr_percentile", "bollinger_upper", "bollinger_lower",
            "bollinger_width", "keltner_upper", "keltner_lower",
            "donchian_high", "donchian_low", "rolling_zscore"} <= set(volatility)
    volume = technical["technical_volume"].measurements
    assert {"obv", "mfi", "cmf", "vwap", "volume_source",
            "volume_is_tick_proxy"} <= set(volume)
    assert volume["volume_is_tick_proxy"] is True
    assert technical["technical_momentum"].independence_group == "ohlc:EURUSD:M5"
    assert technical["technical_momentum"].data_basis == "completed_ohlc"


def test_market_lenses_do_not_invent_volume_indicators_without_volume():
    from cognition.market_lenses import analyze_market_frames

    bars = []
    for index in range(80):
        close = 10.0 + index * 0.01
        bars.append({
            "timestamp_epoch": 1_700_000_000.0 + index * 60,
            "open": close - 0.005,
            "high": close + 0.01,
            "low": close - 0.01,
            "close": close,
        })
    evidence = analyze_market_frames(
        "SYNTHETIC", {"M1": bars}, source_includes_forming_bar=False,
    )
    assert not any(
        item.source_module.endswith(".technical_volume")
        for item in evidence
    )
