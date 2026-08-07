"""
Risk Governor — independent safety layer with veto authority.
Sits after the Decision Engine.  Can override any decision if it
violates capital protection rules.  Never downstream of intelligence.
"""

from __future__ import annotations

from loguru import logger

from decision.actions import Action, EntryAction, EntryDecision, ManagementDecision
from decision.context import EntryContext, TradeContext
from decision.situation import SituationAssessment
from risk import risk_accumulation as ra


class RiskGovernor:
    """Reviews decisions and applies safety overrides.

    Rules:
    - Never worsen a stop loss.
    - Override HOLD→CLOSE when the situation is catastrophically bad
      (very low alignment + broken structure + deep loss).
    - Veto SCALE_IN when portfolio heat is high.
    - All overrides are logged with full reasoning.

    ``graded_risk`` (off by default → exact legacy first-breach behaviour) flips
    the entry review from a first-breach kill into an *accumulated* read: every
    analytical dimension (portfolio heat, spread, R:R) is measured and folded
    into a bounded size multiplier the orchestrator applies, while genuine
    physics (position limits) stay a hard veto.  ``risk_floor`` bounds that
    multiplier from below — a graded "near every limit" is still a small trade,
    never zero.
    """

    def __init__(self, *, graded_risk: bool = False, risk_floor: float = 0.15) -> None:
        self.graded_risk = bool(graded_risk)
        try:
            self.risk_floor = max(0.0, min(1.0, float(risk_floor)))
        except (TypeError, ValueError):
            self.risk_floor = 0.15

    def review(
        self,
        decision: ManagementDecision,
        ctx: TradeContext,
        sa: SituationAssessment,
    ) -> ManagementDecision:

        # ── Scale-in vs portfolio heat (#35: graded proximity, not binary) ───
        # Adding risk while already hot is the genuinely dangerous move, so the
        # *hard* veto at the cap stays.  But in graded mode the *approach* to the
        # cap trims the add size proportionally (a heat of 1.45 vs a 1.5 cap
        # shrinks the scale-in rather than waiting for a cliff), so the dimmer
        # acts before the kill.
        if decision.action == Action.SCALE_IN:
            heat_cap = 1.5
            if ctx.portfolio_heat_pct > heat_cap:
                logger.info(
                    "[RiskGovernor] VETO SCALE_IN for {} — portfolio heat {:.1f}%",
                    ctx.symbol, ctx.portfolio_heat_pct,
                )
                return ManagementDecision(
                    action=Action.HOLD,
                    reason=f"[GOVERNOR VETO] Scale-in blocked: portfolio heat "
                           f"{ctx.portfolio_heat_pct:.1f}% > {heat_cap:.1f}%. "
                           f"Original: {decision.reason}",
                    confidence=decision.confidence,
                    evidence=decision.evidence + ["governor_veto: high_heat"],
                )
            if self.graded_risk and decision.scale_lots > 0:
                heat_dim = ra.dimension(
                    "scale_in_heat", ctx.portfolio_heat_pct, heat_cap,
                    f"heat {ctx.portfolio_heat_pct:.1f}% vs cap {heat_cap:.1f}%",
                )
                if heat_dim.near_breach:
                    score = ra.accumulate([heat_dim], floor=self.risk_floor)
                    trimmed = round(decision.scale_lots * score.multiplier, 2)
                    if trimmed > 0 and trimmed < decision.scale_lots:
                        logger.info(
                            "[RiskGovernor] TRIM SCALE_IN for {} — heat {:.1f}% near cap "
                            "{:.1f}%: lots {:.2f} → {:.2f} (×{:.2f})",
                            ctx.symbol, ctx.portfolio_heat_pct, heat_cap,
                            decision.scale_lots, trimmed, score.multiplier,
                        )
                        return ManagementDecision(
                            action=Action.SCALE_IN,
                            reason=f"[GOVERNOR TRIM] Scale-in sized down: heat "
                                   f"{ctx.portfolio_heat_pct:.1f}% near cap {heat_cap:.1f}% "
                                   f"(×{score.multiplier:.2f}). Original: {decision.reason}",
                            confidence=decision.confidence,
                            new_sl=decision.new_sl,
                            partial_ratio=decision.partial_ratio,
                            scale_lots=trimmed,
                            evidence=decision.evidence + [
                                f"governor_trim: scale_in_heat ×{score.multiplier:.2f}",
                            ],
                            exit_cause=decision.exit_cause,
                        )

        # ── Override HOLD→CLOSE in catastrophic situations ───────────────
        # The hard CLOSE at the extreme thresholds is genuine safety and stays
        # unchanged.  In graded mode, a situation that is *near* catastrophic but
        # has not tripped all three thresholds escalates HOLD→protective stop
        # (a softer de-risk) instead of waiting for the cliff.
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
            if self.graded_risk and decision.action == Action.HOLD:
                escalation = self._near_catastrophe_escalation(decision, ctx, sa)
                if escalation is not None:
                    return escalation

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

    def _near_catastrophe_escalation(
        self,
        decision: ManagementDecision,
        ctx: TradeContext,
        sa: SituationAssessment,
    ) -> ManagementDecision | None:
        """Graded protective escalation for a near-catastrophic HOLD (#35).

        Measures each catastrophe condition's proximity to its threshold; when
        their combined read is high (most conditions near-tripped) but the hard
        all-three CLOSE did not fire, escalate the HOLD to a protective stop —
        a softer de-risk that acts on the *approach* instead of the cliff.
        """
        dims = [
            ra.dimension(
                "cat_alignment", -float(sa.tf_alignment), 0.6,
                f"alignment {sa.tf_alignment:+.2f}",
            ),
            ra.dimension(
                "cat_structure", float(sa.structure_integrity), 0.15,
                f"structure {sa.structure_integrity:.2f}", lower_is_riskier=True,
            ),
            ra.dimension(
                "cat_profit", -float(sa.profit_state), 1.0,
                f"profit {sa.profit_state:+.1f}R",
            ),
        ]
        near = sum(1 for d in dims if d.proximity >= ra.NEAR_BREACH_AT)
        # Require a broad near-miss (>=2 of 3 conditions close) before nudging a
        # HOLD to a protective stop — a single soft dimension must not churn.
        if near < 2:
            return None
        logger.info(
            "[RiskGovernor] ESCALATE HOLD→protective stop for {} — near-catastrophe "
            "({}/3 conditions near limit)", ctx.symbol, near,
        )
        return ManagementDecision(
            action=Action.SET_PROTECTIVE_STOP,
            reason=(
                f"[GOVERNOR ESCALATE] Near-catastrophe ({near}/3 conditions near limit): "
                f"alignment={sa.tf_alignment:+.2f}, structure={sa.structure_integrity:.2f}, "
                f"profit={sa.profit_state:+.1f}R. Original HOLD: {decision.reason}"
            ),
            confidence=decision.confidence,
            evidence=decision.evidence + [
                "governor_escalate: near_catastrophe",
                f"near_conditions={near}/3",
            ],
        )

    def review_entry(
        self,
        decision: EntryDecision,
        ctx: EntryContext,
        sa: SituationAssessment,
    ) -> EntryDecision:
        """Safety review for entry decisions.

        Legacy (``graded_risk`` off): first-breach hard veto on max-trades,
        portfolio heat, spread and R:R.

        Graded (``graded_risk`` on, #24): measure EVERY dimension instead of
        short-circuiting.  Physics (position limit) stays a hard veto; the
        analytical dimensions (heat, spread, R:R) fold into a bounded
        ``risk_multiplier`` the orchestrator sizes by — a near-/over-limit
        analytical dimension dims the trade rather than killing it.
        """
        if not decision.should_enter:
            return decision

        if not self.graded_risk:
            return self._review_entry_legacy(decision, ctx, sa)

        # ── Physics veto — position limit is a hard cap, never graded ────────
        if ctx.open_trade_count >= ctx.max_open_trades:
            veto_reason = (
                f"max trades reached ({ctx.open_trade_count}/{ctx.max_open_trades})"
            )
            logger.info(
                "[RiskGovernor] VETO entry {} {} — {}",
                ctx.direction, ctx.symbol, veto_reason,
            )
            return EntryDecision(
                action=EntryAction.SKIP,
                reason=f"[GOVERNOR VETO] {veto_reason}. Original: {decision.reason}",
                confidence=decision.confidence,
                conviction=0.0,
                size_multiplier=0.0,
                evidence=decision.evidence + [f"governor_veto: {veto_reason}"],
                governor_vetoed=True,
                governor_reason=veto_reason,
            )

        # ── Analytical dimensions — accumulate, never first-breach kill ──────
        dims: list[ra.RiskDimension] = []
        dims.append(ra.dimension(
            "portfolio_heat", ctx.portfolio_heat_pct, 1.8,
            f"heat {ctx.portfolio_heat_pct:.1f}% vs 1.8%",
        ))
        if ctx.typical_spread > 0:
            dims.append(ra.dimension(
                "spread", ctx.current_spread, ctx.typical_spread * 3.0,
                f"spread {ctx.current_spread:.1f} vs 3× typical "
                f"({ctx.typical_spread * 3.0:.1f})",
            ))
        dims.append(ra.dimension(
            "risk_reward", ctx.risk_reward_2, 1.0,
            f"R:R to TP2 {ctx.risk_reward_2:.2f} vs 1.0", lower_is_riskier=True,
        ))

        score = ra.accumulate(dims, floor=self.risk_floor)
        if score.multiplier >= 1.0 - 1e-9:
            return decision

        near = score.near_breaches + score.breaches
        logger.info(
            "[RiskGovernor] graded entry {} {} — risk ×{:.2f} ({})",
            ctx.direction, ctx.symbol, score.multiplier,
            ", ".join(near) if near else "all clear",
        )
        return EntryDecision(
            action=decision.action,
            reason=(
                f"{decision.reason} | [GOVERNOR GRADED] risk ×{score.multiplier:.2f}"
                + (f" near/over: {', '.join(near)}" if near else "")
            ),
            confidence=decision.confidence,
            conviction=decision.conviction,
            size_multiplier=decision.size_multiplier,
            entry_margin=decision.entry_margin,
            gate_softened=decision.gate_softened,
            de_quality_multiplier=decision.de_quality_multiplier,
            risk_multiplier=score.multiplier,
            risk_near_breaches=near,
            evidence=decision.evidence + [
                f"governor_graded_risk: ×{score.multiplier:.2f}",
                *[f"risk_dim: {d.name}@{d.proximity:.0%}" for d in dims],
            ],
        )

    def _review_entry_legacy(
        self,
        decision: EntryDecision,
        ctx: EntryContext,
        sa: SituationAssessment,
    ) -> EntryDecision:
        """Original first-breach hard-veto entry review (physical/broker only)."""
        vetoed = False
        veto_reason = ""

        if ctx.open_trade_count >= ctx.max_open_trades:
            vetoed = True
            veto_reason = f"max trades reached ({ctx.open_trade_count}/{ctx.max_open_trades})"

        if not vetoed and ctx.portfolio_heat_pct >= 1.8:
            vetoed = True
            veto_reason = f"portfolio heat {ctx.portfolio_heat_pct:.1f}% >= 1.8%"

        if not vetoed and ctx.typical_spread > 0:
            spread_ratio = ctx.current_spread / ctx.typical_spread if ctx.typical_spread > 0 else 0
            if spread_ratio > 3.0:
                vetoed = True
                veto_reason = f"spread {ctx.current_spread:.1f} is {spread_ratio:.1f}× typical"

        if not vetoed and ctx.risk_reward_2 < 1.0:
            vetoed = True
            veto_reason = f"R:R to TP2 below 1:1 ({ctx.risk_reward_2:.2f})"

        if vetoed:
            logger.info(
                "[RiskGovernor] VETO entry {} {} — {}",
                ctx.direction, ctx.symbol, veto_reason,
            )
            return EntryDecision(
                action=EntryAction.SKIP,
                reason=f"[GOVERNOR VETO] {veto_reason}. Original: {decision.reason}",
                confidence=decision.confidence,
                conviction=0.0,
                size_multiplier=0.0,
                evidence=decision.evidence + [f"governor_veto: {veto_reason}"],
                governor_vetoed=True,
                governor_reason=veto_reason,
            )

        return decision
