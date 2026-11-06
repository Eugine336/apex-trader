"""Tests for real strategy-lens composition and Brain integration."""
from __future__ import annotations

import math

from cognition.concepts import CONCEPT_MODULES, run_concepts
from cognition.concepts.base import ConceptReading, Implication, reading_to_evidence
from cognition.contracts import EvidenceDomain
from cognition.concepts.strategy_lens_readings import (
    DATA_GATED_LENSES,
    FULL_LENSES,
    LENS_METHODS,
    StrategyLensReadings,
)
from cognition.strategy_knowledge import STRATEGY_LENSES, assess_strategy_lenses


def _bars(count: int = 180) -> list[dict]:
    rows = []
    close = 100.0
    for index in range(count):
        step = 0.0022 * math.sin(index * 0.37) + 0.0009 * math.cos(index * 0.11)
        close *= math.exp(step)
        width = close * (0.001 + 0.0004 * (1.0 + math.sin(index * 0.19)))
        rows.append({
            "open": close,
            "high": close + width,
            "low": close - width,
            "close": close,
            "volume": 100.0 + index,
            "volume_source": "provider_volume",
            "timestamp_epoch": 1_780_000_000.0 + index * 300.0,
            "source_timestamp_verified": True,
        })
    return rows


def _lens_evidence():
    return [
        item for item in run_concepts("XAUUSD", {"M5": _bars()})
        if item.measurements.get("lens_key") in set(LENS_METHODS)
        or item.measurements.get("concept") in {"crt", "regime_switching"}
    ]


def test_lens_composition_catalog_covers_every_strategy_knowledge_lens_once():
    catalog_keys = {item.key for item in STRATEGY_LENSES}
    assert set(LENS_METHODS) | {"crt", "market_regime_transition"} == catalog_keys
    assert len(LENS_METHODS) == 36
    assert len(FULL_LENSES) == 18
    assert len(DATA_GATED_LENSES) == 18
    assert set(FULL_LENSES).isdisjoint(DATA_GATED_LENSES)


def test_lens_composition_reader_is_registered_and_claims_no_new_methods():
    modules = [item for item in CONCEPT_MODULES if item.key == "strategy_lens_readings"]
    assert len(modules) == 1
    assert isinstance(modules[0], StrategyLensReadings)
    assert modules[0].covers_methods == ()
    assert set(modules[0].covers_lenses) == set(FULL_LENSES)
    assert set(modules[0].data_gated_lenses) == set(DATA_GATED_LENSES)


def test_native_pipeline_composes_method_readings_for_every_lens():
    evidence = _lens_evidence()
    concepts = {
        item.measurements.get("lens_key")
        or (
            "market_regime_transition"
            if item.measurements.get("concept") == "regime_switching"
            else item.measurements.get("concept")
        )
        for item in evidence
        if item.measurements.get("lens_key")
        or item.measurements.get("concept") in {"crt", "regime_switching"}
    }

    assert set(LENS_METHODS) | {"crt", "market_regime_transition"} <= concepts
    full_lens_readings = [
        item for item in evidence
        if item.measurements.get("lens_key") in FULL_LENSES
    ]
    assert {item.measurements["lens_key"] for item in full_lens_readings} == set(FULL_LENSES)
    full_methods = {
        method for module in CONCEPT_MODULES
        for method in getattr(module, "covers_methods", ())
    }
    declared_full_methods = {
        method for lens_key in FULL_LENSES
        for method in LENS_METHODS[lens_key]
    }
    assert declared_full_methods <= full_methods
    assert set(FULL_LENSES).isdisjoint(DATA_GATED_LENSES)
    for item in full_lens_readings:
        required = set(item.measurements["required_methods"])
        available = set(item.measurements["available_methods"])
        missing = set(item.measurements["missing_methods"])
        assert available.isdisjoint(missing)
        assert available | missing == required
        assert item.measurements["missing_methods_semantics"].startswith(
            "No eligible ConceptReading was available"
        )
        assert "unverified reading" in item.measurements["missing_methods_semantics"]
        assert "beyond the cutoff" in item.measurements["missing_methods_semantics"]
    trend = next(
        item for item in evidence
        if item.measurements.get("lens_key") == "trend_following"
    )
    assert trend.measurements["source_kind"] == "concept_reading"
    assert trend.source_module == "concept.strategy_lens.trend_following.m5"
    assert trend.measurements["required_methods"] == [
        "trend_following", "roc_momentum", "momentum_breakout",
    ]
    assert "method_summaries" in trend.measurements
    assert "method_implications" in trend.measurements
    assert trend.measurements["source_timestamp_known"] is True
    assert trend.measurements["source_provenance_verified"] is True
    assert all(
        "implications" in summary
        for summary in trend.measurements["method_summaries"].values()
    )
    assert trend.independence_group == "ohlc:XAUUSD:M5"
    assert trend.polarity == 0.0
    assert trend.measurements["directional_decision_emitted"] is False
    assert trend.measurements["entry_authorization_emitted"] is False



def test_emitted_full_method_readings_use_the_current_cutoff_and_remain_neutral():
    # A FULL reader may be event-conditional or require a non-OHLC input channel.
    # One synthetic M5-only cycle cannot require every registered method to emit:
    # e.g. pivot points require a completed D1 bar and execution costs require a
    # source-attested broker quote. Validate every reading that is actually
    # emitted here; per-method applicability and prerequisite behavior belongs
    # in the reader-specific tests and data-channel integration tests.
    cutoff = _bars()[-1]["timestamp_epoch"]
    evidence = run_concepts("XAUUSD", {"M5": _bars()})
    full_methods = {
        method for module in CONCEPT_MODULES
        for method in getattr(module, "covers_methods", ())
    }
    method_evidence = [
        item for item in evidence
        if isinstance(getattr(item, "measurements", None), dict)
        and item.measurements.get("source_kind") == "concept_reading"
        and item.measurements.get("concept") in full_methods
    ]
    assert method_evidence
    for item in method_evidence:
        measurements = item.measurements
        assert measurements["concept"] in full_methods
        assert item.polarity == 0.0
        if measurements["timeframe"] == "SPEC":
            # Broker contract/carry readings are not candle observations. With
            # no provider-attested specification timestamp in this OHLC-only
            # cycle they must remain explicitly time-unknown, never borrow the
            # latest M5 candle cutoff as fabricated provenance.
            assert measurements["source_timestamp_known"] is False
            assert measurements["source_timestamp_epoch"] is None
            assert measurements["source_timestamp_verified"] is False
            continue
        assert measurements["source_timestamp_known"] is True
        assert measurements["source_bar_epoch"] == cutoff
        assert measurements["source_timestamp_verified"] is True


def test_lens_composition_preserves_method_geometry_and_failure_modes():
    evidence = _lens_evidence()
    trend = next(
        item for item in evidence
        if item.measurements.get("lens_key") == "trend_following"
    )
    facts = trend.measurements

    assert "known_failure_modes" in facts
    assert facts["known_failure_modes"]
    assert "invalidation_candidates" in facts
    assert "objective_candidates" in facts
    assert "trail_anchors" in facts
    assert facts["style_is_not_selected_or_authorized"] is True


def test_brain_strategy_lens_assessment_uses_composed_method_evidence():
    evidence = _lens_evidence()
    assessments = {
        item["key"]: item for item in assess_strategy_lenses(evidence)
    }

    assert any("native method composition" in reason for reason in assessments["pattern_discovery"]["reasons"])
    assert assessments["event_macro"]["status"] == "not_evaluable"
    assert any(
        "complete lens interpretation requires" in item
        for item in assessments["event_macro"]["missing_context"]
    )


def test_data_gated_lens_does_not_claim_full_when_external_inputs_are_absent():
    evidence = _lens_evidence()
    event = next(
        item for item in evidence
        if item.measurements.get("lens_key") == "event_macro"
    )

    assert event.measurements["data_gated_reason"] == DATA_GATED_LENSES["event_macro"]
    assert event.measurements["applicable"] is False
    assert any("economic calendar" in item for item in event.measurements["unknowns"])
    assert event.polarity == 0.0



def test_composite_lens_never_promotes_mixed_method_timestamps_to_verified():
    methods = [
        ConceptReading(
            concept="wyckoff_effort_result",
            timeframe="M5",
            domain=EvidenceDomain.VOLUME,
            applicable=True,
            applicability_reason="completed-bar method reading",
            observation="volume effort compared with range result",
            implications=(),
            facts={},
            source_bar_epoch=1_780_000_000.0,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="wyckoff",
            timeframe="M5",
            domain=EvidenceDomain.STRUCTURE,
            applicable=True,
            applicability_reason="completed-bar method reading",
            observation="phase and event sequence",
            implications=(),
            facts={},
            source_bar_epoch=None,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    readings = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    composite = next(item for item in readings if item.concept == "wyckoff_effort_result")

    assert composite.source_bar_epoch is None
    assert composite.independence_group == "ohlc:XAUUSD:M5"
    assert any("not all synchronized" in item for item in composite.unknowns)

    assessment = next(
        item for item in assess_strategy_lenses([reading_to_evidence("XAUUSD", composite)])
        if item["key"] == "wyckoff_effort_result"
    )
    assert assessment["status"] == "not_evaluable"
    assert any(
        "method readings excluded because source timestamps are unverified" in item
        for item in assessment["missing_context"]
    )



def test_fully_verified_method_composition_can_be_relevant_to_the_brain():
    methods = [
        ConceptReading(
            concept="anomaly_detection",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="sufficient own-history sample",
            observation="current state novelty is measured against prior states",
            implications=(),
            facts={"novelty_own_history_percentile": 0.72},
            source_bar_epoch=_bars()[-1]["timestamp_epoch"],
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="clustering",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="prior states have realized outcomes",
            observation="nearest historical states are available",
            implications=(),
            facts={"historical_state_sample_count": 80},
            source_bar_epoch=_bars()[-1]["timestamp_epoch"],
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    composed = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    lens = next(item for item in composed if item.concept == "pattern_discovery")
    assessment = next(
        item for item in assess_strategy_lenses([reading_to_evidence("XAUUSD", lens)])
        if item["key"] == "pattern_discovery"
    )

    assert lens.source_bar_epoch == _bars()[-1]["timestamp_epoch"]
    assert lens.independence_group == "ohlc:XAUUSD:M5"
    assert assessment["status"] == "relevant"


def test_unverified_method_timestamps_are_not_evaluable():
    methods = [
        ConceptReading(
            concept="anomaly_detection",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="available",
            observation="novelty",
            implications=(),
            facts={},
            source_bar_epoch=1_780_000_000.0,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="clustering",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="available",
            observation="analogues",
            implications=(),
            facts={},
            source_bar_epoch=None,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    composed = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    lens = next(item for item in composed if item.concept == "pattern_discovery")
    assessment = next(
        item for item in assess_strategy_lenses([reading_to_evidence("XAUUSD", lens)])
        if item["key"] == "pattern_discovery"
    )

    assert lens.source_bar_epoch is None
    assert assessment["status"] == "not_evaluable"
    assert any(
        "method readings excluded because source timestamps are unverified" in item
        for item in assessment["missing_context"]
    )







def test_synchronized_but_stale_method_readings_cannot_claim_current_lens_applicability():
    cutoff = _bars()[-1]["timestamp_epoch"]
    stale_epoch = cutoff - 300.0
    methods = [
        ConceptReading(
            concept=method,
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="historical method context",
            observation="method evidence is one completed bar stale",
            implications=(),
            facts={},
            source_bar_epoch=stale_epoch,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        )
        for method in ("anomaly_detection", "clustering")
    ]
    lens = next(
        item for item in StrategyLensReadings().read_with_context(
            "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
        )
        if item.concept == "pattern_discovery"
    )
    assert lens.facts["missing_methods"] == []
    assert lens.facts["style_context_state"] == "stale_method_context"
    assert lens.applicable is False
    assert lens.source_bar_epoch == stale_epoch
    assert any("stale relative to the latest completed-bar cutoff" in reason for reason in lens.unknowns)


def test_method_readings_must_share_one_verified_timestamp_to_make_lens_applicable():
    cutoff = _bars()[-1]["timestamp_epoch"]
    methods = [
        ConceptReading(
            concept="anomaly_detection",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="available",
            observation="novelty",
            implications=(),
            facts={},
            source_bar_epoch=cutoff,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="clustering",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="available",
            observation="analogue",
            implications=(),
            facts={},
            source_bar_epoch=cutoff - 300.0,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    composed = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    lens = next(item for item in composed if item.concept == "pattern_discovery")

    assert lens.facts["missing_methods"] == []
    assert lens.applicable is False
    assert lens.facts["source_provenance_verified"] is False
    assert lens.facts["style_context_state"] == "unsynchronized_method_context"
    assert any("not all synchronized" in item for item in lens.unknowns)




def test_completed_ohlcv_method_readings_preserve_explicit_shared_bar_lineage():
    cutoff = _bars()[-1]["timestamp_epoch"]
    methods = [
        ConceptReading(
            concept=method,
            timeframe="M5",
            domain=EvidenceDomain.VOLUME,
            applicable=True,
            applicability_reason="verified completed OHLCV input",
            observation="method-level volume and auction context",
            implications=(),
            facts={},
            source_bar_epoch=cutoff,
            source_timestamp_verified=True,
            data_basis="completed_ohlcv_with_selected_volume_source",
            independence_group="ohlc:XAUUSD:M5",
        )
        for method in ("wyckoff_effort_result", "wyckoff")
    ]
    lens = next(
        item for item in StrategyLensReadings().read_with_context(
            "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
        )
        if item.concept == "wyckoff_effort_result"
    )
    assert lens.applicable is True
    assert lens.independence_group == "ohlc:XAUUSD:M5"
    assert lens.facts["independence_group_verified"] is True


def test_lens_geometry_preserves_mirror_symmetric_method_levels_without_directional_vote():
    cutoff = _bars()[-1]["timestamp_epoch"]

    def compose(direction: str, invalidation: float, objective: float, milestones: tuple[float, ...]):
        methods = [
            ConceptReading(
                concept=method,
                timeframe="M5",
                domain=EvidenceDomain.MOMENTUM,
                applicable=True,
                applicability_reason="method reading is applicable",
                observation="method geometry is source supplied",
                implications=(
                    Implication(
                        direction=direction,
                        statement="method-level conditional implication",
                        strength=0.61,
                        activated=False,
                        activation_reason="activation remains conditional",
                        invalidation_price=invalidation,
                        invalidation_reason="method-defined structural invalidation",
                        objective_price=objective,
                        milestones=milestones,
                        requires=("independent confirmation",),
                        conflicts_with=("opposing structural break",),
                        management={"protective_action": "trail_to_anchor", "anchor": "confirmed_swing"},
                    ),
                ),
                facts={},
                source_bar_epoch=cutoff,
                source_timestamp_verified=True,
                data_basis="completed_ohlc",
                independence_group="ohlc:XAUUSD:M5",
            )
            for method in ("zscore_mean", "mean_reversion")
        ]
        readings = StrategyLensReadings().read_with_context(
            "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
        )
        return next(item for item in readings if item.concept == "mean_reversion")

    original = compose("LONG", 95.0, 110.0, (103.0, 106.0))
    mirrored = compose("SHORT", 105.0, 90.0, (97.0, 94.0))

    original_invalidations = original.facts["invalidation_candidates"]
    mirrored_invalidations = mirrored.facts["invalidation_candidates"]
    original_objectives = original.facts["objective_candidates"]
    mirrored_objectives = mirrored.facts["objective_candidates"]
    assert len(original_invalidations) == len(mirrored_invalidations) == 2
    assert len(original_objectives) == len(mirrored_objectives) == 2
    for left, right in zip(original_invalidations, mirrored_invalidations):
        assert left["direction"] == "LONG"
        assert right["direction"] == "SHORT"
        assert right["price"] == 200.0 - left["price"]
    for left, right in zip(original_objectives, mirrored_objectives):
        assert right["price"] == 200.0 - left["price"]
    assert original.facts["milestone_candidates"]
    assert mirrored.facts["milestone_candidates"]
    for left, right in zip(
        original.facts["milestone_candidates"],
        mirrored.facts["milestone_candidates"],
    ):
        assert right["price"] == 200.0 - left["price"]
    assert original.implications == mirrored.implications == ()
    assert original.facts["management_candidates"][0]["management"] == {
        "protective_action": "trail_to_anchor", "anchor": "confirmed_swing",
    }
    assert original.facts["management_candidates"][0]["requires"] == [
        "independent confirmation",
    ]
    assert original.facts["geometry_management_contract"]["execution_authority"] is False
    assert original.facts["directional_decision_emitted"] is False
    assert mirrored.facts["entry_authorization_emitted"] is False



def test_provider_timestamp_is_not_substituted_for_completed_bar_replay_epoch():
    cutoff = _bars()[-1]["timestamp_epoch"]
    methods = [
        ConceptReading(
            concept="anomaly_detection",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="source bar is within replay cutoff",
            observation="provider observation time is separate",
            implications=(),
            facts={},
            source_bar_epoch=cutoff,
            source_timestamp_epoch=cutoff + 120.0,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="clustering",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="same completed bar",
            observation="same replay bar",
            implications=(),
            facts={},
            source_bar_epoch=cutoff,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    lens = next(
        item for item in StrategyLensReadings().read_with_context(
            "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
        )
        if item.concept == "pattern_discovery"
    )
    assert lens.facts["future_dated_method_readings"] == []
    assert lens.facts["missing_methods"] == []
    assert lens.source_bar_epoch == cutoff
    # Different provider clocks are not collapsed into one invented timestamp.
    assert lens.source_timestamp_epoch is None
    assert lens.source_timestamp_verified is True
    assert lens.applicable is True


def test_lens_composition_excludes_future_dated_method_readings_from_replay():
    cutoff = _bars()[-1]["timestamp_epoch"]
    methods = [
        ConceptReading(
            concept="anomaly_detection",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="future reading must be excluded",
            observation="future novelty evidence",
            facts={"novelty_own_history_percentile": 0.99},
            implications=(
                Implication(
                    direction="LONG", statement="future-only implication",
                    strength=0.99, activated=True,
                    activation_reason="must not enter the replay cutoff",
                    invalidation_price=99.0, objective_price=110.0,
                    milestones=(105.0,),
                ),
            ),
            source_bar_epoch=cutoff + 300.0,
            source_timestamp_epoch=cutoff + 300.0,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="clustering",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="historical analogue available at cutoff",
            observation="historical analogue evidence",
            implications=(),
            facts={"historical_state_sample_count": 80},
            source_bar_epoch=cutoff,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    composed = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    lens = next(item for item in composed if item.concept == "pattern_discovery")

    assert lens.facts["missing_methods"] == ["anomaly_detection"]
    assert lens.facts["method_implications"].get("anomaly_detection") == []
    assert lens.facts["invalidation_candidates"] == []
    assert lens.facts["objective_candidates"] == []
    assert lens.facts["milestone_candidates"] == []
    assert lens.facts["management_candidates"] == []
    assert "anomaly_detection" in lens.facts["future_dated_method_readings"]
    assert "beyond the cutoff" in lens.facts["missing_methods_semantics"]
    assert lens.source_bar_epoch is None
    # The surviving method has an explicit group; it does not imply that the
    # omitted future method contributed to the composition.
    assert lens.independence_group == "ohlc:XAUUSD:M5"
    assert any(
        "future-dated method readings excluded" in item
        for item in lens.unknowns
    )



def test_measured_regime_fit_rules_cover_each_supported_style_without_directional_authority():
    from cognition.strategy_knowledge import _lens_regime_fit

    cases = {
        "trend_following": ({"trend_context": "uptrend"}, "supports"),
        "trend_strength": ({"trend_context": "downtrend"}, "supports"),
        "moving_average": ({"trend_context": "range"}, "challenges"),
        "mean_reversion": ({"trend_context": "range"}, "supports"),
        "range_auction": ({"trend_context": "uptrend"}, "challenges"),
        "momentum": ({"momentum": "accelerating"}, "supports"),
        "breakout": ({"volatility": "expanding"}, "supports"),
        "volatility_breakout": ({"volatility": "compressing"}, "challenges"),
        "volatility_regime": ({"volatility": "compressing"}, "supports"),
        "order_flow_auction": ({"order_flow": "buying"}, "supports"),
    }
    for lens_key, (regime, expected) in cases.items():
        fit, explanation = _lens_regime_fit(lens_key, regime)
        assert fit == expected, (lens_key, fit, expected)
        assert explanation
    assert _lens_regime_fit("trend_following", {"trend_context": "conflicted"})[0] == "conflicted"
    assert _lens_regime_fit("trend_following", {"trend_context": "mixed"})[0] == "unknown"
    for lens_key in (
        "breakout", "failed_breakout", "oscillator_divergence",
        "microstructure", "carry_funding",
    ):
        fit, explanation = _lens_regime_fit(lens_key, {"momentum": "accelerating"})
        assert fit == "unknown"
        assert explanation.strip()



def test_every_lens_reports_unknown_regime_fit_when_no_measured_field_supports_it():
    from cognition.strategy_knowledge import _lens_regime_fit

    for lens in STRATEGY_LENSES:
        fit, explanation = _lens_regime_fit(lens.key, {})
        assert fit == "unknown"
        assert explanation.strip(), lens.key


def test_measured_regime_challenges_style_without_vetoing_native_lens():
    reading = ConceptReading(
        concept="trend_following",
        timeframe="M5",
        domain=EvidenceDomain.OTHER,
        applicable=True,
        applicability_reason="complete trend-following method context",
        observation="composed trend-following context",
        implications=(),
        facts={
            "lens_key": "trend_following",
            "missing_methods": [],
            "data_gated_reason": None,
        },
        source_bar_epoch=1_780_000_000.0,
        source_timestamp_verified=True,
        data_basis="completed_ohlc",
    )
    assessment = next(
        item for item in assess_strategy_lenses(
            [reading_to_evidence("XAUUSD", reading)],
            regime={"trend_context": "range"},
        )
        if item["key"] == "trend_following"
    )

    assert assessment["status"] == "relevant"
    assert assessment["regime_fit"] == "challenges"
    assert any("challenges this style" in reason for reason in assessment["reasons"])


def test_verified_ohlcv_method_basis_can_support_complete_lens_provenance():
    reading = ConceptReading(
        concept="wyckoff_effort_result",
        timeframe="M5",
        domain=EvidenceDomain.VOLUME,
        applicable=True,
        applicability_reason="verified completed OHLCV source",
        observation="volume method evidence",
        implications=(),
        facts={
            "lens_key": "wyckoff_effort_result",
            "required_methods": ["wyckoff_effort_result", "wyckoff"],
            "available_methods": ["wyckoff_effort_result", "wyckoff"],
            "missing_methods": [],
            "data_gated_reason": None,
        },
        source_bar_epoch=1_780_000_000.0,
        source_timestamp_verified=True,
        data_basis="completed_ohlcv_with_selected_volume_source",
    )
    assessment = next(
        item for item in assess_strategy_lenses([reading_to_evidence("XAUUSD", reading)])
        if item["key"] == "wyckoff_effort_result"
    )

    assert assessment["status"] == "relevant"


def test_reading_adapter_requires_explicit_verification_for_bar_timestamp():
    reading = ConceptReading(
        concept="anomaly_detection",
        timeframe="M5",
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=True,
        applicability_reason="test reading",
        observation="timestamp provenance contract",
        implications=(),
        facts={},
        source_bar_epoch=1_780_000_000.0,
        data_basis="completed_ohlc",
    )

    evidence = reading_to_evidence("XAUUSD", reading)

    assert evidence.source_timestamp_verified is False
    assert evidence.source_timestamp_epoch is None
    assert evidence.measurements["source_timestamp_known"] is False


def test_composite_lens_does_not_promote_unverified_method_timestamps():
    methods = [
        ConceptReading(
            concept="anomaly_detection",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="available",
            observation="novelty",
            implications=(),
            facts={},
            source_bar_epoch=1_780_000_000.0,
            source_timestamp_verified=False,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="clustering",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="available",
            observation="analogue",
            implications=(),
            facts={},
            source_bar_epoch=1_780_000_000.0,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
    ]
    composed = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    lens = next(item for item in composed if item.concept == "pattern_discovery")
    evidence = reading_to_evidence("XAUUSD", lens)

    assert lens.source_bar_epoch is None
    assert evidence.source_timestamp_verified is False
    assert evidence.measurements["source_timestamp_known"] is False
    assert lens.independence_group == "ohlc:XAUUSD:M5"
    assert any(
        "source-verified completed-bar timestamp" in item
        for item in lens.unknowns
    )


def test_mixed_source_lens_never_receives_synthetic_ohlc_independence_group():
    methods = [
        ConceptReading(
            concept="anomaly_detection",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="OHLC-derived",
            observation="novelty",
            implications=(),
            facts={},
            source_bar_epoch=1_780_000_000.0,
            source_timestamp_verified=True,
            data_basis="completed_ohlc",
            independence_group="ohlc:XAUUSD:M5",
        ),
        ConceptReading(
            concept="clustering",
            timeframe="M5",
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=True,
            applicability_reason="non-candle source",
            observation="analogue",
            implications=(),
            facts={},
            source_bar_epoch=1_780_000_000.0,
            source_timestamp_verified=True,
            data_basis="historical_outcome",
            independence_group="",
        ),
    ]
    composed = StrategyLensReadings().read_with_context(
        "XAUUSD", {"M5": _bars()}, {"concept_readings": methods},
    )
    lens = next(item for item in composed if item.concept == "pattern_discovery")
    evidence = reading_to_evidence("XAUUSD", lens)

    assert lens.independence_group == ""
    assert evidence.independence_group == ""
    assert evidence.measurements["independence_group_verified"] is False

