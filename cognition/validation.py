"""APEX TRADER — Cognition validation harness (Constitution Part XIII).

Part XIII requires the Brain-driven path to be *validated* before it is trusted
in ``authoritative`` mode. This harness lets the one Brain be replayed offline
over a sequence of :class:`~cognition.contracts.MarketState` snapshots (synthetic
fixtures or reconstructed history) with **no broker and no execution** — it only
calls :meth:`brain.reason` and tallies the outcome distribution + reasoning
quality, producing a :class:`ValidationReport` with a go/no-go ``ready`` verdict.

Two entry points:

* :class:`ReplayHarness` — drive the Brain over market states, optionally with
  aligned realised outcomes, and get a report (decision-type distribution, open
  rate, mean confidence/uncertainty, fault count, calibration when outcomes are
  supplied, and a readiness verdict).
* :func:`readiness_verdict` — a pure gate over a live cognition status dict
  (Brain available, no fault storm, calibration meaningful and well-calibrated)
  that the controlled-rollout runbook consults before promoting a symbol group
  to ``authoritative``.

Pure standard library; fail-safe — a validation fault degrades to "not ready"
with a recorded reason, never an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from cognition.influence import CalibrationTracker

# Decision-type value for a campaign open (kept as a literal so this module does
# not need the enum; the Brain emits ``decision.decision_type.value``).
_OPEN = "open_campaign"


@dataclass
class ValidationReport:
    """The result of replaying the Brain over a batch of market states."""

    total: int = 0
    faults: int = 0
    by_decision_type: dict = field(default_factory=dict)
    open_rate: float = 0.0
    mean_confidence: float = 0.0
    mean_uncertainty: float = 0.0
    calibration: Optional[dict] = None
    ready: bool = False
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "faults": self.faults,
            "by_decision_type": dict(self.by_decision_type),
            "open_rate": round(self.open_rate, 4),
            "mean_confidence": round(self.mean_confidence, 4),
            "mean_uncertainty": round(self.mean_uncertainty, 4),
            "calibration": self.calibration,
            "ready": self.ready,
            "reasons": list(self.reasons),
        }


class ReplayHarness:
    """Replays the Brain over market states offline (no execution). Fail-safe."""

    def __init__(
        self,
        brain: Any,
        *,
        max_reliability_gap: float = 0.2,
        min_calibration_samples: int = 20,
    ) -> None:
        self._brain = brain
        self.max_reliability_gap = max(0.0, float(max_reliability_gap))
        self.min_calibration_samples = max(1, int(min_calibration_samples))

    def run(
        self,
        market_states: Any,
        *,
        outcomes: Optional[list] = None,
        now: Optional[float] = None,
    ) -> ValidationReport:
        """Drive the Brain over ``market_states``; return a ValidationReport.

        ``outcomes`` (optional) is a list of realised booleans aligned to the
        market states; when supplied, the Brain's per-state confidence is scored
        against them to measure calibration (reliability gap + Brier).
        """
        report = ValidationReport()
        states = list(market_states or [])
        report.total = len(states)
        if not states:
            report.reasons.append("no market states to replay")
            return report

        cal = CalibrationTracker()
        conf_sum = 0.0
        unc_sum = 0.0
        opens = 0
        scored = 0
        for i, ms in enumerate(states):
            try:
                output = self._brain.reason(ms, now=now)
                decision = getattr(output, "decision", None)
                dtype = getattr(getattr(decision, "decision_type", None), "value", None) \
                    or str(getattr(decision, "decision_type", "unknown"))
                report.by_decision_type[dtype] = report.by_decision_type.get(dtype, 0) + 1
                conf = float(getattr(decision, "confidence", 0.0) or 0.0)
                unc = float(getattr(decision, "uncertainty", 0.0) or 0.0)
                conf_sum += conf
                unc_sum += unc
                if dtype == _OPEN:
                    opens += 1
                if outcomes is not None and i < len(outcomes):
                    cal.observe(conf, bool(outcomes[i]))
                    scored += 1
            except Exception as exc:  # noqa: BLE001 — a replay fault is data, not a crash
                report.faults += 1
                report.reasons.append(f"reason() fault at index {i}: {exc}")

        n = report.total
        report.open_rate = opens / n if n else 0.0
        report.mean_confidence = conf_sum / n if n else 0.0
        report.mean_uncertainty = unc_sum / n if n else 0.0
        if scored > 0:
            report.calibration = cal.metrics()

        report.ready, report.reasons = self._verdict(report)
        return report

    def _verdict(self, report: ValidationReport) -> "tuple[bool, list]":
        reasons: list = list(report.reasons)
        ok = True
        if not bool(getattr(self._brain, "available", False)):
            ok = False
            reasons.append("brain not available (no reasoner) — cannot trust authoritative")
        if report.total == 0:
            ok = False
            reasons.append("no decisions produced")
        if report.faults > 0:
            ok = False
            reasons.append(f"{report.faults} reasoning fault(s) during replay")
        cal = report.calibration
        if cal is not None and int(cal.get("samples", 0)) >= self.min_calibration_samples:
            gap = float(cal.get("reliability_gap", 1.0))
            if gap > self.max_reliability_gap:
                ok = False
                reasons.append(
                    f"calibration reliability gap {gap:.3f} exceeds "
                    f"{self.max_reliability_gap:.3f}")
        if ok and not reasons:
            reasons.append("replay clean — Brain available, no faults, calibration acceptable")
        return ok, reasons


def readiness_verdict(
    cognition_status: dict,
    *,
    min_calibration_samples: int = 20,
    max_reliability_gap: float = 0.2,
    max_brain_faults: int = 0,
) -> "tuple[bool, list]":
    """Pure go/no-go gate over a live cognition status dict (rollout use).

    Consulted by the controlled-rollout runbook before promoting a symbol group
    to ``authoritative``: the Brain must be available, fault-free, and — once
    calibration is statistically meaningful — well-calibrated. Returns
    ``(ready, reasons)``. Never raises.
    """
    reasons: list = []
    ok = True
    try:
        st = cognition_status or {}
        brain = st.get("brain", {}) if isinstance(st.get("brain"), dict) else {}
        cal = st.get("calibration", {}) if isinstance(st.get("calibration"), dict) else {}
        if not bool(brain.get("available", False)):
            ok = False
            reasons.append("brain not available")
        faults = int(brain.get("faults", 0) or 0)
        if faults > max_brain_faults:
            ok = False
            reasons.append(f"brain faults {faults} exceed {max_brain_faults}")
        samples = int(cal.get("samples", 0) or 0)
        if samples < min_calibration_samples:
            ok = False
            reasons.append(
                f"calibration samples {samples} below floor {min_calibration_samples} "
                "— reasoning quality not yet measurable")
        else:
            gap = float(cal.get("reliability_gap", 1.0) or 1.0)
            if gap > max_reliability_gap:
                ok = False
                reasons.append(
                    f"reliability gap {gap:.3f} exceeds {max_reliability_gap:.3f}")
        if ok:
            reasons.append("ready — brain available, fault-free, calibration acceptable")
    except Exception as exc:  # noqa: BLE001
        return False, [f"readiness check fault: {exc}"]
    return ok, reasons


__all__ = ["ValidationReport", "ReplayHarness", "readiness_verdict"]
