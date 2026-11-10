import json
import time

import pytest
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
from cognition.brain import CognitiveBrain
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
        source_timestamp_verified=True,
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
        source_timestamp_verified=True,
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
        geometry_evidence_ids=[market.evidence[0].evidence_id],
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


def test_candidate_snapshot_rejects_cross_symbol_future_and_provisional_quote_sources():
    class Store:
        def __init__(self):
            self.events = []

        def emit(self, event_type, severity, **kwargs):
            self.events.append((event_type, kwargs))
            return "event-source-time"

    store = Store()
    loop = CognitionLoop(
        brain=object(),
        consolidator=EvidenceConsolidator(),
        symbols_provider=lambda: ["EURUSD"],
        candidate_event_store=store,
    )
    stamp = time.time()
    market = MarketState(symbol="EURUSD", created_epoch=stamp)

    feature = Evidence(
        source_module="test.completed_structure",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        timestamp_epoch=stamp - 1.0,
        source_timestamp_epoch=stamp - 1.0,
        source_timestamp_verified=True,
        measurements={"timeframe": "H1", "trend_state": "uptrend"},
        relevance_horizon_seconds=60.0,
    )
    market.add(feature)

    def quote_evidence(symbol, evidence_id, source_epoch, *, verified=True, provisional=False, mid=1.1):
        measurements = {
            "price": {
                "bid": mid - 0.0001,
                "ask": mid + 0.0001,
                "mid": mid,
                "timestamp_epoch": source_epoch,
                "source_timestamp_verified": verified,
                "quote_kind": "bid_ask",
                "symbol": symbol,
                "raw_fields": {
                    "source_adapter": "mt5",
                    "source_timezone": "broker_server",
                    "time": int(source_epoch),
                    "time_msc": int(source_epoch * 1000),
                    "timestamp_utc_epoch": source_epoch,
                    "source_utc_offset_seconds": 0.0,
                    "source_utc_offset_uncertainty_seconds": 0.1,
                    "source_clock_sources": ["ntp:source-a", "ntp:source-b"],
                },
            },
        }
        return Evidence(
            source_module="market.live_quote",
            domain=EvidenceDomain.MULTI_TIMEFRAME,
            symbol=symbol,
            evidence_id=evidence_id,
            timestamp_epoch=stamp,
            source_timestamp_epoch=source_epoch if verified else None,
            source_timestamp_verified=verified,
            relevance_horizon_seconds=60.0,
            provisional=provisional,
            measurements=measurements,
        )

    # Newer but foreign, provisional, future and unverified quotes must not
    # displace the valid EURUSD quote.
    market.add(quote_evidence("GBPUSD", "foreign-quote", stamp - 0.05, mid=9.0))
    market.add(quote_evidence("EURUSD", "provisional-quote", stamp - 0.02,
                              provisional=True, mid=8.0))
    market.add(quote_evidence("EURUSD", "future-quote", stamp + 5.0, mid=7.0))
    market.add(quote_evidence("EURUSD", "unverified-quote", stamp - 0.01,
                              verified=False, mid=6.0))
    valid_quote = quote_evidence("EURUSD", "valid-quote", stamp - 0.5, mid=1.1)
    market.add(valid_quote)

    opportunity = Opportunity(
        opportunity_id="source-time-opportunity",
        direction="LONG",
        state=OpportunityState.FORMING,
        thesis="candidate with attributable geometry",
        entry_price=1.1,
        stop_price=1.09,
        target_price=1.13,
        geometry_evidence_ids=[feature.evidence_id],
    )
    decision = DecisionPackage(
        symbol="EURUSD",
        decision_type=DecisionType.OPPORTUNITY_FORMING,
        opportunities=[opportunity],
        created_epoch=stamp,
    )
    loop._record_candidate_snapshot(SimpleNamespace(decision=decision), market)

    payload = store.events[0][1]["payload"]
    assert payload["reference_quote"]["evidence_id"] == "valid-quote"
    assert payload["reference_quote"]["symbol"] == "EURUSD"
    assert payload["reference_quote"]["raw_fields"]["source_adapter"] == "mt5"
    assert payload["point_in_time_complete"] is True
    point_ids = {row["evidence_id"] for row in payload["point_in_time_evidence"]}
    assert point_ids == {feature.evidence_id, "valid-quote"}


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
        source_module="market.live_quote",
        domain=EvidenceDomain.MULTI_TIMEFRAME,
        symbol="EURUSD",
        timestamp_epoch=1000.0,
        source_timestamp_epoch=1000.0,
        source_timestamp_verified=True,
        confidence=0.9,
        uncertainty=0.1,
        measurements={"price": {
            "bid": 1.099, "ask": 1.101, "mid": 1.1,
            "timestamp_epoch": 1000.0, "source_timestamp_verified": True,
            "quote_kind": "bid_ask",
            "symbol": "EURUSD",
            "raw_fields": {
                "source_adapter": "mt5",
                "source_timezone": "broker_server",
                "time": 1000,
                "time_msc": 1_000_000,
                "timestamp_utc_epoch": 1000.0,
                "source_utc_offset_seconds": 0.0,
                "source_utc_offset_uncertainty_seconds": 0.1,
                "source_clock_sources": ["ntp:source-a", "ntp:source-b"],
            },
        }},
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
        source_module="market.live_quote",
        domain=EvidenceDomain.MULTI_TIMEFRAME,
        symbol="EURUSD",
        timestamp_epoch=1301.0,
        source_timestamp_epoch=1301.0,
        source_timestamp_verified=True,
        confidence=0.9,
        uncertainty=0.1,
        measurements={"price": {
            "bid": 1.109, "ask": 1.111, "mid": 1.11,
            "timestamp_epoch": 1301.0, "source_timestamp_verified": True,
            "quote_kind": "bid_ask",
            "symbol": "EURUSD",
            "raw_fields": {
                "source_adapter": "mt5",
                "source_timezone": "broker_server",
                "time": 1301,
                "time_msc": 1_301_000,
                "timestamp_utc_epoch": 1301.0,
                "source_utc_offset_seconds": 0.0,
                "source_utc_offset_uncertainty_seconds": 0.1,
                "source_clock_sources": ["ntp:source-a", "ntp:source-b"],
            },
        }},
    ))
    # These newer-looking records are not valid EURUSD, point-in-time quotes.
    later_market.add(Evidence(
        source_module="market.live_quote",
        domain=EvidenceDomain.MULTI_TIMEFRAME,
        symbol="GBPUSD",
        timestamp_epoch=1301.0,
        source_timestamp_epoch=1301.0,
        source_timestamp_verified=True,
        measurements={"price": {
            "bid": 8.999, "ask": 9.001, "mid": 9.0,
            "timestamp_epoch": 1301.0, "source_timestamp_verified": True,
            "quote_kind": "bid_ask",
            "symbol": "GBPUSD",
        }},
    ))
    later_market.add(Evidence(
        source_module="market.live_quote",
        domain=EvidenceDomain.MULTI_TIMEFRAME,
        symbol="EURUSD",
        timestamp_epoch=1301.0,
        source_timestamp_epoch=1301.0,
        source_timestamp_verified=True,
        provisional=True,
        measurements={"price": {
            "bid": 7.999, "ask": 8.001, "mid": 8.0,
            "timestamp_epoch": 1301.0, "source_timestamp_verified": True,
            "symbol": "EURUSD",
        }},
    ))
    later_market.add(Evidence(
        source_module="market.live_quote",
        domain=EvidenceDomain.MULTI_TIMEFRAME,
        symbol="EURUSD",
        timestamp_epoch=1301.0,
        source_timestamp_verified=False,
        measurements={"price": {
            "bid": 5.999, "ask": 6.001, "mid": 6.0,
            "timestamp_epoch": 1301.0, "source_timestamp_verified": False,
            "quote_kind": "bid_ask",
            "symbol": "EURUSD",
        }},
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
    assert payload["outcome_evidence_id"] == later_market.evidence[0].evidence_id
    assert payload["outcome_quote"]["symbol"] == "EURUSD"
    assert payload["outcome_quote"]["raw_fields"]["source_adapter"] == "mt5"
    assert payload["observed_price"] == pytest.approx(1.11)



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
            return [
                {**row, "_event_seq": row["seq"]}
                for row in rows[offset:offset + limit]
            ]

    events = []
    start = 1_700_000_000.0

    def mt5_quote_provenance(epoch):
        return {
            "source_adapter": "mt5",
            "source_timezone": "broker_server",
            "time": int(epoch),
            "time_msc": int(epoch * 1000),
            "provider_timestamp_epoch": epoch,
            "provider_timestamp_semantics": "broker_server_epoch",
            "source_utc_offset_seconds": 0.0,
            "timestamp_utc_epoch": epoch,
            "source_utc_offset_uncertainty_seconds": 0.1,
            "source_clock_sources": ["ntp:source-a", "ntp:source-b"],
        }

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
                "quote_kind": "bid_ask",
                "mid": 100.0,
                "bid": 99.99,
                "ask": 100.01,
                "source_timestamp_epoch": decision_epoch,
                "timestamp_epoch": decision_epoch,
                "source_timestamp_verified": True,
                "symbol": "EURUSD",
                "raw_fields": mt5_quote_provenance(decision_epoch),
                "evidence_id": f"quote-{index}",
            },
            "point_in_time_complete": True,
            "point_in_time_evidence": [
                {
                    "evidence_id": f"feature-{index}",
                    "symbol": "EURUSD",
                    "source_timestamp_epoch": decision_epoch - 3600.0,
                    "source_timestamp_verified": True,
                    "relevance_horizon_seconds": 3600.0,
                    "provisional": False,
                    "measurements": {
                        "source_kind": "market_lens",
                        "source_timestamp_known": True,
                        "source_timestamp_origin": "broker_ohlcv",
                        "source_broker": "fixture-broker",
                        "source_bar_epoch": decision_epoch - 3600.0,
                        "timeframe": "H1",
                        "data_basis": "completed_ohlc",
                        "provisional": False,
                    },
                },
                {
                    "evidence_id": f"quote-{index}",
                    "symbol": "EURUSD",
                    "source_module": "market.live_quote",
                    "source_timestamp_epoch": decision_epoch,
                    "source_timestamp_verified": True,
                    "relevance_horizon_seconds": 90.0,
                    "provisional": False,
                    "measurements": {
                        "price": {
                            "symbol": "EURUSD",
                            "quote_kind": "bid_ask",
                            "bid": 99.99,
                            "ask": 100.01,
                            "mid": 100.0,
                            "timestamp_epoch": decision_epoch,
                            "source_timestamp_verified": True,
                            "raw_fields": mt5_quote_provenance(decision_epoch),
                        },
                    },
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
                "quote_kind": "bid_ask",
                "mid": 100.0 + return_r,
                "bid": 99.99 + return_r,
                "ask": 100.01 + return_r,
                "source_timestamp_epoch": decision_epoch + 300.0,
                "timestamp_epoch": decision_epoch + 300.0,
                "source_timestamp_verified": True,
                "symbol": "EURUSD",
                "source_module": "market.live_quote",
                "raw_fields": mt5_quote_provenance(decision_epoch + 300.0),
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

        # A second family on the same decision has a deliberately different
        # return distribution. It must produce its own artifact, not be pooled
        # into the continuation cohort.
        mean_id = f"{decision_id}:opportunity:mean-{index}"
        mean_snapshot = json.loads(json.dumps(snapshot))
        mean_snapshot["candidate_id"] = mean_id
        mean_snapshot["candidate_ref"] = f"mean-{index}"
        mean_snapshot["candidate"]["opportunity_id"] = f"mean-{index}"
        mean_snapshot["candidate"]["candidate_family"] = "mean_reversion"
        mean_snapshot["candidate"]["geometry_evidence_ids"] = [f"mean-feature-{index}"]
        mean_snapshot["reference_quote"]["evidence_id"] = f"mean-quote-{index}"
        mean_snapshot["point_in_time_evidence"][0]["evidence_id"] = f"mean-feature-{index}"
        mean_snapshot["point_in_time_evidence"][1]["evidence_id"] = f"mean-quote-{index}"
        mean_outcome = json.loads(json.dumps(outcome))
        mean_return_r = 0.4 if index % 2 == 0 else -0.2
        mean_outcome.update({
            "candidate_id": mean_id,
            "candidate_family": "mean_reversion",
            "outcome_label": "FAVOURABLE" if mean_return_r > 0 else "UNFAVOURABLE",
            "directional_return_r": mean_return_r,
            "observed_price": 100.0 + mean_return_r,
            "reference_evidence_id": f"mean-quote-{index}",
            "outcome_evidence_id": f"mean-outcome-{index}",
        })
        mean_outcome["outcome_quote"].update({
            "mid": 100.0 + mean_return_r,
            "bid": 99.99 + mean_return_r,
            "ask": 100.01 + mean_return_r,
            "evidence_id": f"mean-outcome-{index}",
        })
        mean_disposition = {
            **disposition,
            "candidate_id": mean_id,
        }
        for seq, event_type, payload in (
            (10000 + index * 3 + 1, "COGNITIVE_CANDIDATE_SNAPSHOT", mean_snapshot),
            (10000 + index * 3 + 2, "COGNITIVE_CANDIDATE_HORIZON_OUTCOME", mean_outcome),
            (10000 + index * 3 + 3, "COGNITIVE_CANDIDATE_DISPOSITION", mean_disposition),
        ):
            events.append({
                "seq": seq,
                "event_type": event_type,
                "symbol": "EURUSD",
                "severity": "DEBUG",
                "payload_json": json.dumps(payload),
            })

    now = start + 149 * 600.0 + 300.0
    producer = CalibrationProducer(Store(events), refresh_interval_seconds=30.0)
    artifacts = producer.refresh("EURUSD", now=now)
    artifact = next(
        item for item in artifacts
        if item.direction == "LONG" and item.horizon_seconds == 300
        and item.candidate_family == "continuation"
    )
    mean_reversion_artifact = next(
        item for item in artifacts
        if item.direction == "LONG" and item.horizon_seconds == 300
        and item.candidate_family == "mean_reversion"
    )
    assert artifact.sample_count == 30
    assert artifact.walk_forward_folds == 3
    assert artifact.selection_coverage == 1.0
    assert artifact.expected_return_r == 0.05
    assert artifact.holdout_expected_return_r == 0.05
    assert artifact.brier_score == 0.25
    metrics = artifact.to_measurements()
    assert metrics["probability_calibration_verified"] is False
    assert metrics["brier_score_semantics"] == (
        "development_base_rate_forecast_scored_on_untouched_holdout"
    )
    assert metrics["development_holdout_prevalence_gap"] == artifact.reliability_gap
    assert metrics["current_regime_edge_demonstrated"] is False
    assert metrics["predictive_ability_demonstrated"] is False
    assert "not_a_calibrated_probability" in metrics["probability_semantics"]
    assert artifact.candidate_family == "continuation"
    assert mean_reversion_artifact.sample_count == 30
    assert mean_reversion_artifact.expected_return_r == 0.1
    assert mean_reversion_artifact.holdout_expected_return_r == 0.1
    assert mean_reversion_artifact.candidate_family == "mean_reversion"
    assert resolve_calibration_artifact(
        artifact.artifact_id, "EURUSD", "LONG", "continuation", 300, now=now,
    ) is artifact
    assert resolve_calibration_artifact(
        artifact.artifact_id, "EURUSD", "LONG", "mean_reversion", 300, now=now,
    ) is None
    assert resolve_calibration_artifact(
        artifact.artifact_id, "EURUSD", "SHORT", "continuation", 300, now=now,
    ) is None

    # A bare source_timestamp_verified boolean is not enough to mint an
    # artifact. Both decision-time and horizon quotes need adapter provenance.
    unattested_events = []
    for row in events:
        copied = dict(row)
        payload = json.loads(row["payload_json"])
        if row["event_type"] == "COGNITIVE_CANDIDATE_SNAPSHOT":
            payload["reference_quote"].pop("raw_fields", None)
            payload["point_in_time_evidence"][1]["measurements"]["price"].pop(
                "raw_fields", None
            )
        elif row["event_type"] == "COGNITIVE_CANDIDATE_HORIZON_OUTCOME":
            payload["outcome_quote"].pop("raw_fields", None)
        copied["payload_json"] = json.dumps(payload)
        unattested_events.append(copied)
    assert CalibrationProducer(
        Store(unattested_events), refresh_interval_seconds=30.0,
    ).refresh("EURUSD", now=now) == []

    wrong_source_module_events = []
    for row in events:
        copied = dict(row)
        payload = json.loads(row["payload_json"])
        if row["event_type"] == "COGNITIVE_CANDIDATE_SNAPSHOT":
            payload["point_in_time_evidence"][1]["source_module"] = "market.price_action"
        elif row["event_type"] == "COGNITIVE_CANDIDATE_HORIZON_OUTCOME":
            payload["outcome_quote"]["source_module"] = "market.price_action"
        copied["payload_json"] = json.dumps(payload)
        wrong_source_module_events.append(copied)
    assert CalibrationProducer(
        Store(wrong_source_module_events), refresh_interval_seconds=30.0,
    ).refresh("EURUSD", now=now) == []

    duplicate_snapshot_events = [dict(row) for row in events]
    duplicate_snapshot_events.append({**events[0], "seq": 10000})
    assert CalibrationProducer(
        Store(duplicate_snapshot_events), refresh_interval_seconds=30.0,
    ).refresh("EURUSD", now=now) == []

    duplicate_outcome_events = [dict(row) for row in events]
    duplicate_outcome_events.append({**events[1], "seq": 10001})
    assert CalibrationProducer(
        Store(duplicate_outcome_events), refresh_interval_seconds=30.0,
    ).refresh("EURUSD", now=now) == []

    mismatched_outcome_events = []
    for row in events:
        copied = dict(row)
        if row["event_type"] == "COGNITIVE_CANDIDATE_HORIZON_OUTCOME":
            outcome_payload = json.loads(row["payload_json"])
            outcome_payload["observed_price"] = float(outcome_payload["observed_price"]) + 0.5
            outcome_payload["directional_return_r"] = (
                float(outcome_payload["directional_return_r"]) + 0.5
            )
            outcome_payload["outcome_label"] = "FAVOURABLE"
            copied["payload_json"] = json.dumps(outcome_payload)
        mismatched_outcome_events.append(copied)
    assert CalibrationProducer(
        Store(mismatched_outcome_events), refresh_interval_seconds=30.0,
    ).refresh("EURUSD", now=now) == []

    mismatched_quote_events = []
    for row in events:
        copied = dict(row)
        if row["event_type"] == "COGNITIVE_CANDIDATE_SNAPSHOT":
            snapshot_payload = json.loads(row["payload_json"])
            quote_row = snapshot_payload["point_in_time_evidence"][1]
            quote_row["measurements"]["price"]["symbol"] = "GBPUSD"
            copied["payload_json"] = json.dumps(snapshot_payload)
        mismatched_quote_events.append(copied)
    assert CalibrationProducer(
        Store(mismatched_quote_events), refresh_interval_seconds=30.0,
    ).refresh("EURUSD", now=now) == []

    forming_geometry_events = []
    for row in events:
        copied = dict(row)
        if row["event_type"] == "COGNITIVE_CANDIDATE_SNAPSHOT":
            snapshot_payload = json.loads(row["payload_json"])
            feature_row = snapshot_payload["point_in_time_evidence"][0]
            feature_row["source_timestamp_epoch"] = (
                float(snapshot_payload["decision_epoch"]) - 60.0
            )
            feature_row["measurements"]["source_bar_epoch"] = (
                float(snapshot_payload["decision_epoch"]) - 60.0
            )
            copied["payload_json"] = json.dumps(snapshot_payload)
        forming_geometry_events.append(copied)
    assert CalibrationProducer(
        Store(forming_geometry_events), refresh_interval_seconds=30.0,
    ).refresh("EURUSD", now=now) == []



def test_developing_world_model_generation_time_is_not_source_time():
    from cognition.evidence_adapters import evidence_from_developing_bias

    generated_at = 1_900_000_000.0
    evidence = evidence_from_developing_bias("EURUSD", {
        "alignment_degree": 0.8,
        "trend_strength": 0.7,
        "conflict_score": 0.1,
        "model_generated_at_epoch": generated_at,
        "source_timestamp_verified": False,
        "provisional": True,
        "data_basis": "developing_world_model",
    })

    assert len(evidence) == 1
    assert evidence[0].source_timestamp_verified is False
    assert evidence[0].source_timestamp_epoch is None
    assert evidence[0].provisional is True
    assert evidence[0].measurements["model_generated_at_epoch"] == generated_at


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
    assert explicit_source.source_timestamp_verified is False
    assert explicit_source.source_timestamp_epoch is None

    verified_source = Evidence(
        source_module="test.verified_source",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        timestamp_epoch=1_900_000_100.0,
        source_timestamp_epoch=1_900_000_000.0,
        source_timestamp_verified=True,
    )
    assert verified_source.source_timestamp_verified is True
    assert verified_source.source_timestamp_epoch == 1_900_000_000.0

    measurement_timestamp_only = Evidence(
        source_module="test.measurement_timestamp_only",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        timestamp_epoch=1_900_000_100.0,
        measurements={"source_timestamp_epoch": 1_900_000_000.0},
    )
    assert measurement_timestamp_only.source_timestamp_verified is False
    assert measurement_timestamp_only.source_timestamp_epoch is None

    forged_truthy_flag = Evidence(
        source_module="test.forged_truthy_flag",
        domain=EvidenceDomain.STRUCTURE,
        symbol="EURUSD",
        timestamp_epoch=1_900_000_100.0,
        source_timestamp_epoch=1_900_000_000.0,
        source_timestamp_verified="true",
    )
    assert forged_truthy_flag.source_timestamp_verified is False
    assert forged_truthy_flag.source_timestamp_epoch is None

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
    assert Opportunity.from_reply({
        "id": "known-family", "candidate_family": "continuation",
    }).candidate_family == "continuation"
    assert Opportunity.from_reply({
        "id": "unknown-family", "candidate_family": "invented_style",
    }).candidate_family == "unclassified"



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



def test_malformed_considered_opportunity_is_retained_as_non_actionable():
    opinion = SimpleNamespace(
        opportunities=[{
            "direction": "LONG",
            "state": "ACTIVE",
            "candidate_family": "continuation",
        }],
        market_is_untradeable=False,
        market_state={"regime": "trending"},
        preferred_opportunity_id="",
    )
    opportunities, _, harvesting = CognitiveBrain._build_opportunity_set(opinion)
    assert harvesting is True
    assert len(opportunities) == 1
    assert opportunities[0].opportunity_id == "malformed:0"
    assert opportunities[0].state == OpportunityState.UNKNOWN
    assert opportunities[0].candidate_family == "unclassified"
    assert opportunities[0].is_actionable is False



def test_market_desk_adapter_requires_explicit_broker_quote_provenance():
    from datetime import datetime, timezone
    from cognition.evidence_adapters import evidence_from_market_desk

    now = time.time()
    payload = {
        "symbol": "EURUSD",
        "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "source_timestamp_verified": True,
        "data_quality_ok": True,
        "quote": {
            "symbol": "EURUSD",
            "bid": 1.0999,
            "ask": 1.1001,
            "spread": 0.0002,
            "timestamp_epoch": now,
            "source_timestamp_verified": True,
            "quote_kind": "bid_ask",
        },
        "completed_bars": {},
        "pattern_observations": [],
    }

    valid = evidence_from_market_desk("EURUSD", payload)
    assert len(valid) == 2
    root = next(item for item in valid if item.source_module == "market_desk")
    quote_evidence = next(
        item for item in valid if item.source_module == "market_desk.live_quote"
    )
    assert root.source_timestamp_verified is False
    assert root.source_timestamp_epoch is None
    assert root.timestamp_epoch >= time.time() - 5.0
    assert quote_evidence.source_timestamp_verified is True
    assert abs(quote_evidence.source_timestamp_epoch - now) < 1e-6
    assert quote_evidence.timestamp_epoch >= time.time() - 5.0
    # Source and ingestion clocks are sampled independently; host scheduling
    # and float conversion can make them equal or differ by sub-microseconds.
    assert abs(quote_evidence.timestamp_epoch - now) < 1.0

    missing_attestation = dict(payload)
    missing_attestation.pop("source_timestamp_verified")
    assert evidence_from_market_desk("EURUSD", missing_attestation) == []

    mismatched_quote_time = {
        **payload,
        "quote": {**payload["quote"], "timestamp_epoch": now - 2.0},
    }
    assert evidence_from_market_desk("EURUSD", mismatched_quote_time) == []

    mismatched_quote_symbol = {
        **payload,
        "quote": {**payload["quote"], "symbol": "GBPUSD"},
    }
    assert evidence_from_market_desk("EURUSD", mismatched_quote_symbol) == []

    inconsistent_spread = {
        **payload,
        "quote": {**payload["quote"], "spread": 0.5},
    }
    assert evidence_from_market_desk("EURUSD", inconsistent_spread) == []



def test_candidate_ledger_retains_missing_hypothesis_ids_and_duplicate_opportunity_ids():
    from types import SimpleNamespace

    class Store:
        def __init__(self):
            self.events = []
            self.rows = []

        def emit(self, event_type, severity, **kwargs):
            payload = kwargs["payload"]
            self.events.append(payload)
            self.rows.append({
                "seq": len(self.rows) + 1,
                "event_type": event_type,
                "symbol": kwargs.get("symbol"),
                "payload_json": json.dumps(payload),
            })
            return f"event-{len(self.events)}"

        def query_events(self, *, event_types, symbol, limit, offset, **kwargs):
            rows = [
                row for row in self.rows
                if row["event_type"] in event_types and row["symbol"] == symbol
            ]
            return rows[offset:offset + limit]

    store = Store()
    loop = CognitionLoop(
        brain=object(),
        consolidator=EvidenceConsolidator(),
        symbols_provider=lambda: [],
        candidate_event_store=store,
    )
    stamp = time.time()
    first = Opportunity(
        opportunity_id="duplicate-opportunity",
        direction="LONG",
        state=OpportunityState.FORMING,
        thesis="first representation",
    )
    second = Opportunity(
        opportunity_id="duplicate-opportunity",
        direction="SHORT",
        state=OpportunityState.ACTIVE,
        thesis="conflicting duplicate representation",
    )

    class MissingIdHypothesis:
        direction_implication = "LONG"
        hypothesis_kind = "continuation"

        def to_dict(self):
            return {
                "direction_implication": "LONG",
                "hypothesis_kind": "continuation",
                "thesis": "native hypothesis without stable identity",
            }

    class StableHypothesis:
        hypothesis_id = "reused-hypothesis-id"
        direction_implication = "LONG"
        hypothesis_kind = "continuation"

        def __init__(self, direction, thesis):
            self.direction_implication = direction
            self.thesis = thesis

        def to_dict(self):
            return {
                "hypothesis_id": self.hypothesis_id,
                "direction_implication": self.direction_implication,
                "hypothesis_kind": self.hypothesis_kind,
                "thesis": self.thesis,
            }

    conflicting_first = StableHypothesis("LONG", "first hypothesis meaning")
    conflicting_second = StableHypothesis("SHORT", "different meaning with reused ID")

    decision = SimpleNamespace(
        decision_id="decision-malformed-ledger",
        symbol="EURUSD",
        created_epoch=stamp,
        decision_type=DecisionType.OPPORTUNITY_FORMING,
        opportunities=[first, second],
        hypotheses=[
            MissingIdHypothesis(),
            conflicting_first,
            conflicting_second,
        ],
        questions_answered={},
    )
    market = MarketState(symbol="EURUSD", created_epoch=stamp)
    loop._record_candidate_snapshot(SimpleNamespace(decision=decision), market)

    assert len(store.events) == 5
    by_kind = {}
    for payload in store.events:
        by_kind.setdefault(payload["candidate_kind"], []).append(payload)
    assert len(by_kind["opportunity"]) == 2
    duplicate = by_kind["opportunity"][1]
    assert duplicate["candidate_ref"] == "malformed:duplicate-opportunity:1"
    assert duplicate["candidate"]["candidate_parse_status"] == "DUPLICATE_STABLE_ID"
    malformed = by_kind["hypothesis"][0]
    assert malformed["candidate_ref"] == "malformed-hypothesis:0"
    assert malformed["candidate"]["candidate_parse_status"] == "MISSING_STABLE_ID"
    assert malformed["candidate"]["candidate_family"] == "unclassified"
    assert malformed["candidate"]["calibration_candidate"] is False

    assert len(by_kind["hypothesis"]) == 3
    duplicate_hypothesis = by_kind["hypothesis"][2]
    assert duplicate_hypothesis["candidate_ref"] == "malformed:duplicate-hypothesis:2"
    assert duplicate_hypothesis["candidate"]["candidate_parse_status"] == "DUPLICATE_STABLE_ID"
    assert duplicate_hypothesis["candidate"]["candidate_family"] == "unclassified"
    assert duplicate_hypothesis["candidate"]["calibration_candidate"] is False

    snapshot_ids = {
        payload["candidate_id"] for payload in store.events
        if payload.get("candidate_kind") in {"opportunity", "hypothesis"}
    }
    loop._record_candidate_disposition(
        SimpleNamespace(decision=decision, campaign=None),
        stage="final",
    )
    dispositions = [
        payload for payload in store.events
        if payload.get("stage") == "final"
    ]
    assert len(dispositions) == len(snapshot_ids) == 5
    assert {payload["candidate_id"] for payload in dispositions} == snapshot_ids
    malformed_disposition = next(
        payload for payload in dispositions
        if payload["candidate_parse_status"] == "MISSING_STABLE_ID"
    )
    assert malformed_disposition["candidate_ref"] == "malformed-hypothesis:0"
    duplicate_disposition = next(
        payload for payload in dispositions
        if payload["candidate_parse_status"] == "DUPLICATE_STABLE_ID"
        and payload["candidate_kind"] == "opportunity"
    )
    assert duplicate_disposition["candidate_ref"] == "malformed:duplicate-opportunity:1"



def test_calibration_counts_only_non_overlapping_decision_horizons():
    from cognition.calibration_artifacts import CalibrationProducer

    groups = [
        {"decision_id": "d0", "decision_epoch": 1000.0},
        {"decision_id": "d1", "decision_epoch": 1060.0},
        {"decision_id": "d2", "decision_epoch": 1300.0},
        {"decision_id": "d3", "decision_epoch": 1301.0},
        {"decision_id": "d4", "decision_epoch": 1601.0},
    ]
    selected = CalibrationProducer._non_overlapping_decision_groups(groups, 300)
    assert [item["decision_id"] for item in selected] == ["d0", "d3"]
