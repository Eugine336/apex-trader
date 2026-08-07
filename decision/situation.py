"""
Situation Engine — reads the analysis outputs and computes continuous
situation dimensions.  Situations EMERGE from data — they are never
hard-coded lookup tables.  Labels are generated AFTER the math, for
the decision journal only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decision.context import EntryContext, TradeContext


def compute_in_trade_context_pressure(ctx: TradeContext) -> tuple[int, int, list[str]]:
    """Summarise the independent signals currently OPPOSING an open trade.

    Diagnostic aggregation of evidence the management builders already gather:
    opposing HTF/M5 structure breaks, an opposing scanner bias, an opposing live
    consensus panel, a sustained fast-opposition streak, and being in loss.
    Returns ``(context_pressure, opposing_boost, details)`` where
    ``context_pressure`` is the count of opposing signals, ``opposing_boost`` is
    the subset that are structural breaks, and ``details`` lists each.

    This populates the previously-dead ``TradeContext.context_pressure`` /
    ``opposing_boost`` / ``pressure_details`` fields with real values for
    journaling and dashboards.  It is intentionally NOT added as a separate
    CLOSE term in :meth:`DecisionEngine.decide_management` — those same opposing
    signals are already scored individually there (structure, consensus,
    fast-opposition, OQ/EQ decay), so re-scoring this aggregate would
    double-count and over-close a trade.
    """
    is_long = ctx.is_long
    details: list[str] = []

    def _opposes(event: str) -> bool:
        ev = str(event or "").upper()
        directional = ("BEARISH" in ev) if is_long else ("BULLISH" in ev)
        return directional and ("BOS" in ev or "CHOCH" in ev)

    structural = 0
    for tf, ev in (
        ("D1", ctx.d1_event), ("H4", ctx.h4_event),
        ("H1", ctx.h1_event), ("M5", ctx.m5_event),
    ):
        if _opposes(ev):
            structural += 1
            details.append(f"{tf} {ev}")

    pressure = structural

    scan_opposing = (
        (is_long and ctx.scan_direction == "SHORT")
        or (not is_long and ctx.scan_direction == "LONG")
    )
    if scan_opposing and ctx.scan_score >= 65:
        pressure += 1
        details.append(f"scan {ctx.scan_direction} {ctx.scan_score}")

    votes = list(getattr(ctx, "consensus_votes", None) or [])
    if votes:
        trade_dir = "LONG" if is_long else "SHORT"
        for_mag = 0.0
        against_mag = 0.0
        for v in votes:
            vdir = str(getattr(v, "direction", "") or "").upper()
            if vdir not in ("LONG", "SHORT"):
                continue
            mag = max(0.0, float(getattr(v, "confidence", 0.0) or 0.0)) * max(
                0.0, float(getattr(v, "weight", 1.0) or 0.0)
            )
            if vdir == trade_dir:
                for_mag += mag
            else:
                against_mag += mag
        if against_mag > for_mag and against_mag > 0:
            pressure += 1
            details.append("consensus opposes")

    if ctx.fast_opposition_streak >= 3:
        pressure += 1
        details.append(f"fast-opp streak {ctx.fast_opposition_streak}")

    if ctx.profit_r < -0.3:
        pressure += 1
        details.append(f"loss {ctx.profit_r:.1f}R")

    return int(pressure), int(structural), details


@dataclass
class SituationAssessment:
    """Continuous-valued read of the current market situation for one trade."""

    # ── Core dimensions (all continuous) ─────────────────────────────────
    tf_alignment: float = 0.0
    """
    -1.0 = all timeframes strongly oppose the trade direction.
     0.0 = mixed / neutral.
    +1.0 = all timeframes strongly support the trade direction.
    """

    momentum: float = 0.0
    """
    -1.0 = strong momentum against the trade.
     0.0 = neutral momentum.
    +1.0 = strong momentum in favour of the trade.
    """

    structure_integrity: float = 0.5
    """
    0.0 = key structure levels have been broken against the trade.
    1.0 = structure fully intact and supporting the trade.
    """

    profit_state: float = 0.0
    """R-multiple of the trade.  Negative = in loss."""

    price_vs_structure: float = 0.0
    """
    Live price relative to the key swing structure that supports the trade,
    in [-1, 0]:
      0.0 = price respecting structure (no break against the trade).
     -1.0 = price has decisively broken the nearest supporting swing against
            the trade direction (thesis structurally invalidated).
    Computed from the live price vs the TradeContext swing levels (NOT an entry
    zone's invalidation_level, which does not exist in the management path), so
    a structural break is seen the moment price trades through it rather than on
    the next slow candle close.
    """

    tick_momentum: float = 0.0
    """
    Live sub-candle momentum signed for the trade, in [-1, +1]: negative = price
    moving against the position right now. Mirrors ``TradeContext.tick_momentum``
    — a live pulse alongside the (M1-close) ``momentum`` dimension.
    """

    urgency: float = 0.0
    """
    0.0 = no time pressure.
    1.0 = immediate action required (news imminent, session closing).
    """

    maturity: float = 0.0
    """
    0.0 = just opened.
    1.0 = held for extended period.
    Derived from hold time, normalised against expected trade duration.
    """

    read_confidence: float = 0.5
    """
    0.0 = insufficient data to classify.
    1.0 = very high confidence in the situation read.
    """

    # ── Label (derived from dimensions — for logging only) ───────────────
    primary_label: str = "UNKNOWN"
    evidence: list[str] = field(default_factory=list)

    # ── Per-timeframe alignment vector (kept alongside the scalar) ────────
    # Signed alignment in [-1, +1] for each higher timeframe, so a consumer can
    # see *which* timeframe conflicts instead of only the collapsed scalar
    # ``tf_alignment``. A conflict (D1 +0.8, H4 -0.6) is then distinguishable
    # from genuine neutrality (all ~0) — the scalar alone hides that.
    tf_components: dict = field(default_factory=dict)

    def tf_vector(self) -> dict:
        """Return the per-timeframe signed alignment map ({"D1": float, ...})."""
        return dict(self.tf_components)

    # ── Component breakdowns (#16, #28) ───────────────────────────────────
    # The scalar dimensions above each fold several distinct sources into one
    # number. These maps keep the contributing parts alongside the scalar so a
    # consumer (orchestrator / dashboard) can tell *what* drove the read — e.g.
    # candle-momentum vs event-momentum, or which timeframe's structure broke —
    # instead of only seeing the collapsed value. Purely additive; the scalars
    # above are unchanged.
    momentum_components: dict = field(default_factory=dict)
    structure_components: dict = field(default_factory=dict)
    urgency_components: dict = field(default_factory=dict)
    confidence_components: dict = field(default_factory=dict)

    # ── Directional consensus dimension (signed scalar + full breakdown) ──
    # ``consensus_alignment`` is in [-1, +1]: +1 = the whole module panel agrees
    # with the trade direction, -1 = the panel opposes it, 0 = mixed/no panel.
    # It is an agreement-weighted read of the per-module votes — but the full
    # panel (which modules voted for/against, their confidence, high-authority
    # opposition) is kept in ``consensus_components`` so the decision engine
    # reasons over the structure, not a collapsed number.
    consensus_alignment: float = 0.0
    consensus_components: dict = field(default_factory=dict)

    def momentum_vector(self) -> dict:
        """Return the momentum component breakdown ({"candle": float, ...})."""
        return dict(self.momentum_components)

    def structure_vector(self) -> dict:
        """Return the structure-integrity component breakdown ({"H4": float, ...})."""
        return dict(self.structure_components)

    def urgency_vector(self) -> dict:
        """Return the urgency component breakdown ({"news": float, ...})."""
        return dict(self.urgency_components)

    def consensus_vector(self) -> dict:
        """Return the directional-consensus breakdown (per-module for/against)."""
        return dict(self.consensus_components)


class SituationEngine:
    """Reads analysis outputs → computes continuous situation dimensions."""

    # ── TF alignment weights ─────────────────────────────────────────────
    _D1_WEIGHT = 0.40
    _H4_WEIGHT = 0.35
    _H1_WEIGHT = 0.25

    # In-trade management reweights to give the fast M5 structure a real voice
    # so the thesis read is not pinned between slow H1/H4/D1 closes. Sum = 1.0.
    _MGMT_D1_WEIGHT = 0.35
    _MGMT_H4_WEIGHT = 0.30
    _MGMT_H1_WEIGHT = 0.20
    _MGMT_M5_WEIGHT = 0.15

    def __init__(self, adopted_observation_minutes: float = 10.0) -> None:
        # Observation window for adopted/orphan trades before a real label is
        # derived (config-driven; was a hardcoded 10-minute literal).
        self.adopted_observation_minutes = max(0.0, float(adopted_observation_minutes))

    def assess_open_trade(self, ctx: TradeContext) -> SituationAssessment:
        sa = SituationAssessment()
        evidence: list[str] = []

        # ── 1. Timeframe alignment ───────────────────────────────────────
        sa.tf_alignment = self._compute_tf_alignment(ctx, evidence)
        sa.tf_components = {
            "D1": round(self._trend_alignment_score(ctx.d1_trend, ctx.d1_confidence, ctx.is_long), 4),
            "H4": round(self._trend_alignment_score(ctx.h4_trend, ctx.h4_confidence, ctx.is_long), 4),
            "H1": round(self._trend_alignment_score(ctx.h1_trend, ctx.h1_confidence, ctx.is_long), 4),
            "M5": round(self._trend_alignment_score(ctx.m5_trend, ctx.m5_confidence, ctx.is_long), 4),
        }

        # ── 2. Momentum ─────────────────────────────────────────────────
        sa.momentum = self._compute_momentum(ctx, evidence, sa.momentum_components)

        # ── 3. Structure integrity ───────────────────────────────────────
        sa.structure_integrity = self._compute_structure_integrity(
            ctx, evidence, sa.structure_components,
        )

        # ── 4. Profit state ──────────────────────────────────────────────
        sa.profit_state = ctx.profit_r
        if sa.profit_state > 0.5:
            evidence.append(f"profit +{sa.profit_state:.1f}R")
        elif sa.profit_state < -0.3:
            evidence.append(f"loss {sa.profit_state:.1f}R")

        # ── 4b. Live price-vs-structure + tick momentum ──────────────────
        # Two live (sub-candle) reads so the in-trade thesis is verified against
        # what price is doing NOW, not only what the last closed candle showed.
        sa.price_vs_structure = self._compute_price_vs_structure(ctx, evidence)
        sa.tick_momentum = max(-1.0, min(1.0, float(getattr(ctx, "tick_momentum", 0.0) or 0.0)))

        # ── 5. Urgency ──────────────────────────────────────────────────
        sa.urgency = self._compute_urgency(ctx, evidence, sa.urgency_components)

        # ── 6. Maturity ──────────────────────────────────────────────────
        sa.maturity = min(1.0, ctx.hold_minutes / 240.0)

        # ── 7. Read confidence ───────────────────────────────────────────
        sa.read_confidence = self._compute_confidence(ctx, sa.confidence_components)

        # ── 7b. Directional consensus panel (live, in-trade) ─────────────
        # The same unbiased module vote panel the entry plane used, re-voted on
        # fresh data and folded into a signed alignment relative to the open
        # position — so the in-trade thesis check sees the whole market panel,
        # not a structure-only re-derivation. Full for/against breakdown kept.
        self._assess_consensus(sa, ctx, evidence, ctx.is_long)

        # ── 8. Derive label ──────────────────────────────────────────────
        sa.primary_label = self._derive_label(sa, ctx)
        sa.evidence = evidence
        return sa

    def assess_entry(self, ctx: EntryContext) -> SituationAssessment:
        """Compute situation dimensions for a potential entry."""
        sa = SituationAssessment()
        evidence: list[str] = []

        is_long = ctx.is_long

        # ── 1. Timeframe alignment ───────────────────────────────────────
        d1 = self._trend_alignment_score(ctx.d1_trend, ctx.d1_confidence, is_long)
        h4 = self._trend_alignment_score(ctx.h4_trend, ctx.h4_confidence, is_long)
        h1 = self._trend_alignment_score(ctx.h1_trend, ctx.h1_confidence, is_long)
        sa.tf_alignment = max(-1.0, min(1.0,
            d1 * self._D1_WEIGHT + h4 * self._H4_WEIGHT + h1 * self._H1_WEIGHT
        ))
        sa.tf_components = {"D1": round(d1, 4), "H4": round(h4, 4), "H1": round(h1, 4)}
        parts = []
        if abs(d1) > 0.1:
            parts.append(f"D1={'support' if d1 > 0 else 'oppose'}({ctx.d1_confidence:.2f})")
        if abs(h4) > 0.1:
            parts.append(f"H4={'support' if h4 > 0 else 'oppose'}({ctx.h4_confidence:.2f})")
        if abs(h1) > 0.1:
            parts.append(f"H1={'support' if h1 > 0 else 'oppose'}({ctx.h1_confidence:.2f})")
        if parts:
            evidence.append(f"tf_alignment={sa.tf_alignment:+.2f} [{', '.join(parts)}]")

        # ── 2. Momentum (M1 candles + structural events) ────────────────
        m = 0.0
        candle_m = (ctx.m1_aligned_count - 2.5) / 2.5
        candle_contrib = candle_m * 0.50
        m += candle_contrib
        m1_event = ctx.m1_event
        opposing_events = {"BOS_BEARISH", "CHOCH_BEARISH"} if is_long else {"BOS_BULLISH", "CHOCH_BULLISH"}
        supporting_events = {"BOS_BULLISH", "CHOCH_BULLISH"} if is_long else {"BOS_BEARISH", "CHOCH_BEARISH"}
        event_contrib = 0.0
        if m1_event in opposing_events:
            event_contrib = -0.30
        elif m1_event in supporting_events:
            event_contrib = 0.25
        m += event_contrib
        sa.momentum = max(-1.0, min(1.0, m))
        sa.momentum_components = {
            "candle": round(candle_contrib, 4),
            "event": round(event_contrib, 4),
            "trajectory": 0.0,
            "m1_aligned_count": ctx.m1_aligned_count,
            "m1_event": m1_event,
        }
        if abs(sa.momentum) > 0.1:
            evidence.append(f"momentum={sa.momentum:+.2f} [M1 {ctx.m1_aligned_count}/5, event={m1_event}]")

        # ── 3. Structure integrity (zone quality + HTF events) ──────────
        integrity = 0.5
        zone_type = ctx.entry_type
        zone_contrib = 0.0
        if zone_type == "FVG_OB_OVERLAP":
            zone_contrib = 0.30
            evidence.append("FVG+OB overlap zone (highest quality)")
        elif zone_type == "OB_MIDPOINT":
            zone_contrib = 0.20
            evidence.append("Order Block zone")
        elif zone_type == "FVG_MIDPOINT":
            zone_contrib = 0.15
            evidence.append("FVG zone")
        elif zone_type == "SWEEP_REVERSAL":
            zone_contrib = 0.25
            evidence.append("Sweep reversal zone")
        integrity += zone_contrib

        h4_opposing = (
            (is_long and ctx.h4_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
            or (not is_long and ctx.h4_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
        )
        h4_contrib = 0.0
        if h4_opposing:
            h4_contrib = -0.30
            integrity += h4_contrib
            evidence.append(f"H4 opposing event: {ctx.h4_event}")

        d1_opposing = (
            (is_long and ctx.d1_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
            or (not is_long and ctx.d1_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
        )
        d1_contrib = 0.0
        if d1_opposing:
            d1_contrib = -0.25
            integrity += d1_contrib
            evidence.append(f"D1 opposing event: {ctx.d1_event}")
        sa.structure_integrity = max(0.0, min(1.0, integrity))
        sa.structure_components = {
            "zone": round(zone_contrib, 4),
            "H4": round(h4_contrib, 4),
            "D1": round(d1_contrib, 4),
            "zone_type": zone_type or "",
        }

        # ── 4. No profit state for entries ───────────────────────────────
        sa.profit_state = 0.0

        # ── 5. Urgency ──────────────────────────────────────────────────
        u = 0.0
        news_u = 0.0
        session_u = 0.0
        if ctx.minutes_to_high_impact_news < 15:
            news_u = 1.0 - ctx.minutes_to_high_impact_news / 15.0
            u = max(u, news_u)
            evidence.append(f"news in {ctx.minutes_to_high_impact_news:.0f}min ({ctx.news_impact})")
        if not ctx.session_tradeable:
            session_u = 0.6
            u = max(u, session_u)
            evidence.append("session not tradeable")
        sa.urgency = min(1.0, u)
        sa.urgency_components = {
            "news": round(news_u, 4),
            "session": round(session_u, 4),
        }

        # ── 6. No maturity for entries ───────────────────────────────────
        sa.maturity = 0.0

        # ── 7. Read confidence ───────────────────────────────────────────
        c = 0.3
        if ctx.d1_trend != "UNKNOWN":
            c += 0.25
        if ctx.h4_trend != "UNKNOWN":
            c += 0.20
        if ctx.m1_trend != "UNKNOWN":
            c += 0.15
        if ctx.entry_type:
            c += 0.10
        sa.read_confidence = min(1.0, c)
        _quality_vals = [
            v for v in (ctx.d1_confidence, ctx.h4_confidence, ctx.h1_confidence)
            if isinstance(v, (int, float))
        ]
        sa.confidence_components = {
            "presence": round(sa.read_confidence, 4),
            "data_quality": (
                round(sum(_quality_vals) / len(_quality_vals), 4) if _quality_vals else 0.0
            ),
        }

        # ── 7b. Setup-quality (OQ/EQ) read ───────────────────────────────
        # OQ/EQ gate the setup READY in the scanner; here their influence is
        # carried into the entry read instead of being discarded after the gate.
        # A high entry-quality setup earns confidence; a low opportunity-quality
        # setup loses some. ``0.0`` means "not computed this cycle" (the quality
        # layer did not run) → no adjustment, mirroring the management plane's
        # ``None = no quality pressure`` semantics. Bounded and additive so it
        # nudges confidence, never hard-gates the entry.
        oq = float(getattr(ctx, "oq", 0.0) or 0.0)
        eq = float(getattr(ctx, "eq", 0.0) or 0.0)
        quality_adj = 0.0
        if eq > 6.0:
            quality_adj += min((eq - 6.0) / 4.0 * 0.10, 0.10)
            evidence.append(f"entry quality high ({eq:.1f}/10)")
        if 0.0 < oq < 3.0:
            quality_adj -= min((3.0 - oq) / 3.0 * 0.10, 0.10)
            evidence.append(f"opportunity quality low ({oq:.1f}/10)")
        if quality_adj:
            sa.read_confidence = max(0.0, min(1.0, sa.read_confidence + quality_adj))
            sa.confidence_components["quality_oq"] = round(oq, 2)
            sa.confidence_components["quality_eq"] = round(eq, 2)
            sa.confidence_components["quality_adj"] = round(quality_adj, 4)

        # ── 8. Directional consensus panel ──────────────────────────────
        # Fold the per-module vote panel into a signed alignment relative to
        # the trade direction, keeping the full for/against breakdown. The panel
        # is intelligence the entry plane already computed but historically only
        # reached the dashboard — here it becomes a decision dimension.
        self._assess_consensus(sa, ctx, evidence, is_long)

        # ── 9. Label ────────────────────────────────────────────────────
        sa.primary_label = self._derive_entry_label(sa, ctx)
        sa.evidence = evidence
        return sa

    def _assess_consensus(
        self,
        sa: SituationAssessment,
        ctx: EntryContext,
        evidence: list[str],
        is_long: bool,
    ) -> None:
        """Derive the signed consensus alignment + full for/against breakdown.

        Never collapses the panel to a single gate — the signed scalar is kept
        ALONGSIDE the per-module breakdown (contributors, dissenters, high-
        authority opposition) so ``decide_entry`` can weigh the structure.
        """
        votes = list(getattr(ctx, "consensus_votes", None) or [])
        if not votes:
            sa.consensus_alignment = 0.0
            sa.consensus_components = {}
            return

        trade_dir = "LONG" if is_long else "SHORT"
        high_authority = {"currency_strength"}  # mirrors ConsensusConfig default
        for_mods: list[str] = []
        against_mods: list[str] = []
        for_mag = 0.0
        against_mag = 0.0
        high_auth_oppose: list[str] = []

        for v in votes:
            vdir = str(getattr(v, "direction", "NEUTRAL") or "NEUTRAL").upper()
            if vdir not in ("LONG", "SHORT"):
                continue
            conf = float(getattr(v, "confidence", 0.0) or 0.0)
            weight = float(getattr(v, "weight", 1.0) or 0.0)
            module = str(getattr(v, "module", "") or "")
            mag = max(0.0, conf) * max(0.0, weight)
            if mag <= 0:
                continue
            if vdir == trade_dir:
                for_mods.append(module)
                for_mag += mag
            else:
                against_mods.append(module)
                against_mag += mag
                if module in high_authority and conf >= 0.6:
                    high_auth_oppose.append(module)

        total = for_mag + against_mag
        alignment = (for_mag - against_mag) / total if total > 0 else 0.0
        sa.consensus_alignment = round(max(-1.0, min(1.0, alignment)), 4)
        sa.consensus_components = {
            "for": for_mods,
            "against": against_mods,
            "for_magnitude": round(for_mag, 4),
            "against_magnitude": round(against_mag, 4),
            "high_authority_oppose": high_auth_oppose,
            "participation": len(for_mods) + len(against_mods),
        }
        if total > 0 and (for_mods or against_mods):
            evidence.append(
                f"consensus={sa.consensus_alignment:+.2f} "
                f"[for: {', '.join(for_mods) or 'none'}; "
                f"against: {', '.join(against_mods) or 'none'}"
                f"{'; HA-oppose: ' + ', '.join(high_auth_oppose) if high_auth_oppose else ''}]"
            )

    def _derive_entry_label(
        self, sa: SituationAssessment, ctx: EntryContext,
    ) -> str:
        if sa.urgency > 0.7:
            return "URGENT_RISK"
        if sa.structure_integrity < 0.2:
            return "WEAK_STRUCTURE"
        if sa.tf_alignment > 0.4 and sa.momentum > 0.1:
            return "TREND_CONTINUATION"
        if sa.tf_alignment > 0.3 and sa.momentum < -0.2:
            return "COUNTER_MOMENTUM"
        if abs(sa.tf_alignment) < 0.2:
            return "RANGE_ENTRY"
        if sa.tf_alignment < -0.3:
            return "COUNTER_TREND"
        return "MIXED"

    # ── Private helpers ──────────────────────────────────────────────────

    def _trend_alignment_score(
        self, trend: str, confidence: float, is_long: bool,
    ) -> float:
        """Returns -1..+1 for a single TF."""
        if trend == "UNKNOWN" or trend == "RANGING":
            return 0.0
        aligned = (
            (is_long and trend == "BULLISH")
            or (not is_long and trend == "BEARISH")
        )
        val = confidence if aligned else -confidence
        return max(-1.0, min(1.0, val))

    def _compute_price_vs_structure(
        self, ctx: TradeContext, evidence: list[str],
    ) -> float:
        """Live price vs the swing structure supporting the trade → [-1, 0].

        For a long the supporting structure is the swing lows beneath entry; for
        a short it is the swing highs above. When live price has traded through
        the nearest such level (against the trade), the thesis structure is
        breaking — returns a negative signal scaled by how far through it is
        relative to the trade's own risk distance (entry → current_sl). Returns
        0.0 when price still respects structure or the inputs are unavailable.

        Uses ``TradeContext`` swing levels and ``current_sl`` only — deliberately
        NOT an entry zone's ``invalidation_level`` (that object does not exist in
        the management path).
        """
        price = float(getattr(ctx, "current_price", 0.0) or 0.0)
        if price <= 0.0:
            return 0.0
        is_long = ctx.is_long
        entry = float(getattr(ctx, "entry_price", 0.0) or 0.0)

        # Price-scale of one unit of risk: entry → stop distance. Falls back to a
        # small fraction of price so the read still works before an SL is known.
        sl = float(getattr(ctx, "current_sl", 0.0) or 0.0)
        risk_dist = abs(entry - sl) if (entry > 0 and sl > 0) else 0.0
        if risk_dist <= 0.0:
            risk_dist = price * 0.001

        # Gather the supporting swing levels for this side.
        levels: list[float] = []
        for hi, lo in (
            (ctx.d1_swing_high, ctx.d1_swing_low),
            (ctx.h4_swing_high, ctx.h4_swing_low),
            (ctx.h1_swing_high, ctx.h1_swing_low),
        ):
            lvl = lo if is_long else hi
            if lvl is None:
                continue
            try:
                lvl = float(lvl)
            except (TypeError, ValueError):
                continue
            # Only levels that were actually supporting the trade matter: a
            # swing BELOW entry for a long, ABOVE entry for a short.
            if entry > 0:
                if is_long and lvl >= entry:
                    continue
                if (not is_long) and lvl <= entry:
                    continue
            levels.append(lvl)

        if not levels:
            return 0.0

        # The nearest supporting level price would break first.
        if is_long:
            support = max(levels)              # highest swing low beneath
            breach = (support - price) / risk_dist if price < support else 0.0
        else:
            support = min(levels)              # lowest swing high above
            breach = (price - support) / risk_dist if price > support else 0.0

        if breach <= 0.0:
            return 0.0
        signal = -min(breach, 1.0)
        evidence.append(
            f"price broke {'support' if is_long else 'resistance'} "
            f"{support:.5f} ({signal:+.2f})"
        )
        return signal

    def _compute_tf_alignment(
        self, ctx: TradeContext, evidence: list[str],
    ) -> float:
        is_long = ctx.is_long
        d1 = self._trend_alignment_score(ctx.d1_trend, ctx.d1_confidence, is_long)
        h4 = self._trend_alignment_score(ctx.h4_trend, ctx.h4_confidence, is_long)
        h1 = self._trend_alignment_score(ctx.h1_trend, ctx.h1_confidence, is_long)
        m5 = self._trend_alignment_score(ctx.m5_trend, ctx.m5_confidence, is_long)

        alignment = (
            d1 * self._MGMT_D1_WEIGHT
            + h4 * self._MGMT_H4_WEIGHT
            + h1 * self._MGMT_H1_WEIGHT
            + m5 * self._MGMT_M5_WEIGHT
        )

        parts = []
        if abs(d1) > 0.1:
            parts.append(f"D1={'support' if d1>0 else 'oppose'}({ctx.d1_confidence:.2f})")
        if abs(h4) > 0.1:
            parts.append(f"H4={'support' if h4>0 else 'oppose'}({ctx.h4_confidence:.2f})")
        if abs(h1) > 0.1:
            parts.append(f"H1={'support' if h1>0 else 'oppose'}({ctx.h1_confidence:.2f})")
        if abs(m5) > 0.1:
            parts.append(f"M5={'support' if m5>0 else 'oppose'}({ctx.m5_confidence:.2f})")
        if parts:
            evidence.append(f"tf_alignment={alignment:+.2f} [{', '.join(parts)}]")

        return max(-1.0, min(1.0, alignment))

    def _compute_momentum(
        self, ctx: TradeContext, evidence: list[str],
        components: dict | None = None,
    ) -> float:
        """Momentum from M1 candle alignment + structural events + score trajectory."""
        m = 0.0

        # M1 candle alignment: 0..5 → -1..+1
        aligned = ctx.m1_aligned_count
        candle_m = (aligned - 2.5) / 2.5   # 0→-1, 2.5→0, 5→+1
        candle_contrib = candle_m * 0.50
        m += candle_contrib

        # M1 structural event
        event = ctx.m1_event
        is_long = ctx.is_long
        opposing_events = (
            {"BOS_BEARISH", "CHOCH_BEARISH"} if is_long
            else {"BOS_BULLISH", "CHOCH_BULLISH"}
        )
        supporting_events = (
            {"BOS_BULLISH", "CHOCH_BULLISH"} if is_long
            else {"BOS_BEARISH", "CHOCH_BEARISH"}
        )
        event_contrib = 0.0
        if event in opposing_events:
            event_contrib = -0.30
        elif event in supporting_events:
            event_contrib = 0.25
        m += event_contrib

        # Score trajectory: last 3 scores
        trajectory_contrib = 0.0
        if len(ctx.score_history) >= 3:
            recent = ctx.score_history[-3:]
            delta = recent[-1] - recent[0]
            trajectory = max(-1.0, min(1.0, delta / 30.0))
            trajectory_contrib = trajectory * 0.20
            m += trajectory_contrib

        if components is not None:
            # Keep the distinct drivers so a consumer can tell candle-driven
            # momentum apart from event-driven momentum (#16).
            components.update({
                "candle": round(candle_contrib, 4),
                "event": round(event_contrib, 4),
                "trajectory": round(trajectory_contrib, 4),
                "m1_aligned_count": aligned,
                "m1_event": event,
            })

        if abs(m) > 0.1:
            evidence.append(
                f"momentum={m:+.2f} [M1 {aligned}/5 aligned, event={event}]"
            )
        return max(-1.0, min(1.0, m))

    def _compute_structure_integrity(
        self, ctx: TradeContext, evidence: list[str],
        components: dict | None = None,
    ) -> float:
        """How intact is the structural basis for this trade?"""
        integrity = 0.5  # neutral start

        is_long = ctx.is_long
        h4_contrib = 0.0
        h1_contrib = 0.0
        h1_candle_contrib = 0.0
        m5_contrib = 0.0
        d1_contrib = 0.0

        # H4 structure events
        h4_opposing = (
            (is_long and ctx.h4_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
            or (not is_long and ctx.h4_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
        )
        h4_supporting = (
            (is_long and ctx.h4_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
            or (not is_long and ctx.h4_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
        )
        if h4_opposing:
            h4_contrib = -0.35
            evidence.append(f"H4 structure broken ({ctx.h4_event})")
        elif h4_supporting:
            h4_contrib = 0.20
        integrity += h4_contrib

        # H1 structure events
        h1_opposing = (
            (is_long and ctx.h1_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
            or (not is_long and ctx.h1_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
        )
        h1_supporting = (
            (is_long and ctx.h1_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
            or (not is_long and ctx.h1_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
        )
        if h1_opposing:
            h1_contrib = -0.25
            evidence.append(f"H1 structure broken ({ctx.h1_event})")
        elif h1_supporting:
            h1_contrib = 0.15
        integrity += h1_contrib

        # H1 candle close against trade
        if ctx.h1_last_candle_bearish is not None and not ctx.h1_last_candle_doji:
            candle_against = (
                (is_long and ctx.h1_last_candle_bearish)
                or (not is_long and not ctx.h1_last_candle_bearish)
            )
            if candle_against:
                h1_candle_contrib = -0.10
                integrity += h1_candle_contrib
                evidence.append("H1 last candle opposing")

        # M5 structure events — the fast structural signal. Refreshes every 5
        # minutes (vs hourly H1), so a thesis break shows here first. Lower
        # authority than H1/H4 (smaller magnitude) but un-freezes the read.
        m5_opposing = (
            (is_long and ctx.m5_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
            or (not is_long and ctx.m5_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
        )
        m5_supporting = (
            (is_long and ctx.m5_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
            or (not is_long and ctx.m5_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
        )
        if m5_opposing:
            m5_contrib = -0.20
            evidence.append(f"M5 structure broken ({ctx.m5_event})")
        elif m5_supporting:
            m5_contrib = 0.10
        integrity += m5_contrib

        # D1 structure events
        d1_opposing = (
            (is_long and ctx.d1_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
            or (not is_long and ctx.d1_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
        )
        if d1_opposing:
            d1_contrib = -0.30
            integrity += d1_contrib
            evidence.append(f"D1 structure broken ({ctx.d1_event})")

        if components is not None:
            # Keep per-timeframe structure contributions so a consumer can tell
            # *which* timeframe's structure broke (#16), not just the net read.
            components.update({
                "H4": round(h4_contrib, 4),
                "H1": round(h1_contrib, 4),
                "H1_candle": round(h1_candle_contrib, 4),
                "M5": round(m5_contrib, 4),
                "D1": round(d1_contrib, 4),
            })

        return max(0.0, min(1.0, integrity))

    def _compute_urgency(
        self, ctx: TradeContext, evidence: list[str],
        components: dict | None = None,
    ) -> float:
        u = 0.0
        news_u = 0.0
        session_u = 0.0
        if ctx.minutes_to_high_impact_news < 15:
            news_u = 1.0 - ctx.minutes_to_high_impact_news / 15.0
            u = max(u, news_u)
            evidence.append(
                f"news in {ctx.minutes_to_high_impact_news:.0f}min ({ctx.news_impact})"
            )
        if not ctx.session_tradeable:
            session_u = 0.6
            u = max(u, session_u)
            evidence.append("session not tradeable")
        if components is not None:
            # Keep both urgency sources, not just the winning max() (#28) — a
            # consumer can see news vs session pressure independently.
            components.update({
                "news": round(news_u, 4),
                "session": round(session_u, 4),
            })
        return min(1.0, u)

    def _compute_confidence(
        self, ctx: TradeContext, components: dict | None = None,
    ) -> float:
        """How much data do we have to make a good read?"""
        c = 0.3
        if ctx.d1_trend != "UNKNOWN":
            c += 0.25
        if ctx.h4_trend != "UNKNOWN":
            c += 0.20
        if ctx.m1_trend != "UNKNOWN":
            c += 0.15
        if len(ctx.score_history) >= 3:
            c += 0.10
        presence = min(1.0, c)
        if components is not None:
            # Distinguish data *presence* (the scalar above) from data *quality*
            # (the actual HTF read confidences) — presence ≠ quality (#28).
            quality_vals = [
                v for v in (ctx.d1_confidence, ctx.h4_confidence, ctx.h1_confidence)
                if isinstance(v, (int, float))
            ]
            data_quality = (
                round(sum(quality_vals) / len(quality_vals), 4) if quality_vals else 0.0
            )
            components.update({
                "presence": round(presence, 4),
                "data_quality": data_quality,
            })
        return presence

    def _derive_label(
        self, sa: SituationAssessment, ctx: TradeContext,
    ) -> str:
        if ctx.is_adopted and ctx.hold_minutes < self.adopted_observation_minutes:
            return "ADOPTED_OBSERVING"

        if sa.structure_integrity < 0.2:
            return "STRUCTURE_BROKEN"

        if sa.tf_alignment > 0.4 and sa.momentum > 0.1:
            if sa.profit_state < -0.3:
                return "TREND_PULLBACK"
            return "TREND_CONTINUATION"

        if sa.tf_alignment > 0.3 and sa.momentum < -0.2:
            return "TREND_EXHAUSTION"

        if abs(sa.tf_alignment) < 0.2:
            return "RANGE"

        if sa.tf_alignment < -0.3 and sa.structure_integrity < 0.35:
            return "REVERSAL"

        if sa.urgency > 0.7:
            return "URGENT_RISK"

        return "MIXED"
