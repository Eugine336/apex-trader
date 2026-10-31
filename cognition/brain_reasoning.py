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
from hashlib import sha256
from typing import Any, Optional

from cognition.contracts import Evidence, EvidenceDomain, MarketState
from cognition.identity import IDENTITY_VERSION
from cognition.market_context import analyze_market_context
from cognition.pattern_rules import detect_patterns, pattern_name
from cognition.strategy_knowledge import STRATEGY_LENSES, assess_strategy_lenses
from cognition.analytical_knowledge import assess_analytical_knowledge, knowledge_coverage

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
    informative_domains: list[str] = field(default_factory=list)
    uninformative_domains: list[str] = field(default_factory=list)
    coverage_gap: float = 1.0  # 0-1, based on usable measured domains

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

    # Context-sensitive analytical lenses. These are not trade signals.
    strategy_lenses: list[dict] = field(default_factory=list)
    # Broad evidence-grounded knowledge registry coverage. This is analytical
    # context only; it cannot create direction, probability or execution authority.
    analytical_knowledge: list[dict] = field(default_factory=list)
    knowledge_coverage: dict = field(default_factory=dict)
    # Instrument economics, sessions, cross-market associations, historical
    # validation status, unfamiliar observations and epistemic limits.
    market_context: dict = field(default_factory=dict)
    # Point-in-time evidence lineage. Records contain metadata only; raw
    # measurements remain in the original Evidence objects.
    evidence_index: list[dict] = field(default_factory=list)
    pattern_evidence_ids: dict = field(default_factory=dict)
    observed_facts: list[dict] = field(default_factory=list)
    inferences: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    identity_version: str = IDENTITY_VERSION

    def summary(self) -> str:
        cov = ", ".join(f"{k} {v:.2f}" for k, v in sorted(self.domain_coverage.items()))
        pats = "; ".join(pattern_name(p) for p in self.detected_patterns) or "none"
        regime = self.regime_indicators
        structure = str(regime.get("structure", "") or "none")
        order_flow = str(regime.get("order_flow", "") or "balanced")
        lens_names = [
            str(item.get("name", "")) for item in self.strategy_lenses
            if isinstance(item, dict) and item.get("status") in ("relevant", "watch")
        ][:5]
        lens_text = ", ".join(lens_names) or "none assessable"
        return (
            f"{len(self.domain_coverage)} domains ({cov or 'none'}); "
            f"patterns: {pats}; structure {structure}, order flow {order_flow}; "
            f"lenses: {lens_text}; agreement {self.agreement_score:.2f}; "
            f"uncertainty {self.evidence_uncertainty:.2f}"
        )

    def to_dict(self) -> dict:
        return {
            "identity_version": self.identity_version,
            "domain_coverage": {k: round(v, 4) for k, v in self.domain_coverage.items()},
            "informative_domains": list(self.informative_domains),
            "uninformative_domains": list(self.uninformative_domains),
            "coverage_gap": round(self.coverage_gap, 4),
            "observations": list(self.observations),
            "detected_patterns": list(self.detected_patterns),
            "agreement_score": round(self.agreement_score, 4),
            "contradictions": list(self.contradictions),
            "regime_indicators": dict(self.regime_indicators),
            "missing_information": list(self.missing_information),
            "evidence_uncertainty": round(self.evidence_uncertainty, 4),
            "strategy_lenses": [dict(item) for item in self.strategy_lenses],
            "analytical_knowledge": [dict(item) for item in self.analytical_knowledge],
            "knowledge_coverage": dict(self.knowledge_coverage),
            "market_context": dict(self.market_context),
            "evidence_index": [dict(item) for item in self.evidence_index],
            "pattern_evidence_ids": {
                str(key): list(value) for key, value in self.pattern_evidence_ids.items()
            },
            "pattern_evidence_scope": "domain_level_candidate_sources",
            "epistemic_record": {
                "observed_facts": [dict(item) for item in self.observed_facts],
                "inferences": list(self.inferences),
                "assumptions": list(self.assumptions),
                "unknowns": list(self.unknowns),
            },
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
    hypothesis_id: str = ""
    hypothesis_kind: str = "market_state"
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    unresolved_unknowns: list[str] = field(default_factory=list)
    distinguishing_evidence: list[str] = field(default_factory=list)
    horizon: str = "unspecified"
    confidence_components: dict = field(default_factory=dict)
    confidence_semantics: str = "uncalibrated_reasoning_strength"
    evidence_lineage_scope: str = "not_available"
    identity_version: str = IDENTITY_VERSION

    def __post_init__(self) -> None:
        self.confidence = _clamp01(self.confidence)
        d = str(self.direction_implication or "").upper()
        self.direction_implication = d if d in (LONG, SHORT, FLAT) else ""
        self.hypothesis_kind = str(self.hypothesis_kind or "market_state").strip().lower()
        self.supporting_evidence_ids = list(dict.fromkeys(
            str(value) for value in self.supporting_evidence_ids if value
        ))
        self.contradicting_evidence_ids = list(dict.fromkeys(
            str(value) for value in self.contradicting_evidence_ids if value
        ))
        if not self.hypothesis_id:
            identity = "|".join((
                self.hypothesis_kind, self.statement, self.direction_implication,
                ",".join(self.supporting_evidence_ids),
                ",".join(self.contradicting_evidence_ids),
            ))
            self.hypothesis_id = sha256(identity.encode("utf-8")).hexdigest()[:20]

    @property
    def is_directional(self) -> bool:
        return self.direction_implication in (LONG, SHORT)

    def to_dict(self) -> dict:
        return {
            "identity_version": self.identity_version,
            "statement": self.statement,
            "supporting_patterns": [pattern_name(p) for p in self.supporting_patterns],
            "contradicting_patterns": [pattern_name(p) for p in self.contradicting_patterns],
            "confidence": round(self.confidence, 4),
            "direction_implication": self.direction_implication,
            "invalidation": self.invalidation,
            "opportunity_implication": self.opportunity_implication,
            "criticism": self.criticism,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_kind": self.hypothesis_kind,
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "contradicting_evidence_ids": list(self.contradicting_evidence_ids),
            "assumptions": list(self.assumptions),
            "unresolved_unknowns": list(self.unresolved_unknowns),
            "distinguishing_evidence": list(self.distinguishing_evidence),
            "horizon": self.horizon,
            "confidence_components": dict(self.confidence_components),
            "confidence_semantics": self.confidence_semantics,
            "evidence_lineage_scope": self.evidence_lineage_scope,
        }


# Patterns that, if present, argue AGAINST a directional continuation thesis.
_CONTRA_PATTERNS = frozenset({
    "momentum_divergence", "trend_exhaustion", "multi_tf_conflict",
    "liquidity_sweep_absorbed", "liquidity_sweep_reclaimed",
    "liquidity_sweep_with_momentum_exhaustion", "failed_breakout",
    "volatility_compression", "range_rotation", "statistical_displacement",
    "execution_degraded", "portfolio_concentrated",
})

# Patterns that reinforce a directional continuation thesis.
_CONTINUATION_PATTERNS = frozenset({
    "displacement_confirmed", "volume_confirmed_breakout", "structural_break",
    "momentum_acceleration", "trend_following_context",
    "volatility_expansion_breakout", "event_window_breakout",
    "order_flow_at_structure", "multi_tf_aligned", "fvg_liquidity_confluence",
    "order_flow_imbalance",
})

# Pattern-to-domain provenance. A rule may require more than one domain; all
# contributing source IDs are retained rather than pretending the pattern itself
# is an independent observation.
_PATTERN_DOMAINS = {
    "liquidity_sweep_absorbed": ("liquidity", "order_flow"),
    "liquidity_sweep_with_momentum_exhaustion": ("liquidity", "momentum"),
    "displacement_confirmed": ("structure", "volume"),
    "momentum_divergence": ("momentum", "structure"),
    "statistical_displacement": ("momentum",),
    "multi_tf_aligned": ("multi_timeframe",),
    "multi_tf_conflict": ("multi_timeframe",),
    "volatility_compression": ("volatility",),
    "volume_confirmed_breakout": ("volume", "volatility"),
    "order_flow_at_structure": ("order_flow", "structure"),
    "fvg_liquidity_confluence": ("structure", "liquidity"),
    "structural_break": ("structure",),
    "momentum_acceleration": ("momentum",),
    "execution_degraded": ("execution_quality",),
    "portfolio_concentrated": ("portfolio",),
    "order_flow_imbalance": ("order_flow",),
    "liquidity_sweep_reclaimed": ("liquidity", "structure"),
    "failed_breakout": ("structure", "volume"),
    "trend_following_context": ("structure", "multi_timeframe"),
    "range_rotation": ("structure", "volatility"),
    "volatility_expansion_breakout": ("structure", "volatility"),
    "trend_exhaustion": ("momentum", "structure"),
    "event_window_breakout": ("macro", "structure"),
    "m1_price_action_at_boundary": ("structure",),
}


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
        *, now: Optional[float] = None,
    ) -> EvidenceAnalysis:
        """Read the evidence fresh at the caller's decision-time cutoff. Never raises."""
        try:
            fresh = list(market_state.fresh_evidence(now))
        except Exception:  # noqa: BLE001 — analysis must never break reasoning
            fresh = []
        consolidation = consolidation or {}

        # Confidence and measurement coverage are separate. A high-confidence
        # record with an empty measurement payload is not usable market evidence.
        by_domain: "dict[str, list[float]]" = {}
        informative_domains: set[str] = set()
        core_domains = {
            "structure", "liquidity", "momentum", "volume", "order_flow", "volatility",
        }
        provenance_only_keys = {
            "source_kind", "timeframe", "opportunity_horizon", "timestamp",
            "timestamp_epoch", "source_timestamp", "source_timestamp_epoch",
            "symbol", "instrument", "source", "source_module", "evidence_id",
            "provisional", "forming", "is_forming", "completed", "is_completed",
            "measurement_version", "source_verified", "specification_verified",
            "session_calendar_verified", "verified", "confidence", "uncertainty",
        }
        for e in fresh:
            dom = getattr(getattr(e, "domain", None), "value", None) or "other"
            by_domain.setdefault(dom, []).append(_clamp01(getattr(e, "confidence", 0.0)))
            measurements = getattr(e, "measurements", {})
            if (
                str(dom) in core_domains
                and isinstance(measurements, dict)
                and any(str(key) not in provenance_only_keys for key in measurements)
            ):
                informative_domains.add(str(dom))
        domain_coverage = {d: (sum(v) / len(v) if v else 0.0) for d, v in by_domain.items()}
        core_domain_count = 6
        coverage_gap = round(
            1.0 - min(1.0, len(informative_domains) / core_domain_count), 4,
        )
        uninformative_domains = sorted(
            core_domains.intersection(domain_coverage) - informative_domains
        )

        detected = detect_patterns(fresh)
        names = [pattern_name(p) for p in detected]

        evidence_index: list[dict] = []
        observed_facts: list[dict] = []
        domain_evidence_ids: dict[str, list[str]] = {}
        for item in fresh:
            evidence_id = str(getattr(item, "evidence_id", "") or "")
            domain = str(getattr(getattr(item, "domain", None), "value", "other") or "other")
            if not evidence_id:
                continue
            record = {
                "evidence_id": evidence_id,
                "domain": domain,
                "source_module": str(getattr(item, "source_module", "") or ""),
                "independence_group": str(getattr(item, "independence_group", "") or ""),
                "domain": str(getattr(getattr(item, "domain", ""), "value", getattr(item, "domain", ""))),
                "symbol": str(getattr(item, "symbol", "") or ""),
                "timeframe": str(
                    (getattr(item, "measurements", {}) or {}).get("timeframe", "")
                    if isinstance(getattr(item, "measurements", {}), dict) else ""
                ),
                "source_timestamp_epoch": _num(getattr(
                    item, "source_timestamp_epoch", None
                )),
                "source_timestamp_verified": bool(getattr(item, "source_timestamp_verified", False)),
                "data_basis": str(getattr(item, "data_basis", "") or ""),
                "provisional": bool(getattr(item, "provisional", False)),
                "timestamp_epoch": _num(getattr(item, "timestamp_epoch", None)),
                "confidence": _clamp01(getattr(item, "confidence", 0.0)),
                "uncertainty": _clamp01(getattr(item, "uncertainty", 1.0), 1.0),
                "timeframe": str(
                    getattr(item, "measurements", {}).get("timeframe", "") or ""
                ).upper(),
                "opportunity_horizon": str(
                    getattr(item, "measurements", {}).get("opportunity_horizon", "") or ""
                ),
                "measurement_keys": sorted(
                    str(key) for key in getattr(item, "measurements", {})
                    if str(key) not in provenance_only_keys
                ) if isinstance(getattr(item, "measurements", {}), dict) else [],
            }
            evidence_index.append(record)
            measurements = getattr(item, "measurements", {})
            observed_facts.append({
                **record,
                "observation": str(getattr(item, "observation", "") or ""),
                "measurements": dict(measurements) if isinstance(measurements, dict) else {},
            })
            domain_evidence_ids.setdefault(domain, []).append(evidence_id)
        pattern_evidence_ids = {
            name: list(dict.fromkeys(
                evidence_id
                for domain in _PATTERN_DOMAINS.get(name, ())
                for evidence_id in domain_evidence_ids.get(domain, ())
            ))
            for name in names
        }

        regime = self._classify_regime(fresh, names)
        observations = self._observations(fresh, detected, regime)
        indicator_panels = regime.get("technical_indicators", {})
        if indicator_panels:
            indicator_counts = {
                timeframe: len(values)
                for timeframe, values in indicator_panels.items()
                if isinstance(values, dict)
            }
            observations.append(
                "Completed-bar technical indicator panel available by timeframe: "
                + ", ".join(
                    f"{timeframe} ({count} measurements)"
                    for timeframe, count in sorted(indicator_counts.items())
                )
                + "; indicators are descriptive measurements, not standalone entry rules."
            )
        contradictions = self._contradictions(fresh, names, regime)
        missing = self._missing(domain_coverage, informative_domains)
        agreement = self._agreement(
            names, contradictions, regime,
            pattern_evidence_ids=pattern_evidence_ids,
            evidence_index=evidence_index,
        )
        strategy_lenses = assess_strategy_lenses(fresh, names, regime)
        analytical_knowledge = assess_analytical_knowledge(fresh, regime)
        knowledge_coverage_record = knowledge_coverage(analytical_knowledge)
        # Preserve explicit unknowns and epistemic limits. This context is
        # descriptive evidence, not a strategy selector or action authority.
        market_context = analyze_market_context(fresh, getattr(market_state, "symbol", ""))
        # Surface these unknowns in the Brain's normal reasoning record too;
        # a separate diagnostic blob that the reasoning chain ignores is not enough.
        for item in market_context.get("missing_information", []):
            if item not in missing:
                missing.append(item)
        if market_context.get("unclassified_observations"):
            observations.append(
                f"{len(market_context['unclassified_observations'])} unfamiliar measurement group(s) "
                "preserved as unclassified evidence; semantics and predictive value are not established"
            )
        if market_context.get("historical_evidence", {}).get("records"):
            observations.append(
                "historical performance claims remain unverified unless point-in-time and "
                "selection-bias controls pass; model confidence is not predictive validation"
            )

        ev_unc = _clamp01(consolidation.get("aggregate_uncertainty", None)) \
            if consolidation.get("aggregate_uncertainty", None) is not None \
            else (1.0 if not fresh else _clamp01(
                sum(_clamp01(getattr(e, "uncertainty", 0.0)) for e in fresh) / len(fresh)))

        return EvidenceAnalysis(
            domain_coverage=domain_coverage,
            informative_domains=sorted(informative_domains),
            uninformative_domains=uninformative_domains,
            coverage_gap=coverage_gap,
            observations=observations,
            detected_patterns=detected,
            agreement_score=agreement,
            contradictions=contradictions,
            regime_indicators=regime,
            missing_information=missing,
            evidence_uncertainty=ev_unc,
            strategy_lenses=strategy_lenses,
            analytical_knowledge=analytical_knowledge,
            knowledge_coverage=knowledge_coverage_record,
            market_context=market_context,
            evidence_index=evidence_index,
            pattern_evidence_ids=pattern_evidence_ids,
            observed_facts=observed_facts,
            inferences=(
                list(names)
                + [
                    f"{key}={regime[key]}"
                    for key in (
                        "structure", "order_flow", "volatility", "momentum",
                        "trend_context", "liquidity_sweep_side",
                    )
                    if regime.get(key) not in (None, "", "unknown", "none")
                ]
                + list(contradictions)
            ),
            assumptions=[],
            unknowns=list(missing),
        )

    def _classify_regime(self, fresh: "list[Evidence]", names: "list[str]") -> dict:
        """Classify the market regime + directional signals from measurements."""
        from cognition.pattern_rules import in_domain, numeric, state_is, has_flag, obs_has

        struct = in_domain(fresh, EvidenceDomain.STRUCTURE)
        mom = in_domain(fresh, EvidenceDomain.MOMENTUM)
        vlt = in_domain(fresh, EvidenceDomain.VOLATILITY)
        flow = in_domain(fresh, EvidenceDomain.ORDER_FLOW)
        liq = in_domain(fresh, EvidenceDomain.LIQUIDITY)
        m1_records: list[dict] = []
        for item in fresh:
            measurements = getattr(item, "measurements", {})
            if isinstance(measurements, dict) and measurements.get("source_kind") == "m1_price_action":
                m1_records.append(measurements)
        m1_tags: list[str] = []
        m1_bullish: list[str] = []
        m1_bearish: list[str] = []
        for item in m1_records:
            for key, dest in (("pattern_tags", m1_tags),
                              ("bullish_patterns", m1_bullish),
                              ("bearish_patterns", m1_bearish)):
                values = item.get(key, [])
                if isinstance(values, (list, tuple, set)):
                    dest.extend(str(value) for value in values if value)
        m1_price_action = {
            "pattern_tags": list(dict.fromkeys(m1_tags)),
            "bullish_patterns": list(dict.fromkeys(m1_bullish)),
            "bearish_patterns": list(dict.fromkeys(m1_bearish)),
            "near_range_boundary": any(bool(item.get("near_range_boundary")) for item in m1_records),
            "provisional": any(bool(item.get("provisional")) for item in m1_records),
        }
        # Keep each timeframe's directional structure observation separate. The
        # aggregate here reports only whether a repeated trend context exists;
        # it does not choose a LONG/SHORT market bias.
        trend_readings: dict[str, set[str]] = {}
        ranged_timeframes: set[str] = set()
        for item in struct:
            measurements = getattr(item, "measurements", {})
            if not isinstance(measurements, dict) or measurements.get("source_kind") != "market_lens":
                continue
            tf = str(measurements.get("timeframe", "") or "").upper()
            state = str(measurements.get("trend_state", "") or "").lower()
            if tf and state in {"uptrend", "downtrend"}:
                trend_readings.setdefault(tf, set()).add(state)
            if tf and bool(measurements.get("range_rotation")):
                ranged_timeframes.add(tf)
        # A timeframe is usable only when its independent readings agree.
        trend_states = {
            tf: next(iter(states))
            for tf, states in trend_readings.items()
            if len(states) == 1
        }
        conflicted_trend_timeframes = sorted(
            tf for tf, states in trend_readings.items() if len(states) > 1
        )
        up_n = sum(value == "uptrend" for value in trend_states.values())
        down_n = sum(value == "downtrend" for value in trend_states.values())
        if len(trend_states) >= 2 and max(up_n, down_n) / len(trend_states) >= 0.67:
            trend_context = "uptrend" if up_n > down_n else "downtrend"
        elif len(ranged_timeframes) >= 2:
            trend_context = "range"
        else:
            trend_context = "mixed"

        # Preserve opposing regime observations instead of allowing the first
        # matching branch to hide a live contradiction.
        volatility_compressing = (
            "volatility_compression" in names
            or state_is(vlt, "volatility_state", "compressing", "contracting")
            or has_flag(vlt, "compression", "squeeze")
        )
        volatility_expanding = (
            "volatility_expansion_breakout" in names
            or state_is(vlt, "volatility_state", "expanding")
            or has_flag(vlt, "expansion")
        )
        volatility = (
            "conflicted" if volatility_compressing and volatility_expanding else
            "compressing" if volatility_compressing else
            "expanding" if volatility_expanding else "unknown"
        )
        momentum_accelerating = (
            "momentum_acceleration" in names
            or state_is(mom, "momentum_state", "accelerating")
            or has_flag(mom, "acceleration")
        )
        momentum_decelerating = (
            "liquidity_sweep_with_momentum_exhaustion" in names
            or "trend_exhaustion" in names
            or state_is(mom, "momentum_state", "decelerating", "exhausting")
            or has_flag(mom, "deceleration", "exhaustion")
        )
        momentum = (
            "conflicted" if momentum_accelerating and momentum_decelerating else
            "accelerating" if momentum_accelerating else
            "decelerating" if momentum_decelerating else "unknown"
        )

        # Preserve independent readings. Conflicting sources must not be
        # collapsed by "first value wins" into a synthetic directional fact.
        displacement_values = [
            value for item in struct
            if (value := _num(getattr(item, "measurements", {}).get("displacement"))) is not None
            and value != 0.0
        ]
        displacement_signs = {1 if value > 0 else -1 for value in displacement_values}
        disp = (
            sum(displacement_values) / len(displacement_values)
            if displacement_values else numeric(struct, "displacement")
        )
        if len(displacement_signs) > 1:
            structure_dir = "conflicted"
        elif displacement_signs:
            structure_dir = LONG if next(iter(displacement_signs)) > 0 else SHORT
        elif has_flag(struct, "bos") or "structural_break" in names:
            structure_dir = "unknown"
        else:
            structure_dir = "none"

        # Order-flow sign. Each source contributes one signed reading; mixed
        # signs remain conflicted instead of whichever item happened to arrive first.
        flow_values: list[float] = []
        for item in flow:
            measurements = getattr(item, "measurements", {})
            if not isinstance(measurements, dict):
                continue
            value = _num(measurements.get("delta"))
            if value is None:
                value = _num(measurements.get("imbalance"))
            if value is not None and value != 0.0:
                flow_values.append(value)
        flow_signs = {1 if value > 0 else -1 for value in flow_values}
        if len(flow_signs) > 1:
            order_flow = "conflicted"
        elif flow_signs:
            order_flow = "buying" if next(iter(flow_signs)) > 0 else "selling"
        elif has_flag(flow, "buying") and has_flag(flow, "selling"):
            order_flow = "conflicted"
        elif has_flag(flow, "buying") or obs_has(flow, "aggressive buying"):
            order_flow = "buying"
        elif has_flag(flow, "selling") or obs_has(flow, "aggressive selling"):
            order_flow = "selling"
        else:
            order_flow = "balanced"

        # Liquidity sweep side — a sell-side sweep that is absorbed implies an
        # upward reversal (the Brain reads the reversal itself).
        has_sell_sweep = state_is(liq, "sweep_side", "sell", "sell_side", "sellside") or obs_has(liq, "sell-side", "sell side")
        has_buy_sweep = state_is(liq, "sweep_side", "buy", "buy_side", "buyside") or obs_has(liq, "buy-side", "buy side")
        sweep_side = (
            "conflicted" if has_sell_sweep and has_buy_sweep else
            "sell" if has_sell_sweep else ("buy" if has_buy_sweep else "none")
        )

        # Preserve the complete per-timeframe indicator panel for native
        # reasoning. These are measurements, not votes or precomputed direction.
        technical_indicators: dict[str, dict[str, Any]] = {}
        technical_evidence_ids: dict[str, list[str]] = {}
        for item in fresh:
            measurements = getattr(item, "measurements", {})
            if not isinstance(measurements, dict):
                continue
            if measurements.get("source_kind") != "market_lens":
                continue
            source = str(getattr(item, "source_module", "") or "")
            if ".technical_" not in source:
                continue
            timeframe = str(measurements.get("timeframe", "") or "").upper()
            if not timeframe:
                continue
            panel = technical_indicators.setdefault(timeframe, {})
            for key, value in measurements.items():
                if key in {
                    "source_kind", "timeframe", "provisional", "data_basis",
                    "source_timestamp_known", "source_bar_epoch", "source_timestamp_epoch",
                    "measurement_version", "calculation_method", "indicator_periods",
                }:
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    parsed = _num(value)
                    if parsed is not None:
                        panel[str(key)] = parsed
                elif isinstance(value, (str, bool)):
                    panel[str(key)] = value
            evidence_id = str(getattr(item, "evidence_id", "") or "")
            if evidence_id:
                technical_evidence_ids.setdefault(timeframe, []).append(evidence_id)

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
            "displacement_readings": list(displacement_values),
            "displacement_conflicted": len(displacement_signs) > 1,
            "order_flow": order_flow,
            "order_flow_readings": list(flow_values),
            "order_flow_conflicted": len(flow_signs) > 1,
            "liquidity_sweep_side": sweep_side,
            # Statistical displacement remains a measurement. It is not collapsed
            # into a directional bias; the hypothesis generator below can form a
            # mean-reversion counter-thesis from its signed z-score.
            "statistical_zscore": (
                None if len({
                    1 if value > 0 else -1
                    for item in mom
                    if (value := _num(getattr(item, "measurements", {}).get("rolling_zscore_30"))) not in (None, 0.0)
                }) > 1 else numeric(mom, "rolling_zscore_30")
            ),
            "statistical_zscore_conflicted": len({
                1 if value > 0 else -1
                for item in mom
                if (value := _num(getattr(item, "measurements", {}).get("rolling_zscore_30"))) not in (None, 0.0)
            }) > 1,
            "ema_spread_atr": numeric(mom, "ema_spread_atr"),
            "trend_context": trend_context,
            "trend_timeframes": len(trend_states),
            "trend_conflicted_timeframes": conflicted_trend_timeframes,
            "range_timeframes": len(ranged_timeframes),
            "m1_price_action": m1_price_action,
            "technical_indicators": technical_indicators,
            "technical_indicator_evidence_ids": technical_evidence_ids,
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
        from cognition.pattern_rules import in_domain

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
        structural_signs = {
            1 if value > 0 else -1
            for item in in_domain(fresh, EvidenceDomain.STRUCTURE)
            if (value := _num(getattr(item, "measurements", {}).get("displacement"))) not in (None, 0.0)
        }
        flow_signs = set()
        for item in in_domain(fresh, EvidenceDomain.ORDER_FLOW):
            measurements = getattr(item, "measurements", {})
            if not isinstance(measurements, dict):
                continue
            value = _num(measurements.get("delta"))
            if value is None:
                value = _num(measurements.get("imbalance"))
            if value not in (None, 0.0):
                flow_signs.add(1 if value > 0 else -1)
        if len(structural_signs) > 1:
            out.append("independent structural sources disagree on displacement direction")
        if len(flow_signs) > 1:
            out.append("independent order-flow sources disagree on pressure direction")
        if len(structural_signs) == 1 and len(flow_signs) == 1 and structural_signs != flow_signs:
            out.append("structural displacement and order-flow pressure point in opposite directions")
        m1 = regime.get("m1_price_action", {})
        if m1.get("bullish_patterns") and m1.get("bearish_patterns"):
            out.append("M1 price-action observations contain both bullish and bearish patterns")
        if regime.get("trend_conflicted_timeframes"):
            out.append(
                "independent trend readings conflict on timeframe(s): "
                + ", ".join(regime["trend_conflicted_timeframes"])
            )
        if regime.get("volatility") == "conflicted":
            out.append("volatility evidence simultaneously reports compression and expansion")
        if regime.get("momentum") == "conflicted":
            out.append("momentum evidence simultaneously reports acceleration and exhaustion")
        if regime.get("statistical_zscore_conflicted"):
            out.append("momentum sources report statistical displacement on opposing sides of the mean")
        return list(dict.fromkeys(out))

    @staticmethod
    def _missing(
        domain_coverage: dict, informative_domains: Optional[set[str]] = None,
    ) -> "list[str]":
        core = ["structure", "liquidity", "momentum", "volume", "order_flow", "volatility"]
        present = set(domain_coverage.keys())
        usable = set(informative_domains) if informative_domains is not None else present
        missing = [
            f"no {domain} evidence" if domain not in present
            else f"no usable measurement in {domain}"
            for domain in core if domain not in usable
        ]
        return missing

    @staticmethod
    def _agreement(
        names: "list[str]", contradictions: "list[str]", regime: dict,
        *, pattern_evidence_ids: Optional[dict] = None,
        evidence_index: Optional[list[dict]] = None,
    ) -> float:
        score = 0.6
        continuation = {n for n in names if n in _CONTINUATION_PATTERNS}
        # Multiple patterns derived from the same underlying feed/bar are not
        # independent confirmations. Count distinct provenance groups, not
        # the number of labels emitted by correlated observers.
        group_by_id = {}
        for item in (evidence_index or []):
            if not isinstance(item, dict) or not item.get("evidence_id"):
                continue
            group = str(item.get("independence_group", "") or "")
            if not group:
                timeframe = str(item.get("timeframe", "") or "unknown")
                symbol = str(item.get("symbol", "") or "unknown")
                domain = str(item.get("domain", "") or "other")
                if str(item.get("data_basis", "")).lower() == "completed_ohlc":
                    group = f"ohlc:{symbol}:{timeframe}"
                else:
                    group = f"{domain}:{symbol}:{timeframe}"
            group_by_id[str(item["evidence_id"])] = group
        independent_groups = {
            group_by_id.get(str(evidence_id), "")
            for name in continuation
            for evidence_id in (pattern_evidence_ids or {}).get(name, [])
            if group_by_id.get(str(evidence_id), "")
        }
        support_count = (
            len(independent_groups)
            if independent_groups
            else len(continuation)
        )
        score += 0.12 * min(3, support_count)
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
            if ("displacement_confirmed" in names or "volume_confirmed_breakout" in names
                    or "structural_break" in names or "momentum_acceleration" in names
                    or "trend_following_context" in names
                    or "volatility_expansion_breakout" in names
                    or "event_window_breakout" in names):
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
                    hypothesis_kind="continuation",
                ))

            # 2) Liquidity-sweep reversal thesis.
            if any(name in names for name in (
                "liquidity_sweep_absorbed", "liquidity_sweep_with_momentum_exhaustion",
                "liquidity_sweep_reclaimed", "failed_breakout",
            )):
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
                        "liquidity_sweep_absorbed", "liquidity_sweep_with_momentum_exhaustion",
                        "liquidity_sweep_reclaimed", "failed_breakout")],
                    against=[n for n in names if n in (
                        "displacement_confirmed", "volume_confirmed_breakout",
                        "momentum_acceleration", "trend_following_context")],
                    analysis=analysis,
                    direction=direction,
                    invalidation="Fresh flow breaks decisively beyond the swept level",
                    opportunity=f"{direction} reversal off the sweep" if direction in (LONG, SHORT) else "reversal",
                    hypothesis_kind="liquidity_reversal",
                ))

            # Trend exhaustion is a competing, context-dependent hypothesis.
            # It is never a command to fade strong price discovery merely because
            # one momentum reading weakened.
            if "trend_exhaustion" in names or "momentum_divergence" in names:
                structure_state = str(regime.get("structure", "") or "").upper()
                if structure_state in (LONG, SHORT):
                    direction = SHORT if structure_state == LONG else LONG
                else:
                    flow_state = str(regime.get("order_flow", "") or "")
                    direction = SHORT if flow_state == "buying" else (
                        LONG if flow_state == "selling" else FLAT
                    )
                hyps.append(self._make(
                    statement=(
                        f"Price/momentum evidence on {symbol} suggests the current "
                        "move may be losing participation. Exhaustion is one explanation; "
                        "continued price discovery remains a competing explanation."
                    ),
                    support=[name for name in names if name in ("trend_exhaustion", "momentum_divergence")],
                    against=[name for name in names if name in (
                        "displacement_confirmed", "volume_confirmed_breakout",
                        "momentum_acceleration", "trend_following_context",
                    )],
                    analysis=analysis,
                    direction=direction,
                    invalidation="Momentum re-accelerates and completed price closes extend beyond the current structural extreme",
                    opportunity=(
                        f"{direction} exhaustion/reversal hypothesis requiring a confirmed structural response"
                        if direction in (LONG, SHORT) else "exhaustion hypothesis with direction unresolved"
                    ),
                    hypothesis_kind="trend_exhaustion",
                ))

            # A range is a market state, not an instruction to trade. It can be
            # inferred either from a detected range-rotation pattern or from
            # repeated, independently observed timeframe states.
            range_context = (
                "range_rotation" in names
                or (
                    str(regime.get("trend_context", "")).lower() == "range"
                    and (_num(regime.get("range_timeframes")) or 0.0) >= 2
                )
            )
            if range_context:
                range_support = ["range_rotation"] if "range_rotation" in names else []
                hyps.append(self._make(
                    statement=(
                        f"Multiple completed timeframes on {symbol} are rotating inside "
                        "recent ranges; a range-edge reaction and a genuine breakout "
                        "remain competing possibilities."
                    ),
                    support=range_support,
                    against=[name for name in names if name in (
                        "volatility_expansion_breakout", "event_window_breakout",
                        "volume_confirmed_breakout",
                    )],
                    analysis=analysis,
                    direction=FLAT,
                    invalidation="A completed candle accepts beyond the range with sustained expansion",
                    opportunity="monitor both range-edge reactions and conditional breakout alternatives",
                    hypothesis_kind="range_rotation",
                ))

            # 3) Statistical displacement / mean-reversion hypothesis.
            # This can co-exist with a continuation thesis; the Brain compares both
            # instead of allowing a detector to issue or rank a trade direction.
            if "statistical_displacement" in names:
                zscore = regime.get("statistical_zscore")
                try:
                    zvalue = float(zscore)
                except (TypeError, ValueError):
                    zvalue = 0.0
                direction = SHORT if zvalue > 0.0 else (LONG if zvalue < 0.0 else FLAT)
                hyps.append(self._make(
                    statement=(
                        f"{symbol} statistical displacement is {abs(zvalue):.2f} standard deviations "
                        "from its rolling mean while the EMA spread is contained; a return toward "
                        "the mean is a competing hypothesis, not an assumed outcome."
                    ),
                    support=["statistical_displacement"],
                    against=[n for n in names if n in (
                        "displacement_confirmed", "volume_confirmed_breakout",
                        "structural_break", "momentum_acceleration",
                    )],
                    analysis=analysis,
                    direction=direction,
                    invalidation="Price continues extending away from the rolling mean and trend strength expands",
                    opportunity=(
                        f"{direction} mean-reversion hypothesis if displacement fails to extend"
                        if direction in (LONG, SHORT) else "mean-reversion hypothesis"
                    ),
                    hypothesis_kind="mean_reversion",
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
                    hypothesis_kind="order_flow_reaction",
                ))

            # M1 price-action is contextual evidence, not a universal trigger.
            # Only consider a native hypothesis when an observed pattern is at a
            # measured local boundary; forming candles remain explicitly provisional.
            if "m1_price_action_at_boundary" in names:
                m1 = regime.get("m1_price_action", {})
                bullish_patterns = list(m1.get("bullish_patterns", []) or [])
                bearish_patterns = list(m1.get("bearish_patterns", []) or [])
                if bullish_patterns and not bearish_patterns:
                    direction = LONG
                elif bearish_patterns and not bullish_patterns:
                    direction = SHORT
                else:
                    direction = FLAT
                provisional_note = " The latest M1 read is provisional." if m1.get("provisional") else ""
                hyps.append(self._make(
                    statement=(
                        f"M1 price action near a measured local range boundary on {symbol}: "
                        f"observations={', '.join(m1.get('pattern_tags', []) or [])}. "
                        "This is a contextual microstructure hypothesis, not an entry instruction."
                        + provisional_note
                    ),
                    support=["m1_price_action_at_boundary"],
                    against=[name for name in names if name in (
                        "structural_break", "multi_tf_conflict", "momentum_divergence",
                    )],
                    analysis=analysis,
                    direction=direction,
                    invalidation="Price accepts beyond the observed local boundary or subsequent evidence rejects the candle interpretation",
                    opportunity=(
                        f"{direction} M1 reaction hypothesis requiring broader market confirmation"
                        if direction in (LONG, SHORT) else "mixed M1 reaction; await clarifying evidence"
                    ),
                    hypothesis_kind="m1_context",
                ))

            # Regime-derived trend context is a separate native hypothesis family.
            # This prevents the Brain from treating a quiet pattern detector as proof
            # that no thesis exists when several completed timeframes independently
            # show a persistent trend. It remains conditional and is not an order.
            trend_context = str(regime.get("trend_context", "") or "").lower()
            try:
                trend_timeframes = int(regime.get("trend_timeframes", 0) or 0)
            except (TypeError, ValueError):
                trend_timeframes = 0
            has_directional_thesis = any(
                h.direction_implication in (LONG, SHORT) for h in hyps
            )
            if (
                not has_directional_thesis
                and trend_timeframes >= 2
                and trend_context in ("uptrend", "downtrend")
            ):
                direction = LONG if trend_context == "uptrend" else SHORT
                momentum = str(regime.get("momentum", "") or "").lower()
                caveat = (
                    " Momentum is decelerating, so a pullback or trend transition "
                    "is a live competing explanation."
                    if momentum == "decelerating"
                    else " Continuation still requires fresh follow-through and executable net value."
                )
                hyps.append(self._make(
                    statement=(
                        f"Completed-timeframe structure on {symbol} is aligned with a "
                        f"{trend_context}; conditional continuation is plausible, not certain."
                        + caveat
                    ),
                    support=[n for n in names if n in _CONTINUATION_PATTERNS],
                    against=[n for n in names if n in _CONTRA_PATTERNS],
                    analysis=analysis,
                    direction=direction,
                    invalidation=(
                        "Completed higher-timeframe structure breaks, alignment degrades, "
                        "or cost-adjusted expectancy becomes non-positive"
                    ),
                    opportunity=(
                        f"{direction} trend-context hypothesis; wait for fresh confirmation "
                        "and common opportunity admission"
                    ),
                    hypothesis_kind="trend_context",
                ))

            # 4) Consolidation / no-edge thesis (when nothing exploitable was found).
            if not hyps:
                hyps.append(self._make(
                    statement="Market state is unresolved from available evidence; no directional edge is established.",
                    support=[],
                    against=names,
                    analysis=analysis,
                    direction=FLAT,
                    invalidation="Fresh, independent evidence supports a testable market-state hypothesis",
                    opportunity="observe; do not infer that missing evidence means a trade or a proven consolidation",
                    hypothesis_kind="no_trade",
                ))

            # Preserve an explicit, non-directional hypothesis for unfamiliar
            # measurements. Unknown semantics are not reverse-engineered into a
            # convenient known pattern or a directional implication.
            unclassified = analysis.market_context.get("unclassified_observations", [])
            unknown_by_source: dict[str, set[str]] = {}
            for record in unclassified:
                if not isinstance(record, dict):
                    continue
                source = str(record.get("source", "") or "")
                unknown_by_source.setdefault(source, set()).update(
                    str(key) for key in (record.get("measurement_keys", []) or []) if key
                )
            for source, unknown_keys in sorted(unknown_by_source.items()):
                keys = sorted(unknown_keys)
                source_ids = [
                    item["evidence_id"] for item in analysis.evidence_index
                    if item.get("source_module") == source
                ]
                hyps.append(BrainHypothesis(
                    statement=(
                        f"Unclassified measurements from {source or 'an unknown source'} "
                        f"({', '.join(str(key) for key in keys[:8])}) are present on {symbol}; "
                        "their units, semantics and predictive relevance are not established."
                    ),
                    confidence=0.0,
                    direction_implication=FLAT,
                    invalidation="The measurement definition, units, provenance and independent interpretation are documented and tested",
                    opportunity_implication="unknown-pattern observation; retain as context without assigning a direction or trade permission",
                    hypothesis_kind="unknown_pattern",
                    supporting_evidence_ids=source_ids,
                    assumptions=["No semantic or directional meaning is assumed for unfamiliar measurements."],
                    unresolved_unknowns=[
                        "measurement semantics and units are undocumented",
                        "predictive relevance has not been established",
                    ],
                    distinguishing_evidence=[
                        "obtain the producer's measurement definition and units",
                        "validate source timestamp, freshness and symbol scope",
                        "test whether independent evidence corroborates the interpretation",
                    ],
                    confidence_components={
                        "unclassified_measurement_count": len(keys),
                        "calibrated_probability": False,
                    },
                    evidence_lineage_scope="source_exact" if source_ids else "not_available",
                ))

            # Volatility expansion/compression is a market-state hypothesis, not
            # a directional trigger. The Brain waits for structural acceptance
            # and a separate opportunity thesis before deriving direction.
            if regime.get("volatility") in {"expanding", "compressing"}:
                volatility_ids = [
                    item["evidence_id"] for item in analysis.evidence_index
                    if item.get("domain") == "volatility"
                ]
                state = str(regime["volatility"])
                hyps.append(BrainHypothesis(
                    statement=(
                        f"{symbol} volatility is {state}; expansion can continue price discovery "
                        "or exhaust into a snapback, while compression can precede expansion "
                        "in either direction. The next structural acceptance remains unresolved."
                    ),
                    confidence=min(
                        0.5, _clamp01(analysis.domain_coverage.get("volatility", 0.0)),
                    ),
                    direction_implication=FLAT,
                    invalidation="Completed-bar structural acceptance resolves the direction of the volatility transition",
                    opportunity_implication="volatility-regime hypothesis; monitor both continuation and rejection without assigning direction",
                    hypothesis_kind="volatility_regime",
                    supporting_evidence_ids=volatility_ids,
                    assumptions=["Volatility state alone does not establish directional intent."],
                    unresolved_unknowns=[
                        "whether expansion is accepted or rejected",
                        "whether volatility change is transient or persistent",
                    ],
                    distinguishing_evidence=[
                        "completed-bar acceptance beyond a measured boundary",
                        "follow-through in independent order-flow and volume observations",
                        "rejection back into the prior range",
                    ],
                    confidence_components={
                        "volatility_domain_quality": round(
                            _clamp01(analysis.domain_coverage.get("volatility", 0.0)), 4,
                        ),
                        "calibrated_probability": False,
                    },
                    evidence_lineage_scope="domain_level_candidate_sources" if volatility_ids else "not_available",
                ))

            # State-first competitor set (I-2 / I-4).
            # Form each explanation from the current regime even when the pattern
            # detector returns nothing. Pattern names below are evidence that can
            # support or contradict an existing hypothesis; they are not gates on
            # whether the market hypothesis exists.
            structure_state = str(regime.get("structure", "") or "").upper()
            flow_state = str(regime.get("order_flow", "") or "").lower()
            trend_state = str(regime.get("trend_context", "") or "").lower()
            try:
                range_frames = max(0, int(regime.get("range_timeframes", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                range_frames = 0

            quality_domains = (
                list(analysis.informative_domains)
                if analysis.evidence_index else list(analysis.domain_coverage)
            )
            coverage_quality = (
                sum(_clamp01(analysis.domain_coverage.get(domain, 0.0)) for domain in quality_domains)
                / len(quality_domains)
                if quality_domains else 0.0
            )
            coverage_completeness = min(1.0, len(quality_domains) / 6.0)
            coverage_gap = (
                _clamp01(analysis.coverage_gap)
                if analysis.coverage_gap != 1.0 or not analysis.domain_coverage
                else 1.0 - coverage_completeness
            )
            agreement = _clamp01(analysis.agreement_score)
            uncertainty = _clamp01(analysis.evidence_uncertainty, 1.0)
            evidence_quality = (
                coverage_quality + coverage_completeness + agreement + (1.0 - uncertainty)
            ) / 4.0
            counter_pressure = (
                coverage_quality + coverage_completeness + (1.0 - agreement) + uncertainty
            ) / 4.0

            if structure_state in (LONG, SHORT):
                state_direction = structure_state
            elif flow_state == "buying":
                state_direction = LONG
            elif flow_state == "selling":
                state_direction = SHORT
            else:
                state_direction = FLAT
            opposite_state_direction = (
                SHORT if state_direction == LONG else
                LONG if state_direction == SHORT else FLAT
            )

            # A recognisable pattern modifies the evidence attached to a
            # hypothesis; its absence never removes the hypothesis from the set.
            hypothesis_text = " ".join(
                f"{item.statement} {item.opportunity_implication}"
                for item in hyps
            ).lower()
            state_candidates = (
                {
                    "key": "continuation",
                    "needle": ("continuation", "continue"),
                    "statement": (
                        f"Continuation remains a conditional explanation for {symbol} "
                        "if the observed regime persists; a sustained structural or flow "
                        "failure would challenge it."
                    ),
                    "direction": state_direction,
                    "confidence": evidence_quality,
                    "invalidation": "The current structural/flow state fails on fresh completed-bar evidence",
                    "opportunity": (
                        f"continuation candidate; activation requires fresh follow-through "
                        f"consistent with the observed regime; candidate_direction={state_direction}"
                    ),
                    "support": support,
                    "against": against,
                },
                {
                    "key": "pullback",
                    "needle": ("pullback", "correction"),
                    "statement": (
                        f"A pullback or correction is a competing explanation for {symbol}; "
                        "its depth and acceptance must be assessed against the prevailing regime."
                    ),
                    "direction": opposite_state_direction,
                    "confidence": counter_pressure if state_direction in (LONG, SHORT) else coverage_quality,
                    "invalidation": "The pullback fails to develop and the prior directional structure resumes",
                    "opportunity": (
                        f"pullback/correction candidate; activation requires a measured retracement "
                        f"and fresh response evidence; candidate_direction={opposite_state_direction}"
                    ),
                    "support": [n for n in names if "pullback" in n or "retracement" in n or "correction" in n],
                    "against": support,
                },
                {
                    "key": "reversal",
                    "needle": ("reversal", "reversal candidate"),
                    "statement": (
                        f"A reversal is an alternative explanation for {symbol}, conditional on "
                        "fresh evidence that the prevailing structure or order flow is failing."
                    ),
                    "direction": opposite_state_direction,
                    "confidence": counter_pressure,
                    "invalidation": "The prevailing structural extreme is extended and accepted with supporting flow",
                    "opportunity": (
                        f"reversal candidate; do not activate without independent invalidation evidence; "
                        f"candidate_direction={opposite_state_direction}"
                    ),
                    "support": against,
                    "against": support,
                },
                {
                    "key": "range rotation",
                    "needle": ("range rotation", "range-rotation", "range-edge"),
                    "statement": (
                        f"Range rotation remains a candidate explanation for {symbol}; "
                        "the observed boundaries and acceptance outside them determine whether it fits."
                    ),
                    "direction": FLAT,
                    "confidence": (
                        (coverage_quality + coverage_completeness + (min(1.0, range_frames / 2.0) if range_frames else 0.0)) / 3.0
                        if trend_state == "range" or range_frames >= 2
                        else (coverage_quality + coverage_completeness) / 6.0
                    ),
                    "invalidation": "Fresh completed-bar evidence accepts beyond a measured range boundary",
                    "opportunity": "range rotation candidate; evaluate both edges and breakout alternatives before selecting direction",
                    "support": [n for n in names if "range" in n or "compression" in n],
                    "against": [n for n in names if "breakout" in n or "displacement" in n],
                },
                {
                    "key": "failed breakout",
                    "needle": ("failed breakout", "breakout failure"),
                    "statement": (
                        f"A failed breakout remains a competing explanation for {symbol}; "
                        "it requires evidence of rejection after price tests beyond a relevant boundary."
                    ),
                    "direction": opposite_state_direction,
                    "confidence": counter_pressure,
                    "invalidation": "Price accepts beyond the boundary with continued participation",
                    "opportunity": (
                        f"failed-breakout candidate; activation requires boundary breach followed by "
                        f"rejection/failed acceptance; candidate_direction={opposite_state_direction}"
                    ),
                    "support": [n for n in names if "failed_breakout" in n or "liquidity_sweep_reclaimed" in n],
                    "against": [n for n in names if "volume_confirmed_breakout" in n or "displacement_confirmed" in n],
                },
                {
                    "key": "no-trade",
                    "needle": ("no-trade", "no trade", "wait candidate"),
                    "statement": (
                        f"No-trade/wait remains an explicit competing outcome for {symbol}; "
                        "missing, stale, contradictory or non-executable evidence must not be filled by inference."
                    ),
                    "direction": FLAT,
                    "confidence": (
                        coverage_gap
                        + uncertainty
                        + (1.0 - agreement)
                    ) / 3.0,
                    "invalidation": "Fresh independent evidence resolves the key uncertainty and supports an admitted opportunity",
                    "opportunity": "no-trade/wait candidate; observe until evidence, activation, execution cost and risk are adequately established",
                    "support": [n for n in names if "execution_degraded" in n],
                    "against": [],
                },
            )
            for candidate in state_candidates:
                if any(token in hypothesis_text for token in candidate["needle"]):
                    continue
                hyps.append(BrainHypothesis(
                    statement=candidate["statement"],
                    supporting_patterns=list(candidate["support"]),
                    contradicting_patterns=list(candidate["against"]),
                    confidence=_clamp01(candidate["confidence"]),
                    direction_implication=candidate["direction"],
                    invalidation=candidate["invalidation"],
                    opportunity_implication=candidate["opportunity"],
                    hypothesis_kind=str(candidate["key"]).replace(" ", "_").replace("-", "_"),
                    supporting_evidence_ids=list(dict.fromkeys(
                        evidence_id for name in candidate["support"]
                        for evidence_id in analysis.pattern_evidence_ids.get(pattern_name(name), [])
                    )),
                    contradicting_evidence_ids=list(dict.fromkeys(
                        evidence_id for name in candidate["against"]
                        for evidence_id in analysis.pattern_evidence_ids.get(pattern_name(name), [])
                    )),
                    assumptions=list(analysis.assumptions),
                    unresolved_unknowns=list(analysis.unknowns or analysis.missing_information),
                    distinguishing_evidence=(
                        [f"support: {pattern_name(name)}" for name in candidate["support"]]
                        + [f"challenge: {pattern_name(name)}" for name in candidate["against"]]
                        + [f"invalidation: {candidate['invalidation']}"]
                    ),
                    horizon=self._evidence_horizon(
                        analysis, candidate["support"], candidate["against"],
                    ),
                    confidence_components=self._confidence_components(
                        candidate["support"], candidate["against"], analysis,
                    ),
                    evidence_lineage_scope=(
                        "domain_level_candidate_sources"
                        if candidate["support"] or candidate["against"] else "not_available"
                    ),
                ))
                hypothesis_text += " " + candidate["key"]

            # Strategy-lens workbench: each evidence-relevant analytical
            # family becomes an explicit, non-directional hypothesis candidate.
            # The lens catalog supplies questions and data requirements only;
            # it does not supply a side, forecast probability or trade permission.
            lens_registry = {lens.key: lens for lens in STRATEGY_LENSES}
            for assessment in analysis.strategy_lenses:
                if not isinstance(assessment, dict) or assessment.get("status") != "relevant":
                    continue
                lens_key = str(assessment.get("key", "") or "")
                lens = lens_registry.get(lens_key)
                if lens is None:
                    continue
                lens_domains = set(lens.evidence_domains)
                source_ids = [
                    item["evidence_id"] for item in analysis.evidence_index
                    if item.get("domain") in lens_domains
                ]
                if not source_ids:
                    continue
                lens_support = [
                    name for name in names
                    if lens_domains.intersection(_PATTERN_DOMAINS.get(name, ()))
                ]
                lens_challenges = [
                    name for name in names
                    if name in _CONTRA_PATTERNS
                    and lens_domains.intersection(_PATTERN_DOMAINS.get(name, ()))
                ]
                relevance = _clamp01(assessment.get("relevance", 0.0))
                lens_confidence = min(
                    0.30,
                    0.10
                    + 0.04 * min(3, len(assessment.get("reasons", []) or []))
                    + 0.02 * min(3, len(source_ids)),
                )
                hyps.append(BrainHypothesis(
                    statement=(
                        f"{lens.name} is relevant to the current {symbol} evidence. "
                        f"Current anchors: {'; '.join(assessment.get('reasons', []) or [])}. "
                        "This is a lens-specific explanation to test, not a validated edge."
                    )[:500],
                    supporting_patterns=lens_support,
                    contradicting_patterns=lens_challenges,
                    confidence=lens_confidence,
                    direction_implication=FLAT,
                    invalidation=(
                        "The lens's required observations are contradicted, stale, or no longer relevant"
                    ),
                    opportunity_implication=(
                        f"{lens.name} hypothesis; evaluate its required evidence and limitations "
                        "without assigning direction from the lens itself"
                    ),
                    hypothesis_kind=f"strategy_lens:{lens_key}",
                    supporting_evidence_ids=source_ids,
                    contradicting_evidence_ids=list(dict.fromkeys(
                        evidence_id for name in lens_challenges
                        for evidence_id in analysis.pattern_evidence_ids.get(name, [])
                    )),
                    assumptions=[
                        "Lens relevance does not establish predictive value or profitability."
                    ],
                    unresolved_unknowns=list(dict.fromkeys(
                        list(assessment.get("missing_context", []) or [])
                        + [lens.limitation]
                    )),
                    distinguishing_evidence=list(lens.data_requirements),
                    horizon=self._evidence_horizon(
                        analysis, lens_support, lens_challenges,
                    ),
                    confidence_components={
                        "lens_relevance": relevance,
                        "source_count": len(source_ids),
                        "score_kind": "lens_relevance_not_forecast_confidence",
                        "calibrated_probability": False,
                    },
                    evidence_lineage_scope="domain_level_candidate_sources",
                ))

            # Generate explicit counter-hypotheses for the strongest distinct
            # directional explanations. When no directional thesis exists, still
            # challenge the strongest market-state interpretation.
            directional_primaries = sorted(
                (item for item in hyps if item.is_directional),
                key=lambda item: item.confidence,
                reverse=True,
            )
            if directional_primaries:
                for primary in directional_primaries[:3]:
                    hyps.append(self._counter(primary, analysis))
            else:
                primary = max(hyps, key=lambda item: item.confidence)
                hyps.append(self._counter(primary, analysis))
            return hyps
        except Exception:  # noqa: BLE001 — hypothesis generation must never break reasoning
            return [BrainHypothesis(
                statement="No structured hypothesis could be formed from the evidence.",
                confidence=0.0, direction_implication=FLAT,
                invalidation="", opportunity_implication="none")]

    def _make(
        self, *, statement: str, support: "list[str]", against: "list[str]",
        analysis: EvidenceAnalysis, direction: str, invalidation: str, opportunity: str,
        hypothesis_kind: str = "market_state", horizon: str = "unspecified",
    ) -> BrainHypothesis:
        conf = self._hypothesis_confidence(support, against, analysis)
        support_ids = list(dict.fromkeys(
            evidence_id for name in support
            for evidence_id in analysis.pattern_evidence_ids.get(pattern_name(name), [])
        ))
        against_ids = list(dict.fromkeys(
            evidence_id for name in against
            for evidence_id in analysis.pattern_evidence_ids.get(pattern_name(name), [])
        ))
        components = self._confidence_components(support, against, analysis)
        return BrainHypothesis(
            statement=statement[:300], supporting_patterns=list(support),
            contradicting_patterns=list(against), confidence=conf,
            direction_implication=direction, invalidation=invalidation,
            opportunity_implication=opportunity,
            hypothesis_kind=hypothesis_kind,
            supporting_evidence_ids=support_ids,
            contradicting_evidence_ids=against_ids,
            assumptions=list(analysis.assumptions),
            unresolved_unknowns=list(analysis.unknowns or analysis.missing_information),
            distinguishing_evidence=(
                [f"support: {pattern_name(item)}" for item in support]
                + [f"challenge: {pattern_name(item)}" for item in against]
                + ([f"invalidation: {invalidation}"] if invalidation else [])
            ),
            horizon=(
                horizon if horizon != "unspecified"
                else self._evidence_horizon(analysis, support, against)
            ),
            confidence_components=components,
            evidence_lineage_scope=(
                "domain_level_candidate_sources"
                if support_ids or against_ids else "not_available"
            ),
        )

    @staticmethod
    def _evidence_horizon(
        analysis: EvidenceAnalysis, support: "list[str]", against: "list[str]",
    ) -> str:
        evidence_ids = {
            evidence_id
            for name in list(support) + list(against)
            for evidence_id in analysis.pattern_evidence_ids.get(pattern_name(name), [])
        }
        horizons = {
            str(item.get("opportunity_horizon", "")).strip()
            for item in analysis.evidence_index
            if item.get("evidence_id") in evidence_ids
            and str(item.get("opportunity_horizon", "")).strip()
        }
        if len(horizons) == 1:
            return next(iter(horizons))
        if len(horizons) > 1:
            return "conflicting_source_horizons"
        return "unspecified"

    @staticmethod
    def _confidence_components(
        support: "list[str]", against: "list[str]", analysis: EvidenceAnalysis,
    ) -> dict:
        quality_domains = (
            list(analysis.informative_domains)
            if analysis.evidence_index else list(analysis.domain_coverage)
        )
        domain_values = [
            analysis.domain_coverage.get(domain, 0.0)
            for domain in quality_domains
        ]
        mean_domain_quality = (
            sum(_clamp01(value) for value in domain_values) / len(domain_values)
            if domain_values else 0.0
        )
        support_ids = {
            evidence_id for name in support
            for evidence_id in analysis.pattern_evidence_ids.get(pattern_name(name), [])
        }
        against_ids = {
            evidence_id for name in against
            for evidence_id in analysis.pattern_evidence_ids.get(pattern_name(name), [])
        }
        return {
            "mean_domain_quality": round(mean_domain_quality, 4),
            "coverage_completeness": round(1.0 - _clamp01(analysis.coverage_gap), 4),
            "evidence_agreement": round(_clamp01(analysis.agreement_score), 4),
            "evidence_uncertainty": round(_clamp01(analysis.evidence_uncertainty, 1.0), 4),
            "supporting_evidence_count": len(support_ids),
            "contradicting_evidence_count": len(against_ids),
            "calibrated_probability": False,
        }

    @staticmethod
    def _hypothesis_confidence(
        support: "list[str]", against: "list[str]", analysis: EvidenceAnalysis,
    ) -> float:
        base = 0.5 + 0.12 * len(set(support)) - 0.12 * len(set(against))
        quality_domains = (
            list(analysis.informative_domains)
            if analysis.evidence_index else list(analysis.domain_coverage)
        )
        mean_conf = (
            sum(analysis.domain_coverage.get(domain, 0.0) for domain in quality_domains)
            / len(quality_domains)
            if quality_domains else 0.0
        )
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
        # This is a heuristic reasoning-strength score, not calibrated residual
        # probability. Calibration requires independent historical validation.
        conf = _clamp01(max(0.0, (1.0 - primary.confidence)) * (0.5 + 0.5 * analysis.agreement_score))
        return BrainHypothesis(
            statement=statement, supporting_patterns=list(primary.contradicting_patterns),
            contradicting_patterns=list(primary.supporting_patterns), confidence=conf,
            direction_implication=direction,
            invalidation="The primary thesis is confirmed by follow-through",
            opportunity_implication="counter-thesis / caution",
            hypothesis_kind="counter_hypothesis",
            supporting_evidence_ids=list(primary.contradicting_evidence_ids),
            contradicting_evidence_ids=list(primary.supporting_evidence_ids),
            assumptions=list(primary.assumptions),
            unresolved_unknowns=list(primary.unresolved_unknowns),
            distinguishing_evidence=[
                "test the counter-thesis against fresh independent evidence",
                f"primary invalidation: {primary.invalidation}" if primary.invalidation else "",
            ],
            confidence_components={
                "counter_confidence": round(conf, 4),
                "primary_hypothesis_id": primary.hypothesis_id,
                "calibrated_probability": False,
            },
            evidence_lineage_scope=primary.evidence_lineage_scope,
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
            counters = [
                item for item in hypotheses
                if getattr(item, "hypothesis_kind", "") == "counter_hypothesis"
                or "counter" in str(getattr(item, "opportunity_implication", "")).lower()
            ]
            counter_by_primary = {
                str(item.confidence_components.get("primary_hypothesis_id")): item
                for item in counters
                if isinstance(item.confidence_components, dict)
                and item.confidence_components.get("primary_hypothesis_id")
            }
            unlinked_counters = [
                item for item in counters
                if not (
                    isinstance(item.confidence_components, dict)
                    and item.confidence_components.get("primary_hypothesis_id")
                )
            ]
            fallback_counter = (
                max(unlinked_counters, key=lambda item: item.confidence)
                if unlinked_counters else None
            )
            refined: list[BrainHypothesis] = []
            for i, h in enumerate(hypotheses):
                notes: list[str] = []
                factor = 1.0
                # Compare only the counter that was generated for this exact
                # primary hypothesis. A stronger challenge to another thesis
                # must not contaminate this one's confidence.
                counter = counter_by_primary.get(h.hypothesis_id) or fallback_counter
                # 1) A detected pattern contradicts this hypothesis.
                active_contra = [n for n in h.contradicting_patterns if n in names]
                if active_contra:
                    factor *= 0.8
                    notes.append(f"contradicted by detected pattern(s): {', '.join(sorted(set(active_contra)))}")
                # 2) Missing information critical to a directional hypothesis.
                if h.is_directional:
                    # Any independently recorded contradiction challenges the
                    # thesis, including a single structural reading opposed by
                    # order flow (neither domain needs to be internally conflicted).
                    if analysis.contradictions:
                        contradiction_factor = max(
                            0.65, 1.0 - 0.05 * len(analysis.contradictions),
                        )
                        factor *= contradiction_factor
                        notes.append(
                            "challenged by independent contradiction(s): "
                            + "; ".join(analysis.contradictions[:3])
                        )
                    needs = self._required_domains(h)
                    absent = [d for d in needs if d not in analysis.domain_coverage]
                    if absent:
                        factor *= 0.85
                        notes.append(f"relies on absent evidence: {', '.join(absent)}")
                    if analysis.evidence_index and not h.supporting_evidence_ids:
                        factor *= 0.75
                        notes.append("no directly attributable supporting evidence IDs")
                    regime = analysis.regime_indicators
                    if regime.get("structure") == "conflicted" or regime.get("order_flow") == "conflicted":
                        factor *= 0.75
                        notes.append("independent sources conflict on structure or order flow")
                    structure = str(regime.get("structure", "")).upper()
                    flow = str(regime.get("order_flow", "")).lower()
                    opposite = self._opposite(h.direction_implication)
                    if structure == opposite and flow == ("selling" if opposite == SHORT else "buying"):
                        factor *= 0.7
                        notes.append("both structure and order flow oppose this hypothesis")
                # 3) The counter-hypothesis is actually stronger.
                if counter is not None and h is not counter and counter.confidence > h.confidence:
                    factor *= 0.85
                    notes.append("counter-hypothesis is stronger")
                # 4) Survives criticism ⇒ a modest reinforcement.
                if (
                    not notes and h.is_directional
                    and analysis.agreement_score >= 0.7
                    and (h.supporting_evidence_ids or not analysis.evidence_index)
                ):
                    factor *= 1.04
                    notes.append("survived criticism: attributable support and no active challenge")
                h.confidence = _clamp01(h.confidence * factor)
                h.criticism = "; ".join(notes) if notes else "no material criticism"
                refined.append(h)
            return refined
        except Exception:  # noqa: BLE001 — self-criticism must never break reasoning
            return hypotheses

    @staticmethod
    def _required_domains(h: BrainHypothesis) -> "list[str]":
        sup = {pattern_name(p) for p in h.supporting_patterns}
        # A pure statistical-displacement thesis is supported by its signed
        # momentum z-score and contained EMA-spread measurement; requiring a
        # structure module that was not part of that detector would penalise it
        # for an unrelated missing domain.
        needs = set() if sup and sup <= {"statistical_displacement"} else {"structure"}
        if sup & {"momentum_acceleration", "liquidity_sweep_with_momentum_exhaustion",
                  "statistical_displacement"}:
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
        # Counter-hypotheses are adversarial challenges, not independent
        # recommendations. They may reduce confidence in a primary thesis but
        # must never become the selected direction merely because the primary
        # has low confidence. Compare all actual explanations, including no-trade.
        candidates = [
            item for item in hypotheses
            if item.hypothesis_kind != "counter_hypothesis"
        ] or list(hypotheses)
        return max(
            candidates,
            key=lambda h: (
                h.confidence,
                not h.is_directional,
                h.hypothesis_kind == "no_trade",
            ),
        )

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
