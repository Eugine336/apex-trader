"""Management maps a delivered concept milestone to its geometry-derived partial plan."""

from types import SimpleNamespace

from cognition.contracts import EvidenceDomain
from cognition.native_management import NativePositionManager


class _State:
    symbol = "EURUSD"

    def __init__(self, evidence):
        self._e = list(evidence)

    def fresh_evidence(self, now=None):
        return list(self._e)

    def consolidation(self, now=None):
        return {"aggregate_uncertainty": 0.2}


def _lens(close, epoch=1000.0):
    return SimpleNamespace(
        symbol="EURUSD", domain=EvidenceDomain.STRUCTURE, source_module="market_lens.structure.m5",
        measurements={"source_kind": "market_lens", "source_timestamp_known": True,
                      "data_basis": "completed_ohlc", "completed": True, "provisional": False,
                      "timeframe": "H1", "current_close": close, "source_bar_epoch": epoch},
        confidence=0.5, uncertainty=0.5, evidence_id="e-%s" % close,
        relevance_horizon_seconds=1800.0)


def _assess(side, close, profit_r, geometry, extra=None):
    ctx = {"campaign_id": "c1", "original_thesis": "bullish CRT", "invalidation_conditions": ["close below sweep"]}
    if geometry is not None:
        ctx["concept_geometry"] = geometry
    position = SimpleNamespace(symbol="EURUSD", direction=side, campaign_id="c1", profit_r=profit_r)
    state = _State([_lens(close)] + list(extra or []))
    return NativePositionManager().assess(position, state, campaign_context=ctx, now=2000.0)


LONG_GEO = {"concept": "crt", "concept_timeframe": "H1", "milestones": [1.0820], "stop_price": 1.0795,
            "target_price": 1.0840, "entry_price": 1.0818, "symbol": "EURUSD",
            "direction": "LONG", "evidence_timestamp_epoch": 900.0}
SHORT_GEO = {"concept": "crt", "concept_timeframe": "H1", "milestones": [1.0820], "stop_price": 1.0845,
             "target_price": 1.0800, "entry_price": 1.0822, "symbol": "EURUSD",
             "direction": "SHORT", "evidence_timestamp_epoch": 900.0}


def test_long_milestone_passed_in_profit_scales_out_geometry_fraction():
    v = _assess("LONG", 1.0824, 0.4, LONG_GEO)
    assert v.action == "scale_out" and v.thesis_state == "milestone_delivered"
    assert v.management_plan["kind"] == "milestone_partial"
    assert v.management_plan["milestone_id"] == "crt:H1:1:1.082"
    assert v.management_plan["partial_fraction"] == 0.5
    assert "milestone 1/1 at 1.082" in v.reason and "crt" in v.reason


def test_short_mirror_scales_out_geometry_fraction():
    v = _assess("SHORT", 1.0816, 0.4, SHORT_GEO)
    assert v.action == "scale_out"
    assert v.management_plan["kind"] == "milestone_partial"
    assert v.management_plan["partial_fraction"] == 0.5


def test_before_the_milestone_the_position_is_simply_held():
    assert _assess("LONG", 1.0812, 0.2, LONG_GEO).action == "hold"


def test_not_in_profit_means_no_protection_signal():
    assert _assess("LONG", 1.0824, -0.1, LONG_GEO).action == "hold"
    assert _assess("LONG", 1.0824, None, LONG_GEO).action == "hold"


def test_positions_without_a_concept_are_unaffected():
    assert _assess("LONG", 1.0824, 0.4, None).action == "hold"


def test_stale_or_provisional_closes_are_ignored():
    prov = SimpleNamespace(
        symbol="EURUSD", domain=EvidenceDomain.STRUCTURE, source_module="market_lens.structure.m1",
        measurements={"source_kind": "market_lens", "source_timestamp_known": True, "provisional": True,
                      "current_close": 1.0900, "source_bar_epoch": 5000.0},
        confidence=0.5, uncertainty=0.5, evidence_id="p")
    # newest completed close is still below the milestone; the provisional print must not count
    v = _assess("LONG", 1.0812, 0.2, LONG_GEO, extra=[prov])
    assert v.action == "hold"
