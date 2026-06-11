"""
Situation Engine — reads the analysis outputs and computes continuous
situation dimensions.  Situations EMERGE from data — they are never
hard-coded lookup tables.  Labels are generated AFTER the math, for
the decision journal only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decision.context import TradeContext


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


class SituationEngine:
    """Reads analysis outputs → computes continuous situation dimensions."""

    # ── TF alignment weights ─────────────────────────────────────────────
    _D1_WEIGHT = 0.40
    _H4_WEIGHT = 0.35
    _H1_WEIGHT = 0.25

    def assess_open_trade(self, ctx: TradeContext) -> SituationAssessment:
        sa = SituationAssessment()
        evidence: list[str] = []

        # ── 1. Timeframe alignment ───────────────────────────────────────
        sa.tf_alignment = self._compute_tf_alignment(ctx, evidence)

        # ── 2. Momentum ─────────────────────────────────────────────────
        sa.momentum = self._compute_momentum(ctx, evidence)

        # ── 3. Structure integrity ───────────────────────────────────────
        sa.structure_integrity = self._compute_structure_integrity(ctx, evidence)

        # ── 4. Profit state ──────────────────────────────────────────────
        sa.profit_state = ctx.profit_r
        if sa.profit_state > 0.5:
            evidence.append(f"profit +{sa.profit_state:.1f}R")
        elif sa.profit_state < -0.3:
            evidence.append(f"loss {sa.profit_state:.1f}R")

        # ── 5. Urgency ──────────────────────────────────────────────────
        sa.urgency = self._compute_urgency(ctx, evidence)

        # ── 6. Maturity ──────────────────────────────────────────────────
        sa.maturity = min(1.0, ctx.hold_minutes / 240.0)

        # ── 7. Read confidence ───────────────────────────────────────────
        sa.read_confidence = self._compute_confidence(ctx)

        # ── 8. Derive label ──────────────────────────────────────────────
        sa.primary_label = self._derive_label(sa, ctx)
        sa.evidence = evidence
        return sa

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

    def _compute_tf_alignment(
        self, ctx: TradeContext, evidence: list[str],
    ) -> float:
        is_long = ctx.is_long
        d1 = self._trend_alignment_score(ctx.d1_trend, ctx.d1_confidence, is_long)
        h4 = self._trend_alignment_score(ctx.h4_trend, ctx.h4_confidence, is_long)
        h1 = self._trend_alignment_score(ctx.h1_trend, ctx.h1_confidence, is_long)

        alignment = (
            d1 * self._D1_WEIGHT
            + h4 * self._H4_WEIGHT
            + h1 * self._H1_WEIGHT
        )

        parts = []
        if abs(d1) > 0.1:
            parts.append(f"D1={'support' if d1>0 else 'oppose'}({ctx.d1_confidence:.2f})")
        if abs(h4) > 0.1:
            parts.append(f"H4={'support' if h4>0 else 'oppose'}({ctx.h4_confidence:.2f})")
        if abs(h1) > 0.1:
            parts.append(f"H1={'support' if h1>0 else 'oppose'}({ctx.h1_confidence:.2f})")
        if parts:
            evidence.append(f"tf_alignment={alignment:+.2f} [{', '.join(parts)}]")

        return max(-1.0, min(1.0, alignment))

    def _compute_momentum(
        self, ctx: TradeContext, evidence: list[str],
    ) -> float:
        """Momentum from M1 candle alignment + structural events + score trajectory."""
        m = 0.0

        # M1 candle alignment: 0..5 → -1..+1
        aligned = ctx.m1_aligned_count
        candle_m = (aligned - 2.5) / 2.5   # 0→-1, 2.5→0, 5→+1
        m += candle_m * 0.50

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
        if event in opposing_events:
            m -= 0.30
        elif event in supporting_events:
            m += 0.25

        # Score trajectory: last 3 scores
        if len(ctx.score_history) >= 3:
            recent = ctx.score_history[-3:]
            delta = recent[-1] - recent[0]
            trajectory = max(-1.0, min(1.0, delta / 30.0))
            m += trajectory * 0.20

        if abs(m) > 0.1:
            evidence.append(
                f"momentum={m:+.2f} [M1 {aligned}/5 aligned, event={event}]"
            )
        return max(-1.0, min(1.0, m))

    def _compute_structure_integrity(
        self, ctx: TradeContext, evidence: list[str],
    ) -> float:
        """How intact is the structural basis for this trade?"""
        integrity = 0.5  # neutral start

        is_long = ctx.is_long

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
            integrity -= 0.35
            evidence.append(f"H4 structure broken ({ctx.h4_event})")
        elif h4_supporting:
            integrity += 0.20

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
            integrity -= 0.25
            evidence.append(f"H1 structure broken ({ctx.h1_event})")
        elif h1_supporting:
            integrity += 0.15

        # H1 candle close against trade
        if ctx.h1_last_candle_bearish is not None and not ctx.h1_last_candle_doji:
            candle_against = (
                (is_long and ctx.h1_last_candle_bearish)
                or (not is_long and not ctx.h1_last_candle_bearish)
            )
            if candle_against:
                integrity -= 0.10
                evidence.append("H1 last candle opposing")

        # D1 structure events
        d1_opposing = (
            (is_long and ctx.d1_event in ("BOS_BEARISH", "CHOCH_BEARISH"))
            or (not is_long and ctx.d1_event in ("BOS_BULLISH", "CHOCH_BULLISH"))
        )
        if d1_opposing:
            integrity -= 0.30
            evidence.append(f"D1 structure broken ({ctx.d1_event})")

        return max(0.0, min(1.0, integrity))

    def _compute_urgency(
        self, ctx: TradeContext, evidence: list[str],
    ) -> float:
        u = 0.0
        if ctx.minutes_to_high_impact_news < 15:
            u = max(u, 1.0 - ctx.minutes_to_high_impact_news / 15.0)
            evidence.append(
                f"news in {ctx.minutes_to_high_impact_news:.0f}min ({ctx.news_impact})"
            )
        if not ctx.session_tradeable:
            u = max(u, 0.6)
            evidence.append("session not tradeable")
        return min(1.0, u)

    def _compute_confidence(self, ctx: TradeContext) -> float:
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
        return min(1.0, c)

    def _derive_label(
        self, sa: SituationAssessment, ctx: TradeContext,
    ) -> str:
        if ctx.is_adopted and ctx.hold_minutes < 10:
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
