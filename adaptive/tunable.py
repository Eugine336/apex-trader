"""
APEX TRADER — Tunable Protocol

The shared contract every auto-tuning component implements so the central
``TunerAgent`` can schedule, order, validate, audit, and (if needed) roll back
its tuning — without knowing anything about the component's internals.

Today the learners tune themselves on scattered, independent triggers
(AdaptiveOptimizer on 50 trades / 7d, GateTuner on a 6h timer, the planner
Calibrator on its own 50-trade threshold, signal grading per scan cycle).
Nothing coordinates ordering (EVEstimator should see fresh PairLearner data;
the gate tuner tunes the gate the EV estimate feeds) and nothing keeps a
unified audit trail. This protocol is the seam that lets one agent own all of
that.

Leaf module — dataclasses + typing only. No learning-layer imports, so any
component can depend on it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


class TuneFrequency(Enum):
    """When a tunable wants to be considered for tuning."""

    ON_TRADE_CLOSE = "on_trade_close"    # after every trade closes
    ON_TRADE_BATCH = "on_trade_batch"    # after N trades (min_trades cadence)
    PERIODIC = "periodic"                 # time-based interval (min_interval)
    ON_DEMAND = "on_demand"              # only when explicitly forced
    PER_SCAN_CYCLE = "per_scan_cycle"    # every scan loop iteration


@dataclass
class TuneContext:
    """The state handed to every tunable when checking / executing tuning.

    Carries only generic scheduling signals — the actual training data each
    tunable needs is bound into the tunable at construction (a data-provider
    callable), keeping this context small and the agent component-agnostic.
    """

    total_trades: int = 0
    trades_since_last_tune: int = 0
    seconds_since_last_tune: float = 0.0
    current_prices: dict = field(default_factory=dict)  # pair -> price (signal grading)
    force: bool = False   # bypass should_tune gating


@dataclass
class TuneResult:
    """The outcome of a single tune attempt (one row in the audit log)."""

    tunable_name: str
    success: bool
    params_before: dict = field(default_factory=dict)
    params_after: dict = field(default_factory=dict)
    reason: str = ""
    duration_ms: float = 0.0
    error: Optional[str] = None
    rollback_performed: bool = False
    skipped: bool = False     # should_tune returned False (not an error)
    changed: bool = False     # params actually moved

    def to_row(self) -> dict:
        """Flatten to a JSON-serialisable audit row."""
        import json

        return {
            "tunable_name": self.tunable_name,
            "success": 1 if self.success else 0,
            "params_before": json.dumps(self.params_before, default=str),
            "params_after": json.dumps(self.params_after, default=str),
            "reason": self.reason,
            "duration_ms": round(float(self.duration_ms), 3),
            "error": self.error,
            "rollback_performed": 1 if self.rollback_performed else 0,
            "skipped": 1 if self.skipped else 0,
            "changed": 1 if self.changed else 0,
        }


@runtime_checkable
class Tunable(Protocol):
    """Every component that wants to be auto-tuned implements this protocol.

    Implementations MUST be exception-safe and idempotent: ``tune`` may be
    retried, and a raised exception is caught by the agent (which then rolls
    back). ``get_current_params`` / ``validate_params`` must never raise.
    """

    @property
    def tunable_name(self) -> str: ...

    @property
    def frequency(self) -> TuneFrequency: ...

    @property
    def dependencies(self) -> list[str]:
        """Names of other Tunables that must run (successfully) before this one."""
        ...

    @property
    def min_trades_required(self) -> int:
        """Minimum total trades before this tuner produces meaningful output."""
        ...

    @property
    def min_interval_seconds(self) -> float:
        """Minimum seconds between successive tune runs."""
        ...

    def should_tune(self, ctx: TuneContext) -> bool:
        """Whether this component should be tuned right now."""
        ...

    def get_current_params(self) -> dict:
        """Snapshot the current tuned parameter values (never raises)."""
        ...

    def tune(self, ctx: TuneContext) -> TuneResult:
        """Execute the tuning. Idempotent and exception-safe."""
        ...

    def validate_params(self, params: dict) -> tuple[bool, str]:
        """Validate params are within safe bounds. Returns ``(ok, reason)``."""
        ...

    def rollback(self) -> bool:
        """Revert to the previously snapshotted params. Returns success."""
        ...


class TuningGuardMixin:
    """Lets a self-tuning component defer to a central ``TunerAgent``.

    Once an agent is attached (``set_tuner_agent``) and that agent declares
    itself the sole authority, any *direct* call to the component's own
    tune / calibrate / retrain entry point is blocked and logged as a bypass
    attempt — UNLESS the agent is the one currently driving the tune (it
    authorises its own delegated calls). When no agent is attached the
    component behaves exactly as before, so this is backward-compatible.

    The mixin only duck-types against the agent (``is_sole_authority``,
    ``is_authorizing``, ``log_bypass_attempt``) so it never imports the agent
    module — keeping this a leaf with no learning-layer dependency.
    """

    _tuner_agent = None  # class default; set per-instance via set_tuner_agent

    def set_tuner_agent(self, agent) -> None:
        """Attach (or clear, with ``None``) the central tuner agent."""
        self._tuner_agent = agent

    def _tuning_blocked(self, method: str) -> bool:
        """Whether a direct call to ``method`` should be blocked right now.

        Returns ``True`` (and records a bypass attempt) only when an agent is
        attached, it is the sole authority, and it is NOT currently authorising
        this call. Never raises — a faulty agent must not break the component.
        """
        agent = getattr(self, "_tuner_agent", None)
        if agent is None:
            return False
        try:
            if not getattr(agent, "is_sole_authority", False):
                return False
            if agent.is_authorizing():
                return False
            agent.log_bypass_attempt(type(self).__name__, method)
            return True
        except Exception:  # noqa: BLE001 — never let agent faults break tuning
            return False


__all__ = [
    "TuneFrequency",
    "TuneContext",
    "TuneResult",
    "Tunable",
    "TuningGuardMixin",
]
