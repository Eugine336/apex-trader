"""Dashboard risk-mode control may tighten (FREEZE) but never loosen."""

import types

from dashboard.state_controls import ControlsMixin


def _controls(mode="NORMAL"):
    from brain.drawdown_guard import DrawdownMode

    guard = types.SimpleNamespace(mode=DrawdownMode(mode))
    ed = types.SimpleNamespace(_ctx=types.SimpleNamespace(drawdown_guard=guard))
    cls = ControlsMixin
    obj = cls.__new__(cls)
    obj._event_driven_system = ed
    return obj, guard


def test_freeze_is_applied_to_the_attribute_the_guard_reads():
    obj, guard = _controls("NORMAL")
    out = obj.set_risk_mode("frozen")
    assert out["status"] == "ok"
    assert guard.mode.value == "FROZEN"


def test_loosening_is_rejected_and_does_not_change_state():
    obj, guard = _controls("FROZEN")
    for requested in ("NORMAL", "CAUTION", "RECOVERY"):
        out = obj.set_risk_mode(requested)
        assert out["status"] == "rejected"
        assert guard.mode.value == "FROZEN"


def test_dashboard_pause_is_durable_and_audited_across_state_recreation(tmp_path):
    from dashboard.control_store import DashboardControlStore
    from dashboard.state import LiveState

    path = tmp_path / "dashboard-controls.db"
    store = DashboardControlStore(path)
    state = LiveState(control_store=store)
    event_system = types.SimpleNamespace(_ctx=None, _paused=False)
    state.set_event_driven_system(event_system)

    paused = state.pause_trading()
    assert paused["status"] == "paused"
    assert event_system._paused is True

    restarted_state = LiveState(control_store=DashboardControlStore(path))
    start_observed_pause = []
    restarted_system = types.SimpleNamespace(
        _ctx=None, _paused=False,
        start=lambda: start_observed_pause.append(True),
    )
    restarted_state.start_event_driven_system(restarted_system)
    assert restarted_system._paused is True
    assert start_observed_pause == [True]

    resumed = restarted_state.resume_trading()
    assert resumed["status"] == "resumed"
    assert restarted_system._paused is False
    assert DashboardControlStore(path).get("entries_paused") is False
    assert [row["action"] for row in DashboardControlStore(path).list_recent(2)] == [
        "resume_entries", "pause_entries",
    ]


def test_start_control_does_not_claim_to_restart_a_stopped_engine():
    obj = ControlsMixin.__new__(ControlsMixin)
    obj._event_driven_system = types.SimpleNamespace(is_running=False)
    result = obj.start_trading()
    assert result["status"] == "restart_required"


def test_durable_dashboard_pause_blocks_brain_origination_before_shared_executor():
    from event_driven_bootstrap import EventDrivenSystem

    submitted = []
    engine = types.SimpleNamespace(
        _paused=True,
        _aggregator=types.SimpleNamespace(submit=lambda intents: submitted.extend(intents)),
    )
    sink = EventDrivenSystem._make_origination_sink(engine)
    result = sink(types.SimpleNamespace(symbol="EURUSD", direction="LONG"))
    assert result is False
    assert submitted == []


def test_durable_control_restore_clears_stale_pause_and_fails_closed_on_missing_freeze_guard(tmp_path):
    from dashboard.control_store import DashboardControlStore

    store = DashboardControlStore(tmp_path / "controls.db")
    store.set("entries_paused", False, action="resume_entries")
    unpaused = types.SimpleNamespace(_paused=True, _ctx=None)
    store.apply_to(unpaused)
    assert unpaused._paused is False

    store.set("manual_risk_mode", "FROZEN", action="set_risk_mode_frozen")
    missing_guard = types.SimpleNamespace(_paused=False, _ctx=None)
    store.apply_to(missing_guard)
    assert missing_guard._paused is True
