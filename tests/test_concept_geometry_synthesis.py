from types import SimpleNamespace

from cognition.opportunity_geometry import _geometry_from_concept_readings


def _reading(concept, evidence_id, stop, target, *, timeframe="M5", source_time=1000.0):
    return {
        "item": SimpleNamespace(evidence_id=evidence_id),
        "time": source_time,
        "reference": 100.0,
        "concept": concept,
        "timeframe": timeframe,
        "reference_timeframe": timeframe,
        "implications": [{
            "direction": "LONG",
            "activated": True,
            "strength": 0.7,
            "invalidation_price": stop,
            "objective_price": target,
            "invalidation_reason": f"{concept} invalidation",
            "activation_reason": f"{concept} trigger confirmed",
            "milestones": [102.0, 103.0],
        }],
    }


def test_geometry_uses_median_levels_from_compatible_concept_readings():
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
    assert geometry.stop_price == 98.5
    assert geometry.target_price == 104.5
    assert geometry.evidence_ids == ("e1", "e2")
    assert geometry.supporting_concepts == ("crt", "market_structure")
    assert len(geometry.geometry_sources) == 2
    assert geometry.milestones == (102.0, 103.0)


def test_materially_conflicting_concept_stops_fail_closed():
    geometry = _geometry_from_concept_readings(
        [
            _reading("crt", "e1", 99.0, 105.0),
            _reading("liquidity", "e2", 80.0, 105.0),
        ],
        "LONG",
        "EURUSD",
    )
    assert geometry is None


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
