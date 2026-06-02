"""
APEX TRADER — Portfolio Risk State Machine (M8 Phase 4a)

Unified portfolio-risk engine fed by BOTH live capital-at-risk heat
and correlation exposure.  Non-destructive only: NORMAL ↔ DEFENSIVE.

States:
  NORMAL     — business as usual
  DEFENSIVE  — freeze entries/scale-ins, advance eligible→BE, tighten stops

Phase 4b/4c will add REDUCING and EMERGENCY (slots reserved, not implemented).

Design principles:
  • Risk-directed exits (future) MUST bypass entry circuit-breakers / cooldowns.
  • Adaptive learning may NEVER override this engine.
  • Unknown/orphan metadata = neutral, never "weakest."
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from loguru import logger


# ── States ──────────────────────────────────────────────────────────────────

class PortfolioRiskState(Enum):
    NORMAL = auto()
    DEFENSIVE = auto()
    # Phase 4b/4c extension points — NOT implemented in this phase
    # REDUCING = auto()
    # EMERGENCY = auto()


# ── Data structures ─────────────────────────────────────────────────────────

@dataclass
class PositionRisk:
    """Per-position capital-at-risk snapshot."""
    order_id: str
    symbol: str
    direction: str
    risk_dollars: float
    is_at_breakeven: bool
    is_fallback: bool  # True if computed from static proxy


@dataclass
class PortfolioRiskSnapshot:
    """Aggregate risk snapshot fed to the state machine."""
    live_heat_pct: float
    position_risks: list[PositionRisk]
    correlation_safe: bool
    max_currency_exposure: float
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class StateTransition:
    """Returned by the state machine on every evaluation."""
    state: PortfolioRiskState
    changed: bool
    reason: str
    live_heat_pct: float
    correlation_safe: bool


# ── Orphan-neutral helper ───────────────────────────────────────────────────

_ORPHAN_ENTRY_TYPES = {"ORPHAN_ADOPTED", "orphan_adopted"}
_UNKNOWN_REGIMES = {"UNKNOWN", "unknown", ""}


def is_position_data_insufficient(
    score: int,
    regime: str,
    entry_type: str,
) -> bool:
    """
    True when position metadata is insufficient for quality ranking.

    Adopted orphans (score=0, regime=UNKNOWN) must be treated as NEUTRAL
    — never as the weakest.  This helper is shared by 4a (eligibility)
    and future 4b (ranking).
    """
    if entry_type in _ORPHAN_ENTRY_TYPES:
        return True
    if score == 0 and regime in _UNKNOWN_REGIMES:
        return True
    return False


# ── Capital-at-risk calculator ──────────────────────────────────────────────

def compute_position_risk_dollars(
    *,
    direction: str,
    entry_price: float,
    sl: float,
    lots: float,
    pip_size: float,
    pip_value_per_lot: float,
    at_breakeven: bool,
    stake_usd: float = 0.0,
    multiplier: int = 100,
    is_deriv_stake: bool = False,
) -> tuple[float, bool]:
    """
    Compute how much account currency is at risk for a single position.

    Returns (risk_dollars, is_fallback).
    is_fallback=True means we couldn't compute and used a conservative proxy.

    MT5/forex/indices:
        pips_to_stop = |entry − sl| / pip_size
        risk_$ = pips_to_stop × pip_value_per_lot × lots

    Deriv (stake-based):
        P&L = stake × multiplier × (Δprice / price)
        Max-loss = stake × multiplier × (sl_distance / entry_price)
        → risk_$ = that max-loss, capped at stake (which is the absolute max)

    If at breakeven → risk ≈ 0 (clamped to 0).
    """
    if at_breakeven:
        return 0.0, False

    if entry_price <= 0 or sl <= 0 or pip_size <= 0:
        return 0.0, True

    is_long = direction.upper() in ("BUY", "LONG")
    sl_distance = (entry_price - sl) if is_long else (sl - entry_price)

    if sl_distance <= 0:
        return 0.0, False

    if is_deriv_stake and stake_usd > 0:
        risk = stake_usd * multiplier * (sl_distance / entry_price)
        risk = min(risk, stake_usd)
        return max(0.0, round(risk, 2)), False

    pips_to_stop = sl_distance / pip_size
    risk = pips_to_stop * pip_value_per_lot * lots
    return max(0.0, round(risk, 2)), False


def compute_live_heat_pct(
    position_risks: list[PositionRisk],
    equity: float,
) -> float:
    """
    Live portfolio heat = Σ(risk_$) / equity × 100.

    Guard: if equity is zero or negative, return 100.0 (maximally defensive).
    """
    if equity <= 0:
        return 100.0
    total_risk = sum(pr.risk_dollars for pr in position_risks)
    return round(total_risk / equity * 100, 4)


# ── Breakeven eligibility ──────────────────────────────────────────────────

def is_eligible_for_defensive_breakeven(
    *,
    direction: str,
    entry_price: float,
    sl: float,
    current_price: float,
    tp1_hit: bool,
    partial_closed: bool,
    at_breakeven: bool,
    be_eligible_r_multiple: float = 1.0,
) -> bool:
    """
    A position is eligible for defensive BE advancement only after
    demonstrated favorable excursion — never on a not-yet-progressed trade.

    Eligible if ANY of:
      • tp1_hit or partial_closed (already proven)
      • favorable excursion ≥ be_eligible_r_multiple × original risk

    Already at breakeven → not eligible (nothing to do).
    """
    if at_breakeven:
        return False

    if tp1_hit or partial_closed:
        return True

    if entry_price <= 0 or sl <= 0 or current_price <= 0:
        return False

    is_long = direction.upper() in ("BUY", "LONG")
    original_risk = abs(entry_price - sl)
    if original_risk < 1e-10:
        return False

    excursion = (
        (current_price - entry_price) if is_long
        else (entry_price - current_price)
    )
    return excursion >= be_eligible_r_multiple * original_risk


# ── State machine ───────────────────────────────────────────────────────────

class PortfolioRiskStateMachine:
    """
    Pure state machine: takes metric inputs + monotonic time, returns
    (state, transition_event).  No broker I/O, no side-effects.

    Hysteresis:
      Enter DEFENSIVE when heat >= heat_defensive_pct OR correlation unsafe.
      Exit DEFENSIVE only when BOTH heat < heat_recovery_pct AND correlation safe,
      sustained for recovery_dwell_seconds.
    """

    def __init__(
        self,
        heat_defensive_pct: float = 1.5,
        heat_recovery_pct: float = 1.0,
        recovery_dwell_seconds: float = 120.0,
    ):
        if heat_recovery_pct >= heat_defensive_pct:
            raise ValueError(
                f"heat_recovery_pct ({heat_recovery_pct}) must be < "
                f"heat_defensive_pct ({heat_defensive_pct})"
            )

        self.heat_defensive_pct = heat_defensive_pct
        self.heat_recovery_pct = heat_recovery_pct
        self.recovery_dwell_seconds = recovery_dwell_seconds

        self._state = PortfolioRiskState.NORMAL
        self._recovery_eligible_since: Optional[float] = None

    @property
    def state(self) -> PortfolioRiskState:
        return self._state

    def evaluate(self, snapshot: PortfolioRiskSnapshot) -> StateTransition:
        """
        Evaluate metrics and return the (possibly changed) state.
        Must be called every loop iteration.
        """
        now = snapshot.timestamp
        heat = snapshot.live_heat_pct
        corr_safe = snapshot.correlation_safe

        heat_breached = heat >= self.heat_defensive_pct
        corr_breached = not corr_safe

        if self._state == PortfolioRiskState.NORMAL:
            if heat_breached or corr_breached:
                self._state = PortfolioRiskState.DEFENSIVE
                self._recovery_eligible_since = None
                reasons = []
                if heat_breached:
                    reasons.append(
                        f"heat {heat:.2f}% >= {self.heat_defensive_pct:.2f}%"
                    )
                if corr_breached:
                    reasons.append(
                        f"correlation unsafe (max_currency={snapshot.max_currency_exposure:.4f})"
                    )
                reason = "Entered DEFENSIVE: " + "; ".join(reasons)
                logger.warning("[PortfolioRisk] {}", reason)
                return StateTransition(
                    state=self._state,
                    changed=True,
                    reason=reason,
                    live_heat_pct=heat,
                    correlation_safe=corr_safe,
                )
            return StateTransition(
                state=self._state,
                changed=False,
                reason="NORMAL",
                live_heat_pct=heat,
                correlation_safe=corr_safe,
            )

        # ── DEFENSIVE state ──────────────────────────────────────────
        recovery_ok = (heat < self.heat_recovery_pct) and corr_safe

        if not recovery_ok:
            self._recovery_eligible_since = None
            return StateTransition(
                state=self._state,
                changed=False,
                reason=(
                    f"DEFENSIVE (heat={heat:.2f}%, "
                    f"corr_safe={corr_safe})"
                ),
                live_heat_pct=heat,
                correlation_safe=corr_safe,
            )

        if self._recovery_eligible_since is None:
            self._recovery_eligible_since = now
            return StateTransition(
                state=self._state,
                changed=False,
                reason=(
                    f"DEFENSIVE — recovery eligible, "
                    f"dwell started ({self.recovery_dwell_seconds}s required)"
                ),
                live_heat_pct=heat,
                correlation_safe=corr_safe,
            )

        elapsed = now - self._recovery_eligible_since
        if elapsed >= self.recovery_dwell_seconds:
            self._state = PortfolioRiskState.NORMAL
            self._recovery_eligible_since = None
            reason = (
                f"Recovered to NORMAL: heat {heat:.2f}% < "
                f"{self.heat_recovery_pct:.2f}% for "
                f"{elapsed:.0f}s"
            )
            logger.info("[PortfolioRisk] {}", reason)
            return StateTransition(
                state=self._state,
                changed=True,
                reason=reason,
                live_heat_pct=heat,
                correlation_safe=corr_safe,
            )

        return StateTransition(
            state=self._state,
            changed=False,
            reason=(
                f"DEFENSIVE — recovery dwell "
                f"{elapsed:.0f}/{self.recovery_dwell_seconds:.0f}s"
            ),
            live_heat_pct=heat,
            correlation_safe=corr_safe,
        )

    def reset(self) -> None:
        """Reset to NORMAL (e.g. on startup before any positions exist)."""
        self._state = PortfolioRiskState.NORMAL
        self._recovery_eligible_since = None


# ── Exit-authority scaffolding (Phase 4b/4c) ────────────────────────────────
#
# When Phase 4b/4c introduce risk-directed exits (position reduction or
# emergency liquidation), those exits MUST:
#   1. Bypass the entry Execution circuit breaker.
#   2. Bypass entry cooldowns and opportunity-density throttles.
#   3. Be routed through a dedicated close path, NOT the entry path.
#
# Signature reserved for future implementation:
#
# def execute_risk_directed_exit(
#     order_id: str,
#     reason: str,
#     *,
#     partial_ratio: float = 1.0,  # 1.0 = full close
# ) -> bool:
#     """
#     Close or reduce a position on risk authority.
#     Must never be gated by entry-initiation controls.
#     """
#     raise NotImplementedError("Phase 4b/4c")
