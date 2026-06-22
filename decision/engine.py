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
from brain.smoothing import clamp, smoothstep


# Reason prefix stamped on a severe-decay hard-close. main_loop matches this to
# persist a counterfactual shadow ("would it have hit its stop anyway, or did we
# sell a future winner?") so the hard-close can be validated, not just trusted.
SEVERE_THESIS_CLOSE_PREFIX = "SEVERE thesis collapse while in profit"

# Exit-cause tag (an ExitCause value string) stamped on a CLOSE that the
# fast-cluster opposition decay contributed to, so the executor can attribute
# the exit to that management behaviour for the learners (PR10).
FAST_OPPOSITION_EXIT_CAUSE = "fast_opposition_decay"


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
        reversal_weighted_evidence: bool = False,
        reversal_required_strength: float = 2.0,
        reversal_momentum_full: float = 0.6,
        htf_aligned_size_bonus: float = 0.15,
        htf_aligned_threshold: float = 0.5,
        scalp_htf_scale: float = 0.0,
        swing_htf_scale: float = 1.0,
        mixed_htf_scale: float = 0.5,
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
        fast_opposition_decay_enabled: bool = True,
        fast_opposition_min_streak: int = 3,
        fast_opposition_max_streak: int = 8,
        fast_opposition_decay_weight: float = 0.30,
        fast_opposition_profit_threshold: float = 0.3,
        tf_conflict_aware: bool = False,
        soften_gate: bool = False,
        gate_safety_margin: float = -1.0,
        gate_quality_floor: float = 0.15,
        conviction_size_min: float = 0.5,
        conviction_size_max: float = 1.5,
        market_mode_threshold: float = 0.40,
        adopted_observation_minutes: float = 10.0,
        consensus_aware: bool = True,
        consensus_enter_weight: float = 0.30,
        consensus_skip_weight: float = 0.45,
        consensus_min_alignment: float = 0.15,
        consensus_conviction_weight: float = 0.15,
        consensus_high_authority_veto: bool = True,
        range_edge_required: bool = False,
        range_edge_htf_min: float = 0.20,
        range_edge_consensus_min: float = 0.40,
        range_edge_skip_penalty: float = 0.10,
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
        # #17 — weighted reversal evidence. Legacy counts independent signals
        # (M5 sweep + M1 BOS + momentum) and gates on an integer ≥ count, so a
        # +0.21 momentum reads identical to +0.95 and two strong signals lose to
        # three weak ones. When enabled, each signal contributes a continuous
        # strength and the gate compares the summed strength to a threshold.
        self.reversal_weighted_evidence = bool(reversal_weighted_evidence)
        self.reversal_required_strength = max(0.0, float(reversal_required_strength))
        self.reversal_momentum_full = max(1e-6, float(reversal_momentum_full))
        # HTF = bounded context — size bonus when the full stack agrees.
        self.htf_aligned_size_bonus = max(0.0, htf_aligned_size_bonus)
        self.htf_aligned_threshold = htf_aligned_threshold
        # HTF demotion by ranker horizon — a fast SCALP idea should not be
        # vetoed by an opposing HTF it does not trade on; a SWING idea should
        # still respect it. Scales enter/skip/conviction HTF weights. A trade
        # with no ranker horizon ("") keeps full authority (scale 1.0).
        self.scalp_htf_scale = max(0.0, min(1.0, scalp_htf_scale))
        self.swing_htf_scale = max(0.0, min(1.0, swing_htf_scale))
        self.mixed_htf_scale = max(0.0, min(1.0, mixed_htf_scale))
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
        # PR10 — fast-cluster opposition decay (loser stuck against the current).
        self.fast_opposition_decay_enabled = fast_opposition_decay_enabled
        self.fast_opposition_min_streak = max(1, int(fast_opposition_min_streak))
        self.fast_opposition_max_streak = max(
            self.fast_opposition_min_streak, int(fast_opposition_max_streak)
        )
        self.fast_opposition_decay_weight = max(0.0, fast_opposition_decay_weight)
        self.fast_opposition_profit_threshold = fast_opposition_profit_threshold
        # #5 — TF-conflict awareness. When on, the enter/skip scoring reads the
        # per-timeframe alignment VECTOR (not just the collapsed scalar) so a
        # split stack (e.g. D1+H1 support but H4 opposes) erodes the HTF ENTER
        # bonus and adds a bounded SKIP penalty — instead of the averaged scalar
        # hiding the conflict. Scaled by the same horizon demotion as the other
        # HTF weights, so a demoted SCALP is unaffected. Inert when off.
        self.tf_conflict_aware = bool(tf_conflict_aware)
        # #6 — enter/skip dimmer. When ``soften_gate`` is on (set by the caller
        # only when the orchestrator is the final sizer), a non-positive ENTER/SKIP
        # margin no longer hard-kills the setup: as long as the margin stays above
        # ``gate_safety_margin`` (a genuinely hopeless floor that still hard-SKIPs)
        # the trade flows through as ENTER carrying a bounded quality multiplier in
        # ``[gate_quality_floor, 1.0]``, leaving the final "how big?" to the
        # orchestrator round table. Inert by default → legacy hard-SKIP behaviour.
        self.soften_gate = bool(soften_gate)
        self.gate_safety_margin = float(gate_safety_margin)
        self.gate_quality_floor = max(0.0, min(1.0, float(gate_quality_floor)))
        # #18 — conviction → size multiplier as a continuous, configurable range.
        # Conviction 0→``conviction_size_min``, 1→``conviction_size_max``, mapped
        # linearly (no tier cliffs). Defaults (0.5–1.5) preserve the legacy
        # mapping exactly; widen (e.g. 0.25–2.0) without code changes. The
        # dominant conviction dimension is surfaced in the decision evidence so
        # sizing provenance is visible, not hidden behind one clamped scalar.
        lo = float(conviction_size_min)
        hi = float(conviction_size_max)
        if hi < lo:
            lo, hi = hi, lo
        self.conviction_size_min = max(0.0, lo)
        self.conviction_size_max = max(self.conviction_size_min, hi)
        # #29 — MARKET vs PENDING preference cutoff. The *inputs* feeding the
        # market score are now smooth ramps (no 0.3/0.5/80 cliffs); this is the
        # final preference threshold on the already-continuous score.
        self.market_mode_threshold = max(0.0, min(1.0, float(market_mode_threshold)))
        # Adopted/orphan trades get a protective observation window (config-driven,
        # was a hardcoded 10-minute literal at the decide_management gate).
        self.adopted_observation_minutes = max(0.0, float(adopted_observation_minutes))
        # ── Directional-consensus integration ───────────────────────────
        # The per-module vote panel becomes a decision dimension: it adds ENTER
        # support when it agrees with the trade direction, SKIP pressure when it
        # opposes, and feeds conviction (→ size). A high-authority module
        # opposing with confidence is a hard veto. Bounded so it informs rather
        # than dominates; fully inert when ``consensus_aware`` is False.
        self.consensus_aware = bool(consensus_aware)
        self.consensus_enter_weight = max(0.0, float(consensus_enter_weight))
        self.consensus_skip_weight = max(0.0, float(consensus_skip_weight))
        self.consensus_min_alignment = max(0.0, float(consensus_min_alignment))
        self.consensus_conviction_weight = max(0.0, float(consensus_conviction_weight))
        self.consensus_high_authority_veto = bool(consensus_high_authority_veto)
        # ── No-directional-edge range guard ──────────────────────────────
        # A flat "range" setup (|tf_alignment| < range_edge_htf_min) with no
        # strong directional consensus has no statistical reason to favour
        # either side, yet the baseline ENTER inclination + zone-shape / R:R
        # bonuses can still net a positive margin and open a directional trade
        # on noise — the dominant slow-bleed loss pattern. When enabled, such
        # no-edge ranges are forced to a non-softenable SKIP unless real
        # direction is present: the HTF stack (|tf_alignment| ≥ range_edge_htf_min)
        # or the module panel (consensus_alignment ≥ range_edge_consensus_min).
        # Inert by default → legacy "trade the zone on R:R alone" behaviour.
        self.range_edge_required = bool(range_edge_required)
        self.range_edge_htf_min = max(0.0, float(range_edge_htf_min))
        self.range_edge_consensus_min = max(0.0, float(range_edge_consensus_min))
        self.range_edge_skip_penalty = max(0.0, float(range_edge_skip_penalty))

    @staticmethod
    def _tf_conflict_opposition(sa: SituationAssessment) -> float:
        """Magnitude of per-timeframe opposition the scalar tf_alignment hides.

        The scalar is a weighted average, so a strongly-opposing timeframe can be
        averaged into a mild net value that reads like consensus. This returns the
        summed magnitude (bounded [0, 1]) of the timeframe components whose sign
        opposes the net read — 0.0 when the stack genuinely agrees (or there is no
        vector to inspect).
        """
        comps = sa.tf_vector() if hasattr(sa, "tf_vector") else {}
        if not comps:
            return 0.0
        try:
            vals = [float(v) for v in comps.values()]
        except (TypeError, ValueError):
            return 0.0
        if not vals:
            return 0.0
        net = float(getattr(sa, "tf_alignment", 0.0))
        if net >= 0:
            opp = sum(-v for v in vals if v < 0)
        else:
            opp = sum(v for v in vals if v > 0)
        return max(0.0, min(1.0, opp))

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

    def _horizon_htf_scale(self, horizon: str) -> float:
        """HTF authority multiplier for a ranker-selected trade's horizon.

        ``1.0`` (full authority, unchanged) for any trade with no ranker horizon
        — e.g. the scalar fallback. SCALP/SWING/MIXED map to the configured
        scales so a fast idea is judged on its lower-timeframe evidence rather
        than being overruled by an opposing higher timeframe.
        """
        h = str(horizon or "").upper()
        if h == "SCALP":
            return self.scalp_htf_scale
        if h == "SWING":
            return self.swing_htf_scale
        if h == "MIXED":
            return self.mixed_htf_scale
        return 1.0

    def _apply_horizon_scaling(self, w: DecisionWeights, horizon: str) -> DecisionWeights:
        """Scale HTF enter/skip/conviction weights by the trade's horizon.

        Mirrors :meth:`_weights_for_regime` — the freed HTF conviction mass is
        moved onto momentum so the conviction weights still sum to the same
        total. Inert (returns ``w`` unchanged) when the scale is 1.0.
        """
        s = self._horizon_htf_scale(horizon)
        if s >= 1.0:
            return w
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

    def _reversal_evidence_strength(
        self, ctx: EntryContext, sa: SituationAssessment
    ) -> tuple[float, list[str]]:
        """#17: continuous reversal-evidence strength for a counter-HTF setup.

        Mirrors :meth:`_reversal_evidence` but each signal contributes a graded
        strength instead of a binary +1:

        * **M5 sweep** and **aligned M1 BOS/CHoCH** are discrete events → 1.0 each
          when present.
        * **Momentum** ramps continuously from 0 at ``reversal_min_momentum`` to
          1.0 at ``reversal_momentum_full`` (and can exceed 1.0 for very strong
          momentum, capped at 1.5), so +0.21 and +0.95 no longer read the same.

        Returns ``(strength, labels)``; the caller gates on
        ``strength >= reversal_required_strength``.
        """
        labels: list[str] = []
        strength = 0.0
        et = str(ctx.entry_type).upper()
        mc = str(ctx.micro_confirmation).lower()
        if "SWEEP" in et or "sweep" in mc:
            strength += 1.0
            labels.append("M5 sweep")
        m1ev = str(ctx.m1_event).upper()
        aligned_dir = ("BULLISH" in m1ev) if ctx.is_long else ("BEARISH" in m1ev)
        if ("BOS" in m1ev or "CHOCH" in m1ev) and aligned_dir:
            strength += 1.0
            labels.append("M1 BOS")
        if sa.momentum >= self.reversal_min_momentum:
            span = max(self.reversal_momentum_full - self.reversal_min_momentum, 1e-6)
            mom_strength = (sa.momentum - self.reversal_min_momentum) / span
            mom_strength = max(0.0, min(1.5, mom_strength))
            strength += mom_strength
            labels.append(f"momentum {sa.momentum:+.2f}×{mom_strength:.2f}")
        return strength, labels

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

    def _fast_opposition_pressure(
        self, ctx: TradeContext,
    ) -> tuple[float, list[str]]:
        """Bounded CLOSE pressure from a sustained fast-cluster opposition (PR10).

        Returns ``(close_pressure, reasons)``. Inert (0.0, []) unless the
        feature is on, the fast-evidence cluster has opposed the position for at
        least ``fast_opposition_min_streak`` consecutive cycles, AND the trade is
        NOT meaningfully in profit (``profit_r`` below the threshold). The
        pressure ramps linearly with the streak up to ``fast_opposition_max_streak``
        and is capped by ``fast_opposition_decay_weight`` — additive only, never
        a hard override, and never applied to a winner.
        """
        if not self.fast_opposition_decay_enabled:
            return 0.0, []
        streak = int(getattr(ctx, "fast_opposition_streak", 0) or 0)
        if streak < self.fast_opposition_min_streak:
            return 0.0, []
        if ctx.profit_r >= self.fast_opposition_profit_threshold:
            return 0.0, []

        ramp = min(streak / max(self.fast_opposition_max_streak, 1), 1.0)
        pressure = self.fast_opposition_decay_weight * ramp
        reason = (
            f"fast cluster opposing {streak} cycles "
            f"(profit {ctx.profit_r:+.1f}R)"
        )
        logger.info(
            "FAST_OPP_DECAY: {} streak={} pressure={:.2f} profit_r={:.2f}",
            ctx.symbol, streak, pressure, ctx.profit_r,
        )
        return pressure, [reason]

    def decide_management(
        self,
        ctx: TradeContext,
        sa: SituationAssessment,
    ) -> ManagementDecision:
        if ctx.is_adopted and ctx.hold_minutes < self.adopted_observation_minutes:
            return self._decide_adopted_observation(ctx, sa)

        scores: dict[Action, float] = {}
        reasons: dict[Action, str] = {}
        evidence_map: dict[Action, list[str]] = {}

        # Live OQ/EQ decay pressure (P3) — folded into the CLOSE/TIGHTEN scores
        # below. Computed once; inert when the live scores are unavailable.
        oq_close_pressure, oq_tighten_pressure, oq_eq_reasons = (
            self._oq_eq_decay_pressure(ctx)
        )

        # Fast-cluster opposition decay (PR10) — bounded CLOSE pressure when the
        # fast-evidence cluster has opposed a non-winning position for several
        # consecutive cycles. Computed once; inert when off / streak too short /
        # in profit.
        fast_opp_pressure, fast_opp_reasons = self._fast_opposition_pressure(ctx)

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

        # Live directional consensus — the unbiased module panel re-voted on
        # fresh data. When it now OPPOSES the open position the thesis that
        # justified the trade no longer stands, so add bounded CLOSE pressure
        # (additive, capped — never dominates the trade's own structure/R). The
        # full panel is in sa.consensus_components; this reads the signed scalar.
        if self.consensus_aware:
            consensus_align = float(getattr(sa, "consensus_alignment", 0.0) or 0.0)
            if consensus_align < -self.consensus_min_alignment:
                penalty = min(abs(consensus_align) * self.consensus_skip_weight, 0.30)
                close_score += penalty
                comps = sa.consensus_vector() if hasattr(sa, "consensus_vector") else {}
                against = list(comps.get("against", []) or [])
                close_reason_parts.append(
                    f"consensus opposes ({consensus_align:+.2f})"
                    + (f" [{', '.join(against)}]" if against else "")
                )

        # Live OQ/EQ decay — the conditions/geometry that justified this trade
        if oq_close_pressure > 0.0:
            close_score += oq_close_pressure
            close_reason_parts.extend(oq_eq_reasons)

        # Fast-cluster opposition decay (PR10) — bounded additive CLOSE pressure
        # for a non-winning trade the fast cluster has opposed for N cycles.
        if fast_opp_pressure > 0.0:
            close_score += fast_opp_pressure
            close_reason_parts.extend(fast_opp_reasons)

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

        # PR10: attribute the close to fast-cluster opposition decay when that
        # pressure contributed to a CLOSE verdict, so the executor tags the
        # learners' exit_cause feature accordingly.
        if best_action == Action.CLOSE and fast_opp_pressure > 0.0:
            decision.exit_cause = FAST_OPPOSITION_EXIT_CAUSE

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
        # HTF demotion — when the opportunity ranker selected this trade, scale
        # HTF authority by its horizon so a fast SCALP idea is judged on its
        # lower-timeframe evidence instead of being overruled by an opposing
        # higher timeframe. Inert (no change) when no ranker horizon is set.
        horizon = str(getattr(ctx, "horizon", "") or "")
        htf_scale = self._horizon_htf_scale(horizon)
        if htf_scale < 1.0:
            w = self._apply_horizon_scaling(w, horizon)
            evidence.append(
                f"HTF demoted ×{htf_scale:.2f} ({horizon.upper()} horizon)"
            )

        # ── ENTER score ──────────────────────────────────────────────────
        enter_score = 0.20  # baseline: slight inclination to trade

        # #5 — hidden per-timeframe opposition the scalar tf_alignment averages
        # away. Bounded [0, 1]; 0 when the stack agrees or the feature is off.
        tf_opp = self._tf_conflict_opposition(sa) if self.tf_conflict_aware else 0.0

        if sa.tf_alignment > 0.2:
            contrib = sa.tf_alignment * w.enter_htf
            if tf_opp > 0:
                # A split stack erodes the HTF ENTER bonus — the supportive net
                # value is partly an illusion created by averaging.
                contrib *= max(0.0, 1.0 - tf_opp)
                evidence.append(
                    f"HTF aligned ({sa.tf_alignment:+.2f}) but split "
                    f"(opp {tf_opp:.2f}) +{contrib:.2f}"
                )
            else:
                evidence.append(f"HTF aligned ({sa.tf_alignment:+.2f}) +{contrib:.2f}")
            enter_score += contrib

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

        # ── Directional consensus → ENTER support ────────────────────────
        # When the module panel agrees with the trade direction it adds bounded
        # ENTER conviction (scaled by how strongly it aligns). The full panel is
        # in sa.consensus_components — this reads the signed alignment but the
        # SKIP side below also inspects the dissent structure.
        consensus_align = float(getattr(sa, "consensus_alignment", 0.0) or 0.0)
        if self.consensus_aware and consensus_align > self.consensus_min_alignment:
            contrib = consensus_align * self.consensus_enter_weight
            enter_score += contrib
            comps = sa.consensus_vector() if hasattr(sa, "consensus_vector") else {}
            for_mods = ", ".join(comps.get("for", [])[:4]) or "panel"
            evidence.append(
                f"consensus supports ({consensus_align:+.2f}) [{for_mods}] +{contrib:.2f}"
            )

        # ── SKIP score ───────────────────────────────────────────────────
        skip_score = 0.0
        skip_parts: list[str] = []

        if sa.tf_alignment < -0.1:
            penalty = abs(sa.tf_alignment) * w.skip_htf
            skip_score += penalty
            skip_parts.append(f"HTF opposing ({sa.tf_alignment:+.2f}) +{penalty:.2f}")

        # #5 — a net-supportive scalar that nonetheless hides a strongly-opposing
        # timeframe still adds a bounded SKIP penalty (scaled by w.skip_htf, which
        # is already horizon-demoted, so a SCALP idea is unaffected). This is what
        # the averaged scalar alone could never express.
        if tf_opp > 0 and sa.tf_alignment >= -0.1:
            penalty = tf_opp * w.skip_htf
            if penalty > 0:
                skip_score += penalty
                skip_parts.append(
                    f"HTF split (hidden opposition {tf_opp:.2f}) +{penalty:.2f}"
                )

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

        # ── Directional consensus → SKIP pressure + high-authority veto ──
        # The module panel opposing the trade direction adds SKIP pressure
        # scaled by how strongly it dissents. A high-authority module (e.g.
        # currency_strength) opposing with confidence is a hard veto: a large
        # SKIP penalty that, combined with the bounded ENTER side, drives the
        # margin negative so the trade is not taken. This consumes the full
        # dissent structure, not a collapsed score.
        if self.consensus_aware:
            comps = sa.consensus_vector() if hasattr(sa, "consensus_vector") else {}
            ha_oppose = list(comps.get("high_authority_oppose", []) or [])
            if consensus_align < -self.consensus_min_alignment:
                penalty = abs(consensus_align) * self.consensus_skip_weight
                skip_score += penalty
                against = ", ".join(comps.get("against", [])[:4]) or "panel"
                skip_parts.append(
                    f"consensus opposes ({consensus_align:+.2f}) [{against}] +{penalty:.2f}"
                )
            if self.consensus_high_authority_veto and ha_oppose:
                # Hard veto contribution — a high-authority dissent should be
                # decisive, not averaged away.
                veto_pen = 0.60
                skip_score += veto_pen
                skip_parts.append(
                    f"high-authority consensus veto [{', '.join(ha_oppose)}] +{veto_pen:.2f}"
                )

        # ── Reversal trade type (roadmap E) ──────────────────────────────
        # A counter-HTF setup is only taken as a REVERSAL when it carries strong
        # lower-timeframe evidence (M5 sweep + M1 BOS + momentum). Without enough
        # evidence it's a falling-knife counter-trend → push to SKIP. Qualified
        # reversals are allowed but sized DOWN (haircut applied below).
        is_reversal = False
        if self.reversal_enabled and self._is_counter_htf(ctx):
            if self.reversal_weighted_evidence:
                ev_strength, ev_labels = self._reversal_evidence_strength(ctx, sa)
                if ev_strength >= self.reversal_required_strength:
                    is_reversal = True
                    evidence.append(f"counter-HTF reversal [{', '.join(ev_labels)}]")
                else:
                    skip_score += self.reversal_no_evidence_skip_penalty
                    skip_parts.append(
                        f"counter-trend without reversal evidence "
                        f"(strength {ev_strength:.2f}/{self.reversal_required_strength:.2f}) "
                        f"+{self.reversal_no_evidence_skip_penalty:.2f}"
                    )
            else:
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

        # ── No-directional-edge range guard ──────────────────────────────
        # When neither the HTF stack nor a strong module-panel consensus
        # supplies direction, a flat "range" setup is being entered on zone
        # shape + R:R alone — no statistical edge, just transaction-cost decay.
        # Neutralise the accumulated ENTER inclination and force a SKIP. A
        # qualified reversal carries an HTF lean (handled above) and is exempt.
        range_no_edge = False
        if (
            self.range_edge_required
            and not is_reversal
            and abs(sa.tf_alignment) < self.range_edge_htf_min
        ):
            has_consensus_edge = (
                self.consensus_aware
                and consensus_align >= self.range_edge_consensus_min
            )
            if not has_consensus_edge:
                range_no_edge = True
                penalty = enter_score + self.range_edge_skip_penalty
                skip_score += penalty
                skip_parts.append(
                    f"no directional edge (range align {sa.tf_alignment:+.2f}, "
                    f"consensus {consensus_align:+.2f}) +{penalty:.2f}"
                )

        # ── Pick winner ──────────────────────────────────────────────────
        margin = enter_score - skip_score
        gate_softened = False
        de_quality_mult = 1.0
        if margin <= 0:
            # #6 — bounded dimmer instead of a hard kill. The enter/skip binary
            # (margin <= 0 → SKIP) was the last CRITICAL collapse: a marginally
            # negative margin (-0.01) died exactly like a hopeless one (-5.0),
            # and the setup never reached the orchestrator round table. When the
            # orchestrator is the final sizer (``soften_gate``) and the margin is
            # only *mildly* negative (above the hard ``gate_safety_margin`` floor),
            # let the trade flow through as ENTER carrying a bounded quality
            # multiplier instead — the round table decides *how big*, not whether.
            # A margin at/below the safety floor is genuinely hopeless → hard SKIP.
            try:
                soften = self.soften_gate and margin > self.gate_safety_margin
            except Exception:
                soften = False
            # A no-edge range never softens — there is no thesis for the
            # orchestrator round table to size; it is a genuine no-trade.
            if range_no_edge:
                soften = False
            if not soften:
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
                    entry_margin=margin,
                    evidence=skip_parts,
                )
            gate_softened = True
            # margin in (gate_safety_margin, 0] → multiplier in (gate_quality_floor, 1.0].
            # e.g. margin -0.01 → ~0.99 (nearly full), margin -0.50 → 0.50, and a
            # deeply negative margin clamps up from the quality floor.
            de_quality_mult = round(max(1.0 + margin, self.gate_quality_floor), 4)
            evidence.append(
                f"DE gate softened: margin={margin:+.2f} → quality×{de_quality_mult:.2f} "
                f"(orchestrator sizes)"
            )

        # ── Decide MARKET vs PENDING ─────────────────────────────────────
        entry_action = self._decide_entry_action(ctx, sa)

        conviction = self.compute_conviction(sa, weights=w)
        size_mult = self._conviction_to_size_multiplier(conviction)
        dom_label, dom_contrib = self._dominant_conviction_dimension(sa, w)
        evidence.append(
            f"conviction {conviction:.2f} (driven by {dom_label} +{dom_contrib:.2f}) "
            f"→ size×{size_mult:.2f}"
        )
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
            f"{' [DE-SOFTENED]' if gate_softened else ''}"
        )
        return EntryDecision(
            action=entry_action,
            reason=reason,
            confidence=max(0.0, min(1.0, margin + 0.3)),
            conviction=conviction,
            size_multiplier=size_mult,
            entry_margin=margin,
            gate_softened=gate_softened,
            de_quality_multiplier=de_quality_mult,
            evidence=evidence,
        )

    def _decide_entry_action(
        self, ctx: EntryContext, sa: SituationAssessment,
    ) -> EntryAction:
        """Choose MARKET vs PENDING based on situation, not fixed rules.

        #29 — the contributions are smooth ramps that *complete* at the old
        thresholds, so a value just below a boundary earns ~95% of its weight
        instead of zero (no cliff), while values at/above the old threshold are
        unchanged. The micro-confirmation term stays discrete (it is a named
        event, not a continuous scalar)."""
        if ctx.entry_mode == "MARKET":
            return EntryAction.ENTER_MARKET

        market_score = 0.0
        # momentum: full weight by the old 0.3 boundary, ramped in from 0.1.
        market_score += 0.30 * smoothstep(sa.momentum, 0.1, 0.3)
        # tf_alignment: full weight by the old 0.5 boundary, ramped from 0.3.
        market_score += 0.20 * smoothstep(sa.tf_alignment, 0.3, 0.5)
        # discrete micro-confirmation event.
        if ctx.micro_confirmation in ("choch_bos", "engulfing", "pin_bar"):
            market_score += 0.25
        # scan_score: full weight by the old 80 boundary, ramped from 60.
        market_score += 0.15 * smoothstep(float(ctx.scan_score), 60.0, 80.0)

        if market_score >= self.market_mode_threshold:
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
        # Consensus nudges conviction (→ size) within a bounded band: panel
        # agreement lifts it, dissent trims it. Kept small so the panel informs
        # sizing without overwhelming the structural conviction model.
        if self.consensus_aware and self.consensus_conviction_weight > 0.0:
            align = float(getattr(sa, "consensus_alignment", 0.0) or 0.0)
            c += align * self.consensus_conviction_weight
        return max(0.0, min(1.0, c))

    @staticmethod
    def _dominant_conviction_dimension(
        sa: SituationAssessment, w: DecisionWeights,
    ) -> tuple[str, float]:
        """Return the (label, weighted-contribution) of the conviction
        dimension that contributed most — sizing provenance for #18."""
        contribs = {
            "HTF": (sa.tf_alignment + 1.0) / 2.0 * w.conviction_htf,
            "structure": sa.structure_integrity * w.conviction_structure,
            "momentum": (sa.momentum + 1.0) / 2.0 * w.conviction_momentum,
            "confidence": sa.read_confidence * w.conviction_confidence,
        }
        label = max(contribs, key=lambda k: contribs[k])
        return label, round(contribs[label], 3)

    def _conviction_to_size_multiplier(self, conviction: float) -> float:
        """Map conviction 0–1 to a size multiplier on a continuous, configurable
        range (#18). Linear interpolation between ``conviction_size_min`` and
        ``conviction_size_max`` — no tier cliffs, so 0.879 and 0.851 map to
        distinct multipliers. Defaults (0.5–1.5) reproduce the legacy mapping."""
        c = clamp(conviction, 0.0, 1.0)
        lo, hi = self.conviction_size_min, self.conviction_size_max
        return round(lo + (hi - lo) * c, 3)
