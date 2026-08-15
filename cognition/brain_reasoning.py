"""APEX TRADER — The Brain's native reasoning layer (independent reasoner).

Constitution Article II requires the Brain to "think in market states,
hypotheses, competing explanations, evidence, uncertainty, opportunities,
invalidation, expected value ..." with direction produced AFTER reasoning.
Article IX requires the Brain to "create its OWN hypotheses" and Article X to be
"capable of discovering relationships ... opportunities that were never
explicitly programmed". Article IV names SELF-CRITICISM and COUNTER-HYPOTHESES as
structural stages.

This module implements that native intelligence WITHOUT any LLM call. The
:class:`BrainReasoner`:

* :meth:`analyze_evidence` reads the fresh :class:`~cognition.contracts.Evidence`
  and produces a structured :class:`EvidenceAnalysis` (domain coverage, detected
  structural patterns, regime indicators, contradictions, what's missing).
* :meth:`generate_hypotheses` forms the Brain's OWN
  :class:`BrainHypothesis` list from the detected patterns — always including at
  least one counter-hypothesis (Article IV).
* :meth:`self_criticize` challenges each hypothesis and adjusts its confidence.
* :meth:`compare_opportunities` ranks a discovered opportunity against the book
  (Article XXVIII).

The advisors (LLM) INFORM this reasoning; they are not the reasoning. When no
advisor is available the Brain can still originate from this layer alone
(fulfilling Article X). Pure standard library; every method is fail-safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from cognition.contracts import Evidence, EvidenceDomain, MarketState
from cognition.pattern_rules import detect_patterns, pattern_name

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return min(1.0, max(0.0, f))


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


@dataclass
class EvidenceAnalysis:
    """The Brain's own reading of the evidence, before any advisor is consulted."""

    # What domains are present and their strength.
    domain_coverage: dict = field(default_factory=dict)  # {domain: mean_confidence}
    coverage_gap: float = 1.0  # 0-1, how much of the market picture is missing

    # Evidence-derived market state observations (plain-text).
    observations: "list[str]" = field(default_factory=list)

    # Structural pattern detection (from evidence measurements).
    detected_patterns: "list[str]" = field(default_factory=list)

    # Evidence agreement / disagreement.
    agreement_score: float = 0.0  # 0-1, how much evidence points the same way
    contradictions: "list[str]" = field(default_factory=list)

    # Market-state classification (from evidence, not from an LLM).
    regime_indicators: dict = field(default_factory=dict)

    # What is missing (domains / data not present).
    missing_information: "list[str]" = field(default_factory=list)

    # Overall uncertainty from the evidence alone.
    evidence_uncertainty: float = 1.0

    def summary(self) -> str:
        cov = ", ".join(f"{k} {v:.2f}" for k, v in sorted(self.domain_coverage.items()))
        pats = "; ".join(pattern_name(p) for p in self.detected_patterns) or "none"
        regime = self.regime_indicators
        structure = str(regime.get("structure", "") or "none")
        order_flow = str(regime.get("order_flow", "") or "balanced")
        return (
            f"{len(self.domain_coverage)} domains ({cov or 'none'}); "
            f"patterns: {pats}; structure {structure}, order flow {order_flow}; "
            f"agreement {self.agreement_score:.2f}; "
            f"uncertainty {self.evidence_uncertainty:.2f}"
        )

    def to_dict(self) -> dict:
        return {
            "domain_coverage": {k: round(v, 4) for k, v in self.domain_coverage.items()},
            "coverage_gap": round(self.coverage_gap, 4),
            "observations": list(self.observations),
            "detected_patterns": list(self.detected_patterns),
            "agreement_score": round(self.agreement_score, 4),
            "contradictions": list(self.contradictions),
            "regime_indicators": dict(self.regime_indicators),
            "missing_information": list(self.missing_information),
            "evidence_uncertainty": round(self.evidence_uncertainty, 4),
        }


@dataclass
class BrainHypothesis:
    """A hypothesis the Brain formed from its own evidence analysis."""

    statement: str
    supporting_patterns: "list[str]" = field(default_factory=list)
    contradicting_patterns: "list[str]" = field(default_factory=list)
    confidence: float = 0.0
    direction_implication: str = ""  # LONG, SHORT, FLAT, or "" (no implication)
    invalidation: str = ""
    opportunity_implication: str = ""
    criticism: str = ""  # self-criticism notes (Article IV)

    def __post_init__(self) -> None:
        self.confidence = _clamp01(self.confidence)
        d = str(self.direction_implication or "").upper()
        self.direction_implication = d if d in (LONG, SHORT, FLAT) else ""

    @property
    def is_directional(self) -> bool:
        return self.direction_implication in (LONG, SHORT)

    def to_dict(self) -> dict:
        return {
            "statement": self.statement,
            "supporting_patterns": [pattern_name(p) for p in self.supporting_patterns],
            "contradicting_patterns": [pattern_name(p) for p in self.contradicting_patterns],
            "confidence": round(self.confidence, 4),
            "direction_implication": self.direction_implication,
            "invalidation": self.invalidation,
            "opportunity_implication": self.opportunity_implication,
            "criticism": self.criticism,
        }


# Patterns that, if present, argue AGAINST a directional continuation thesis.
_CONTRA_PATTERNS = frozenset({
    "momentum_divergence", "multi_tf_conflict", "liquidity_sweep_absorbed",
    "liquidity_sweep_with_momentum_exhaustion", "volatility_compression",
    "execution_degraded", "portfolio_concentrated",
})

# Patterns that reinforce a directional continuation thesis.
_CONTINUATION_PATTERNS = frozenset({
    "displacement_confirmed", "volume_confirmed_breakout", "structural_break",
    "momentum_acceleration", "order_flow_at_structure", "multi_tf_aligned",
    "fvg_liquidity_confluence", "order_flow_imbalance",
})


class BrainReasoner:
    """The Brain's native, LLM-free reasoner (Articles II / IV / IX / X)."""

    def __init__(
        self,
        *,
        agreement_boost: float = 1.1,
        disagreement_penalty: float = 0.8,
        independent_min_confidence: float = 0.7,
        independent_max_uncertainty: float = 0.5,
    ) -> None:
        self.agreement_boost = max(1.0, float(agreement_boost))
        self.disagreement_penalty = min(1.0, max(0.0, float(disagreement_penalty)))
        self.independent_min_confidence = _clamp01(independent_min_confidence)
        self.independent_max_uncertainty = _clamp01(independent_max_uncertainty)

    # ── Component 1 — evidence analysis engine ────────────────────────────

    def analyze_evidence(
        self, market_state: MarketState, consolidation: Optional[dict] = None,
    ) -> EvidenceAnalysis:
        """Read the fresh evidence into a structured analysis (no LLM). Never raises."""
        try:
            fresh = list(market_state.fresh_evidence())
        except Exception:  # noqa: BLE001 — analysis must never break reasoning
            fresh = []
        consolidation = consolidation or {}

        # Per-domain mean confidence.
        by_domain: "dict[str, list[float]]" = {}
        for e in fresh:
            dom = getattr(getattr(e, "domain", None), "value", None) or "other"
            by_domain.setdefault(dom, []).append(_clamp01(getattr(e, "confidence", 0.0)))
        domain_coverage = {d: (sum(v) / len(v) if v else 0.0) for d, v in by_domain.items()}
        coverage_gap = round(1.0 - min(1.0, len(domain_coverage) / 6.0), 4)

        detected = detect_patterns(fresh)
        names = [pattern_name(p) for p in detected]

        regime = self._classify_regime(fresh, names)
        observations = self._observations(fresh, detected, regime)
        contradictions = self._contradictions(fresh, names, regime)
        missing = self._missing(domain_coverage)
        agreement = self._agreement(names, contradictions, regime)

        ev_unc = _clamp01(consolidation.get("aggregate_uncertainty", None)) \
            if consolidation.get("aggregate_uncertainty", None) is not None \
            else (1.0 if not fresh else _clamp01(
                sum(_clamp01(getattr(e, "uncertainty", 0.0)) for e in fresh) / len(fresh)))

        return EvidenceAnalysis(
            domain_coverage=domain_coverage,
            coverage_gap=coverage_gap,
            observations=observations,
            detected_patterns=detected,
            agreement_score=agreement,
            contradictions=contradictions,
            regime_indicators=regime,
            missing_information=missing,
            evidence_uncertainty=ev_unc,
        )

    def _classify_regime(self, fresh: "list[Evidence]", names: "list[str]") -> dict:
        """Classify the market regime + directional signals from measurements."""
        from cognition.pattern_rules import in_domain, numeric, state_is, has_flag, obs_has

        struct = in_domain(fresh, EvidenceDomain.STRUCTURE)
        mom = in_domain(fresh, EvidenceDomain.MOMENTUM)
        vlt = in_domain(fresh, EvidenceDomain.VOLATILITY)
        flow = in_domain(fresh, EvidenceDomain.ORDER_FLOW)
        liq = in_domain(fresh, EvidenceDomain.LIQUIDITY)

        # Volatility regime.
        if "volatility_compression" in names or state_is(vlt, "volatility_state", "compressing", "contracting"):
            volatility = "compressing"
        elif state_is(vlt, "volatility_state", "expanding") or has_flag(vlt, "expansion"):
            volatility = "expanding"
        else:
            volatility = "unknown"

        # Momentum regime.
        if "momentum_acceleration" in names or state_is(mom, "momentum_state", "accelerating"):
            momentum = "accelerating"
        elif ("liquidity_sweep_with_momentum_exhaustion" in names
              or state_is(mom, "momentum_state", "decelerating", "exhausting")
              or has_flag(mom, "deceleration")):
            momentum = "decelerating"
        else:
            momentum = "unknown"

        # Structural displacement sign (Brain forms direction from the raw sign).
        disp = numeric(struct, "displacement")
        if disp is not None and disp != 0.0:
            structure_dir = LONG if disp > 0 else SHORT
        elif has_flag(struct, "bos") or "structural_break" in names:
            structure_dir = "unknown"
        else:
            structure_dir = "none"

        # Order-flow sign.
        delta = numeric(flow, "delta")
        if delta is None:
            delta = numeric(flow, "imbalance")
        if delta is not None and delta != 0.0:
            order_flow = "buying" if delta > 0 else "selling"
        elif has_flag(flow, "buying") or obs_has(flow, "aggressive buying"):
            order_flow = "buying"
        elif has_flag(flow, "selling") or obs_has(flow, "aggressive selling"):
            order_flow = "selling"
        else:
            order_flow = "balanced"

        # Liquidity sweep side — a sell-side sweep that is absorbed implies an
        # upward reversal (the Brain reads the reversal itself).
        if state_is(liq, "sweep_side", "sell", "sell_side", "sellside") or obs_has(liq, "sell-side", "sell side"):
            sweep_side = "sell"
        elif state_is(liq, "sweep_side", "buy", "buy_side", "buyside") or obs_has(liq, "buy-side", "buy side"):
            sweep_side = "buy"
        else:
            sweep_side = "none"

        # The regime is a set of INDEPENDENT structured observations — a
        # displacement magnitude+direction, an order-flow reading, a sweep event,
        # plus the volatility/momentum regimes. It deliberately does NOT collapse
        # these into a single directional lean (V-001): direction is not decided
        # here in the pre-reasoning classifier, it EMERGES per-hypothesis from the
        # relevant evidence dimensions during hypothesis formation.
        return {
            "volatility": volatility,
            "momentum": momentum,
            "structure": structure_dir,
            "displacement": disp if disp is not None else 0.0,
            "order_flow": order_flow,
            "liquidity_sweep_side": sweep_side,
        }

    @staticmethod
    def _observations(fresh: "list[Evidence]", detected: "list[str]", regime: dict) -> "list[str]":
        obs: list[str] = []
        obs.append(
            f"regime: volatility {regime.get('volatility')}, momentum "
            f"{regime.get('momentum')}, order flow {regime.get('order_flow')}"
        )
        for p in detected:
            obs.append(f"observed {p}")
        if not detected:
            obs.append("no exploitable structural pattern detected in the evidence")
        return obs[:20]

    @staticmethod
    def _contradictions(fresh: "list[Evidence]", names: "list[str]", regime: dict) -> "list[str]":
        out: list[str] = []
        if "multi_tf_conflict" in names:
            out.append("higher and lower timeframe structural readings disagree")
        if "momentum_divergence" in names:
            out.append("price direction and momentum diverge")
        if ("displacement_confirmed" in names
                and regime.get("momentum") == "decelerating"):
            out.append("displacement present but momentum decelerating")
        if "execution_degraded" in names:
            out.append("execution quality degraded relative to the thesis")
        return out

    @staticmethod
    def _missing(domain_coverage: dict) -> "list[str]":
        core = ["structure", "liquidity", "momentum", "volume", "order_flow", "volatility"]
        present = set(domain_coverage.keys())
        return [f"no {d} evidence" for d in core if d not in present]

    @staticmethod
    def _agreement(names: "list[str]", contradictions: "list[str]", regime: dict) -> float:
        score = 0.6
        score += 0.12 * sum(1 for n in names if n in _CONTINUATION_PATTERNS)
        score -= 0.2 * len(contradictions)
        # Reward a COHERENT directional read that EMERGES from the independent
        # structured dimensions (structure displacement sign agreeing with the
        # order-flow sign), rather than reading a pre-collapsed directional lean
        # scalar (V-001).
        structure = str(regime.get("structure", "") or "")
        flow = regime.get("order_flow")
        flow_dir = LONG if flow == "buying" else (SHORT if flow == "selling" else "")
        if structure in (LONG, SHORT) and structure == flow_dir:
            score += 0.1
        return _clamp01(score)

    # ── Component 2 — hypothesis generator ────────────────────────────────

    def generate_hypotheses(self, analysis: EvidenceAnalysis, symbol: str) -> "list[BrainHypothesis]":
        """Form the Brain's OWN hypotheses from the detected patterns (Article IX).

        Always emits at least one primary reading plus at least one
        counter-hypothesis (Article IV — COUNTER-HYPOTHESES). Never raises.
        """
        try:
            names = [pattern_name(p) for p in analysis.detected_patterns]
            regime = analysis.regime_indicators
            hyps: list[BrainHypothesis] = []

            support = [n for n in names if n in _CONTINUATION_PATTERNS]
            against = [n for n in names if n in _CONTRA_PATTERNS]

            # 1) Displacement / continuation thesis.
            if "displacement_confirmed" in names or "volume_confirmed_breakout" in names \
                    or "structural_break" in names or "momentum_acceleration" in names:
                direction = self._continuation_direction(regime)
                hyps.append(self._make(
                    statement=(
                        f"Structural displacement backed by participation on {symbol} "
                        "suggests genuine directional intent; the move is likely to continue."),
                    support=[n for n in support],
                    against=against,
                    analysis=analysis,
                    direction=direction,
                    invalidation="Displacement fails and price reclaims the origin of the move",
                    opportunity=f"{direction} continuation while structure and flow persist"
                    if direction in (LONG, SHORT) else "directional continuation",
                ))

            # 2) Liquidity-sweep reversal thesis.
            if "liquidity_sweep_absorbed" in names or "liquidity_sweep_with_momentum_exhaustion" in names:
                side = regime.get("liquidity_sweep_side")
                if side == "sell":
                    direction = LONG
                elif side == "buy":
                    direction = SHORT
                else:
                    # No explicit sweep side — a reversal opposes the prevailing
                    # order flow (derived from the order-flow dimension itself, not
                    # a pre-collapsed directional lean).
                    flow = regime.get("order_flow")
                    direction = SHORT if flow == "buying" else (LONG if flow == "selling" else FLAT)
                hyps.append(self._make(
                    statement=(
                        "Liquidity was taken and the resulting flow was absorbed/exhausted; "
                        "the move was a liquidity grab, not genuine directional flow, so price "
                        "may reverse."),
                    support=[n for n in names if n in (
                        "liquidity_sweep_absorbed", "liquidity_sweep_with_momentum_exhaustion")],
                    against=[n for n in names if n in ("displacement_confirmed", "momentum_acceleration")],
                    analysis=analysis,
                    direction=direction,
                    invalidation="Fresh flow breaks decisively beyond the swept level",
                    opportunity=f"{direction} reversal off the sweep" if direction in (LONG, SHORT) else "reversal",
                ))

            # 3) Order-flow-at-structure thesis.
            if "order_flow_at_structure" in names and not any(
                    h.direction_implication in (LONG, SHORT) for h in hyps):
                of = regime.get("order_flow")
                direction = LONG if of == "buying" else (SHORT if of == "selling" else FLAT)
                hyps.append(self._make(
                    statement="Aggressive order flow is arriving at a structural level — flow meeting structure.",
                    support=["order_flow_at_structure"],
                    against=against,
                    analysis=analysis,
                    direction=direction,
                    invalidation="The structural level fails and flow reverses through it",
                    opportunity=f"{direction} reaction at structure" if direction in (LONG, SHORT) else "reaction",
                ))

            # 4) Consolidation / no-edge thesis (when nothing exploitable was found).
            if not hyps:
                hyps.append(self._make(
                    statement="Market is in consolidation/noise. No exploitable structure detected.",
                    support=[],
                    against=names,
                    analysis=analysis,
                    direction=FLAT,
                    invalidation="A clear structural pattern (displacement, sweep, break) emerges",
                    opportunity="none",
                ))

            # ALWAYS generate at least one counter-hypothesis (Article IV).
            hyps.append(self._counter(hyps[0], analysis))
            return hyps
        except Exception:  # noqa: BLE001 — hypothesis generation must never break reasoning
            return [BrainHypothesis(
                statement="No structured hypothesis could be formed from the evidence.",
                confidence=0.0, direction_implication=FLAT,
                invalidation="", opportunity_implication="none")]

    def _make(
        self, *, statement: str, support: "list[str]", against: "list[str]",
        analysis: EvidenceAnalysis, direction: str, invalidation: str, opportunity: str,
    ) -> BrainHypothesis:
        conf = self._hypothesis_confidence(support, against, analysis)
        return BrainHypothesis(
            statement=statement[:300], supporting_patterns=list(support),
            contradicting_patterns=list(against), confidence=conf,
            direction_implication=direction, invalidation=invalidation,
            opportunity_implication=opportunity,
        )

    @staticmethod
    def _hypothesis_confidence(
        support: "list[str]", against: "list[str]", analysis: EvidenceAnalysis,
    ) -> float:
        base = 0.5 + 0.12 * len(set(support)) - 0.12 * len(set(against))
        mean_conf = (sum(analysis.domain_coverage.values()) / len(analysis.domain_coverage)
                     if analysis.domain_coverage else 0.0)
        quality = 0.7 + 0.3 * mean_conf
        return _clamp01(min(0.95, base) * quality)

    def _counter(self, primary: BrainHypothesis, analysis: EvidenceAnalysis) -> BrainHypothesis:
        """A structural counter-hypothesis to the leading reading (Article IV)."""
        if primary.direction_implication in (LONG, SHORT):
            statement = (
                "The observed pattern could be a false signal: the displacement may be "
                "exhaustion rather than continuation, or the setup may fail at the level.")
            direction = self._opposite(primary.direction_implication)
        else:
            statement = (
                "A structural move could still be forming beneath the noise; absence of a "
                "pattern now does not preclude one developing.")
            direction = ""
        # A counter-hypothesis carries the residual probability mass.
        conf = _clamp01(max(0.0, (1.0 - primary.confidence)) * (0.5 + 0.5 * analysis.agreement_score))
        return BrainHypothesis(
            statement=statement, supporting_patterns=list(primary.contradicting_patterns),
            contradicting_patterns=list(primary.supporting_patterns), confidence=conf,
            direction_implication=direction,
            invalidation="The primary thesis is confirmed by follow-through",
            opportunity_implication="counter-thesis / caution",
        )

    @staticmethod
    def _continuation_direction(regime: dict) -> str:
        """Direction of a continuation thesis — it EMERGES from the structured
        dimensions that bear on it: the structural displacement sign first, else
        the order-flow sign. Never a pre-collapsed directional lean (V-001)."""
        s = str(regime.get("structure", "") or "").upper()
        if s in (LONG, SHORT):
            return s
        flow = regime.get("order_flow")
        if flow == "buying":
            return LONG
        if flow == "selling":
            return SHORT
        return FLAT

    @staticmethod
    def _opposite(direction: str) -> str:
        d = str(direction or "").upper()
        return SHORT if d == LONG else (LONG if d == SHORT else FLAT)

    # ── Component 5 — self-criticism stage ────────────────────────────────

    def self_criticize(
        self, hypotheses: "list[BrainHypothesis]", analysis: EvidenceAnalysis,
    ) -> "list[BrainHypothesis]":
        """Challenge each hypothesis and adjust confidence up/down (Article IV). Never raises."""
        try:
            names = [pattern_name(p) for p in analysis.detected_patterns]
            counter = hypotheses[-1] if hypotheses else None
            refined: list[BrainHypothesis] = []
            for i, h in enumerate(hypotheses):
                notes: list[str] = []
                factor = 1.0
                # 1) A detected pattern contradicts this hypothesis.
                active_contra = [n for n in h.contradicting_patterns if n in names]
                if active_contra:
                    factor *= 0.8
                    notes.append(f"contradicted by detected pattern(s): {', '.join(sorted(set(active_contra)))}")
                # 2) Missing information critical to a directional hypothesis.
                if h.is_directional:
                    needs = self._required_domains(h)
                    absent = [d for d in needs if d not in analysis.domain_coverage]
                    if absent:
                        factor *= 0.85
                        notes.append(f"relies on absent evidence: {', '.join(absent)}")
                # 3) The counter-hypothesis is actually stronger.
                if counter is not None and h is not counter and counter.confidence > h.confidence:
                    factor *= 0.85
                    notes.append("counter-hypothesis is stronger")
                # 4) Survives criticism ⇒ a modest reinforcement.
                if not notes and h.is_directional and analysis.agreement_score >= 0.7:
                    factor *= 1.08
                    notes.append("survived criticism: no active contradictions, coverage adequate")
                h.confidence = _clamp01(h.confidence * factor)
                h.criticism = "; ".join(notes) if notes else "no material criticism"
                refined.append(h)
            return refined
        except Exception:  # noqa: BLE001 — self-criticism must never break reasoning
            return hypotheses

    @staticmethod
    def _required_domains(h: BrainHypothesis) -> "list[str]":
        needs = {"structure"}
        sup = {pattern_name(p) for p in h.supporting_patterns}
        if sup & {"momentum_acceleration", "liquidity_sweep_with_momentum_exhaustion"}:
            needs.add("momentum")
        if sup & {"volume_confirmed_breakout", "displacement_confirmed"}:
            needs.add("volume")
        if sup & {"order_flow_at_structure", "order_flow_imbalance"}:
            needs.add("order_flow")
        if sup & {"liquidity_sweep_absorbed", "fvg_liquidity_confluence"}:
            needs.add("liquidity")
        return sorted(needs)

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def leading(hypotheses: "list[BrainHypothesis]") -> Optional[BrainHypothesis]:
        """The Brain's leading hypothesis: highest-confidence directional one,
        else the highest-confidence hypothesis overall."""
        if not hypotheses:
            return None
        directional = [h for h in hypotheses if h.is_directional]
        pool = directional or list(hypotheses)
        return max(pool, key=lambda h: h.confidence)

    # ── Component 6 — book-wide opportunity comparison ────────────────────

    def compare_opportunities(
        self, new_opportunity: BrainHypothesis, symbol: str, existing_positions: Any,
    ) -> str:
        """Rank a discovered opportunity against the book (Article XXVIII).

        Returns one of ``superior`` (better than the weakest existing position),
        ``complementary`` (diversifies the book), ``redundant`` (same exposure) or
        ``inferior`` (an existing position is better). Never raises.
        """
        try:
            positions = [p for p in list(existing_positions or []) if p is not None]
            if not positions:
                return "superior"
            new_dir = new_opportunity.direction_implication
            new_conf = _clamp01(new_opportunity.confidence)

            def _sym(p: Any) -> str:
                return str(getattr(p, "symbol", None) or (p.get("symbol") if isinstance(p, dict) else "") or "").upper()

            def _dir(p: Any) -> str:
                return str(getattr(p, "direction", None) or (p.get("direction") if isinstance(p, dict) else "") or "").upper()

            def _score(p: Any) -> float:
                for attr in ("entry_confidence", "confidence"):
                    v = getattr(p, attr, None) if not isinstance(p, dict) else p.get(attr)
                    if v is not None:
                        return _clamp01(v)
                return 0.5

            sym = str(symbol or "").upper()
            same_symbol_same_dir = any(
                _sym(p) == sym and _dir(p) == new_dir for p in positions)
            if same_symbol_same_dir:
                return "redundant"

            weakest = min(_score(p) for p in positions)
            shares_leg = any(self._shares_leg(sym, _sym(p)) for p in positions)
            if new_conf > weakest:
                return "superior"
            if not shares_leg:
                return "complementary"
            return "inferior"
        except Exception:  # noqa: BLE001 — comparison must never break reasoning
            return "complementary"

    @staticmethod
    def _shares_leg(a: str, b: str) -> bool:
        def legs(s: str) -> set:
            s = str(s or "").upper().strip()
            if len(s) == 6 and s.isalpha():
                return {s[:3], s[3:]}
            return {s} if s else set()
        return bool(legs(a) & legs(b))


__all__ = ["EvidenceAnalysis", "BrainHypothesis", "BrainReasoner", "LONG", "SHORT", "FLAT"]
