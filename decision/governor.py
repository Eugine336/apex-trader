"""
Risk Governor — independent safety layer with veto authority.
Sits after the Decision Engine.  Can override any decision if it
violates capital protection rules.  Never downstream of intelligence.
"""

from __future__ import annotations

from loguru import logger

from decision.actions import Action, ManagementDecision
from decision.context import TradeContext
from decision.situation import SituationAssessment


class RiskGovernor:
    """Reviews decisions and applies safety overrides.

    Rules:
    - Never worsen a stop loss.
    - Override HOLD→CLOSE when the situation is catastrophically bad
      (very low alignment + broken structure + deep loss).
    - Veto SCALE_IN when portfolio heat is high.
    - All overrides are logged with full reasoning.
    """

    def review(
        self,
        decision: ManagementDecision,
        ctx: TradeContext,
        sa: SituationAssessment,
    ) -> ManagementDecision:

        # ── Veto SCALE_IN at high portfolio heat ─────────────────────────
        if decision.action == Action.SCALE_IN:
            if ctx.portfolio_heat_pct > 1.5:
                logger.info(
                    "[RiskGovernor] VETO SCALE_IN for {} — portfolio heat {:.1f}%",
                    ctx.symbol, ctx.portfolio_heat_pct,
                )
                return ManagementDecision(
                    action=Action.HOLD,
                    reason=f"[GOVERNOR VETO] Scale-in blocked: portfolio heat "
                           f"{ctx.portfolio_heat_pct:.1f}% > 1.5%. "
                           f"Original: {decision.reason}",
                    confidence=decision.confidence,
                    evidence=decision.evidence + ["governor_veto: high_heat"],
                )

        # ── Override HOLD→CLOSE in catastrophic situations ───────────────
        if decision.action in (Action.HOLD, Action.OBSERVE):
            catastrophic = (
                sa.tf_alignment < -0.6
                and sa.structure_integrity < 0.15
                and sa.profit_state < -1.0
            )
            if catastrophic:
                logger.warning(
                    "[RiskGovernor] OVERRIDE HOLD→CLOSE for {} — "
                    "alignment={:.2f} structure={:.2f} profit={:.1f}R",
                    ctx.symbol, sa.tf_alignment,
                    sa.structure_integrity, sa.profit_state,
                )
                return ManagementDecision(
                    action=Action.CLOSE,
                    reason=f"[GOVERNOR OVERRIDE] Catastrophic situation: "
                           f"alignment={sa.tf_alignment:+.2f}, "
                           f"structure={sa.structure_integrity:.2f}, "
                           f"profit={sa.profit_state:+.1f}R. "
                           f"Original decision was {decision.action.value}: {decision.reason}",
                    confidence=0.85,
                    evidence=decision.evidence + [
                        "governor_override: catastrophic",
                        f"alignment={sa.tf_alignment:+.2f}",
                        f"structure={sa.structure_integrity:.2f}",
                        f"profit={sa.profit_state:+.1f}R",
                    ],
                )

        # ── Never worsen a stop loss ─────────────────────────────────────
        if decision.action in (Action.TIGHTEN_SL, Action.SET_PROTECTIVE_STOP):
            if decision.new_sl is not None and ctx.current_sl != 0.0:
                sl_worsened = (
                    (ctx.is_long and decision.new_sl < ctx.current_sl)
                    or (not ctx.is_long and decision.new_sl > ctx.current_sl)
                )
                if sl_worsened:
                    logger.info(
                        "[RiskGovernor] BLOCKED SL worsening for {} — "
                        "proposed {:.5f} worse than current {:.5f}",
                        ctx.symbol, decision.new_sl, ctx.current_sl,
                    )
                    return ManagementDecision(
                        action=Action.HOLD,
                        reason=f"[GOVERNOR BLOCKED] SL worsening prevented: "
                               f"proposed {decision.new_sl:.5f} vs current {ctx.current_sl:.5f}. "
                               f"Original: {decision.reason}",
                        confidence=decision.confidence,
                        evidence=decision.evidence + ["governor_blocked: sl_worsening"],
                    )

        return decision
