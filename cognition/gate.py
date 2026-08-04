"""APEX TRADER — Cognition entry gate (Brain authority on the decision path).

Step C of the constitutional migration: the single AI Cognitive Brain becomes an
authority on whether an entry proceeds. This gate consults the Brain's latest
:class:`~cognition.brain.BrainOutput` for a symbol and decides whether a
legacy-proposed entry in ``direction`` is backed by the Brain.

It is deliberately a **one-way** authority — it can only make the system *more*
conservative; it never originates a trade (that is Step D). Modes:

* ``off``    — disabled; always allow (no evaluation).
* ``shadow`` — evaluate and record what it *would* do, but always allow. This is
  the safe default: it proves the Brain's judgement against the live path with
  zero behavioural change.
* ``veto``   — block an entry when the Brain has a fresh read that does NOT back
  this direction (dominant thesis is FLAT/opposite, or below its bar).

Fail-open everywhere: no Brain, no read yet (cold start), a stale read, or any
fault ALLOWS the entry — the gate never blocks on absent evidence or a bug,
exactly like the ThesisEngine gate it sits beside.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("apex.cognition.gate")

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_VETO = "veto"
_VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_VETO)


@dataclass
class GateVerdict:
    allow: bool
    mode: str
    would_veto: bool
    aligned: Optional[bool]
    reason: str
    brain_decision_type: str = ""
    brain_direction: str = ""

    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "mode": self.mode,
            "would_veto": self.would_veto,
            "aligned": self.aligned,
            "reason": self.reason,
            "brain_decision_type": self.brain_decision_type,
            "brain_direction": self.brain_direction,
        }


def normalise_mode(value: Any, default: str = MODE_SHADOW) -> str:
    m = str(value or "").strip().lower()
    return m if m in _VALID_MODES else default


class CognitionGate:
    """Consults the Brain's latest read to allow/deny a proposed entry. Fail-open."""

    def __init__(self, brain: Optional[Any] = None, *, mode: str = MODE_SHADOW) -> None:
        self._brain = brain
        self.mode = normalise_mode(mode)
        self._evaluations = 0
        self._would_veto = 0
        self._vetoed = 0
        self._lock = threading.Lock()

    def evaluate(self, symbol: str, direction: str, now: Optional[float] = None) -> GateVerdict:
        if self.mode == MODE_OFF or self._brain is None:
            return GateVerdict(True, self.mode, False, None, "gate off / no brain")
        try:
            want = str(direction or "").upper()
            output = self._brain.latest(symbol)
            if output is None:
                # Cold start — cannot gate on absent evidence (fail-open).
                self._bump(evaluated=True)
                return GateVerdict(True, self.mode, False, None,
                                   "no brain read yet (fail-open)")
            decision = output.decision
            dtype = getattr(decision, "decision_type", None)
            dtype_val = getattr(dtype, "value", str(dtype))
            authorises = bool(getattr(decision, "authorises_action", False))
            brain_dir = str(getattr(output, "direction", "") or "").upper()
            aligned = authorises and brain_dir == want and want in ("LONG", "SHORT")

            would_veto = not aligned
            # In veto mode a fresh non-aligned read blocks; shadow only records.
            allow = aligned or (self.mode != MODE_VETO)
            self._bump(evaluated=True, would_veto=would_veto,
                       vetoed=(would_veto and not allow))

            if would_veto and not allow:
                logger.info(
                    "[cognition-gate] VETO %s %s — Brain read %s/%s does not back this entry",
                    symbol, want, dtype_val, brain_dir or "-",
                )
            elif would_veto:
                logger.debug(
                    "[cognition-gate] shadow WOULD-VETO %s %s (Brain %s/%s)",
                    symbol, want, dtype_val, brain_dir or "-",
                )
            reason = ("brain backs entry" if aligned
                      else f"brain read {dtype_val}/{brain_dir or '-'} does not back {want}")
            return GateVerdict(allow, self.mode, would_veto, aligned, reason,
                               brain_decision_type=dtype_val, brain_direction=brain_dir)
        except Exception as exc:  # noqa: BLE001 — a gate fault must never block a trade
            logger.warning("[cognition-gate] %s evaluation errored — allowing (fail-safe): %s",
                           symbol, exc)
            return GateVerdict(True, self.mode, False, None, f"gate fault (fail-open): {exc}")

    def _bump(self, *, evaluated: bool = False, would_veto: bool = False,
              vetoed: bool = False) -> None:
        with self._lock:
            if evaluated:
                self._evaluations += 1
            if would_veto:
                self._would_veto += 1
            if vetoed:
                self._vetoed += 1

    def get_status(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "evaluations": self._evaluations,
                "would_veto": self._would_veto,
                "vetoed": self._vetoed,
            }


__all__ = ["CognitionGate", "GateVerdict", "normalise_mode",
           "MODE_OFF", "MODE_SHADOW", "MODE_VETO"]
