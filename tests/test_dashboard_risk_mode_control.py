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
