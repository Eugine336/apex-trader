"""
Decision Engine — takes a SituationAssessment + TradeContext and
selects the best action via continuous weighted scoring.

No hard-coded thresholds that override context.  Every action gets a
score; the highest wins.  The weights themselves come from the
situation dimensions, not from lookup tables.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from decision.actions import Action, EntryAction, EntryDecision, ManagementDecision
from decision.context import EntryContext, TradeContext
from decision.situation import SituationAssessment


# Reason prefix stamped on a severe-decay hard-close. main_loop matches this to
# persist a counterfactual shadow ("would it have hit its stop anyway, or did we
# sell a future winner?") so the hard-close can be validated, not just trusted.
SEVERE_THESIS_CLOSE_PREFIX = "SEVERE thesis collapse while in profit"


@dataclass(frozen=True)
class DecisionWeights:
    """Weights for the entry decision/conviction model (roadmap C).

    Defaults make M5 the primary decision engine: M5 structure quality and M1
    momentum carry the ENTER/SKIP decision and trade SIZE, while HTF (D1/H4/H1)
    alignment is context. Override from DecisionConfig to retune without code
    changes.
    """

    # Conviction (≈ sum 1.0) → size multiplier.
    conviction_htf: float = 0.20
    conviction_structure: float = 0.40
    conviction_momentum: float = 0.30
    conviction_confidence: float = 0.10
    # ENTER/SKIP scoring coefficients.
    enter_htf: float = 0.20
    enter_structure: float = 0.45
    enter_momentum: float = 0.30
    skip_htf: float = 0.20
    skip_momentum: float = 0.30


class DecisionEngine:
    """Scores every possible action and picks the best one."""

    def __init__(
        self,
        weights: DecisionWeights | None = None,
        *,
        regime_weighting_enabled: bool = True,
        regime_ranging_htf_scale: float = 0.5,
        reversal_enabled: bool = True,
        reversal_min_momentum: float = 0.2,
        reversal_required_evidence: int = 3,
        reversal_size_multiplier: float = 0.5,
        reversal_no_evidence_skip_penalty: float = 0.30,
        htf_aligned_size_bonus: float = 0.15,
        htf_aligned_threshold: float = 0.5,
        thesis_secure_enabled: bool = True,
        thesis_secure_min_profit_usd: float = 15.0,
        thesis_secure_min_profit_pips: float = 12.0,
        thesis_deterioration_threshold: float = 0.35,
        thesis_close_threshold: float = 0.80,
        thesis_healthy_structure: float = 0.5,
        thesis_healthy_momentum: float = 0.0,
        thesis_lock_fraction: float = 0.5,
        thesis_struct_ref: float = 0.6,
        thesis_conviction_cycles: int = 3,
        thesis_conviction_drop: float = 10.0,
        thesis_conviction_full_drop: float = 30.0,
        oq_eq_decay_enabled: bool = True,
        oq_floor: float = 5.0,
        eq_floor: float = 5.0,
        oq_decay_significant: float = 2.0,
    ) -> None:
        self.weights = weights or DecisionWeights()
        # Roadmap D — regime-dependent weighting.
        self.regime_weighting_enabled = regime_weighting_enabled
        self.regime_ranging_htf_scale = max(0.0, min(1.0, regime_ranging_htf_scale))
        # Roadmap E — reversal trade type.
        self.reversal_enabled = reversal_enabled
        self.reversal_min_momentum = reversal_min_momentum
        self.reversal_required_evidence = int(reversal_required_evidence)
        self.reversal_size_multiplier = max(0.0, min(1.0, reversal_size_multiplier))
        self.reversal_no_evidence_skip_penalty = max(0.0, reversal_no_evidence_skip_penalty)
        # HTF = bounded context — size bonus when the full stack agrees.
        self.htf_aligned_size_bonus = max(0.0, htf_aligned_size_bonus)
        self.htf_aligned_threshold = htf_aligned_threshold
        # Roadmap G — thesis-deterioration secure (R-independent profit protection).
        self.thesis_secure_enabled = thesis_secure_enabled
        self.thesis_secure_min_profit_usd = max(0.0, thesis_secure_min_profit_usd)
        self.thesis_secure_min_profit_pips = max(0.0, thesis_secure_min_profit_pips)
        self.thesis_deterioration_threshold = thesis_deterioration_threshold
        self.thesis_close_threshold = thesis_close_threshold
        self.thesis_healthy_structure = thesis_healthy_structure
        self.thesis_healthy_momentum = thesis_healthy_momentum
        self.thesis_lock_fraction = max(0.0, min(1.0, thesis_lock_fraction))
        self.thesis_struct_ref = thesis_struct_ref
        self.thesis_conviction_cycles = int(thesis_conviction_cycles)
        self.thesis_conviction_drop = max(0.0, thesis_conviction_drop)
        self.thesis_conviction_full_drop = max(1e-6, thesis_conviction_full_drop)
        # P3 — live OQ/EQ decay management.
        self.oq_eq_decay_enabled = oq_eq_decay_enabled
        self.oq_floor = oq_floor
        self.eq_floor = eq_floor
        self.oq_decay_significant = max(0.0, oq_decay_significant)

    # ── Roadmap D/E helpers ───────────────────────────────────────────────

    def _weights_for_regime(self, regime: str) -> DecisionWeights:
        """D: in ranging/reversal regimes, shift influence off the (slow) HTF
        and onto M1 momentum — HTF reacts last, so it matters less when price is
        ranging or reversing. Trending regimes keep the base M5-primary weights.
        Neutral when regime weighting is disabled."""
        if not self.regime_weighting_enabled:
            return self.weights
        r = str(regime).upper()
        is_ranging = ("RANG" in r) or ("REVERS" in r) or ("CHOP" in r)
        if not is_ranging:
            return self.weights
        w = self.weights
        s = self.regime_ranging_htf_scale
        htf_conv_freed = w.conviction_htf * (1.0 - s)  # keep conviction sum stable
        return DecisionWeights(
            conviction_htf=w.conviction_htf * s,
            conviction_structure=w.conviction_structure,
            conviction_momentum=w.conviction_momentum + htf_conv_freed,
            conviction_confidence=w.conviction_confidence,
            enter_htf=w.enter_htf * s,
            enter_structure=w.enter_structure,
            enter_momentum=w.enter_momentum,
            skip_htf=w.skip_htf * s,
            skip_momentum=w.skip_momentum,
        )

    @staticmethod
    def _is_counter_htf(ctx: EntryContext) -> bool:
        """True when the trade direction opposes the H4 trend (continuation vs
        reversal). Uses the H4 trend string, tolerating enum/`Trend.X` forms."""
        h4 = str(ctx.h4_trend).upper()
        return ("BEARISH" in h4) if ctx.is_long else ("BULLISH" in h4)

    def _reversal_evidence(
        self, ctx: EntryContext, sa: SituationAssessment
    ) -> tuple[int, list[str]]:
        """Count the independent reversal signals for a counter-HTF setup:
        an M5 liquidity sweep, an aligned M1 BOS/CHoCH, and strong momentum."""
        labels: list[str] = []
        et = str(ctx.entry_type).upper()
        mc = str(ctx.micro_confirmation).lower()
        if "SWEEP" in et or "sweep" in mc:
            labels.append("M5 sweep")
        m1ev = str(ctx.m1_event).upper()
        aligned_dir = ("BULLISH" in m1ev) if ctx.is_long else ("BEARISH" in m1ev)
        if ("BOS" in m1ev or "CHOCH" in m1ev) and aligned_dir:
            labels.append("M1 BOS")
        if sa.momentum >= self.reversal_min_momentum:
            labels.append(f"momentum {sa.momentum:+.2f}")
        return len(labels), labels

    def _oq_eq_decay_pressure(
        self, ctx: TradeContext,
    ) -> tuple[float, float, list[str]]:
        """Bounded CLOSE/TIGHTEN pressure from live OQ/EQ decay (P3).

        Returns ``(close_pressure, tighten_pressure, reasons)``. Inert (all
        zero) when the decay feature is off or the live scores were not
        recomputed this cycle (``live_oq``/``live_eq`` is None) — so the entry
        path and any context without fresh quality scores are unaffected.
        """
        if not self.oq_eq_decay_enabled:
            return 0.0, 0.0, []

        close_pressure = 0.0
        tighten_pressure = 0.0
        reasons: list[str] = []

        live_oq = ctx.live_oq
        if live_oq is not None:
            if live_oq < self.oq_floor:
                # The market conditions that justified entry are gone.
                severity = min((self.oq_floor - live_oq) / max(self.oq_floor, 1e-6), 1.0)
                close_pressure += severity * 0.30
                tighten_pressure += severity * 0.25
                reasons.append(f"OQ collapsed ({live_oq:.1f} < {self.oq_floor:.1f})")
            elif (
                ctx.oq_decay is not None
                and ctx.oq_decay > self.oq_decay_significant
            ):
                # Still above the floor but deteriorating fast — protect profit.
                extra = ctx.oq_decay - self.oq_decay_significant
                tighten_pressure += min(extra * 0.08, 0.20)
                reasons.append(f"OQ decayed {ctx.oq_decay:.1f} since entry")

        live_eq = ctx.live_eq
        if live_eq is not None and live_eq < self.eq_floor:
            # Entry geometry degraded — tighten rather than ride a poor location.
            severity = min((self.eq_floor - live_eq) / max(self.eq_floor, 1e-6), 1.0)
            tighten_pressure += severity * 0.25
            reasons.append(f"EQ degraded ({live_eq:.1f} < {self.eq_floor:.1f})")

        return close_pressure, tighten_pressure, reasons

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

        # Live OQ/EQ decay pressure (P3) — folded into the CLOSE/TIGHTEN scores
        # below. Computed once; inert when the live scores are unavailable.
        oq_close_pressure, oq_tighten_pressure, oq_eq_reasons = (
            self._oq_eq_decay_pressure(ctx)
        )

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

        # Independent trade manager (roadmap F): HTF informs exits but must not
        # DOMINATE them. Its CLOSE weight is demoted below the trade's OWN
        # signals (structure ×1.0, momentum, loss-R) so a lagging HTF flip
        # cannot force a close on its own — the trade's structure / R / momentum
        # govern the exit. (HOLD-side HTF support is benign and left unchanged.)
        mgmt_close_htf_coeff = 0.15  # was 0.35
        if sa.tf_alignment < -0.3:
            penalty = abs(sa.tf_alignment) * mgmt_close_htf_coeff
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
        # Active loss-response: a losing trade whose read is no longer clearly
        # supportive should be acted on EARLY rather than ridden passively to the
        # broker stop. Engages only once structure/momentum stops supporting the
        # trade, and scales with how deep the loss is — so a healthy pullback in
        # an intact trend (structure ≥ 0.5 and momentum ≥ 0) is still held.
        if sa.profit_state < -0.6 and (sa.structure_integrity < 0.5 or sa.momentum < 0.0):
            depth = min(abs(sa.profit_state), 2.0)
            close_score += min(0.10 + (depth - 0.6) * 0.25, 0.45)
            close_reason_parts.append(
                f"active loss-response ({sa.profit_state:.1f}R, "
                f"structure={sa.structure_integrity:.2f}, momentum={sa.momentum:+.2f})"
            )
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

        # Live OQ/EQ decay — the conditions/geometry that justified this trade
        # have deteriorated since entry (P3). Bounded additive pressure.
        if oq_close_pressure > 0.0:
            close_score += oq_close_pressure
            close_reason_parts.extend(oq_eq_reasons)

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

        if oq_tighten_pressure > 0.0:
            tighten_score += oq_tighten_pressure
            tighten_reason.extend(oq_eq_reasons)

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

        # ── Thesis-deterioration secure (roadmap G) ──────────────────────
        # If the normal scoring would default to HOLD, ask the trader's
        # question — "is the reason I'm holding still valid?" — instead of
        # riding a decaying winner because no R-gate could score. Overrides
        # ONLY a would-be HOLD; a real CLOSE/TIGHTEN/BE verdict is respected.
        if best_action == Action.HOLD and self.thesis_secure_enabled:
            secure = self._maybe_thesis_secure(ctx, sa)
            if secure is not None:
                return secure

        confidence = min(1.0, best_score)
        margin = best_score - sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else best_score

        # P3: surface when live OQ/EQ decay drove a protective verdict.
        if (
            oq_eq_reasons
            and best_action in (Action.CLOSE, Action.TIGHTEN_SL)
            and ctx.live_oq is not None
        ):
            logger.info(
                "OQ_DECAY: {} entry_oq={} live_oq={:.1f} decay={} → {}",
                ctx.symbol,
                f"{ctx.entry_oq:.1f}" if ctx.entry_oq is not None else "n/a",
                ctx.live_oq,
                f"{ctx.oq_decay:.1f}" if ctx.oq_decay is not None else "n/a",
                best_action.value,
            )

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

    # ── Thesis-deterioration secure (roadmap G) ──────────────────────────
    def _thesis_deterioration_score(
        self, ctx: TradeContext, sa: SituationAssessment,
    ) -> tuple[float, list[str]]:
        """Score how far the *reason for holding* has decayed (0..1).

        Combines the trade's own live signals — structure weakening, momentum
        turning against it, declining re-score conviction (the conviction-
        collapse signal that otherwise only runs in the dormant legacy path),
        and a bounded HTF-flip term — into a single deterioration score.
        """
        evidence: list[str] = []
        score = 0.0

        # Structure weakening (primary thesis component).
        if sa.structure_integrity < self.thesis_struct_ref:
            comp = (self.thesis_struct_ref - sa.structure_integrity) / max(self.thesis_struct_ref, 1e-6)
            score += min(comp, 1.0) * 0.35
            evidence.append(f"structure {sa.structure_integrity:.2f}")

        # Momentum turned against the position — the clearest intraday "thesis
        # dying" signal.
        if sa.momentum < 0:
            score += min(abs(sa.momentum), 1.0) * 0.40
            evidence.append(f"momentum {sa.momentum:+.2f}")

        # Conviction collapse — declining re-score over the recent window.
        hist = ctx.score_history or []
        if len(hist) >= self.thesis_conviction_cycles:
            window = hist[-self.thesis_conviction_cycles:]
            drop = window[0] - window[-1]
            if drop >= self.thesis_conviction_drop:
                score += 0.20 * min(drop / self.thesis_conviction_full_drop, 1.0)
                evidence.append(f"conviction {window[0]}->{window[-1]}")

        # HTF flipped against (bounded — HTF is demoted context).
        if sa.tf_alignment < 0:
            score += min(abs(sa.tf_alignment), 1.0) * 0.10
            evidence.append(f"HTF {sa.tf_alignment:+.2f}")

        return min(score, 1.0), evidence

    def _compute_profit_lock_sl(self, ctx: TradeContext) -> float | None:
        """Stop that banks a fraction of the OPEN profit (R-independent).

        Unlike ``_compute_tightened_sl`` (which keys off the reconstructed
        original risk and is therefore meaningless for adopted trades), this
        locks ``thesis_lock_fraction`` of the *current economic profit* into
        the stop. Returns None if it would not improve on the current stop.
        """
        from config import get_pip_size

        if ctx.pnl_pips <= 0:
            return None
        pip = get_pip_size(ctx.symbol)
        lock_pips = ctx.pnl_pips * self.thesis_lock_fraction
        if ctx.is_long:
            new_sl = ctx.entry_price + lock_pips * pip
            if new_sl <= ctx.current_sl:
                return None
        else:
            new_sl = ctx.entry_price - lock_pips * pip
            if new_sl >= ctx.current_sl:
                return None
        return round(new_sl, 5)

    def _maybe_thesis_secure(
        self, ctx: TradeContext, sa: SituationAssessment,
    ) -> ManagementDecision | None:
        """Secure profit when economic profit exists AND the thesis is decaying.

        Returns a ManagementDecision (SET_PROTECTIVE_STOP locking part of the
        profit, or MOVE_TO_BREAKEVEN if a profit-lock stop is not yet an
        improvement) when the trader's "is the reason still valid?" test fails
        while in real profit — otherwise None (let HOLD stand).
        """
        econ_profit = (
            (self.thesis_secure_min_profit_usd > 0 and ctx.pnl_dollars >= self.thesis_secure_min_profit_usd)
            or (self.thesis_secure_min_profit_pips > 0 and ctx.pnl_pips >= self.thesis_secure_min_profit_pips)
        )
        if not econ_profit:
            return None

        # Healthy pullback in an intact trend — keep holding, don't exit noise.
        if (
            sa.structure_integrity >= self.thesis_healthy_structure
            and sa.momentum >= self.thesis_healthy_momentum
        ):
            return None

        deterioration, det_evidence = self._thesis_deterioration_score(ctx, sa)
        if deterioration < self.thesis_deterioration_threshold:
            return None

        confidence = min(1.0, deterioration)

        # ── Severe decay → bank profit at market ─────────────────────────
        # When most dimensions are collapsing at once, securing the stop just
        # gives the move back as price runs to it. Above the close threshold,
        # close the (profitable) trade instead of trailing a doomed runner.
        if deterioration >= self.thesis_close_threshold:
            return ManagementDecision(
                action=Action.CLOSE,
                reason=(
                    f"SEVERE thesis collapse while in profit "
                    f"(+{ctx.pnl_pips:.1f}p/${ctx.pnl_dollars:.2f}, "
                    f"deterioration {deterioration:.2f}: {', '.join(det_evidence)})"
                ),
                confidence=confidence,
                evidence=det_evidence,
            )

        reason = (
            f"thesis decay while in profit "
            f"(+{ctx.pnl_pips:.1f}p/${ctx.pnl_dollars:.2f}, "
            f"deterioration {deterioration:.2f}: {', '.join(det_evidence)})"
        )
        new_sl = self._compute_profit_lock_sl(ctx)
        if new_sl is not None:
            return ManagementDecision(
                action=Action.SET_PROTECTIVE_STOP,
                reason=reason,
                confidence=confidence,
                new_sl=new_sl,
                evidence=det_evidence,
            )
        # Cannot lock past the current stop yet (e.g. dollar profit from swap
        # but ~0 pips) — secure breakeven, but only if that improves the stop.
        if not ctx.at_breakeven:
            be_improves = (
                (ctx.is_long and ctx.current_sl < ctx.entry_price)
                or (not ctx.is_long and ctx.current_sl > ctx.entry_price)
            )
            if be_improves:
                return ManagementDecision(
                    action=Action.MOVE_TO_BREAKEVEN,
                    reason=reason,
                    confidence=confidence,
                    evidence=det_evidence,
                )
        return None

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
        from config import get_pip_size
        risk_price = ctx.original_risk_pips * get_pip_size(ctx.symbol)
        tighten_distance = risk_price * 0.5
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

    # ── Entry decisions ─────────────────────────────────────────────────

    def decide_entry(
        self,
        ctx: EntryContext,
        sa: SituationAssessment,
    ) -> EntryDecision:
        """Score ENTER vs SKIP using situation dimensions — no hard thresholds."""
        evidence: list[str] = []
        # Roadmap D — pick regime-appropriate weights (ranging/reversal shifts
        # influence off HTF onto M1 momentum; trending keeps the base weights).
        w = self._weights_for_regime(ctx.regime)

        # ── ENTER score ──────────────────────────────────────────────────
        enter_score = 0.20  # baseline: slight inclination to trade

        if sa.tf_alignment > 0.2:
            contrib = sa.tf_alignment * w.enter_htf
            enter_score += contrib
            evidence.append(f"HTF aligned ({sa.tf_alignment:+.2f}) +{contrib:.2f}")

        if sa.structure_integrity > 0.5:
            contrib = (sa.structure_integrity - 0.5) * w.enter_structure
            enter_score += contrib
            evidence.append(f"structure quality ({sa.structure_integrity:.2f}) +{contrib:.2f}")

        if sa.momentum > 0.1:
            contrib = sa.momentum * w.enter_momentum
            enter_score += contrib
            evidence.append(f"supportive momentum ({sa.momentum:+.2f}) +{contrib:.2f}")

        if ctx.risk_reward_2 > 2.0:
            rr_bonus = min((ctx.risk_reward_2 - 2.0) * 0.05, 0.15)
            enter_score += rr_bonus
            evidence.append(f"good R:R ({ctx.risk_reward_2:.1f}) +{rr_bonus:.2f}")

        if sa.read_confidence > 0.7:
            enter_score += 0.05
            evidence.append(f"high data confidence ({sa.read_confidence:.2f})")

        # ── SKIP score ───────────────────────────────────────────────────
        skip_score = 0.0
        skip_parts: list[str] = []

        if sa.tf_alignment < -0.1:
            penalty = abs(sa.tf_alignment) * w.skip_htf
            skip_score += penalty
            skip_parts.append(f"HTF opposing ({sa.tf_alignment:+.2f}) +{penalty:.2f}")

        if sa.structure_integrity < 0.3:
            penalty = (0.3 - sa.structure_integrity) * 0.60
            skip_score += penalty
            skip_parts.append(f"weak structure ({sa.structure_integrity:.2f}) +{penalty:.2f}")

        if sa.momentum < -0.2:
            penalty = abs(sa.momentum) * w.skip_momentum
            skip_score += penalty
            skip_parts.append(f"opposing momentum ({sa.momentum:+.2f}) +{penalty:.2f}")

        if sa.urgency > 0.5:
            penalty = sa.urgency * 0.25
            skip_score += penalty
            skip_parts.append(f"high urgency ({sa.urgency:.2f}) +{penalty:.2f}")

        if sa.read_confidence < 0.4:
            penalty = (0.4 - sa.read_confidence) * 0.30
            skip_score += penalty
            skip_parts.append(f"low confidence ({sa.read_confidence:.2f}) +{penalty:.2f}")

        if ctx.risk_reward_2 < 1.5:
            penalty = (1.5 - ctx.risk_reward_2) * 0.20
            skip_score += penalty
            skip_parts.append(f"weak R:R ({ctx.risk_reward_2:.1f}) +{penalty:.2f}")

        # ── Reversal trade type (roadmap E) ──────────────────────────────
        # A counter-HTF setup is only taken as a REVERSAL when it carries strong
        # lower-timeframe evidence (M5 sweep + M1 BOS + momentum). Without enough
        # evidence it's a falling-knife counter-trend → push to SKIP. Qualified
        # reversals are allowed but sized DOWN (haircut applied below).
        is_reversal = False
        if self.reversal_enabled and self._is_counter_htf(ctx):
            ev_count, ev_labels = self._reversal_evidence(ctx, sa)
            if ev_count >= self.reversal_required_evidence:
                is_reversal = True
                evidence.append(f"counter-HTF reversal [{', '.join(ev_labels)}]")
            else:
                skip_score += self.reversal_no_evidence_skip_penalty
                skip_parts.append(
                    f"counter-trend without reversal evidence "
                    f"({ev_count}/{self.reversal_required_evidence}) "
                    f"+{self.reversal_no_evidence_skip_penalty:.2f}"
                )

        # ── Pick winner ──────────────────────────────────────────────────
        margin = enter_score - skip_score
        if margin <= 0:
            reason = (
                f"[{sa.primary_label}] SKIP: {'; '.join(skip_parts)} | "
                f"enter={enter_score:.2f} skip={skip_score:.2f} margin={margin:.2f}"
            )
            return EntryDecision(
                action=EntryAction.SKIP,
                reason=reason,
                confidence=min(1.0, abs(margin) + 0.3),
                conviction=0.0,
                size_multiplier=0.0,
                evidence=skip_parts,
            )

        # ── Decide MARKET vs PENDING ─────────────────────────────────────
        entry_action = self._decide_entry_action(ctx, sa)

        conviction = self.compute_conviction(sa, weights=w)
        size_mult = self._conviction_to_size_multiplier(conviction)
        if is_reversal:
            # Reversals run smaller until they prove themselves (roadmap E).
            size_mult = round(size_mult * self.reversal_size_multiplier, 2)
        elif (
            self.htf_aligned_size_bonus > 0.0
            and not self._is_counter_htf(ctx)
            and sa.tf_alignment >= self.htf_aligned_threshold
        ):
            # HTF = bounded context: when the full stack agrees, size up a
            # bounded amount (capped). Mutually exclusive with the reversal
            # haircut — a trade is either with-HTF or counter-HTF, never both.
            size_mult = round(min(size_mult * (1.0 + self.htf_aligned_size_bonus), 2.0), 2)
            evidence.append(
                f"HTF stack aligned ({sa.tf_alignment:+.2f}) — size +{self.htf_aligned_size_bonus:.0%}"
            )

        reason = (
            f"[{sa.primary_label}] {entry_action.value}: "
            f"{'; '.join(evidence[:4])} | "
            f"enter={enter_score:.2f} skip={skip_score:.2f} margin={margin:.2f} "
            f"conviction={conviction:.2f} size×{size_mult:.2f}"
            f"{' [REVERSAL]' if is_reversal else ''}"
        )
        return EntryDecision(
            action=entry_action,
            reason=reason,
            confidence=min(1.0, margin + 0.3),
            conviction=conviction,
            size_multiplier=size_mult,
            evidence=evidence,
        )

    def _decide_entry_action(
        self, ctx: EntryContext, sa: SituationAssessment,
    ) -> EntryAction:
        """Choose MARKET vs PENDING based on situation, not fixed rules."""
        if ctx.entry_mode == "MARKET":
            return EntryAction.ENTER_MARKET

        market_score = 0.0
        if sa.momentum > 0.3:
            market_score += 0.30
        if sa.tf_alignment > 0.5:
            market_score += 0.20
        if ctx.micro_confirmation in ("choch_bos", "engulfing", "pin_bar"):
            market_score += 0.25
        if ctx.scan_score >= 80:
            market_score += 0.15

        if market_score >= 0.40:
            return EntryAction.ENTER_MARKET
        return EntryAction.ENTER_PENDING

    def compute_conviction(
        self, sa: SituationAssessment, weights: DecisionWeights | None = None
    ) -> float:
        """Continuous conviction score from situation dimensions."""
        w = weights or self.weights
        c = (
            (sa.tf_alignment + 1.0) / 2.0 * w.conviction_htf
            + sa.structure_integrity * w.conviction_structure
            + (sa.momentum + 1.0) / 2.0 * w.conviction_momentum
            + sa.read_confidence * w.conviction_confidence
        )
        return max(0.0, min(1.0, c))

    @staticmethod
    def _conviction_to_size_multiplier(conviction: float) -> float:
        """Map conviction 0–1 to size multiplier 0.5–1.5."""
        return round(0.5 + conviction, 2)
