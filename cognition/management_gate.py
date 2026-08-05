"""APEX TRADER — Management gate (Phase F: Brain authority over open campaigns).

Constitution Parts V, VI.4/5: the single Brain — not the legacy management
engine — governs whether an OPEN campaign adds exposure. This gate applies the
same authority model as the entry gate, but ONLY to *exposure-adding* actions
(scale-in, re-entry, add). **De-risking actions (exit, scale-out, tighten,
protective stop) are NEVER gated** — the Brain can request more caution but must
never be able to block the system from reducing risk (that would fight the
deterministic safety floor, Part X).

So: adding risk requires a fresh, aligned Brain authorization (reusing the entry
gate's alignment check on the Brain's latest read); removing risk always
proceeds. Fail-open in the soft modes, fail-closed under ``authoritative`` for
adds (no reasoning ⇒ no new exposure).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from cognition.gate import MODE_SHADOW, CognitionGate, GateVerdict

logger = logging.getLogger("apex.cognition.management_gate")

# Actions that ADD exposure — gated by the Brain.
_RISK_ADDING = frozenset({
    "scale_in", "scale-in", "re_entry", "re-entry", "reentry", "add", "increase", "open",
})
# Actions that REDUCE or hold exposure — never gated.
_RISK_REDUCING = frozenset({
    "exit", "exit_full", "exit_partial", "scale_out", "scale-out", "tighten",
    "tighten_risk", "protect", "protect_profit", "close", "stop", "reduce",
    "trail", "partial_close", "hold",
})


def classify_action(action: Any) -> str:
    """Return 'add' (exposure-adding), 'reduce' (de-risking/hold), or 'other'."""
    a = str(action or "").strip().lower()
    if a in _RISK_ADDING:
        return "add"
    if a in _RISK_REDUCING:
        return "reduce"
    # Unknown actions are treated conservatively as de-risking/neutral so the
    # gate never blocks something it doesn't recognise as adding risk.
    return "other"


class ManagementGate:
    """Governs exposure-adding management actions via the single Brain. Fail-safe."""

    def __init__(self, brain: Optional[Any] = None, *, mode: str = MODE_SHADOW,
                 max_decision_age_seconds: float = 300.0) -> None:
        # Reuse the entry gate's exact alignment + fail philosophy for adds.
        self._add_gate = CognitionGate(
            brain, mode=mode, max_decision_age_seconds=max_decision_age_seconds)
        self.mode = self._add_gate.mode
        self._add_evaluations = 0
        self._add_blocked = 0

    def evaluate(self, symbol: str, direction: str, action: Any,
                 now: Optional[float] = None) -> GateVerdict:
        kind = classify_action(action)
        if kind != "add":
            return GateVerdict(True, self.mode, False, None,
                               f"'{action}' is de-risking/neutral — always allowed")
        verdict = self._add_gate.evaluate(symbol, direction, now)
        self._add_evaluations += 1
        if not verdict.allow:
            self._add_blocked += 1
            logger.info(
                "[management-gate] %s add BLOCKED %s %s — Brain does not authorise "
                "more exposure: %s", self.mode, symbol, direction, verdict.reason,
            )
        return verdict

    def get_status(self) -> dict:
        return {
            "mode": self.mode,
            "add_evaluations": self._add_evaluations,
            "add_blocked": self._add_blocked,
            "add_gate": self._add_gate.get_status(),
        }


__all__ = ["ManagementGate", "classify_action"]
