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
import time
from dataclasses import dataclass, field
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
    management_plan: dict[str, Any] = field(default_factory=dict)
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
            "management_plan": dict(self.management_plan),
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
    independence_group: str
    source_timestamp_verified: bool
    provisional: bool


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
            # Deprecated compatibility argument: the fixed three-domain
            # threshold is not a source-independence or campaign-validity test.
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
                # Campaign assessments require an explicit instrument identity.
                # Global/unknown-symbol telemetry may inform whole-book risk but
                # cannot invalidate an individual instrument's thesis.
                if not ev_symbol:
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
                        independence_group=str(
                            getattr(ev, "independence_group", "")
                            or measurements.get("independence_group", "") or ""
                        ).strip(),
                        source_timestamp_verified=(
                            getattr(ev, "source_timestamp_verified", False) is True
                        ),
                        provisional=bool(getattr(ev, "provisional", False)),
                    ))
                    for key in ("expected_value_after_cost_r", "net_ev_after_cost_r"):
                        value = _finite_number(measurements.get(key))
                        if value is not None:
                            ev_after_cost_values.append(value)

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

            concept_action = self._concept_milestone_guidance(
                context, held_side, position, fresh, symbol, now=now,
            )

            # The originating concept's completed-close invalidation and
            # objective are campaign-defining boundaries. They take precedence
            # over conflicting secondary assessments and risk-review states.
            if concept_action is not None and concept_action.get("kind") in {
                "objective_exit", "invalidation_exit",
            }:
                plan_kind = str(concept_action.get("kind", "") or "")
                thesis_state = (
                    "objective_reached" if plan_kind == "objective_exit"
                    else "concept_invalidation_confirmed"
                )
                return NativeManagementVerdict(
                    action="exit",
                    thesis_state=thesis_state,
                    reason=str(concept_action.get("reason", "concept geometry boundary reached")),
                    management_plan=concept_action,
                    **base,
                )

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
                # Corroboration is based on distinct producer-declared source
                # lineages, not domain labels. Multiple concepts reading the same
                # OHLC lineage count once. Unknown provenance is not independent.
                invalidation_groups = {
                    item.independence_group for item in invalidated
                    if item.independence_group
                    and item.source_timestamp_verified
                    and not item.provisional
                }
                context_complete = bool(original_thesis and invalidation_conditions)
                if (
                    len(invalidation_groups) >= 2
                    and uncertainty <= max_unc
                    and context_complete
                ):
                    return NativeManagementVerdict(
                        action="exit", thesis_state="invalidated",
                        reason=(
                            "original thesis invalidation is corroborated by distinct, fresh, "
                            "source-verified evidence lineages"
                        ),
                        **base,
                    )
                missing = []
                if len(invalidation_groups) < 2:
                    missing.append(
                        "two fresh, source-verified independent invalidation lineages "
                        f"({len(invalidation_groups)}/2)"
                    )
                if uncertainty > max_unc:
                    missing.append("market uncertainty to clear")
                if not context_complete:
                    missing.append("original thesis and invalidation conditions")
                return NativeManagementVerdict(
                    action="hold", thesis_state="invalidation_unconfirmed",
                    reason="invalidation marker is not sufficient to exit; missing " + ", ".join(missing),
                    **base,
                )

            # Fresh concept anchors and milestones de-risk the existing campaign;
            # they never create an add or reversal.
            if concept_action is not None:
                plan_kind = str(concept_action.get("kind", "") or "")
                if plan_kind == "trailing_stop_update":
                    return NativeManagementVerdict(
                        action="tighten_risk",
                        thesis_state="concept_anchor_advanced",
                        reason=str(concept_action.get("reason", "concept structural anchor advanced")),
                        management_plan=concept_action,
                        **base,
                    )
                return NativeManagementVerdict(
                    action="scale_out",
                    thesis_state="milestone_delivered",
                    reason=str(concept_action.get("reason", "concept milestone reached")),
                    management_plan=concept_action,
                    **base,
                )

            expected_horizon = _finite_number(
                context.get("expected_holding_horizon_seconds")
            )
            elapsed_holding = _finite_number(getattr(position, "hold_seconds", None))
            if (
                expected_horizon is not None and expected_horizon > 0.0
                and elapsed_holding is not None and elapsed_holding >= expected_horizon
            ):
                return NativeManagementVerdict(
                    action="hold",
                    thesis_state="time_decay_review",
                    reason=(
                        "the campaign's original expected holding horizon has elapsed; "
                        "reassess the thesis and its remaining expected value using fresh evidence"
                    ),
                    management_plan={
                        "kind": "time_decay_review",
                        "expected_horizon_seconds": expected_horizon,
                        "elapsed_holding_seconds": elapsed_holding,
                        "elapsed_ratio": elapsed_holding / expected_horizon,
                    },
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
            analysis_uncertainty = uncertainty
            analysis_contradictions: list[str] = []
            try:
                from cognition.pattern_rules import pattern_name

                native_patterns = {
                    pattern_name(item)
                    for item in list(getattr(analysis, "detected_patterns", []) or [])
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

            # This fallback can only reduce existing risk; it cannot EXIT or
            # add/reverse exposure. Do not make protective de-risking wait for an
            # arbitrary domain count or a low-uncertainty reading. The pattern or
            # contradicted challenger must still be present in this fresh native
            # analysis; full EXIT remains reserved for campaign-linked invalidation
            # or the originating concept's verified geometry boundary.
            has_adverse_evidence = bool(adverse_patterns) or bool(
                directional_challengers and analysis_contradictions
            )
            if has_adverse_evidence:
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
    def _concept_milestone_guidance(
        context: dict, held_side: str, position: Any, fresh: list, symbol: str,
        *, now: Optional[float] = None,
    ) -> Optional[dict]:
        """Build campaign-scoped, completed-bar management from original geometry.

        The original concept's stop and objective remain authoritative. New
        completed closes can invalidate or complete that thesis; fresh structural
        anchors can advance protection; milestones are idempotently identified
        by campaign, concept, bar and level.
        """
        geometry = context.get("concept_geometry")
        if not isinstance(geometry, dict) or held_side not in {"LONG", "SHORT"}:
            return None
        timeframe = str(geometry.get("concept_timeframe", "") or "").strip().upper()
        concept = str(geometry.get("concept", "") or "").strip()
        if not timeframe or not concept:
            return None
        geometry_symbol = str(geometry.get("symbol", "") or "").strip().upper()
        geometry_direction = str(geometry.get("direction", "") or "").strip().upper()
        if geometry_symbol != symbol.strip().upper() or geometry_direction != held_side:
            return None

        decision_time = _finite_number(now) if now is not None else None
        if decision_time is None:
            decision_time = time.time()
        entry_epoch = _finite_number(geometry.get("evidence_timestamp_epoch"))
        # Without the original completed-bar timestamp, a later fresh bar
        # cannot be proven to belong to this campaign's post-entry lifecycle.
        if entry_epoch is None or entry_epoch <= 0.0:
            return None
        campaign_id = str(context.get("campaign_id", "") or "").strip()
        pending_milestone = bool(context.get("pending_milestone_id"))
        completed = {
            str(item) for item in context.get("completed_milestones", []) or []
            if str(item).strip()
        }
        milestones = sorted({
            value for value in (
                _finite_number(item) for item in (geometry.get("milestones") or [])
            ) if value is not None and value > 0.0
        }, reverse=held_side == "SHORT")
        profit_r = _finite_number(getattr(position, "profit_r", None))
        latest_by_epoch: dict[float, list[tuple[float, Any]]] = {}
        structure_rows: list[tuple[float, float, float]] = []

        for evidence in fresh:
            if str(getattr(evidence, "symbol", "") or "").strip().upper() != symbol.upper():
                continue
            measurements = getattr(evidence, "measurements", None)
            if not isinstance(measurements, dict) or measurements.get("source_kind") != "market_lens":
                continue
            if (
                measurements.get("source_timestamp_known") is not True
                or measurements.get("data_basis") != "completed_ohlc"
                or measurements.get("provisional") is True
                or measurements.get("completed") is False
            ):
                continue
            epoch = _finite_number(measurements.get("source_bar_epoch"))
            horizon = _finite_number(getattr(evidence, "relevance_horizon_seconds", None))
            close = _finite_number(measurements.get("current_close"))
            if (
                epoch is None or close is None or horizon is None or horizon <= 0.0
                or epoch > decision_time or decision_time - epoch > horizon
                or (entry_epoch is not None and epoch <= entry_epoch)
            ):
                continue
            tf = str(measurements.get("timeframe", "") or "").strip().upper()
            if tf != timeframe:
                continue
            latest_by_epoch.setdefault(epoch, []).append((close, evidence))
            domain = str(getattr(getattr(evidence, "domain", None), "value", "") or "").lower()
            if domain == "structure":
                level_key = "prior_range_low" if held_side == "LONG" else "prior_range_high"
                level = _finite_number(measurements.get(level_key))
                if level is not None:
                    structure_rows.append((epoch, close, level))

        if not latest_by_epoch:
            return None
        latest_epoch = max(latest_by_epoch)
        latest_rows = latest_by_epoch[latest_epoch]
        latest_closes = {row[0] for row in latest_rows}
        # Same completed bar and timeframe must not disagree on its close. Do
        # not let iteration order decide whether a campaign exits or remains open.
        if len(latest_closes) != 1:
            return None
        close = next(iter(latest_closes))
        evidence_ids = tuple(dict.fromkeys(
            str(getattr(item, "evidence_id", "") or "")
            for _price, item in latest_rows
            if str(getattr(item, "evidence_id", "") or "")
        ))
        action_scope = campaign_id or f"{symbol}:{concept}:{timeframe}"

        stop = _finite_number(geometry.get("stop_price"))
        if stop is not None:
            invalidated = close <= stop if held_side == "LONG" else close >= stop
            if invalidated:
                # One stable id per campaign boundary: a still-open position
                # must not create a new close request on every subsequent bar.
                action_id = (
                    f"{action_scope}:concept-invalidation:{timeframe}:{stop:.10g}"
                )
                return {
                    "kind": "invalidation_exit",
                    "action_id": action_id,
                    "evidence_ids": list(evidence_ids),
                    "source_bar_epoch": latest_epoch,
                    "invalidation_price": stop,
                    "reason": (
                        f"{concept} ({timeframe}) invalidation {stop:.10g} was breached "
                        f"by a completed close at {close:.10g}"
                    ),
                }

        target = _finite_number(geometry.get("target_price"))
        if target is not None:
            target_reached = close >= target if held_side == "LONG" else close <= target
            if target_reached:
                action_id = (
                    f"{action_scope}:concept-objective:{timeframe}:{target:.10g}"
                )
                return {
                    "kind": "objective_exit",
                    "action_id": action_id,
                    "evidence_ids": list(evidence_ids),
                    "source_bar_epoch": latest_epoch,
                    "objective_price": target,
                    "reason": (
                        f"{concept} ({timeframe}) objective {target:.10g} was reached "
                        f"by a completed close at {close:.10g}"
                    ),
                }

        current_sl = _finite_number(getattr(position, "sl", None))
        initial_sl = _finite_number(geometry.get("stop_price"))
        known_stops = [value for value in (current_sl, initial_sl) if value is not None]
        protective_floor = (
            max(known_stops) if held_side == "LONG" and known_stops
            else min(known_stops) if held_side == "SHORT" and known_stops
            else None
        )
        trailing_candidates = [
            level for _epoch, row_close, level in structure_rows
            if protective_floor is not None and (
                protective_floor < level < row_close < close
                if held_side == "LONG"
                else close < row_close < level < protective_floor
            )
        ]
        trailing_stop = (
            max(trailing_candidates) if held_side == "LONG" and trailing_candidates
            else min(trailing_candidates) if held_side == "SHORT" and trailing_candidates
            else None
        )

        if not pending_milestone and profit_r is not None and profit_r > 0.0 and milestones:
            remaining = [
                (index, milestone) for index, milestone in enumerate(milestones, start=1)
                if f"{concept}:{timeframe}:{index}:{milestone:.10g}" not in completed
            ]
            crossed = [
                (index, milestone) for index, milestone in remaining
                if (close >= milestone if held_side == "LONG" else close <= milestone)
            ]
            if crossed:
                index, milestone = crossed[0]
                milestone_id = f"{concept}:{timeframe}:{index}:{milestone:.10g}"
                # Equal tranches are derived from the remaining concept milestones
                # plus the terminal objective; no fixed partial fraction is used.
                partial_fraction = 1.0 / (len(remaining) + 1)
                action_id = f"{action_scope}:milestone:{milestone_id}"
                return {
                    "kind": "milestone_partial",
                    "action_id": action_id,
                    "milestone_id": milestone_id,
                    "milestone_index": index,
                    "milestone_count": len(milestones),
                    "milestone_price": milestone,
                    "partial_fraction": partial_fraction,
                    "stop_price": trailing_stop,
                    "evidence_ids": list(evidence_ids),
                    "source_bar_epoch": latest_epoch,
                    "reason": (
                        f"{concept} ({timeframe}) milestone {index}/{len(milestones)} "
                        f"at {milestone:.10g} was passed by a completed close at {close:.10g}"
                    ),
                }

        if trailing_stop is not None and (
            current_sl is None
            or (trailing_stop > current_sl if held_side == "LONG" else trailing_stop < current_sl)
        ):
            action_id = f"{action_scope}:concept-trail:{timeframe}:{trailing_stop:.10g}"
            return {
                "kind": "trailing_stop_update",
                "action_id": action_id,
                "stop_price": trailing_stop,
                "source_bar_epoch": latest_epoch,
                "evidence_ids": list(evidence_ids),
                "reason": (
                    f"{concept} ({timeframe}) completed structure advanced the "
                    f"protective stop to {trailing_stop:.10g}"
                ),
            }
        return None

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
