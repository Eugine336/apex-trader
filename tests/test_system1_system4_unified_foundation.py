import time
from types import SimpleNamespace

from cognition.analytical_knowledge import (
    ANALYTICAL_KNOWLEDGE,
    assess_analytical_knowledge,
    knowledge_coverage,
)
from cognition.contracts import (
    CampaignSpecification,
    DecisionPackage,
    DecisionType,
    Evidence,
    EvidenceDomain,
    Hypothesis,
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
    stamp = time.time()
    market = MarketState(symbol="EURUSD", created_epoch=stamp)
    market.add(Evidence(
        source_module="test.market_lens",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        observation="completed-bar structure",
        confidence=0.8,
        uncertainty=0.2,
        measurements={"source_kind": "market_lens", "timeframe": "H1", "trend_state": "uptrend"},
        timestamp_epoch=stamp,
        source_timestamp_epoch=stamp,
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
        created_epoch=stamp,
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
    assert {"rsi", "rsi_fast", "rsi_slow", "stoch_rsi", "macd",
            "macd_signal", "macd_histogram", "adx", "plus_di", "minus_di",
            "sma_20", "ema_20", "ema_50", "wma", "hma", "stochastic_k",
            "cci", "williams_r", "roc", "roc_10", "trend_strength"} <= set(momentum)
    volatility = technical["technical_volatility"].measurements
    assert {"atr", "atr_percentile", "parkinson_volatility",
            "bollinger_upper", "bollinger_lower", "bollinger_width",
            "keltner", "keltner_upper", "keltner_lower", "donchian_high",
            "donchian_low", "rolling_zscore", "mean_distance"} <= set(volatility)
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



def test_calibration_producer_requires_complete_selection_aware_chronological_data():
    import json

    from cognition.calibration_artifacts import (
        CalibrationProducer,
        resolve_calibration_artifact,
    )

    class Store:
        def __init__(self, events):
            self.events = events

        def query_events(self, *, event_types, symbol, limit=10000, offset=0, **kwargs):
            rows = [
                row for row in self.events
                if row["event_type"] in event_types and row["symbol"] == symbol
            ]
            rows.sort(key=lambda row: row["seq"], reverse=True)
            return rows[offset:offset + limit]

    events = []
    start = 1_700_000_000.0
    for index in range(150):
        decision_epoch = start + index * 600.0
        decision_id = f"decision-{index}"
        candidate_id = f"{decision_id}:opportunity:opp-{index}"
        candidate = {
            "opportunity_id": f"opp-{index}",
            "candidate_family": "continuation",
            "direction": "LONG",
            "state": "active",
            "entry_price": 100.0,
            "stop_price": 99.0,
            "target_price": 102.0,
            "geometry_evidence_ids": [f"feature-{index}"],
        }
        snapshot = {
            "candidate_id": candidate_id,
            "candidate_kind": "opportunity",
            "candidate_state_at_decision": "active",
            "decision_id": decision_id,
            "decision_epoch": decision_epoch,
            "symbol": "EURUSD",
            "candidate": candidate,
            "reference_quote": {
                "mid": 100.0,
                "bid": 99.99,
                "ask": 100.01,
                "source_timestamp_epoch": decision_epoch,
                "source_timestamp_verified": True,
                "symbol": "EURUSD",
                "evidence_id": f"quote-{index}",
            },
            "point_in_time_evidence": [
                {
                    "evidence_id": f"feature-{index}",
                    "source_timestamp_epoch": decision_epoch,
                    "source_timestamp_verified": True,
                },
                {
                    "evidence_id": f"quote-{index}",
                    "source_timestamp_epoch": decision_epoch,
                    "source_timestamp_verified": True,
                },
            ],
        }
        label = "FAVOURABLE" if index % 2 == 0 else "UNFAVOURABLE"
        return_r = 0.2 if index % 2 == 0 else -0.1
        outcome = {
            "candidate_id": candidate_id,
            "decision_id": decision_id,
            "symbol": "EURUSD",
            "direction": "LONG",
            "candidate_family": "continuation",
            "horizon_seconds": 300,
            "observation_status": "OBSERVED",
            "outcome_label": label,
            "directional_return_r": return_r,
            "observed_price": 100.0 + return_r,
            "observed_at_epoch": decision_epoch + 300.0,
            "outcome_source_timestamp_verified": True,
            "reference_evidence_id": f"quote-{index}",
            "outcome_evidence_id": f"outcome-{index}",
            "outcome_quote": {
                "mid": 100.0 + return_r,
                "bid": 99.99 + return_r,
                "ask": 100.01 + return_r,
                "source_timestamp_epoch": decision_epoch + 300.0,
                "source_timestamp_verified": True,
                "symbol": "EURUSD",
                "evidence_id": f"outcome-{index}",
            },
        }
        disposition = {
            "candidate_id": candidate_id,
            "decision_id": decision_id,
            "symbol": "EURUSD",
            "disposition": (
                "PASSED_PORTFOLIO_REVIEW_PENDING_EXECUTION_ADMISSION"
                if index % 3 == 0 else "REJECTED_BY_BRAIN"
            ),
        }
        for seq, event_type, payload in (
            (index * 3 + 1, "COGNITIVE_CANDIDATE_SNAPSHOT", snapshot),
            (index * 3 + 2, "COGNITIVE_CANDIDATE_HORIZON_OUTCOME", outcome),
            (index * 3 + 3, "COGNITIVE_CANDIDATE_DISPOSITION", disposition),
        ):
            events.append({
                "seq": seq,
                "event_type": event_type,
                "symbol": "EURUSD",
                "severity": "DEBUG",
                "payload_json": json.dumps(payload),
            })

    now = start + 149 * 600.0 + 400.0
    producer = CalibrationProducer(Store(events), refresh_interval_seconds=30.0)
    artifacts = producer.refresh("EURUSD", now=now)
    artifact = next(
        item for item in artifacts
        if item.direction == "LONG" and item.horizon_seconds == 300
    )
    assert artifact.sample_count == 30
    assert artifact.walk_forward_folds == 3
    assert artifact.selection_coverage == 1.0
    assert artifact.expected_return_r == 0.05
    assert artifact.holdout_expected_return_r == 0.05
    assert artifact.brier_score == 0.25
    assert artifact.candidate_family == "continuation"
    assert resolve_calibration_artifact(
        artifact.artifact_id, "EURUSD", "LONG", "continuation", 300, now=now,
    ) is artifact
    assert resolve_calibration_artifact(
        artifact.artifact_id, "EURUSD", "LONG", "mean_reversion", 300, now=now,
    ) is None
    assert resolve_calibration_artifact(
        artifact.artifact_id, "EURUSD", "SHORT", "continuation", 300, now=now,
    ) is None



def test_evidence_contract_distinguishes_ingest_time_from_verified_source_time():
    ingestion_only = Evidence(
        source_module="test.ingestion_only",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        timestamp_epoch=1_900_000_000.0,
    )
    assert ingestion_only.timestamp_epoch == 1_900_000_000.0
    assert ingestion_only.source_timestamp_verified is False
    assert ingestion_only.source_timestamp_epoch is None

    explicit_source = Evidence(
        source_module="test.explicit_source",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        timestamp_epoch=1_900_000_100.0,
        source_timestamp_epoch=1_900_000_000.0,
    )
    assert explicit_source.source_timestamp_verified is True
    assert explicit_source.source_timestamp_epoch == 1_900_000_000.0

    unknown = Evidence(
        source_module="test.unknown_source",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
    )
    assert unknown.timestamp_epoch > 0
    assert unknown.source_timestamp_verified is False
    assert unknown.source_timestamp_epoch is None

    explicitly_unknown = Evidence(
        source_module="test.explicitly_unknown_source",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        timestamp_epoch=1_900_000_000.0,
        source_timestamp_verified=False,
    )
    assert explicitly_unknown.source_timestamp_verified is False
    assert explicitly_unknown.source_timestamp_epoch is None



def test_all_cognitive_contracts_force_the_single_canonical_identity():
    from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION

    evidence = Evidence(
        source_module="test.identity",
        identity_version="forged-version",
        timestamp_epoch=100.0,
    )
    state = MarketState(
        symbol="EURUSD",
        identity_version="forged-version",
        identity="alternate identity",
    )
    hypothesis = Hypothesis(
        statement="test hypothesis",
        identity_version="forged-version",
    )
    opportunity = Opportunity(
        opportunity_id="identity-test",
        identity_version="forged-version",
    )
    decision = DecisionPackage(
        symbol="EURUSD",
        identity_version="forged-version",
        identity="alternate identity",
    )
    campaign = CampaignSpecification(
        symbol="EURUSD",
        identity_version="forged-version",
        identity="alternate identity",
    )

    for contract in (evidence, state, hypothesis, opportunity, decision, campaign):
        assert contract.identity_version == IDENTITY_VERSION
    for contract in (state, decision, campaign):
        assert contract.identity == IDENTITY_STATEMENT
    assert state.to_dict()["identity"] == IDENTITY_STATEMENT
    assert state.to_dict()["identity_version"] == IDENTITY_VERSION



def test_native_directional_hypotheses_are_ledgered_when_decision_stands_down():
    class Store:
        def __init__(self):
            self.events = []

        def emit(self, event_type, severity, **kwargs):
            self.events.append((event_type, kwargs))
            return "event-native-hypothesis"

    store = Store()
    loop = CognitionLoop(
        brain=object(),
        consolidator=EvidenceConsolidator(),
        symbols_provider=lambda: [],
        candidate_event_store=store,
    )
    decision = DecisionPackage(
        symbol="EURUSD",
        decision_type=DecisionType.CONTINUE_OBSERVING,
        thesis="no candidate qualified this cycle",
        questions_answered={
            "native_hypothesis_records": [{
                "hypothesis_id": "hypothesis-mean-reversion",
                "hypothesis_kind": "mean_reversion",
                "direction_implication": "SHORT",
                "statement": "positive statistical displacement may revert",
                "horizon": "M15",
                "confidence_semantics": "uncalibrated_reasoning_strength",
            }],
        },
    )
    loop._record_candidate_snapshot(
        SimpleNamespace(decision=decision),
        MarketState(symbol="EURUSD"),
    )
    loop._record_candidate_disposition(
        SimpleNamespace(decision=decision, campaign=None),
        stage="brain",
    )

    snapshots = [
        payload for event_type, kwargs in store.events
        if event_type == "COGNITIVE_CANDIDATE_SNAPSHOT"
        for payload in [kwargs["payload"]]
    ]
    dispositions = [
        kwargs["payload"] for event_type, kwargs in store.events
        if event_type == "COGNITIVE_CANDIDATE_DISPOSITION"
    ]
    assert len(snapshots) == 1
    assert snapshots[0]["candidate_kind"] == "hypothesis"
    assert snapshots[0]["candidate_id"].endswith(":hypothesis:hypothesis-mean-reversion")
    assert snapshots[0]["candidate"]["direction_implication"] == "SHORT"
    assert snapshots[0]["predictive_validity"] == "NOT_ESTABLISHED"
    assert len(dispositions) == 1
    assert dispositions[0]["candidate_id"] == snapshots[0]["candidate_id"]
