from types import SimpleNamespace

from cognition.native_management import NativePositionManager


def _evidence(close, prior_range_low, *, epoch=1000.0, symbol="EURUSD", timeframe="H1"):
    return SimpleNamespace(
        symbol=symbol,
        domain=SimpleNamespace(value="structure"),
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "provisional": False,
            "completed": True,
            "timeframe": timeframe,
            "source_bar_epoch": epoch,
            "current_close": close,
            "prior_range_low": prior_range_low,
        },
    )


def test_milestone_plan_uses_geometry_count_for_dynamic_partial_and_trailing_level():
    context = {
        "concept_geometry": {
            "concept": "market_structure",
            "concept_timeframe": "H1",
            "entry_price": 100.0,
            "stop_price": 98.0,
            "target_price": 106.0,
            "milestones": [102.0, 104.0],
        },
        "completed_milestones": [],
        "pending_milestone_id": "",
    }
    position = SimpleNamespace(profit_r=0.8, sl=98.0, direction="LONG")
    plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [_evidence(100.5, 100.25, epoch=900.0), _evidence(103.0, 101.0, epoch=1000.0)], "EURUSD",
    )

    assert plan["kind"] == "milestone_partial"
    assert plan["milestone_index"] == 1
    assert plan["milestone_price"] == 102.0
    assert plan["partial_fraction"] == 1 / 3
    assert plan["stop_price"] == 101.0


def test_milestone_plan_waits_for_pending_broker_size_confirmation():
    context = {
        "concept_geometry": {
            "concept": "crt",
            "concept_timeframe": "H1",
            "entry_price": 100.0,
            "stop_price": 98.0,
            "target_price": 105.0,
            "milestones": [102.0],
        },
        "completed_milestones": [],
        "pending_milestone_id": "crt:H1:1:102",
    }
    position = SimpleNamespace(profit_r=0.8, sl=98.0, direction="LONG")
    assert NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [_evidence(103.0, 101.0)], "EURUSD",
    ) is None


def test_completed_milestone_advances_to_next_and_target_exits_remaining():
    context = {
        "concept_geometry": {
            "concept": "market_structure",
            "concept_timeframe": "H1",
            "entry_price": 100.0,
            "stop_price": 98.0,
            "target_price": 106.0,
            "milestones": [102.0, 104.0],
        },
        "completed_milestones": ["market_structure:H1:1:102"],
        "pending_milestone_id": "",
    }
    position = SimpleNamespace(profit_r=1.5, sl=101.0, direction="LONG")
    plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [_evidence(104.5, 103.0)], "EURUSD",
    )
    assert plan["kind"] == "milestone_partial"
    assert plan["milestone_index"] == 2
    assert plan["partial_fraction"] == 0.5

    target_plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [_evidence(106.1, 104.0, epoch=2000)], "EURUSD",
    )
    assert target_plan["kind"] == "objective_exit"
