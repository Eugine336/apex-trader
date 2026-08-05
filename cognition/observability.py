"""APEX TRADER — Cognition observability (Constitution Part XII).

Part XII: "nothing important occurs without observability." This module gives the
cognitive architecture a single, flat metrics view aggregated from the live
component status dicts (the Brain, the cognition loop, the entry/management
gates, calibration, memory). It computes the derived signals the rollout depends
on — decision throughput, veto/authorise rates, a reasoning-quality score from
calibration — so "reasoning quality is measurable" (Part XIII acceptance).

It is a read-only aggregator: it holds references to the components and reads
their ``get_status()`` (duck-typed, fail-safe), computing rolling rates from a
bounded sample history. It never reasons, never touches execution, and never
raises — an observability fault must never disturb the system it observes.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Optional


def _status(component: Any) -> dict:
    """Read a component's ``get_status()`` dict; empty on absence/fault."""
    if component is None:
        return {}
    try:
        st = component.get_status()
        return st if isinstance(st, dict) else {}
    except Exception:  # noqa: BLE001 — observing must never raise
        return {}


def _num(d: dict, key: str, default: float = 0.0) -> float:
    try:
        v = d.get(key, default)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


class CognitionObservability:
    """Aggregates cognition component status into a flat, derived metrics view.

    Holds references to the (optional) components and, on each :meth:`metrics`
    call, reads their statuses, records a timestamped throughput sample, and
    returns a snapshot including rolling ``decisions_per_min`` and a
    ``reasoning_quality_score`` derived from Brain calibration.
    """

    def __init__(
        self,
        *,
        brain: Optional[Any] = None,
        loop: Optional[Any] = None,
        gate: Optional[Any] = None,
        management_gate: Optional[Any] = None,
        calibration: Optional[Any] = None,
        memory: Optional[Any] = None,
        window_seconds: float = 300.0,
        history: int = 128,
        min_calibration_samples: int = 20,
    ) -> None:
        self._brain = brain
        self._loop = loop
        self._gate = gate
        self._management_gate = management_gate
        self._calibration = calibration
        self._memory = memory
        self.window_seconds = max(1.0, float(window_seconds))
        self.min_calibration_samples = max(1, int(min_calibration_samples))
        self._samples: deque = deque(maxlen=max(2, int(history)))  # (ts, decisions)
        self._lock = threading.Lock()

    # ── Throughput sampling ────────────────────────────────────────────────

    def _record(self, decisions: float, now: float) -> float:
        """Append a throughput sample and return decisions/min over the window."""
        with self._lock:
            self._samples.append((now, decisions))
            # Drop samples outside the rolling window (keep at least the oldest
            # in-window reference plus the newest).
            cutoff = now - self.window_seconds
            while len(self._samples) > 2 and self._samples[0][0] < cutoff:
                self._samples.popleft()
            if len(self._samples) < 2:
                return 0.0
            t0, d0 = self._samples[0]
            t1, d1 = self._samples[-1]
            elapsed = t1 - t0
            if elapsed <= 0:
                return 0.0
            return round(max(0.0, (d1 - d0)) / elapsed * 60.0, 4)

    # ── Metrics snapshot ────────────────────────────────────────────────────

    def metrics(self, now: Optional[float] = None) -> dict:
        """Flat, derived metrics view of the cognitive architecture. Fail-safe."""
        t = time.time() if now is None else float(now)
        brain = _status(self._brain)
        gate = _status(self._gate)
        mgate = _status(self._management_gate)
        cal = _status(self._calibration)
        mem = _status(self._memory)

        decisions = _num(brain, "decisions")
        decisions_per_min = self._record(decisions, t)

        evaluations = _num(gate, "evaluations")
        vetoed = _num(gate, "vetoed")
        would_veto = _num(gate, "would_veto")
        veto_rate = round(vetoed / evaluations, 4) if evaluations > 0 else 0.0
        would_veto_rate = round(would_veto / evaluations, 4) if evaluations > 0 else 0.0
        authorise_rate = round(1.0 - veto_rate, 4) if evaluations > 0 else 0.0

        cal_samples = int(_num(cal, "samples"))
        reliability_gap = _num(cal, "reliability_gap")
        # Reasoning quality: 1 − reliability gap, only once calibration is
        # statistically meaningful (else None — not yet measurable).
        reasoning_quality_score = (
            round(max(0.0, 1.0 - reliability_gap), 4)
            if cal_samples >= self.min_calibration_samples else None
        )

        return {
            "decisions_total": int(decisions),
            "decisions_per_min": decisions_per_min,
            "campaigns_opened": int(_num(brain, "campaigns_opened")),
            "observed": int(_num(brain, "observed")),
            "managed": int(_num(brain, "managed")),
            "brain_faults": int(_num(brain, "faults")),
            "brain_available": bool(brain.get("available", False)),
            "gate_mode": gate.get("mode", "unknown"),
            "authoritative": bool(gate.get("authoritative", False)),
            "gate_evaluations": int(evaluations),
            "veto_rate": veto_rate,
            "would_veto_rate": would_veto_rate,
            "authorise_rate": authorise_rate,
            "management_gate_mode": mgate.get("mode", "unknown"),
            "calibration_samples": cal_samples,
            "reliability_gap": round(reliability_gap, 4),
            "brier": round(_num(cal, "brier"), 4),
            "reasoning_quality_score": reasoning_quality_score,
            "reasoning_quality_measurable": reasoning_quality_score is not None,
            "memory_completed_campaigns": int(_num(mem, "completed_rows")),
            "orig_intended": int(_num(_status(self._loop), "orig_intended")),
            "orig_submitted": int(_num(_status(self._loop), "orig_submitted")),
        }

    def get_status(self) -> dict:
        return self.metrics()


__all__ = ["CognitionObservability"]
