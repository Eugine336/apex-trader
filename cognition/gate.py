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
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("apex.cognition.gate")

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_VETO = "veto"
MODE_AUTHORITATIVE = "authoritative"
_VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_VETO, MODE_AUTHORITATIVE)


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

    def __init__(self, brain: Optional[Any] = None, *, mode: str = MODE_SHADOW,
                 max_decision_age_seconds: float = 300.0) -> None:
        self._brain = brain
        self.mode = normalise_mode(mode)
        self.max_decision_age_seconds = max(0.0, float(max_decision_age_seconds))
        self._evaluations = 0
        self._would_veto = 0
        self._vetoed = 0
        # V-017 — throttle the "absent cognition failed OPEN" warning to once per
        # gate instance (the condition is persistent; per-entry logging would spam).
        self._warned_absent = False
        self._lock = threading.Lock()

    def _warn_absent_once(self, symbol: str, reason: str) -> None:
        """V-017 — absent cognition that fails OPEN must not pass silently: log it
        at WARNING (once). Non-authoritative modes are transitional and allow an
        entry with NO Brain backing; the operator must be able to see that."""
        with self._lock:
            if self._warned_absent:
                return
            self._warned_absent = True
        logger.warning(
            "[cognition-gate] %s ALLOWING %s with absent cognition (%s, mode=%s) — "
            "non-authoritative gate is fail-open and TRANSITIONAL; entries proceed "
            "without Brain backing. Set gate_mode=authoritative to fail closed.",
            symbol, self.mode.upper(), reason, self.mode,
        )

    @property
    def authoritative(self) -> bool:
        """True when the Brain is the SOLE decider — the legacy path cannot
        trade without a fresh, aligned Brain authorization (fail-closed)."""
        return self.mode == MODE_AUTHORITATIVE

    def evaluate(self, symbol: str, direction: str, now: Optional[float] = None) -> GateVerdict:
        if self.mode == MODE_OFF:
            return GateVerdict(True, self.mode, False, None, "gate off")
        authoritative = self.mode == MODE_AUTHORITATIVE
        if self._brain is None:
            # authoritative ⇒ no reasoner, no trade (fail-closed, single reasoner);
            # shadow/veto ⇒ fail-open (but no longer silent — V-017).
            if not authoritative:
                self._warn_absent_once(symbol, "no brain wired")
            return GateVerdict(not authoritative, self.mode, authoritative, None,
                               "no brain (authoritative fail-closed)" if authoritative
                               else "no brain (fail-open)")
        try:
            want = str(direction or "").upper()
            t = time.time() if now is None else float(now)
            output = self._brain.latest(symbol)
            if output is None:
                # Cold start: authoritative blocks (no decision ⇒ no trade);
                # shadow/veto allow (never block on absent evidence) — but the
                # fail-open allow is logged at WARNING now, never silent (V-017).
                self._bump(evaluated=True, would_veto=authoritative, vetoed=authoritative)
                if not authoritative:
                    self._warn_absent_once(symbol, "no brain read yet (cold start)")
                return GateVerdict(not authoritative, self.mode, authoritative, None,
                                   "no brain read yet (authoritative fail-closed)" if authoritative
                                   else "no brain read yet (fail-open)")
            decision = output.decision
            dtype = getattr(decision, "decision_type", None)
            dtype_val = getattr(dtype, "value", str(dtype))
            authorises = bool(getattr(decision, "authorises_action", False))
            brain_dir = str(getattr(output, "direction", "") or "").upper()
            # Freshness — a stale authorization must not keep authorising trades
            # (renewed authorization per action; Part VI Art 4).
            decided = float(getattr(output, "decided_at_epoch", 0.0) or 0.0)
            fresh = True
            if self.max_decision_age_seconds > 0:
                fresh = decided > 0 and (t - decided) <= self.max_decision_age_seconds
            aligned = authorises and brain_dir == want and want in ("LONG", "SHORT") and fresh

            would_veto = not aligned
            # shadow always allows; veto AND authoritative block a non-aligned read.
            allow = aligned or (self.mode == MODE_SHADOW)
            self._bump(evaluated=True, would_veto=would_veto,
                       vetoed=(would_veto and not allow))

            # V-016 / V-017 — a provider-unavailable Brain read is ABSENT cognition
            # (infrastructure down), not a market view. When a non-authoritative
            # gate lets the entry through anyway, surface it at WARNING — never a
            # silent pass on absent cognition.
            if dtype_val == "reasoner_unavailable" and allow and not aligned:
                self._warn_absent_once(symbol, "brain reasoner unavailable (provider down)")

            if would_veto and not allow:
                logger.info(
                    "[cognition-gate] %s %s %s — Brain read %s/%s (fresh=%s) does not authorise",
                    self.mode.upper(), symbol, want, dtype_val, brain_dir or "-", fresh,
                )
            elif would_veto:
                logger.debug(
                    "[cognition-gate] shadow WOULD-BLOCK %s %s (Brain %s/%s fresh=%s)",
                    symbol, want, dtype_val, brain_dir or "-", fresh,
                )
            if not fresh and authorises and brain_dir == want:
                reason = f"brain authorization stale (> {self.max_decision_age_seconds:.0f}s)"
            elif aligned:
                reason = "brain authorises entry"
            else:
                reason = f"brain read {dtype_val}/{brain_dir or '-'} does not authorise {want}"
            return GateVerdict(allow, self.mode, would_veto, aligned, reason,
                               brain_decision_type=dtype_val, brain_direction=brain_dir)
        except Exception as exc:  # noqa: BLE001 — a gate fault must never wrongly trade
            # authoritative fails CLOSED (do nothing when uncertain); others fail-open.
            allow = not authoritative
            logger.warning("[cognition-gate] %s evaluation errored — %s (fail-%s): %s",
                           symbol, "blocking" if authoritative else "allowing",
                           "closed" if authoritative else "open", exc)
            return GateVerdict(allow, self.mode, authoritative, None,
                               f"gate fault (fail-{'closed' if authoritative else 'open'}): {exc}")

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
                "authoritative": self.mode == MODE_AUTHORITATIVE,
                "max_decision_age_seconds": self.max_decision_age_seconds,
                "evaluations": self._evaluations,
                "would_veto": self._would_veto,
                "vetoed": self._vetoed,
            }


__all__ = ["CognitionGate", "GateVerdict", "normalise_mode",
           "MODE_OFF", "MODE_SHADOW", "MODE_VETO", "MODE_AUTHORITATIVE"]
