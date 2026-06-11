"""
Decision Engine — takes a SituationAssessment + TradeContext and
selects the best action via continuous weighted scoring.

No hard-coded thresholds that override context.  Every action gets a
score; the highest wins.  The weights themselves come from the
situation dimensions, not from lookup tables.
"""

from __future__ import annotations

from loguru import logger

from decision.actions import Action, ManagementDecision
from decision.context import TradeContext
from decision.situation import SituationAssessment


class DecisionEngine:
    """Scores every possible action and picks the best one."""

    def decide_management(
        self,
        ctx: TradeContext,
        sa: SituationAssessment,
    ) -> ManagementDecision:
        if ctx.is_adopted and ctx.hold_minutes < 10:
            return self._decide_adopted_observation(ctx, sa)

        scores: dict[Action, float] = {}
        reasons: dict[Action, str] = {}
        evidence_map: dict[Action, list[str]] = {}

        # ── HOLD ─────────────────────────────────────────────────────────
        hold_score = 0.30  # moderate base — default action
        hold_reason_parts = []

        if sa.tf_alignment > 0.2:
            hold_score += sa.tf_alignment * 0.35
            hold_reason_parts.append(f"HTF aligned ({sa.tf_alignment:+.2f})")
        if sa.structure_integrity > 0.5:
            hold_score += (sa.structure_integrity - 0.5) * 0.30
            hold_reason_parts.append(f"structure intact ({sa.structure_integrity:.2f})")
        if sa.profit_state > 0:
            hold_score += min(sa.profit_state * 0.05, 0.15)
        if sa.momentum > 0.1:
            hold_score += sa.momentum * 0.10

        scores[Action.HOLD] = hold_score
        reasons[Action.HOLD] = "; ".join(hold_reason_parts) if hold_reason_parts else "default hold"
        evidence_map[Action.HOLD] = list(hold_reason_parts)

        # ── CLOSE ────────────────────────────────────────────────────────
        close_score = 0.0
        close_reason_parts = []

        if sa.tf_alignment < -0.3:
            penalty = abs(sa.tf_alignment) * 0.35
            close_score += penalty
            close_reason_parts.append(f"HTF opposing ({sa.tf_alignment:+.2f})")
        if sa.structure_integrity < 0.25:
            close_score += (0.25 - sa.structure_integrity) * 1.0
            close_reason_parts.append(f"structure broken ({sa.structure_integrity:.2f})")
        if sa.profit_state < -0.5 and sa.tf_alignment < 0:
            close_score += min(abs(sa.profit_state) * 0.10, 0.20)
            close_reason_parts.append(f"in loss ({sa.profit_state:.1f}R) against trend")
        if sa.momentum < -0.3 and sa.profit_state < 0:
            close_score += abs(sa.momentum) * 0.15
            close_reason_parts.append(f"adverse momentum ({sa.momentum:+.2f})")
        if sa.urgency > 0.8:
            close_score += sa.urgency * 0.20
            close_reason_parts.append(f"high urgency ({sa.urgency:.2f})")

        # Opposing scan direction with high score
        if ctx.scan_direction:
            scan_opposing = (
                (ctx.is_long and ctx.scan_direction == "SHORT")
                or (not ctx.is_long and ctx.scan_direction == "LONG")
            )
            if scan_opposing and ctx.scan_score >= 65:
                boost = (ctx.scan_score - 65) / 35.0 * 0.30
                close_score += boost
                close_reason_parts.append(
                    f"opposing scan signal ({ctx.scan_direction} score={ctx.scan_score})"
                )

        scores[Action.CLOSE] = close_score
        reasons[Action.CLOSE] = "; ".join(close_reason_parts) if close_reason_parts else "no close pressure"
        evidence_map[Action.CLOSE] = list(close_reason_parts)

        # ── TIGHTEN_SL ───────────────────────────────────────────────────
        tighten_score = 0.0
        tighten_reason = []

        if sa.profit_state > 1.5 and sa.momentum < 0:
            tighten_score += 0.35
            tighten_reason.append(f"in profit ({sa.profit_state:.1f}R) but momentum fading")
        if sa.profit_state > 2.0 and sa.tf_alignment < 0.1:
            tighten_score += 0.20
            tighten_reason.append("extended profit, alignment weakening")
        if sa.urgency > 0.5 and sa.profit_state > 0.5:
            tighten_score += 0.15
            tighten_reason.append("urgency with profit to protect")

        scores[Action.TIGHTEN_SL] = tighten_score
        reasons[Action.TIGHTEN_SL] = "; ".join(tighten_reason) if tighten_reason else "no tighten signal"
        evidence_map[Action.TIGHTEN_SL] = list(tighten_reason)

        # ── MOVE_TO_BREAKEVEN ────────────────────────────────────────────
        be_score = 0.0
        be_reason = []

        if not ctx.at_breakeven and sa.profit_state > 0.8 and sa.momentum < -0.1:
            be_score += 0.40
            be_reason.append(f"profit at {sa.profit_state:.1f}R, momentum turning")
        if not ctx.at_breakeven and sa.urgency > 0.6 and sa.profit_state > 0.3:
            be_score += 0.30
            be_reason.append("urgency with some profit")

        scores[Action.MOVE_TO_BREAKEVEN] = be_score
        reasons[Action.MOVE_TO_BREAKEVEN] = "; ".join(be_reason) if be_reason else "no BE signal"
        evidence_map[Action.MOVE_TO_BREAKEVEN] = list(be_reason)

        # ── Select highest-scoring action ────────────────────────────────
        best_action = max(scores, key=lambda a: scores[a])
        best_score = scores[best_action]

        if best_score < 0.15:
            best_action = Action.HOLD
            best_score = scores[Action.HOLD]

        confidence = min(1.0, best_score)
        margin = best_score - sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else best_score

        reason_str = self._build_reason(
            best_action, reasons[best_action], sa, ctx, margin,
        )

        decision = ManagementDecision(
            action=best_action,
            reason=reason_str,
            confidence=confidence,
            evidence=evidence_map.get(best_action, []),
        )

        if best_action == Action.TIGHTEN_SL:
            decision.new_sl = self._compute_tightened_sl(ctx)

        return decision

    def _decide_adopted_observation(
        self, ctx: TradeContext, sa: SituationAssessment,
    ) -> ManagementDecision:
        """Adopted trades get observation period with protective stop."""
        evidence = [
            f"adopted trade, held {ctx.hold_minutes:.0f}min",
            f"tf_alignment={sa.tf_alignment:+.2f}",
            f"structure={sa.structure_integrity:.2f}",
        ]

        protective_sl = self._compute_protective_stop(ctx, sa)

        if sa.tf_alignment < -0.5 and sa.structure_integrity < 0.2:
            return ManagementDecision(
                action=Action.CLOSE,
                reason=f"Adopted trade clearly against market: "
                       f"tf_alignment={sa.tf_alignment:+.2f}, "
                       f"structure={sa.structure_integrity:.2f}",
                confidence=0.7,
                evidence=evidence,
            )

        if protective_sl is not None:
            return ManagementDecision(
                action=Action.SET_PROTECTIVE_STOP,
                reason=f"Observing adopted trade. Setting protective stop at structure. "
                       f"D1={ctx.d1_trend}, H4={ctx.h4_trend}",
                confidence=0.5,
                new_sl=protective_sl,
                evidence=evidence,
            )

        return ManagementDecision(
            action=Action.OBSERVE,
            reason=f"Observing adopted trade, building context. "
                   f"D1={ctx.d1_trend}, H4={ctx.h4_trend}, "
                   f"alignment={sa.tf_alignment:+.2f}",
            confidence=0.4,
            evidence=evidence,
        )

    def _compute_tightened_sl(self, ctx: TradeContext) -> float | None:
        if ctx.original_risk_pips < 1e-8:
            return None
        pip_size = ctx.original_risk_pips  # already in price terms
        tighten_distance = pip_size * 0.5
        if ctx.is_long:
            new_sl = ctx.current_price - tighten_distance
            if new_sl <= ctx.current_sl:
                return None
        else:
            new_sl = ctx.current_price + tighten_distance
            if new_sl >= ctx.current_sl:
                return None
        return round(new_sl, 5)

    def _compute_protective_stop(
        self, ctx: TradeContext, sa: SituationAssessment,
    ) -> float | None:
        """Place stop at nearest structure level for adopted trades."""
        if ctx.is_long:
            candidates = [
                v for v in [ctx.h4_swing_low, ctx.h1_swing_low, ctx.d1_swing_low]
                if v is not None and v < ctx.current_price
            ]
            if candidates:
                return round(max(candidates) - abs(ctx.current_price * 0.0005), 5)
        else:
            candidates = [
                v for v in [ctx.h4_swing_high, ctx.h1_swing_high, ctx.d1_swing_high]
                if v is not None and v > ctx.current_price
            ]
            if candidates:
                return round(min(candidates) + abs(ctx.current_price * 0.0005), 5)
        return None

    def _build_reason(
        self,
        action: Action,
        detail: str,
        sa: SituationAssessment,
        ctx: TradeContext,
        margin: float,
    ) -> str:
        return (
            f"[{sa.primary_label}] {action.value}: {detail} | "
            f"alignment={sa.tf_alignment:+.2f} momentum={sa.momentum:+.2f} "
            f"structure={sa.structure_integrity:.2f} profit={sa.profit_state:+.1f}R "
            f"urgency={sa.urgency:.2f} confidence={sa.read_confidence:.2f} "
            f"margin={margin:.2f}"
        )
