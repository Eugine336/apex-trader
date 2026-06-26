"""
APEX TRADER — Position Displacer (GAP 4)

Once capital is allocated, a position is committed until management closes it on
its own logic. The system cannot exit a lower-EV open position to make room for a
clearly better new opportunity. This module is the missing upgrade decision: when
a new candidate would be rejected for capacity / budget, it checks whether the
weakest eligible OPEN position has materially lower EV — and, if so, recommends
closing it so the better idea can take the slot.

This is a PURE decision module. It never touches the broker. The caller computes
each open position's EV (current unrealised R + remaining structural target) and
passes them in; the displacer returns a :class:`DisplacementDecision`. The caller
is responsible for submitting the CLOSE through the normal IntentAggregator (so
risk is never bypassed) and then calling :meth:`record_displacement`.

Safety contracts (all configurable):
  * never displace a position already in profit ≥ ``min_profit_protect`` R
    (don't close a winner to chase a new idea),
  * the new candidate's EV must beat the weakest position by ``ev_margin`` R,
  * at most ``max_per_cycle`` displacements per cycle,
  * a cooldown between displacements.

Leaf module: stdlib + loguru only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence


@dataclass
class PositionEV:
    """An open position's expected value, computed by the caller."""

    ticket: str
    symbol: str
    direction: str
    ev: float                 # remaining EV in R (unrealised R + target distance)
    profit_r: float = 0.0     # current unrealised R (for the winner-protect guard)


@dataclass
class DisplacementDecision:
    displace: bool
    target: Optional[PositionEV] = None
    reason: str = ""
    candidate_ev: float = 0.0
    conflicts: list = field(default_factory=list)


class PositionDisplacer:
    """Decide whether to close a weak open position to fund a better idea."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        ev_margin: float = 0.5,
        min_profit_protect: float = 1.0,
        max_per_cycle: int = 1,
        cooldown_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = bool(enabled)
        self._ev_margin = float(ev_margin)
        self._min_profit_protect = float(min_profit_protect)
        self._max_per_cycle = max(1, int(max_per_cycle))
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._clock = clock

        self._last_displacement_at = 0.0
        self._cycle_count = 0
        self._total_displacements = 0

    # ── Decision ──────────────────────────────────────────────────────────

    def evaluate(
        self,
        candidate_ev: float,
        open_positions: Sequence[PositionEV],
        *,
        candidate_symbol: str = "",
    ) -> DisplacementDecision:
        """Return whether to displace a weak position for ``candidate_ev``.

        Picks the weakest EV position that is NOT protected (profit below the
        winner-protect floor) and recommends displacing it only when the new
        idea beats it by at least ``ev_margin`` R, the cooldown has elapsed, and
        the per-cycle cap is not exhausted. The per-cycle counter resets via
        :meth:`reset_cycle` (the caller, e.g. the queue, calls it per drain).
        """
        cev = float(candidate_ev or 0.0)
        if not self.enabled:
            return DisplacementDecision(False, None, "disabled", cev)

        eligible = [
            p for p in (open_positions or [])
            if float(getattr(p, "profit_r", 0.0) or 0.0) < self._min_profit_protect
        ]
        if not eligible:
            return DisplacementDecision(
                False, None, "no displaceable position (all protected/empty)", cev,
            )

        now = self._clock()
        if (
            self._last_displacement_at > 0
            and (now - self._last_displacement_at) < self._cooldown
        ):
            return DisplacementDecision(False, None, "cooldown active", cev)

        if self._cycle_count >= self._max_per_cycle:
            return DisplacementDecision(
                False, None, "max displacements this cycle reached", cev,
            )

        weakest = min(eligible, key=lambda p: float(getattr(p, "ev", 0.0) or 0.0))
        weakest_ev = float(getattr(weakest, "ev", 0.0) or 0.0)
        margin = cev - weakest_ev
        if margin < self._ev_margin:
            return DisplacementDecision(
                False, weakest,
                f"insufficient EV margin ({margin:+.2f}R < {self._ev_margin:.2f}R)",
                cev,
            )

        return DisplacementDecision(
            True, weakest,
            f"{candidate_symbol or 'candidate'} EV {cev:+.2f}R beats "
            f"{weakest.symbol} {weakest.direction} EV {weakest_ev:+.2f}R "
            f"by {margin:+.2f}R",
            cev,
        )

    def record_displacement(self) -> None:
        """Record that the caller actually displaced a position (after the CLOSE)."""
        self._last_displacement_at = self._clock()
        self._cycle_count += 1
        self._total_displacements += 1

    def reset_cycle(self) -> None:
        """Reset the per-cycle displacement counter (call once per dispatch cycle)."""
        self._cycle_count = 0

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "ev_margin": self._ev_margin,
            "min_profit_protect": self._min_profit_protect,
            "max_per_cycle": self._max_per_cycle,
            "cooldown_seconds": self._cooldown,
            "total_displacements": self._total_displacements,
        }


__all__ = ["PositionDisplacer", "PositionEV", "DisplacementDecision"]
