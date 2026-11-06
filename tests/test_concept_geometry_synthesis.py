from types import SimpleNamespace

from cognition.opportunity_geometry import _geometry_from_concept_readings


def _reading(concept, evidence_id, stop, target, *, timeframe="M5", source_time=1000.0, independence_group="ohlc:EURUSD:M5", direction="LONG"):
    return {
        "item": SimpleNamespace(evidence_id=evidence_id),
        "time": source_time,
        "reference": 100.0,
        "concept": concept,
        "timeframe": timeframe,
        "reference_timeframe": timeframe,
        "independence_group": independence_group,
        "implications": [{
            "direction": direction,
            "activated": True,
            "strength": 0.7,
            "invalidation_price": stop,
            "objective_price": target,
            "invalidation_reason": f"{concept} invalidation",
            "activation_reason": f"{concept} trigger confirmed",
            "milestones": [102.0, 103.0],
        }],
    }


def test_geometry_uses_most_protective_stop_and_nearest_real_objective():
    geometry = _geometry_from_concept_readings(
        [
            _reading("crt", "e1", 98.0, 104.0),
            _reading("market_structure", "e2", 99.0, 105.0),
        ],
        "LONG",
        "EURUSD",
    )

    assert geometry is not None
    assert geometry.entry_price == 100.0
    assert geometry.stop_price == 99.0
    assert geometry.target_price == 104.0
    assert geometry.evidence_ids == ("e1", "e2")
    assert geometry.supporting_concepts == ("crt", "market_structure")
    assert len(geometry.geometry_sources) == 2
    assert geometry.milestones == (102.0, 103.0)
    assert geometry.independent_source_count == 1
    assert geometry.independence_groups == ("ohlc:EURUSD:M5",)


def test_distinct_structural_levels_select_the_most_protective_invalidation():
    geometry = _geometry_from_concept_readings(
        [
            _reading("crt", "e1", 99.0, 105.0),
            _reading("liquidity", "e2", 80.0, 105.0),
        ],
        "LONG",
        "EURUSD",
    )

    assert geometry is not None
    assert geometry.stop_price == 99.0
    assert geometry.target_price == 105.0


def test_geometry_does_not_merge_different_timeframes_or_source_bars():
    geometry = _geometry_from_concept_readings(
        [
            _reading("crt", "e1", 98.0, 105.0, timeframe="M5"),
            _reading("market_structure", "e2", 99.0, 106.0, timeframe="H1"),
        ],
        "LONG",
        "EURUSD",
    )
    assert geometry is not None
    assert geometry.evidence_ids in {("e1",), ("e2",)}



def test_geometry_prefers_independent_sources_when_ranking_candidate_groups():
    geometry = _geometry_from_concept_readings(
        [
            _reading("crt", "e1", 98.0, 104.0, timeframe="M5", independence_group="ohlc:EURUSD:M5"),
            _reading("market_structure", "e2", 99.0, 105.0, timeframe="M5", independence_group="ohlc:EURUSD:M5"),
            _reading("order_book", "e3", 98.0, 104.0, timeframe="H1", independence_group="dom:EURUSD"),
            _reading("crt_h1", "e4", 99.0, 105.0, timeframe="H1", independence_group="ohlc:EURUSD:H1"),
        ],
        "LONG",
        "EURUSD",
    )

    assert geometry is not None
    assert geometry.stop_timeframe == "H1"
    assert geometry.independent_source_count == 2
    assert geometry.independence_groups == ("dom:EURUSD", "ohlc:EURUSD:H1")


def test_short_geometry_uses_most_protective_stop_and_nearest_objective():
    geometry = _geometry_from_concept_readings(
        [
            _reading("crt", "e1", 102.0, 95.0, direction="SHORT"),
            _reading("market_structure", "e2", 101.0, 96.0, direction="SHORT"),
        ],
        "SHORT",
        "EURUSD",
    )

    assert geometry is not None
    assert geometry.entry_price == 100.0
    assert geometry.stop_price == 101.0
    assert geometry.target_price == 96.0
    assert geometry.reward_r == 4.0



def test_geometry_preserves_reading_supplied_limit_entry_zone():
    reading = _reading("crt", "e-limit", 98.0, 105.0)
    reading["implications"][0].update({
        "order_type": "LIMIT",
        "entry_zone_low": 99.0,
        "entry_zone_high": 101.0,
        "entry_price": 99.5,
    })

    geometry = _geometry_from_concept_readings([reading], "LONG", "EURUSD")

    assert geometry is not None
    assert geometry.entry_price == 99.5
    assert geometry.order_type == "LIMIT"
    assert geometry.entry_zone_low == 99.0
    assert geometry.entry_zone_high == 101.0
    assert geometry.stop_price == 98.0
    assert geometry.target_price == 105.0


def test_non_market_entry_without_price_or_zone_fails_closed():
    reading = _reading("crt", "e-stop", 98.0, 105.0)
    reading["implications"][0]["order_type"] = "STOP"

    assert _geometry_from_concept_readings([reading], "LONG", "EURUSD") is None


def test_entry_price_outside_reading_zone_fails_closed():
    reading = _reading("crt", "e-outside", 98.0, 105.0)
    reading["implications"][0].update({
        "order_type": "LIMIT",
        "entry_price": 102.0,
        "entry_zone_low": 99.0,
        "entry_zone_high": 101.0,
    })

    assert _geometry_from_concept_readings([reading], "LONG", "EURUSD") is None


def test_concept_readings_with_conflicting_order_types_do_not_merge():
    market = _reading("crt", "e-market", 98.0, 105.0)
    pending = _reading("market_structure", "e-limit", 98.0, 105.0)
    pending["implications"][0].update({
        "order_type": "LIMIT",
        "entry_price": 100.0,
        "entry_zone_low": 99.0,
        "entry_zone_high": 101.0,
    })

    assert _geometry_from_concept_readings([market, pending], "LONG", "EURUSD") is None



def test_malformed_explicit_entry_price_fails_closed():
    reading = _reading("crt", "e-malformed", 98.0, 105.0)
    reading["implications"][0].update({
        "order_type": "LIMIT",
        "entry_price": "not-a-price",
        "entry_zone_low": 99.0,
        "entry_zone_high": 101.0,
    })

    assert _geometry_from_concept_readings([reading], "LONG", "EURUSD") is None
