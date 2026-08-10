"""
APEX TRADER — Portfolio Risk State Machine (M8 Phase 4a + 4b + 4c)

Unified portfolio-risk engine fed by BOTH live capital-at-risk heat
and correlation exposure.

States:
  NORMAL     — business as usual
  DEFENSIVE  — freeze entries/scale-ins, advance eligible→BE, tighten stops
  REDUCING   — graduated partial-close of weakest positions (Phase 4b)
  EMERGENCY  — progressive full-close of weakest positions (Phase 4c)

Precedence (highest → lowest):
  margin_guardian (margin events) ≥ EMERGENCY (survival) > REDUCING >
  DEFENSIVE > normal management > adaptive.

Design principles:
  • Risk-directed exits MUST bypass entry circuit-breakers / cooldowns.
  • Adaptive learning may NEVER override this engine.
  • Unknown/orphan metadata = neutral, never "weakest."
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from loguru import logger


# ── Adaptive heat-ladder scaling bounds ──────────────────────────────────────
# The ladder sqrt-scales its thresholds UP on a micro account so a single
# unavoidable min-lot trade (a large fraction of a tiny balance) does not
# instantly trip DEFENSIVE/EMERGENCY. But raw sqrt-scaling is unbounded: at
# equity=$136 vs a $10k reference the factor is sqrt(73.5)=8.57x, which pushed
# the 4% emergency threshold to ~34% — i.e. the system became DRAMATICALLY LESS
# conservative exactly as capital collapsed. That is backwards. Two guards keep
# the scaled ladder sane:
#   * ``_MAX_HEAT_SCALE`` caps the multiplier so a near-zero balance can never
#     produce an absurd factor (it still permits enough head-room for a min-lot
#     trade — ~8% heat on a ~$200 account — without tripping DEFENSIVE).
#   * The per-threshold ceilings bound the ABSOLUTE result, so emergency can
#     never sit at a self-defeating third of the account. As equity shrinks the
#     ladder now converges to a fixed, conservative ceiling rather than diverging.
_MAX_HEAT_SCALE = 6.0
_HEAT_CEIL_DEFENSIVE = 10.0
_HEAT_CEIL_RECOVERY = 9.0
_HEAT_CEIL_REDUCTION = 12.0
_HEAT_CEIL_EMERGENCY = 15.0


def _scaled_heat_thresholds(
    reference_balance: float, account_equity: float,
    base_defensive: float, base_recovery: float,
    base_reduction: float, base_emergency: float,
) -> "tuple[float, float, float, float, float]":
    """Return ``(scale, defensive, recovery, reduction, emergency)`` for a micro
    account, capping the scale factor and clamping each threshold to its sane
    ceiling. Shared by the constructor and :meth:`recalibrate_for_equity` so the
    two paths can never diverge. Assumes ``account_equity < reference_balance``.
    """
    scale = max(1.0, min(_MAX_HEAT_SCALE, math.sqrt(reference_balance / account_equity)))
    return (
        scale,
        min(base_defensive * scale, _HEAT_CEIL_DEFENSIVE),
        min(base_recovery * scale, _HEAT_CEIL_RECOVERY),
        min(base_reduction * scale, _HEAT_CEIL_REDUCTION),
        min(base_emergency * scale, _HEAT_CEIL_EMERGENCY),
    )


# ── States ──────────────────────────────────────────────────────────────────

class PortfolioRiskState(Enum):
    NORMAL = auto()
    DEFENSIVE = auto()
    REDUCING = auto()
    EMERGENCY = auto()


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
    escalated_to_reducing: bool = False
    escalated_to_emergency: bool = False
    emergency_trigger: str = ""


@dataclass
class PositionWeakness:
    """Composite weakness ranking for a single position."""
    order_id: str
    symbol: str
    direction: str
    weakness_score: float
    r_multiple: float
    entry_score: int
    stagnation_minutes: float
    risk_share_pct: float
    is_insufficient_data: bool
    reason: str


# ── Emergency trigger snapshot (Phase 4c) ───────────────────────────────

@dataclass
class EmergencyTriggerSnapshot:
    """Inputs to the emergency trigger evaluator."""
    live_heat_pct: float
    drawdown_mode: str  # DrawdownMode.value
    reconcile_age_seconds: float  # seconds since last successful reconcile
    managed_count: int  # positions we think are open
    broker_count: Optional[int]  # positions broker reports; None = fetch failed
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class EmergencyTriggerResult:
    """Which emergency triggers fired, if any."""
    extreme_heat: bool = False
    drawdown_frozen: bool = False
    reconcile_failure: bool = False
    broker_exposure_mismatch: bool = False
    # Reconciliation is stale AND the broker is unreachable. This is NOT an
    # actionable emergency: with no trustworthy broker state, force-closing
    # would liquidate good positions on a transient outage ("couldn't reach
    # broker for a moment" must never kill a position). Surfaced for human
    # review only — deliberately excluded from ``any_fired``.
    reconcile_unreachable: bool = False

    @property
    def any_fired(self) -> bool:
        return (
            self.extreme_heat
            or self.drawdown_frozen
            or self.reconcile_failure
            or self.broker_exposure_mismatch
        )

    @property
    def description(self) -> str:
        parts = []
        if self.extreme_heat:
            parts.append("extreme_heat")
        if self.drawdown_frozen:
            parts.append("drawdown_frozen")
        if self.reconcile_failure:
            parts.append("reconcile_failure")
        if self.broker_exposure_mismatch:
            parts.append("broker_exposure_mismatch")
        if self.reconcile_unreachable:
            parts.append("reconcile_unreachable")
        return ", ".join(parts) if parts else "none"


def evaluate_emergency_triggers(
    snap: EmergencyTriggerSnapshot,
    *,
    heat_emergency_pct: float,
    emergency_reconcile_failure_seconds: float,
    emergency_broker_exposure_tolerance: int,
) -> EmergencyTriggerResult:
    """
    Pure function: evaluate all emergency trigger conditions.
    Returns which triggers fired (any → should enter EMERGENCY).
    """
    result = EmergencyTriggerResult()

    if snap.live_heat_pct >= heat_emergency_pct:
        result.extreme_heat = True

    if snap.drawdown_mode == "FROZEN":
        result.drawdown_frozen = True

    if snap.reconcile_age_seconds >= emergency_reconcile_failure_seconds:
        # Stale reconciliation only escalates to a force-close when the broker
        # is actually reachable (broker_count is not None). A confirmed broker
        # snapshot means the staleness reflects a genuine tracking problem we
        # can act on safely. When the broker is unreachable we have no truth to
        # act on — closing blind would dump good positions on a transient
        # connectivity blip — so flag it for human review instead.
        if snap.broker_count is not None:
            result.reconcile_failure = True
        else:
            result.reconcile_unreachable = True

    if snap.broker_count is not None:
        count_diff = abs(snap.managed_count - snap.broker_count)
        if count_diff > emergency_broker_exposure_tolerance:
            result.broker_exposure_mismatch = True

    return result


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
    and 4b (ranking).
    """
    if entry_type in _ORPHAN_ENTRY_TYPES:
        return True
    if score == 0 and regime in _UNKNOWN_REGIMES:
        return True
    return False


# ── Weakest-position ranking (Phase 4b) ─────────────────────────────────

def rank_positions_weakest_first(
    positions: list[dict],
    total_risk_dollars: float,
) -> list[PositionWeakness]:
    """
    Rank open positions from weakest to strongest using a composite score.

    Each position dict must have:
      order_id, symbol, direction, entry_price, sl, current_price,
      score, regime, entry_type, open_time_utc (datetime),
      risk_dollars, lots

    Positions with insufficient data (orphan/unknown) are pinned to
    MEDIAN rank — never auto-ranked weakest.

    Returns a list sorted weakest-first (highest weakness_score first).
    """
    scored: list[PositionWeakness] = []
    insufficient: list[PositionWeakness] = []

    for p in positions:
        oid = p["order_id"]
        symbol = p["symbol"]
        direction = p["direction"]
        entry_price = p["entry_price"]
        sl = p["sl"]
        current_price = p["current_price"]
        score = p["score"]
        regime = p.get("regime", "")
        entry_type = p.get("entry_type", "")
        open_time = p["open_time_utc"]
        risk_dollars = p.get("risk_dollars", 0.0)

        is_long = direction.upper() in ("BUY", "LONG")
        original_risk = abs(entry_price - sl) if sl > 0 and entry_price > 0 else 0.0

        if original_risk > 1e-10:
            excursion = (
                (current_price - entry_price) if is_long
                else (entry_price - current_price)
            )
            r_multiple = excursion / original_risk
        else:
            r_multiple = 0.0

        now_utc = time.time()
        open_ts = open_time.timestamp() if hasattr(open_time, "timestamp") else now_utc
        stagnation_minutes = (now_utc - open_ts) / 60.0

        risk_share = (
            (risk_dollars / total_risk_dollars * 100.0)
            if total_risk_dollars > 0 else 0.0
        )

        is_insuff = is_position_data_insufficient(score, regime, entry_type)

        if is_insuff:
            pw = PositionWeakness(
                order_id=oid,
                symbol=symbol,
                direction=direction,
                weakness_score=0.0,
                r_multiple=r_multiple,
                entry_score=score,
                stagnation_minutes=stagnation_minutes,
                risk_share_pct=risk_share,
                is_insufficient_data=True,
                reason="insufficient data (neutral)",
            )
            insufficient.append(pw)
            continue

        weakness = 0.0
        reasons = []

        if r_multiple < -0.5:
            weakness += 30.0
            reasons.append(f"deep loss {r_multiple:.2f}R")
        elif r_multiple < 0.0:
            weakness += 15.0
            reasons.append(f"losing {r_multiple:.2f}R")
        elif r_multiple < 0.5:
            weakness += 5.0
            reasons.append(f"marginal {r_multiple:.2f}R")

        if score > 0:
            score_weakness = max(0.0, (100 - score) / 100.0) * 25.0
            weakness += score_weakness
            if score < 60:
                reasons.append(f"low score {score}")

        if stagnation_minutes > 120 and abs(r_multiple) < 0.5:
            stall_factor = min(stagnation_minutes / 360.0, 1.0) * 20.0
            weakness += stall_factor
            reasons.append(f"stalled {stagnation_minutes:.0f}min")

        weakness += min(risk_share, 30.0) * 0.5

        pw = PositionWeakness(
            order_id=oid,
            symbol=symbol,
            direction=direction,
            weakness_score=round(weakness, 2),
            r_multiple=round(r_multiple, 3),
            entry_score=score,
            stagnation_minutes=round(stagnation_minutes, 1),
            risk_share_pct=round(risk_share, 2),
            is_insufficient_data=False,
            reason="; ".join(reasons) if reasons else "strong",
        )
        scored.append(pw)

    scored.sort(key=lambda x: (-x.weakness_score, x.order_id))

    if insufficient and scored:
        mid = len(scored) // 2
        for ip in insufficient:
            ip.weakness_score = scored[mid].weakness_score if scored else 0.0
        scored = scored[:mid] + insufficient + scored[mid:]
    elif insufficient:
        scored = insufficient

    return scored


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

    Full ladder: NORMAL → DEFENSIVE → REDUCING → EMERGENCY.
    Hard triggers may jump directly to EMERGENCY from any state.
    De-escalation always steps down: EMERGENCY → REDUCING → DEFENSIVE → NORMAL
    (never skips downward steps).

    Hysteresis:
      Enter DEFENSIVE when heat >= heat_defensive_pct OR correlation unsafe.
      Escalate to REDUCING when in DEFENSIVE for >= reduction_persist_seconds
        OR heat >= heat_reduction_pct.
      Escalate to EMERGENCY when any emergency trigger fires (via
        evaluate_with_emergency).
      Exit DEFENSIVE only when BOTH heat < heat_recovery_pct AND correlation safe,
      sustained for recovery_dwell_seconds.

    Threshold ordering (validated in constructor):
      heat_emergency_pct > heat_reduction_pct > heat_defensive_pct > heat_recovery_pct

    Adaptive scaling:
      When ``account_equity`` is provided and below ``reference_balance``, all
      four thresholds are sqrt-scaled up so micro accounts do not trip
      DEFENSIVE/EMERGENCY on a single min-lot trade. The scale factor is CAPPED
      (``_MAX_HEAT_SCALE``) and each threshold is clamped to a sane ceiling, so
      an equity collapse converges to a fixed conservative ladder instead of
      diverging (a near-empty account never gets a permissive ~34% emergency
      threshold). Ordering is validated AFTER scaling.
    """

    def __init__(
        self,
        heat_defensive_pct: float = 1.5,
        heat_recovery_pct: float = 1.0,
        recovery_dwell_seconds: float = 120.0,
        heat_reduction_pct: float = 2.5,
        reduction_persist_seconds: float = 300.0,
        heat_emergency_pct: float = 4.0,
        reference_balance: float = 10000.0,
        account_equity: Optional[float] = None,
    ):
        # Base thresholds are tuned for ~$10k accounts. On a micro account a
        # single min-lot trade can be several percent of equity, which would
        # instantly trip DEFENSIVE/EMERGENCY and force-close winners. When live
        # equity is below the reference, sqrt-scale all four thresholds up and
        # clamp each to a ceiling so the ladder stays useful without becoming
        # meaningless on tiny balances.
        base_defensive = heat_defensive_pct
        base_recovery = heat_recovery_pct
        base_reduction = heat_reduction_pct
        base_emergency = heat_emergency_pct

        if (
            account_equity is not None
            and account_equity > 0.0
            and account_equity < reference_balance
        ):
            (scale, heat_defensive_pct, heat_recovery_pct,
             heat_reduction_pct, heat_emergency_pct) = _scaled_heat_thresholds(
                reference_balance, account_equity,
                base_defensive, base_recovery, base_reduction, base_emergency,
            )
            logger.info(
                "[PortfolioRisk] Adaptive heat thresholds for equity=${:.2f} "
                "(reference=${:.2f}, scale={:.2f}x): "
                "defensive {:.2f}%→{:.2f}%, recovery {:.2f}%→{:.2f}%, "
                "reduction {:.2f}%→{:.2f}%, emergency {:.2f}%→{:.2f}%",
                account_equity, reference_balance, scale,
                base_defensive, heat_defensive_pct,
                base_recovery, heat_recovery_pct,
                base_reduction, heat_reduction_pct,
                base_emergency, heat_emergency_pct,
            )

        if heat_recovery_pct >= heat_defensive_pct:
            raise ValueError(
                f"heat_recovery_pct ({heat_recovery_pct}) must be < "
                f"heat_defensive_pct ({heat_defensive_pct})"
            )
        if heat_reduction_pct <= heat_defensive_pct:
            raise ValueError(
                f"heat_reduction_pct ({heat_reduction_pct}) must be > "
                f"heat_defensive_pct ({heat_defensive_pct})"
            )
        if heat_emergency_pct <= heat_reduction_pct:
            raise ValueError(
                f"heat_emergency_pct ({heat_emergency_pct}) must be > "
                f"heat_reduction_pct ({heat_reduction_pct})"
            )

        self.heat_defensive_pct = heat_defensive_pct
        self.heat_recovery_pct = heat_recovery_pct
        self.recovery_dwell_seconds = recovery_dwell_seconds
        self.heat_reduction_pct = heat_reduction_pct
        self.reduction_persist_seconds = reduction_persist_seconds
        self.heat_emergency_pct = heat_emergency_pct

        # Retain the UNSCALED base thresholds + reference so the ladder can be
        # re-scaled later once the live broker balance is known. At startup the
        # balance is frequently still 0/None (broker connecting), so the base
        # ~$10k thresholds get installed and a single min-lot trade on a micro
        # account instantly trips DEFENSIVE/EMERGENCY. ``recalibrate_for_equity``
        # self-heals that once equity is available. See its docstring.
        self._base_defensive = base_defensive
        self._base_recovery = base_recovery
        self._base_reduction = base_reduction
        self._base_emergency = base_emergency
        self._reference_balance = reference_balance

        self._state = PortfolioRiskState.NORMAL
        self._recovery_eligible_since: Optional[float] = None
        self._defensive_entered_at: Optional[float] = None

    @property
    def state(self) -> PortfolioRiskState:
        return self._state

    def recalibrate_for_equity(self, account_equity: Optional[float]) -> bool:
        """Re-scale the heat ladder to the live account equity.

        The constructor sqrt-scales thresholds for micro accounts, but at
        startup the broker balance is often not yet known (0/None), so the
        base ~$10k thresholds are installed and a single min-lot trade on a
        small account trips DEFENSIVE/EMERGENCY — force-closing every position
        seconds after entry before the Brain can manage it. Calling this each
        cycle with the live equity self-heals the calibration once the balance
        is known.

        Scales from the retained UNSCALED base thresholds (so it is fully
        idempotent — repeated calls with the same equity never drift), clamps
        each threshold to its ceiling, and only applies when the result is a
        meaningful change AND preserves the ordering invariant. Never makes a
        large account (equity >= reference) less protected than its base
        configuration. Fully self-contained; returns True if thresholds moved.
        """
        try:
            eq = float(account_equity) if account_equity is not None else 0.0
        except (TypeError, ValueError):
            return False
        if eq <= 0.0:
            return False

        ref = self._reference_balance
        if eq >= ref:
            scale = 1.0
            new_def = min(self._base_defensive, _HEAT_CEIL_DEFENSIVE)
            new_rec = min(self._base_recovery, _HEAT_CEIL_RECOVERY)
            new_red = min(self._base_reduction, _HEAT_CEIL_REDUCTION)
            new_eme = min(self._base_emergency, _HEAT_CEIL_EMERGENCY)
        else:
            (scale, new_def, new_rec, new_red, new_eme) = _scaled_heat_thresholds(
                ref, eq, self._base_defensive, self._base_recovery,
                self._base_reduction, self._base_emergency,
            )

        # Reject any scaling that would violate the ordering invariant (a
        # pathological clamp collapse) — keep the current, valid ladder.
        if not (new_rec < new_def < new_red < new_eme):
            return False

        # Skip no-op churn (and its log line) when nothing moved materially.
        if (
            abs(new_def - self.heat_defensive_pct) < 1e-6
            and abs(new_rec - self.heat_recovery_pct) < 1e-6
            and abs(new_red - self.heat_reduction_pct) < 1e-6
            and abs(new_eme - self.heat_emergency_pct) < 1e-6
        ):
            return False

        old_def = self.heat_defensive_pct
        old_eme = self.heat_emergency_pct
        self.heat_defensive_pct = new_def
        self.heat_recovery_pct = new_rec
        self.heat_reduction_pct = new_red
        self.heat_emergency_pct = new_eme
        logger.info(
            "[PortfolioRisk] Recalibrated heat ladder to live equity=${:.2f} "
            "(reference=${:.2f}, scale={:.2f}x): defensive {:.2f}%→{:.2f}%, "
            "emergency {:.2f}%→{:.2f}% (recovery {:.2f}%, reduction {:.2f}%)",
            eq, ref, scale, old_def, new_def, old_eme, new_eme,
            new_rec, new_red,
        )
        return True

    def evaluate(self, snapshot: PortfolioRiskSnapshot) -> StateTransition:
        """
        Evaluate heat/correlation metrics and return the (possibly changed) state.
        Does NOT evaluate emergency triggers — call evaluate_with_emergency for that.
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
                self._defensive_entered_at = now
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

        if self._state == PortfolioRiskState.DEFENSIVE:
            heat_reduction_breach = heat >= self.heat_reduction_pct
            persist_breach = (
                self._defensive_entered_at is not None
                and (now - self._defensive_entered_at) >= self.reduction_persist_seconds
                and (heat_breached or corr_breached)
            )

            if heat_reduction_breach or persist_breach:
                self._state = PortfolioRiskState.REDUCING
                self._recovery_eligible_since = None
                trigger = (
                    f"heat {heat:.2f}% >= {self.heat_reduction_pct:.2f}%"
                    if heat_reduction_breach
                    else f"DEFENSIVE persisted {(now - self._defensive_entered_at):.0f}s >= {self.reduction_persist_seconds:.0f}s"
                )
                reason = f"Escalated to REDUCING: {trigger}"
                logger.warning("[PortfolioRisk] {}", reason)
                return StateTransition(
                    state=self._state,
                    changed=True,
                    reason=reason,
                    live_heat_pct=heat,
                    correlation_safe=corr_safe,
                    escalated_to_reducing=True,
                )

            recovery_ok = (heat < self.heat_recovery_pct) and corr_safe
            return self._check_defensive_recovery(recovery_ok, heat, corr_safe, now)

        if self._state == PortfolioRiskState.REDUCING:
            still_breached = heat_breached or corr_breached
            if not still_breached:
                self._state = PortfolioRiskState.DEFENSIVE
                self._defensive_entered_at = now
                self._recovery_eligible_since = None
                reason = (
                    f"De-escalated REDUCING → DEFENSIVE: "
                    f"heat {heat:.2f}% < {self.heat_defensive_pct:.2f}% and corr safe"
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
                    f"REDUCING (heat={heat:.2f}%, "
                    f"corr_safe={corr_safe})"
                ),
                live_heat_pct=heat,
                correlation_safe=corr_safe,
            )

        # ── EMERGENCY state ─────────────────────────────────────────
        still_breached = heat_breached or corr_breached
        if not still_breached:
            self._state = PortfolioRiskState.REDUCING
            self._recovery_eligible_since = None
            reason = (
                f"De-escalated EMERGENCY → REDUCING: "
                f"heat {heat:.2f}% < {self.heat_defensive_pct:.2f}% and corr safe"
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
                f"EMERGENCY (heat={heat:.2f}%, "
                f"corr_safe={corr_safe})"
            ),
            live_heat_pct=heat,
            correlation_safe=corr_safe,
        )

    def escalate_to_emergency(
        self,
        trigger_result: EmergencyTriggerResult,
        heat: float,
        corr_safe: bool,
    ) -> StateTransition:
        """
        Force-escalate to EMERGENCY from ANY current state.
        Called when emergency triggers fire.
        Returns the transition (always changed=True when entering EMERGENCY).
        """
        if self._state == PortfolioRiskState.EMERGENCY:
            return StateTransition(
                state=self._state,
                changed=False,
                reason=f"EMERGENCY (triggers: {trigger_result.description})",
                live_heat_pct=heat,
                correlation_safe=corr_safe,
            )

        prev = self._state.name
        self._state = PortfolioRiskState.EMERGENCY
        self._recovery_eligible_since = None
        reason = (
            f"Escalated {prev} → EMERGENCY: "
            f"triggers=[{trigger_result.description}]"
        )
        logger.critical("[PortfolioRisk] {}", reason)
        return StateTransition(
            state=self._state,
            changed=True,
            reason=reason,
            live_heat_pct=heat,
            correlation_safe=corr_safe,
            escalated_to_emergency=True,
            emergency_trigger=trigger_result.description,
        )

    def _check_defensive_recovery(
        self,
        recovery_ok: bool,
        heat: float,
        corr_safe: bool,
        now: float,
    ) -> StateTransition:
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
            self._defensive_entered_at = None
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
        self._defensive_entered_at = None


# ── Exit-authority (Phase 4b + 4c) ───────────────────────────────────────
#
# Risk-directed exits (reduction + emergency) MUST:
#   1. Bypass the entry Execution circuit breaker.
#   2. Bypass entry cooldowns and opportunity-density throttles.
#   3. Be routed through a dedicated close path, NOT the entry path.
#
# Precedence (highest → lowest):
#   margin_guardian (margin events) ≥ EMERGENCY (survival) > REDUCING >
#   DEFENSIVE > normal management > adaptive.
#
# Adaptive learning may NEVER veto or downgrade emergency or reduction actions.
