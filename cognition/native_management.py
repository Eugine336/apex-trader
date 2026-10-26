"""Deterministic, provider-independent management of an existing campaign.

Native management consumes fresh, campaign-scoped measurements. It never
interprets provider availability as a market conclusion and never infers thesis
invalidation merely because the market is flat or a new directional read differs
from the held side.

Evidence producers may attach these measurements to fresh Evidence objects:
    campaign_id: the exact open campaign being assessed
    native_thesis_state: intact | evolving | deteriorating | invalidated | superseded
    thesis_invalidated: optional explicit boolean confirmation
    expected_value_after_cost_r: optional current holding EV, net of execution costs
    whole_book_risk_state: optional within_limits | breach | critical

An EXIT requires corroborated campaign-linked invalidation, sufficient independent
market-domain coverage, acceptable market uncertainty, and the original thesis
with its invalidation conditions. Ambiguous or incomplete assessments HOLD.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import isfinite
from typing import Any, Optional

from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION


logger = logging.getLogger("apex.cognition.native_management")


def _clamp01(value: Any, default: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not isfinite(number):
        return default
    return min(1.0, max(0.0, number))


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _affirmative(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in {"true", "yes", "confirmed"}


@dataclass(frozen=True)
class NativeManagementVerdict:
    """Auditable result of one native, thesis-based management assessment."""

    action: str
    thesis_state: str
    reason: str
    confidence: float
    uncertainty: float
    campaign_id: str = ""
    original_thesis: str = ""
    invalidation_conditions: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    evidence_domains: tuple[str, ...] = ()
    execution_cost_r: Optional[float] = None
    expected_value_after_cost_r: Optional[float] = None
    whole_book_risk_state: str = "unknown"
    evidence_count: int = 0
    hypothesis_count: int = 0
    identity_version: str = IDENTITY_VERSION
    identity: str = IDENTITY_STATEMENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_version": self.identity_version,
            "identity": self.identity,
            "action": self.action,
            "thesis_state": self.thesis_state,
            "reason": self.reason,
            "confidence": round(_clamp01(self.confidence, 0.0), 4),
            "uncertainty": round(_clamp01(self.uncertainty), 4),
            "campaign_id": self.campaign_id,
            "original_thesis": self.original_thesis[:500],
            "invalidation_conditions": list(self.invalidation_conditions[:8]),
            "evidence_ids": list(self.evidence_ids[:32]),
            "evidence_domains": list(self.evidence_domains),
            "execution_cost_r": self.execution_cost_r,
            "expected_value_after_cost_r": self.expected_value_after_cost_r,
            "whole_book_risk_state": self.whole_book_risk_state,
            "evidence_count": int(self.evidence_count),
            "hypothesis_count": int(self.hypothesis_count),
        }


@dataclass(frozen=True)
class _CampaignAssessment:
    state: str
    domain: str
    source: str
    evidence_id: str
    confidence: float
    uncertainty: float
    measurements: dict[str, Any]


class NativePositionManager:
    """Deterministic management layer that remains available without providers.

    It deliberately accepts only fresh evidence that explicitly identifies the
    campaign being assessed. A market-direction change, FLAT hypothesis, missing
    field, provider outage, or internal fault is never enough to close a position.
    Protective actions are not gated by advisor availability or advisor quorum.
    """

    _VALID_STATES = frozenset({
        "intact", "strengthening", "evolving", "deteriorating",
        "weakening", "invalidated", "superseded",
    })
    _CONFLICTING_STATES = frozenset({"intact", "strengthening"})

    def assess(
        self,
        position: Any,
        market_state: Any,
        *,
        campaign_context: Optional[dict[str, Any]] = None,
        consolidation: Optional[dict[str, Any]] = None,
        analysis: Any = None,
        hypotheses: Optional[list[Any]] = None,
        now: Optional[float] = None,
        max_uncertainty: float = 0.6,
        min_evidence_domains: int = 3,
    ) -> NativeManagementVerdict:
        """Reassess the held thesis. On any fault, return HOLD (never EXIT)."""
        symbol = str(getattr(position, "symbol", "") or "")
        held_side = str(getattr(position, "direction", "") or "").upper()
        context = campaign_context if isinstance(campaign_context, dict) else {}
        campaign_id = str(
            context.get("campaign_id")
            or getattr(position, "campaign_id", "")
            or ""
        ).strip()
        original_thesis = str(context.get("original_thesis") or "").strip()
        raw_invalidation = context.get("invalidation_conditions") or []
        if isinstance(raw_invalidation, str):
            invalidation_conditions = (raw_invalidation,) if raw_invalidation else ()
        else:
            try:
                invalidation_conditions = tuple(
                    str(item).strip() for item in raw_invalidation if str(item).strip()
                )
            except TypeError:
                invalidation_conditions = ()

        try:
            con = consolidation if isinstance(consolidation, dict) else {}
            if not con and market_state is not None:
                con = market_state.consolidation(now)
            uncertainty = _clamp01(con.get("aggregate_uncertainty", 1.0))
            required_domains = max(3, int(min_evidence_domains))
            max_unc = _clamp01(max_uncertainty)
            fresh = (
                list(market_state.fresh_evidence(now))
                if market_state is not None else []
            )
            if not symbol:
                symbol = str(getattr(market_state, "symbol", "") or "")
            if held_side not in {"LONG", "SHORT"}:
                return self._hold(
                    "unknown held side; native manager refuses a directional inference",
                    campaign_id, original_thesis, invalidation_conditions,
                    uncertainty, len(fresh), len(hypotheses or []),
                )
            if not campaign_id:
                return self._hold(
                    "campaign id missing; current evidence cannot be safely attributed to this position",
                    campaign_id, original_thesis, invalidation_conditions,
                    uncertainty, len(fresh), len(hypotheses or []),
                )
            if not fresh:
                return self._hold(
                    "no fresh evidence; retain the position and preserve broker-side protection",
                    campaign_id, original_thesis, invalidation_conditions,
                    uncertainty, 0, len(hypotheses or []),
                )

            market_domains: set[str] = set()
            assessments: list[_CampaignAssessment] = []
            execution_cost_values: list[float] = []
            ev_after_cost_values: list[float] = []
            book_risk_states: list[tuple[str, str, str]] = []

            for ev in fresh:
                ev_symbol = str(getattr(ev, "symbol", "") or "")
                if ev_symbol and symbol and ev_symbol != symbol:
                    continue
                domain_value = getattr(getattr(ev, "domain", None), "value", None)
                domain = str(domain_value or getattr(ev, "domain", "") or "other").lower()
                source = str(getattr(ev, "source_module", "") or "")
                measurements = getattr(ev, "measurements", {}) or {}
                if not isinstance(measurements, dict):
                    continue

                is_non_market_source = (
                    domain in {
                        "reasoning", "execution_quality", "risk", "portfolio", "portfolio_risk",
                    }
                    or "risk" in source.lower()
                    or "portfolio" in source.lower()
                    or "execution" in source.lower()
                    or "reasoning" in source.lower()
                )
                if not is_non_market_source:
                    market_domains.add(domain)

                for key in ("estimated_total_cost_r", "execution_cost_r", "round_trip_cost_r"):
                    value = _finite_number(measurements.get(key))
                    if value is not None and ("execution" in domain or "execution" in source.lower()):
                        execution_cost_values.append(value)

                assessed_id = str(
                    measurements.get("assessed_campaign_id")
                    or measurements.get("campaign_id")
                    or ""
                ).strip()
                if assessed_id != campaign_id:
                    # A cost or portfolio observation may be useful as context,
                    # but it cannot alter an unrelated campaign's thesis.
                    continue

                risk_raw = (
                    measurements.get("whole_book_risk_state")
                    or measurements.get("portfolio_risk_state")
                    or ""
                )
                risk_state = _text(risk_raw)
                if risk_state and ("risk" in domain or "portfolio" in domain or
                                   "risk" in source.lower() or "portfolio" in source.lower()):
                    book_risk_states.append((risk_state, domain, source))

                # Execution/risk telemetry can tighten risk through the dedicated
                # safety channels above, but it is not an independent MARKET
                # domain capable of corroborating original-thesis invalidation.
                if (
                    domain in {
                        "reasoning", "execution_quality", "risk",
                        "portfolio", "portfolio_risk",
                    }
                    or "risk" in source.lower()
                    or "portfolio" in source.lower()
                ):
                    # Domain strings not represented by EvidenceDomain normalize
                    # to OTHER, so source-family checks are needed to prevent
                    # portfolio/risk telemetry from masquerading as market proof.
                    continue

                raw_state = _text(
                    measurements.get("native_thesis_state")
                    or measurements.get("thesis_state")
                    or ""
                )
                if _affirmative(measurements.get("thesis_invalidated")) or _affirmative(
                    measurements.get("invalidation_confirmed")
                ):
                    raw_state = "invalidated"
                elif _affirmative(measurements.get("thesis_deteriorating")):
                    raw_state = "deteriorating"

                state = {
                    "invalid": "invalidated",
                    "broken": "invalidated",
                    "weakening": "weakening",
                    "deteriorated": "deteriorating",
                    "replaced": "superseded",
                }.get(raw_state, raw_state)
                confidence = _clamp01(getattr(ev, "confidence", 0.0), 0.0)
                evidence_uncertainty = _clamp01(getattr(ev, "uncertainty", 1.0))
                evidence_id = str(
                    getattr(ev, "evidence_id", "") or getattr(ev, "id", "") or ""
                )
                if state in self._VALID_STATES:
                    assessments.append(_CampaignAssessment(
                        state=state,
                        domain=domain,
                        source=source,
                        evidence_id=evidence_id,
                        confidence=confidence,
                        uncertainty=evidence_uncertainty,
                        measurements=measurements,
                    ))
                    for key in ("expected_value_after_cost_r", "net_ev_after_cost_r"):
                        value = _finite_number(measurements.get(key))
                        if value is not None:
                            ev_after_cost_values.append(value)

            domains_present = len(market_domains)
            assessment_domains = {item.domain for item in assessments}
            assessment_states = {item.state for item in assessments}
            evidence_ids = tuple(dict.fromkeys(
                item.evidence_id for item in assessments if item.evidence_id
            ))
            confidence = (
                sum(item.confidence for item in assessments) / len(assessments)
                if assessments else 0.0
            )
            cost_r = max(execution_cost_values) if execution_cost_values else None
            ev_after_cost = min(ev_after_cost_values) if ev_after_cost_values else None
            book_state = next(
                (state for state, _domain, _source in book_risk_states
                 if state in {"breach", "critical", "exceeded", "over_limit"}),
                book_risk_states[-1][0] if book_risk_states else "unknown",
            )
            base = {
                "campaign_id": campaign_id,
                "original_thesis": original_thesis,
                "invalidation_conditions": invalidation_conditions,
                "confidence": confidence,
                "uncertainty": uncertainty,
                "evidence_ids": evidence_ids,
                "evidence_domains": tuple(sorted(assessment_domains)),
                "execution_cost_r": cost_r,
                "expected_value_after_cost_r": ev_after_cost,
                "whole_book_risk_state": book_state,
                "evidence_count": len(fresh),
                "hypothesis_count": len(hypotheses or []),
            }

            # A campaign-scoped, explicit portfolio-risk breach only de-risks the
            # position. It never creates an add or reversal.
            if any(state in {"breach", "critical", "exceeded", "over_limit"}
                   for state, _domain, _source in book_risk_states):
                return NativeManagementVerdict(
                    action="tighten_risk", thesis_state="risk_constrained",
                    reason="campaign-linked whole-book risk evidence exceeds its stated boundary",
                    **base,
                )

            # Conflicting direct readings are not resolved by direction or by
            # whichever source has the strongest scalar confidence.
            if "invalidated" in assessment_states and (
                assessment_states & self._CONFLICTING_STATES
            ):
                return NativeManagementVerdict(
                    action="hold", thesis_state="conflicted",
                    reason="fresh campaign assessments conflict; hold and reassess rather than liquidate",
                    **base,
                )

            invalidated = [item for item in assessments if item.state == "invalidated"]
            if invalidated:
                invalidation_domains = {item.domain for item in invalidated}
                context_complete = bool(original_thesis and invalidation_conditions)
                if (
                    len(invalidation_domains) >= 2
                    and domains_present >= required_domains
                    and uncertainty <= max_unc
                    and context_complete
                ):
                    return NativeManagementVerdict(
                        action="exit", thesis_state="invalidated",
                        reason=(
                            "original thesis invalidation is corroborated across independent "
                            "domains with fresh evidence and adequate market coverage"
                        ),
                        **base,
                    )
                missing = []
                if len(invalidation_domains) < 2:
                    missing.append("independent confirmation")
                if domains_present < required_domains:
                    missing.append(f"market-domain coverage ({domains_present}/{required_domains})")
                if uncertainty > max_unc:
                    missing.append("market uncertainty to clear")
                if not context_complete:
                    missing.append("original thesis and invalidation conditions")
                return NativeManagementVerdict(
                    action="hold", thesis_state="invalidation_unconfirmed",
                    reason="invalidation marker is not sufficient to exit; missing " + ", ".join(missing),
                    **base,
                )

            # Negative expected value is meaningful only when it is explicitly
            # marked as AFTER cost and is tied to this campaign. It tightens risk;
            # it does not auto-liquidate a thesis that has not been invalidated.
            if (
                ev_after_cost is not None
                and ev_after_cost <= 0.0
                and cost_r is not None
                and len(assessment_domains) >= 2
            ):
                return NativeManagementVerdict(
                    action="tighten_risk", thesis_state="ev_deteriorated",
                    reason=(
                        "campaign EV is non-positive after fresh execution costs; "
                        "reduce risk while the original thesis is reassessed"
                    ),
                    **base,
                )

            deteriorating = [
                item for item in assessments
                if item.state in {"deteriorating", "weakening"}
            ]
            if deteriorating:
                deteriorating_domains = {item.domain for item in deteriorating}
                if len(deteriorating_domains) >= 2:
                    profit_r = _finite_number(getattr(position, "profit_r", None))
                    action = "scale_out" if profit_r is not None and profit_r > 0.0 else "tighten_risk"
                    return NativeManagementVerdict(
                        action=action, thesis_state="deteriorating",
                        reason=(
                            "the original campaign thesis is deteriorating in independent "
                            "fresh evidence; reduce exposure without treating uncertainty as invalidation"
                        ),
                        **base,
                    )
                return NativeManagementVerdict(
                    action="hold", thesis_state="deterioration_unconfirmed",
                    reason="only one evidence domain reports thesis deterioration; obtain confirmation",
                    **base,
                )

            superseded = [item for item in assessments if item.state == "superseded"]
            if len({item.domain for item in superseded}) >= 2:
                return NativeManagementVerdict(
                    action="tighten_risk", thesis_state="superseded",
                    reason="fresh campaign-linked evidence says the thesis has been superseded; reduce risk pending reassessment",
                    **base,
                )

            if assessments:
                state = max(
                    assessment_states,
                    key=lambda item: {"strengthening": 4, "intact": 3, "evolving": 2}.get(item, 1),
                )
                return NativeManagementVerdict(
                    action="hold", thesis_state=state,
                    reason="native evidence does not establish invalidation or material deterioration; retain the campaign",
                    **base,
                )

            # Real producers do not have to manufacture new campaign-state
            # measurement keys for the provider-independent Brain to be useful.
            # Reuse its existing structural analysis and self-criticised
            # counter-hypotheses as a conservative *risk-reduction-only* fallback.
            # Generic adverse patterns can never authorize EXIT.
            native_patterns: set[str] = set()
            analysis_domains: set[str] = set()
            analysis_uncertainty = uncertainty
            analysis_contradictions: list[str] = []
            try:
                from cognition.pattern_rules import pattern_name

                native_patterns = {
                    pattern_name(item)
                    for item in list(getattr(analysis, "detected_patterns", []) or [])
                }
                coverage = getattr(analysis, "domain_coverage", {}) or {}
                if isinstance(coverage, dict):
                    # Keep the fallback's coverage denominator consistent with
                    # the source-aware filtering above. Domain normalisation maps
                    # several non-market producers to OTHER, so a key-name-only
                    # filter is insufficient.
                    analysis_domains = {
                        str(domain).lower() for domain in coverage
                        if str(domain).lower() in market_domains
                    }
                analysis_uncertainty = max(
                    uncertainty,
                    _clamp01(getattr(analysis, "evidence_uncertainty", uncertainty)),
                )
                analysis_contradictions = [
                    str(item) for item in (getattr(analysis, "contradictions", []) or [])
                    if str(item).strip()
                ]
            except Exception as exc:  # noqa: BLE001 — native enrichment must not break management
                logger.debug(
                    "[native-management] analysis enrichment ignored for %s: %s",
                    symbol, exc,
                )
                native_patterns = set()
                analysis_domains = set()
                analysis_contradictions = []

            adverse_patterns = native_patterns & {
                "momentum_divergence",
                "trend_exhaustion",
                "multi_tf_conflict",
                "liquidity_sweep_absorbed",
                "liquidity_sweep_reclaimed",
                "liquidity_sweep_with_momentum_exhaustion",
                "failed_breakout",
                "volatility_compression",
                "range_rotation",
                "execution_degraded",
            }
            directional_challengers = []
            for hypothesis in list(hypotheses or []):
                direction = str(
                    getattr(hypothesis, "direction_implication", "") or ""
                ).upper()
                try:
                    hypothesis_confidence = _clamp01(
                        getattr(hypothesis, "confidence", 0.0), 0.0
                    )
                except Exception as exc:  # noqa: BLE001 — malformed hypothesis is non-authoritative
                    logger.debug(
                        "[native-management] invalid hypothesis confidence for %s: %s",
                        symbol, exc,
                    )
                    hypothesis_confidence = 0.0
                active_contradictions = {
                    str(item).lower()
                    for item in (getattr(hypothesis, "contradicting_patterns", []) or [])
                } & native_patterns
                if (
                    direction in {"LONG", "SHORT"}
                    and direction != held_side
                    and hypothesis_confidence >= 0.65
                    and active_contradictions
                ):
                    directional_challengers.append((hypothesis_confidence, hypothesis))

            sufficient_native_coverage = len(analysis_domains) >= required_domains
            uncertainty_acceptable = analysis_uncertainty <= max_unc
            if (
                sufficient_native_coverage
                and uncertainty_acceptable
                and (
                    len(adverse_patterns) >= 2
                    or "execution_degraded" in adverse_patterns
                    or (
                        directional_challengers
                        and bool(analysis_contradictions)
                    )
                )
            ):
                profit_r = _finite_number(getattr(position, "profit_r", None))
                action = "scale_out" if profit_r is not None and profit_r > 0.0 else "tighten_risk"
                challenger = (
                    max(directional_challengers, key=lambda item: item[0])[1]
                    if directional_challengers else None
                )
                challenger_note = (
                    f"; strongest counter-hypothesis is "
                    f"{getattr(challenger, 'direction_implication', '')} "
                    f"at {_clamp01(getattr(challenger, 'confidence', 0.0), 0.0):.2f}"
                    if challenger is not None else ""
                )
                return NativeManagementVerdict(
                    action=action,
                    thesis_state="native_evidence_deteriorating",
                    reason=(
                        "native Brain found corroborated adverse structure/market conflict "
                        "with adequate fresh domain coverage; reduce exposure, not exit"
                        + challenger_note
                    ),
                    confidence=(
                        max(directional_challengers, key=lambda item: item[0])[0]
                        if directional_challengers else 0.6
                    ),
                    uncertainty=analysis_uncertainty,
                    campaign_id=campaign_id,
                    original_thesis=original_thesis,
                    invalidation_conditions=invalidation_conditions,
                    evidence_ids=evidence_ids,
                    evidence_domains=tuple(sorted(analysis_domains)),
                    execution_cost_r=cost_r,
                    expected_value_after_cost_r=ev_after_cost,
                    whole_book_risk_state=book_state,
                    evidence_count=len(fresh),
                    hypothesis_count=len(hypotheses or []),
                )

            return NativeManagementVerdict(
                action="hold", thesis_state="uncertain",
                reason=(
                    "native market analysis completed, but no fresh campaign-linked thesis "
                    "assessment exists; do not infer invalidation from a FLAT read or a changed direction"
                ),
                **base,
            )
        except Exception as exc:  # noqa: BLE001 — faulty cognition must never liquidate
            logger.warning(
                "[native-management] assessment failed for %s; preserving position: %s",
                getattr(position, "symbol", "?"), exc,
            )
            # This manager sits on the management path. A faulty assessment must
            # never liquidate a position or prevent the surrounding cycle.
            return self._hold(
                "native management fault; preserve the position and broker-side safety floor",
                str(getattr(position, "campaign_id", "") or ""),
                str((campaign_context or {}).get("original_thesis", "") or "")
                if isinstance(campaign_context, dict) else "",
                (),
                1.0,
                0,
                len(hypotheses or []),
            )

    @staticmethod
    def _hold(
        reason: str,
        campaign_id: str,
        original_thesis: str,
        invalidation_conditions: tuple[str, ...],
        uncertainty: float,
        evidence_count: int,
        hypothesis_count: int,
    ) -> NativeManagementVerdict:
        return NativeManagementVerdict(
            action="hold",
            thesis_state="uncertain",
            reason=reason,
            confidence=0.0,
            uncertainty=uncertainty,
            campaign_id=campaign_id,
            original_thesis=original_thesis,
            invalidation_conditions=invalidation_conditions,
            evidence_count=evidence_count,
            hypothesis_count=hypothesis_count,
        )


__all__ = ["NativeManagementVerdict", "NativePositionManager"]
