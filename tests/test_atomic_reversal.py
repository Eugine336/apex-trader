"""Regression tests for legacy reversal components under the Apex identity.

The pure ``ReversalManager`` can still be tested as a utility, but the runtime
must not use it to authorize an opposite-side entry. Such an entry must be a new
Brain-originated opportunity and go through the shared opportunity, cost, and
whole-book admission path. The old ``_dispatch_pending_reversal`` execution
hook is retired; a management reverse closes the held side and triggers fresh
Brain reasoning rather than opening the opposite side directly.
"""

from __future__ import annotations

import types

import pytest

import conftest  # noqa: F401 — installs the MetaTrader5 stub for the bootstrap import

from config import ThesisConfig
from management.exit_cause import ExitCause
from management.reversal_manager import LONG, SHORT, ReversalDecision, ReversalManager


# ── Helpers ──────────────────────────────────────────────────────────────

class _Clock:
    """A monotonic clock we can advance by hand for deterministic cooldowns."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


def _rm(**kw) -> ReversalManager:
    params = dict(
        enabled=True, min_thesis_ev=0.1, cooldown_seconds=300.0,
        max_reversals_per_session=3, threshold_escalation=0.5,
    )
    params.update(kw)
    return ReversalManager(**params)


# ── A. ReversalManager — the pure anti-ping-pong decision ──────────────────

def test_basic_reversal_permitted():
    rm = _rm()
    d = rm.evaluate("EURUSD", 0.5)
    assert isinstance(d, ReversalDecision)
    assert d.reverse is True
    assert d.required_threshold == pytest.approx(0.1)
    assert d.reversals_so_far == 0


def test_reversal_below_min_edge_rejected():
    # FLAT / weak competing thesis: edge under the min-EV bar → no reversal.
    rm = _rm()
    d = rm.evaluate("EURUSD", 0.05)
    assert d.reverse is False
    assert d.competing_ev == pytest.approx(0.05)


def test_disabled_never_reverses():
    rm = _rm(enabled=False)
    d = rm.evaluate("EURUSD", 5.0)
    assert d.reverse is False
    assert d.reason == "disabled"


def test_zero_cap_disables_reversals():
    rm = _rm(max_reversals_per_session=0)
    assert rm.evaluate("EURUSD", 5.0).reverse is False


def test_cooldown_blocks_second_reversal():
    clock = _Clock()
    rm = _rm(cooldown_seconds=300.0, clock=clock)
    assert rm.evaluate("EURUSD", 0.5).reverse is True
    rm.record_reversal("EURUSD")
    # Immediately after: cooldown active (and escalated bar) → blocked.
    clock.advance(100.0)
    d = rm.evaluate("EURUSD", 0.5)
    assert d.reverse is False
    assert "cooldown" in d.reason
    # After the cooldown elapses, a strong-enough edge reverses again.
    clock.advance(250.0)  # total 350s > 300s cooldown
    assert rm.evaluate("EURUSD", 0.5).reverse is True


def test_per_session_cap_enforced():
    clock = _Clock()
    rm = _rm(max_reversals_per_session=2, cooldown_seconds=0.0, clock=clock)
    assert rm.evaluate("EURUSD", 5.0).reverse is True
    rm.record_reversal("EURUSD")
    assert rm.evaluate("EURUSD", 5.0).reverse is True
    rm.record_reversal("EURUSD")
    # Third attempt hits the per-session cap regardless of edge.
    d = rm.evaluate("EURUSD", 5.0)
    assert d.reverse is False
    assert "cap" in d.reason
    assert rm.reversals_this_session("EURUSD") == 2


def test_escalating_threshold_grows_each_reversal():
    clock = _Clock()
    rm = _rm(min_thesis_ev=0.1, threshold_escalation=0.5, cooldown_seconds=0.0, clock=clock)
    # 1st reversal bar = 0.1 * (1 + 0.5*0) = 0.10.
    assert rm.evaluate("EURUSD", 0.12).required_threshold == pytest.approx(0.10)
    rm.record_reversal("EURUSD")
    # 2nd reversal bar = 0.1 * (1 + 0.5*1) = 0.15 → 0.12 no longer clears it.
    d = rm.evaluate("EURUSD", 0.12)
    assert d.required_threshold == pytest.approx(0.15)
    assert d.reverse is False
    # A bigger edge clears the raised bar.
    assert rm.evaluate("EURUSD", 0.2).reverse is True


def test_per_symbol_isolation():
    clock = _Clock()
    rm = _rm(cooldown_seconds=300.0, clock=clock)
    rm.record_reversal("EURUSD")
    # GBPUSD has its own counters — unaffected by EURUSD's cooldown.
    assert rm.evaluate("GBPUSD", 0.5).reverse is True


def test_reset_session_clears_counts_and_cooldown():
    clock = _Clock()
    rm = _rm(cooldown_seconds=300.0, clock=clock)
    rm.record_reversal("EURUSD")
    assert rm.reversals_this_session("EURUSD") == 1
    rm.reset_session()
    assert rm.reversals_this_session("EURUSD") == 0
    # Cooldown cleared too → an immediate reversal is permitted again.
    assert rm.evaluate("EURUSD", 0.5).reverse is True


def test_evaluate_is_fail_safe_on_bad_clock():
    def _boom() -> float:
        raise RuntimeError("clock exploded")

    rm = _rm(clock=_boom)
    d = rm.evaluate("EURUSD", 5.0)
    assert d.reverse is False  # never fail INTO a reversal
    assert d.reason == "eval_error"


def test_evaluate_coerces_nan_edge():
    rm = _rm()
    d = rm.evaluate("EURUSD", float("nan"))
    assert d.reverse is False
    assert d.competing_ev == 0.0


def test_stats_reports_totals():
    clock = _Clock()
    rm = _rm(cooldown_seconds=0.0, clock=clock)
    rm.record_reversal("EURUSD")
    rm.record_reversal("GBPUSD")
    s = rm.stats()
    assert s["total_reversals"] == 2
    assert s["session_counts"] == {"EURUSD": 1, "GBPUSD": 1}
    assert s["enabled"] is True


# ── B. ExitCause taxonomy ──────────────────────────────────────────────────

def test_thesis_reversal_cause_exists():
    assert ExitCause.THESIS_REVERSAL.value == "thesis_reversal"


def test_from_reason_maps_reversal_before_flip():
    # A reversal reason (which also mentions the competing/flip context) must map
    # to THESIS_REVERSAL, not the plain THESIS_FLIP.
    assert ExitCause.from_reason("reversal→SHORT: competing SHORT thesis dominant") is ExitCause.THESIS_REVERSAL
    assert ExitCause.from_reason("reverse to short") is ExitCause.THESIS_REVERSAL
    # A plain flip (no reversal wording) still maps to THESIS_FLIP.
    assert ExitCause.from_reason("competing SHORT thesis dominant") is ExitCause.THESIS_FLIP


# ── C. ThesisConfig reversal defaults + validation ─────────────────────────

def test_reversal_config_defaults():
    c = ThesisConfig()
    assert c.atomic_reversal_enabled is False
    assert c.reversal_min_thesis_ev == 0.1
    assert c.reversal_cooldown == 300.0
    assert c.max_reversals_per_session == 3
    assert c.reversal_threshold_escalation == 0.5
    assert c.reversal_max_age_seconds == 60.0


@pytest.mark.parametrize("kw", [
    {"reversal_min_thesis_ev": -0.1},
    {"reversal_cooldown": -1.0},
    {"max_reversals_per_session": -1},
    {"reversal_threshold_escalation": -0.5},
    {"reversal_max_age_seconds": -5.0},
])
def test_reversal_config_validation_rejects_negative(kw):
    with pytest.raises(ValueError):
        ThesisConfig(**kw)


# ── D. PositionEvaluator._maybe_arm_reversal (arm-at-flip hook) ─────────────

def _arm_stub(rm):
    """A minimal stand-in exposing what ``_maybe_arm_reversal`` reads/writes."""
    import event_driven_bootstrap as edb

    stub = types.SimpleNamespace(
        _ctx=types.SimpleNamespace(reversal_manager=rm),
        _pending_reversals={},
    )
    stub._maybe_arm_reversal = types.MethodType(
        edb.PositionEvaluator._maybe_arm_reversal, stub,
    )
    return stub


def test_maybe_arm_reversal_arms_when_gate_permits():
    rm = _rm()
    stub = _arm_stub(rm)
    to_dir = stub._maybe_arm_reversal(
        "T1", "EURUSD", "LONG", {"opp_over_flat": 0.5},
    )
    assert to_dir == "SHORT"
    assert "T1" in stub._pending_reversals
    plan = stub._pending_reversals["T1"]
    assert plan["from_direction"] == "LONG"
    assert plan["to_direction"] == "SHORT"
    assert plan["competing_ev"] == pytest.approx(0.5)


def test_maybe_arm_reversal_short_position_reverses_to_long():
    rm = _rm()
    stub = _arm_stub(rm)
    assert stub._maybe_arm_reversal("T2", "USDJPY", "SHORT", {"opp_over_flat": 0.5}) == "LONG"


def test_maybe_arm_reversal_gate_rejects_no_arm():
    # Competing edge under the min-EV bar → no reversal, plain evidence exit.
    rm = _rm()
    stub = _arm_stub(rm)
    assert stub._maybe_arm_reversal("T3", "EURUSD", "LONG", {"opp_over_flat": 0.02}) is None
    assert stub._pending_reversals == {}


def test_maybe_arm_reversal_disabled_manager_no_arm():
    rm = _rm(enabled=False)
    stub = _arm_stub(rm)
    assert stub._maybe_arm_reversal("T4", "EURUSD", "LONG", {"opp_over_flat": 5.0}) is None
    assert stub._pending_reversals == {}


def test_maybe_arm_reversal_no_manager_no_arm():
    stub = _arm_stub(None)
    assert stub._maybe_arm_reversal("T5", "EURUSD", "LONG", {"opp_over_flat": 5.0}) is None


def test_maybe_arm_reversal_does_not_count_until_dispatch():
    # Arming alone must NOT consume the anti-ping-pong budget — a reversal is
    # only counted when it actually executes.
    rm = _rm()
    stub = _arm_stub(rm)
    stub._maybe_arm_reversal("T6", "EURUSD", "LONG", {"opp_over_flat": 0.5})
    assert rm.reversals_this_session("EURUSD") == 0


# ── E. The legacy direct reverse-open route stays retired ───────────────────


def test_legacy_reverse_dispatcher_is_not_part_of_event_runtime():
    import event_driven_bootstrap as edb

    assert not hasattr(edb.EventDrivenSystem, "_dispatch_pending_reversal")


def test_default_thesis_config_does_not_arm_atomic_reversal():
    assert ThesisConfig().atomic_reversal_enabled is False
