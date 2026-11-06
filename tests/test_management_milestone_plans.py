from types import SimpleNamespace

from cognition.native_management import NativePositionManager


def _evidence(close, prior_range_low, *, prior_range_high=None, epoch=1000.0, symbol="EURUSD", timeframe="H1"):
    return SimpleNamespace(
        symbol=symbol,
        domain=SimpleNamespace(value="structure"),
        relevance_horizon_seconds=900.0,
        evidence_id=f"evidence-{epoch}",
        measurements={
            "source_kind": "market_lens",
            "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "provisional": False,
            "timeframe": timeframe,
            "source_bar_epoch": epoch,
            "current_close": close,
            "prior_range_low": prior_range_low,
            "prior_range_high": prior_range_high,
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
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "LONG",
        },
        "completed_milestones": [],
        "pending_milestone_id": "",
    }
    position = SimpleNamespace(profit_r=0.8, sl=98.0, direction="LONG")
    plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [
            _evidence(100.5, 100.25, epoch=900.0),
            _evidence(103.0, 101.0, epoch=1000.0),
            _evidence(104.0, None, epoch=1100.0),
        ], "EURUSD", now=1100.0,
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
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "LONG",
        },
        "completed_milestones": [],
        "pending_milestone_id": "crt:H1:1:102",
    }
    position = SimpleNamespace(profit_r=0.8, sl=98.0, direction="LONG")
    assert NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [_evidence(103.0, 101.0)], "EURUSD", now=1000.0,
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
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "LONG",
        },
        "completed_milestones": ["market_structure:H1:1:102"],
        "pending_milestone_id": "",
    }
    position = SimpleNamespace(profit_r=1.5, sl=101.0, direction="LONG")
    plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [_evidence(104.5, 103.0)], "EURUSD", now=1100.0,
    )
    assert plan["kind"] == "milestone_partial"
    assert plan["milestone_index"] == 2
    assert plan["partial_fraction"] == 0.5

    target_plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [_evidence(106.1, 104.0, epoch=2000)], "EURUSD", now=2000.0,
    )
    assert target_plan["kind"] == "objective_exit"



def test_completed_close_beyond_original_stop_invalidates_campaign():
    context = {
        "campaign_id": "campaign-1",
        "concept_geometry": {
            "concept": "market_structure",
            "concept_timeframe": "H1",
            "entry_price": 100.0,
            "stop_price": 98.0,
            "target_price": 106.0,
            "milestones": [102.0, 104.0],
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "LONG",
        },
    }
    plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", SimpleNamespace(profit_r=-1.0, sl=98.0),
        [_evidence(97.9, 97.0, epoch=1000.0)], "EURUSD", now=1000.0,
    )

    assert plan["kind"] == "invalidation_exit"
    assert plan["invalidation_price"] == 98.0
    assert plan["action_id"].startswith("campaign-1:concept-invalidation:")
    assert plan["evidence_ids"] == ["evidence-1000.0"]


def test_pending_partial_does_not_suppress_completed_close_invalidation():
    context = {
        "campaign_id": "campaign-1",
        "pending_milestone_id": "market_structure:H1:1:102",
        "concept_geometry": {
            "concept": "market_structure",
            "concept_timeframe": "H1",
            "stop_price": 98.0,
            "target_price": 106.0,
            "milestones": [102.0],
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "LONG",
        },
    }
    plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", SimpleNamespace(profit_r=0.8, sl=98.0),
        [_evidence(97.0, 96.0, epoch=1000.0)], "EURUSD", now=1000.0,
    )
    assert plan is not None
    assert plan["kind"] == "invalidation_exit"


def test_stale_completed_bar_cannot_trigger_management_action():
    context = {
        "campaign_id": "campaign-1",
        "concept_geometry": {
            "concept": "market_structure",
            "concept_timeframe": "H1",
            "stop_price": 98.0,
            "target_price": 106.0,
            "milestones": [102.0],
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "LONG",
        },
    }
    plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", SimpleNamespace(profit_r=0.8, sl=98.0),
        [_evidence(107.0, 106.0, epoch=1000.0)], "EURUSD", now=3000.0,
    )
    assert plan is None


def test_conflicting_closes_for_same_completed_bar_fail_closed():
    context = {
        "campaign_id": "campaign-1",
        "concept_geometry": {
            "concept": "market_structure",
            "concept_timeframe": "H1",
            "stop_price": 98.0,
            "target_price": 106.0,
            "milestones": [102.0],
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "LONG",
        },
    }
    plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", SimpleNamespace(profit_r=0.8, sl=98.0),
        [
            _evidence(97.0, 96.0, epoch=1000.0),
            _evidence(99.0, 98.0, epoch=1000.0),
        ], "EURUSD", now=1000.0,
    )
    assert plan is None


def test_new_completed_higher_low_trails_without_waiting_for_milestone():
    context = {
        "campaign_id": "campaign-1",
        "concept_geometry": {
            "concept": "market_structure",
            "concept_timeframe": "H1",
            "stop_price": 98.0,
            "target_price": 106.0,
            "milestones": [102.0, 104.0],
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "LONG",
        },
    }
    plan = NativePositionManager._concept_milestone_guidance(
        context, "LONG", SimpleNamespace(profit_r=0.2, sl=98.0),
        [
            _evidence(100.5, 100.25, epoch=900.0),
            _evidence(101.5, 101.0, epoch=1000.0),
        ], "EURUSD", now=1000.0,
    )

    assert plan is not None
    assert plan["kind"] == "trailing_stop_update"
    assert plan["stop_price"] == 100.25
    assert plan["action_id"].startswith("campaign-1:concept-trail:")



def test_short_completed_close_beyond_original_stop_invalidates_symmetrically():
    context = {
        "campaign_id": "campaign-short",
        "concept_geometry": {
            "concept": "market_structure",
            "concept_timeframe": "H1",
            "entry_price": 100.0,
            "stop_price": 102.0,
            "target_price": 94.0,
            "milestones": [98.0, 96.0],
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "SHORT",
        },
    }
    plan = NativePositionManager._concept_milestone_guidance(
        context, "SHORT", SimpleNamespace(profit_r=-1.0, sl=102.0),
        [_evidence(102.1, None, prior_range_high=103.0, epoch=1000.0)],
        "EURUSD", now=1000.0,
    )

    assert plan["kind"] == "invalidation_exit"
    assert plan["invalidation_price"] == 102.0
    assert plan["action_id"].startswith("campaign-short:concept-invalidation:")



def test_invalidation_action_id_is_stable_across_repeated_completed_bars():
    context = {
        "campaign_id": "campaign-1",
        "concept_geometry": {
            "concept": "market_structure",
            "concept_timeframe": "H1",
            "stop_price": 98.0,
            "target_price": 106.0,
            "milestones": [102.0],
            "evidence_timestamp_epoch": 800.0,
            "symbol": "EURUSD",
            "direction": "LONG",
        },
    }
    position = SimpleNamespace(profit_r=-1.0, sl=98.0)
    first = NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [_evidence(97.0, 96.0, epoch=1000.0)],
        "EURUSD", now=1000.0,
    )
    second = NativePositionManager._concept_milestone_guidance(
        context, "LONG", position, [_evidence(96.5, 96.0, epoch=1100.0)],
        "EURUSD", now=1100.0,
    )

    assert first["kind"] == second["kind"] == "invalidation_exit"
    assert first["action_id"] == second["action_id"]
