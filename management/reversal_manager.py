"""
APEX TRADER — Reversal Manager (Session 29: atomic reversal, anti-ping-pong).

Session 28 gave the ThesisEngine's competing theses the power to EXIT an open
position when the opposing thesis becomes dominant (a ``THESIS_FLIP``). But the
APEX philosophy — "strengthen, reduce, reverse, or exit — immediately" — wants
more than an exit: when the competing thesis flips dominant the optimal action
is to *reverse*, closing the current side and opening the opposite in a single
decision cycle, so there is no gap between "I should be short" and actually
being short.

The danger with reversals is ping-pong: in a choppy market a naive reverse-on-
every-flip bleeds spread on every flip (LONG → SHORT → LONG → SHORT …), death by
a thousand spreads. This module is the anti-ping-pong gate that makes reversal
safe. It is a **pure decision module** — it never touches the broker, the
ThesisEngine, or the WorldModel. The caller passes in the competing thesis's
effective edge over the do-nothing (Flat) baseline; the manager answers whether
a reversal is permitted right now, and the caller records the reversal once it
actually executes.

Four protections, all configurable:

* **Minimum edge** — the competing thesis's EV over Flat must clear
  ``min_thesis_ev`` (the same do-nothing margin a fresh entry must beat). A
  reversal is a new entry and earns no free pass.
* **Cooldown** — after a reversal, no further reversal on that symbol for
  ``cooldown_seconds``. Since a reversal's fresh position must itself be held a
  minimum time before its own evidence exit can fire, a cooldown longer than
  that min-hold structurally blocks an immediate reverse-back.
* **Per-session cap** — at most ``max_reversals_per_session`` reversals per
  symbol per trading session; after the cap, only plain evidence exits are
  allowed (no more reversals).
* **Escalating threshold** — each subsequent reversal on the same symbol
  requires progressively more edge:
  ``required = min_thesis_ev * (1 + threshold_escalation * reversals_so_far)``
  so with ``threshold_escalation`` = 0.5 the 1st reversal needs 1.0×, the 2nd
  1.5×, the 3rd 2.0× — the manager grows increasingly skeptical of a symbol that
  keeps flip-flopping.

Leaf module: stdlib + loguru only.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from loguru import logger

LONG = "LONG"
SHORT = "SHORT"


def _safe_float(value: object, default: float = 0.0) -> float:
    """Coerce to a finite float, falling back to ``default`` on NaN/inf/error."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


@dataclass
class ReversalDecision:
    """The verdict on whether an open position may reverse right now."""

    reverse: bool
    reason: str = ""
    required_threshold: float = 0.0     # escalating EV bar this reversal had to clear
    competing_ev: float = 0.0           # competing thesis EV over flat that was tested
    reversals_so_far: int = 0           # reversals already taken this session for the symbol

    def to_dict(self) -> dict:
        return {
            "reverse": bool(self.reverse),
            "reason": self.reason,
            "required_threshold": round(self.required_threshold, 4),
            "competing_ev": round(self.competing_ev, 4),
            "reversals_so_far": int(self.reversals_so_far),
        }


class ReversalManager:
    """Anti-ping-pong gate for atomic thesis reversals. Pure decision module."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        min_thesis_ev: float = 0.1,
        cooldown_seconds: float = 300.0,
        max_reversals_per_session: int = 3,
        threshold_escalation: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = bool(enabled)
        self._min_thesis_ev = max(0.0, _safe_float(min_thesis_ev, 0.1))
        self._cooldown = max(0.0, _safe_float(cooldown_seconds, 300.0))
        self._max_per_session = max(0, int(max_reversals_per_session))
        self._escalation = max(0.0, _safe_float(threshold_escalation, 0.5))
        self._clock = clock

        # Per-symbol anti-ping-pong state.
        self._last_reversal_at: dict[str, float] = {}
        self._session_counts: dict[str, int] = {}
        self._total_reversals = 0
        self._lock = threading.Lock()

    # ── Decision ──────────────────────────────────────────────────────────

    def evaluate(self, symbol: str, competing_ev_over_flat: float) -> ReversalDecision:
        """Is a reversal on ``symbol`` permitted right now?

        ``competing_ev_over_flat`` is the opposing (now-dominant) thesis's
        effective EV over the Flat baseline, in R — the same quantity a fresh
        entry must clear. Returns a :class:`ReversalDecision`; ``reverse`` is
        True only when the reversal is enabled, off cooldown, under the
        per-session cap, and the competing edge clears the escalating threshold.
        Fail-safe: any internal fault returns ``reverse=False`` (fall back to a
        plain evidence exit — never fail INTO a reversal).
        """
        cev = _safe_float(competing_ev_over_flat)
        try:
            sym = str(symbol or "")
            if not self.enabled:
                return ReversalDecision(False, "disabled", self._min_thesis_ev, cev, 0)
            with self._lock:
                count = int(self._session_counts.get(sym, 0))
                last = float(self._last_reversal_at.get(sym, 0.0))
                now = self._clock()
            # Per-session cap — after the cap only evidence exits are allowed.
            if self._max_per_session <= 0 or count >= self._max_per_session:
                return ReversalDecision(
                    False,
                    f"per-session reversal cap reached ({count}/{self._max_per_session})",
                    self._min_thesis_ev, cev, count,
                )
            # Cooldown — no reverse-back within the cooldown window.
            if last > 0.0 and (now - last) < self._cooldown:
                remaining = self._cooldown - (now - last)
                return ReversalDecision(
                    False,
                    f"reversal cooldown active ({remaining:.0f}s remaining)",
                    self._min_thesis_ev, cev, count,
                )
            # Escalating threshold — each prior reversal raises the bar.
            required = self._min_thesis_ev * (1.0 + self._escalation * count)
            if cev < required:
                return ReversalDecision(
                    False,
                    (
                        f"competing edge {cev:.3f}R < escalating threshold "
                        f"{required:.3f}R (reversal #{count + 1})"
                    ),
                    required, cev, count,
                )
            return ReversalDecision(
                True,
                (
                    f"competing edge {cev:.3f}R ≥ {required:.3f}R — reversal "
                    f"#{count + 1} permitted"
                ),
                required, cev, count,
            )
        except Exception as exc:  # noqa: BLE001 — never fail into a reversal
            logger.warning(
                "[reversal-manager] evaluate({}) faulted — no reversal: {}",
                symbol, exc,
            )
            return ReversalDecision(False, "eval_error", self._min_thesis_ev, cev, 0)

    def record_reversal(self, symbol: str) -> None:
        """Record that a reversal actually executed on ``symbol``.

        Starts the cooldown clock and increments the per-session counter so the
        escalating threshold and cap apply to the next reversal. Call this only
        after the reversal's opposite-direction entry has been dispatched (not at
        decision time), so a reversal that never executes is not counted.
        """
        try:
            sym = str(symbol or "")
            with self._lock:
                self._last_reversal_at[sym] = self._clock()
                self._session_counts[sym] = int(self._session_counts.get(sym, 0)) + 1
                self._total_reversals += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[reversal-manager] record_reversal({}) faulted: {}", symbol, exc)

    def reversals_this_session(self, symbol: str) -> int:
        """How many reversals ``symbol`` has taken this session (fail-safe)."""
        try:
            with self._lock:
                return int(self._session_counts.get(str(symbol or ""), 0))
        except Exception:  # noqa: BLE001
            return 0

    def reset_session(self) -> None:
        """Clear the per-session counters + cooldown timers (call on day roll)."""
        try:
            with self._lock:
                self._session_counts.clear()
                self._last_reversal_at.clear()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[reversal-manager] reset_session faulted: {}", exc)

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        try:
            with self._lock:
                active = {k: v for k, v in self._session_counts.items() if v > 0}
                total = self._total_reversals
        except Exception:  # noqa: BLE001
            active, total = {}, self._total_reversals
        return {
            "enabled": self.enabled,
            "min_thesis_ev": self._min_thesis_ev,
            "cooldown_seconds": self._cooldown,
            "max_reversals_per_session": self._max_per_session,
            "threshold_escalation": self._escalation,
            "total_reversals": total,
            "session_counts": active,
        }


__all__ = ["ReversalManager", "ReversalDecision", "LONG", "SHORT"]
