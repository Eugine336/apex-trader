"""APEX TRADER — The AI Cognitive Brain (the single reasoner).

Per the APEX Constitution (Part I Art 4; Part II) this is the ONLY subsystem
permitted to reason about the market. It consumes a consolidated
:class:`~cognition.contracts.MarketState` (structured Evidence) and produces a
:class:`~cognition.contracts.DecisionPackage` — and, when it opens a campaign, a
:class:`~cognition.contracts.CampaignSpecification`. It never places orders and
never reinterprets execution results; it reasons and decides, nothing else.

It is backed by a reasoner (the LLM reasoner) but is decoupled from any specific
provider: it only requires a duck-typed object exposing ``available`` and
``reason(symbol, evidence, now=None)`` returning an opinion with ``direction``,
``confidence``, ``rationale``, ``competing_hypotheses`` and
``missing_information``. When no reasoner is available — or it errors, or the
evidence is inadequate — the Brain lawfully defaults to **CONTINUE_OBSERVING**
(Part IV Art 7: there is no obligation to trade; Part II Art 3: if the required
questions cannot be answered with confidence, do not initiate a campaign).

Fail-safe: :meth:`reason` never raises; on any fault it returns a do-nothing
decision. Pure standard library (no third-party imports) so it is natively
importable and testable.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from cognition.contracts import (
    REQUIRED_QUESTIONS,
    CampaignSpecification,
    DecisionPackage,
    DecisionType,
    EvidenceDomain,
    Hypothesis,
    MarketState,
)
from cognition.expected_value import expected_value_r, reward_risk_from_opinion

logger = logging.getLogger("apex.cognition.brain")

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"


@dataclass
class BrainOutput:
    """The Brain's complete output for one reasoning pass."""

    decision: DecisionPackage
    campaign: Optional[CampaignSpecification] = None
    direction: str = FLAT
    decided_at_epoch: float = 0.0

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.to_dict(),
            "campaign": self.campaign.to_dict() if self.campaign is not None else None,
            "direction": self.direction,
            "decided_at_epoch": round(self.decided_at_epoch, 3),
        }


@dataclass
class PositionView:
    """Minimal open-position/campaign state the Brain reasons over to manage it."""

    symbol: str
    direction: str                       # LONG | SHORT
    profit_r: Optional[float] = None
    hold_seconds: float = 0.0
    size: float = 0.0
    campaign_id: str = ""
    entry_confidence: Optional[float] = None


class CognitiveBrain:
    """The sole market reasoner. Evidence in → decision out. Fail-safe."""

    def __init__(
        self,
        reasoner: Optional[Any] = None,
        *,
        min_confidence_to_act: float = 0.55,
        max_uncertainty_to_act: float = 0.6,
        reasoner_name: str = "ai_brain",
        allow_scale_in: bool = False,
        reverse_confidence: float = 0.7,
        exit_floor: float = 0.3,
        reward_r_default: float = 2.0,
        min_expected_value: Optional[float] = None,
        manage_min_interval_seconds: Optional[float] = None,
        default_cost_r: float = 0.0,
    ) -> None:
        self._reasoner = reasoner
        self.min_confidence_to_act = min(1.0, max(0.0, float(min_confidence_to_act)))
        self.max_uncertainty_to_act = min(1.0, max(0.0, float(max_uncertainty_to_act)))
        self.reasoner_name = str(reasoner_name or "ai_brain")
        self.allow_scale_in = bool(allow_scale_in)
        self.reverse_confidence = min(1.0, max(0.0, float(reverse_confidence)))
        self.exit_floor = min(1.0, max(0.0, float(exit_floor)))
        # Payoff geometry used to turn win-probability into a genuine expected
        # value (Part IX Art 1/7): reward-to-risk multiple when the opinion does
        # not supply explicit excursions. ``min_expected_value`` is retained for
        # the Part-C3 EV gate (act only when EV clears it); None ⇒ not enforced
        # here yet, so this change computes/records EV without altering the
        # current act/no-act behaviour.
        self.reward_r_default = max(0.0, float(reward_r_default))
        self.min_expected_value = (
            None if min_expected_value is None else float(min_expected_value)
        )
        # Violation V9 (Part XXIV/XXV) — a per-symbol fallback round-trip
        # execution cost in R, subtracted from the strategic EV so a theoretical
        # thesis that cannot clear spread/slippage is correctly rejected. When
        # the MarketState carries fresh EXECUTION_QUALITY evidence with an
        # ``estimated_total_cost_r`` measurement, that live estimate is used
        # instead; this default applies only when no such evidence is present.
        self.default_cost_r = max(0.0, float(default_cost_r))
        # Management re-reasons an OPEN position on a tighter, INDEPENDENT cadence
        # than origination (Q40 — reasoned management, not a static algo). None ⇒
        # use the reasoner's global interval (unchanged). Applied only on the
        # management path via a distinct throttle bucket, so origination + the
        # advisory council keep their global rate (no extra provider cost across
        # the scanned universe).
        self.manage_min_interval_seconds = (
            None if manage_min_interval_seconds is None
            else max(0.0, float(manage_min_interval_seconds))
        )
        self._reason_supports_override = self._detect_override_support(reasoner)
        self._decisions = 0
        self._campaigns_opened = 0
        self._observed = 0
        self._faults = 0
        self._managed = 0
        self._last: dict[str, BrainOutput] = {}
        self._last_management: dict[str, BrainOutput] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self._reasoner is not None
                    and getattr(self._reasoner, "available", False))

    def _reasoner_degraded(self, symbol: str) -> bool:
        """True when the wired reasoner reports its last ``reason(symbol)`` call
        FAILED (provider down / timeout / unparsable reply) rather than simply
        declining to trade. Fail-safe: a reasoner that does not expose the signal
        is treated as not-degraded, preserving prior behaviour."""
        fn = getattr(self._reasoner, "last_reason_degraded", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(symbol))
        except Exception:  # noqa: BLE001 — a health probe must never break reasoning
            return False

    @staticmethod
    def _detect_override_support(reasoner: Optional[Any]) -> bool:
        """True when ``reasoner.reason`` accepts the ``min_interval`` /
        ``throttle_key`` overrides (so management can run its own faster cadence).
        Fail-safe: a duck-typed stub without them is treated as unsupported."""
        fn = getattr(reasoner, "reason", None)
        if not callable(fn):
            return False
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False
        if "min_interval" in params and "throttle_key" in params:
            return True
        return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    def _manage_reason(self, symbol: str, payload: dict, now: Optional[float]) -> Any:
        """Reasoner call for the MANAGEMENT path — a tighter, independent throttle
        bucket so an open position is re-reasoned faster than origination without
        raising the global (origination + council) rate. Falls back to the plain
        call for a reasoner that does not support the override."""
        if self._reason_supports_override and self.manage_min_interval_seconds is not None:
            return self._reasoner.reason(
                symbol, payload, now=now,
                min_interval=self.manage_min_interval_seconds,
                throttle_key=f"{symbol}\x00manage",
            )
        return self._reasoner.reason(symbol, payload, now=now)

    # ── Reasoning ─────────────────────────────────────────────────────────

    def reason(self, market_state: MarketState, *, now: Optional[float] = None) -> BrainOutput:
        """Reason over consolidated evidence into a decision. Never raises."""
        symbol = getattr(market_state, "symbol", "") or ""
        try:
            consolidation = market_state.consolidation(now)
            # Distinguish "no reasoner configured" (a benign build-time state —
            # no obligation to trade) from "reasoner wired but its provider is
            # unavailable" (an INFRASTRUCTURE failure). The latter must never be
            # surfaced as a market view (Part XVIII Art 5 / Q78/Q80): it is a
            # distinct REASONER_UNAVAILABLE state, not a FLAT/observe conclusion.
            if self._reasoner is None:
                return self._observe(
                    symbol, "no reasoner configured — no obligation to trade", consolidation,
                )
            if not getattr(self._reasoner, "available", False):
                return self._reasoner_unavailable(
                    symbol, "reasoner unavailable — provider down (not a market view)",
                    consolidation,
                )
            opinion = self._reasoner.reason(
                symbol, self._evidence_payload(market_state, consolidation), now=now,
            )
            if opinion is None:
                # An AVAILABLE reasoner that yields no opinion did NOT form a
                # market view. If its last call actually FAILED (provider down /
                # timeout / unparsable reply) that is an INFRASTRUCTURE state —
                # Part XVIII Art 5 / Q78/Q80/Q81/Q106: it must never be surfaced
                # as a FLAT/observe read of the market. Only a benign no-op (e.g.
                # a throttled cycle: provider healthy, simply no fresh call this
                # cycle) may lawfully continue observing.
                if self._reasoner_degraded(symbol):
                    return self._reasoner_unavailable(
                        symbol,
                        "reasoner returned no usable opinion — provider failure / "
                        "unparsable output (not a market view)",
                        consolidation,
                    )
                return self._observe(
                    symbol, "no reasoner opinion this cycle — do nothing", consolidation,
                )
            return self._from_opinion(symbol, market_state, consolidation, opinion, now)
        except Exception as exc:  # noqa: BLE001 — reasoning must never break a cycle
            logger.debug("[brain] reason(%s) ignored a fault: %s", symbol, exc)
            with self._lock:
                self._faults += 1
            return self._observe(symbol, f"reasoning fault: {exc}", {})

    def _from_opinion(
        self, symbol: str, market_state: MarketState, consolidation: dict,
        opinion: Any, now: Optional[float],
    ) -> BrainOutput:
        direction = str(getattr(opinion, "direction", FLAT) or FLAT).upper()
        if direction not in (LONG, SHORT, FLAT):
            direction = FLAT
        confidence = _clamp01(getattr(opinion, "confidence", 0.0))
        rationale = str(getattr(opinion, "rationale", "") or "")
        competing = list(getattr(opinion, "competing_hypotheses", []) or [])
        missing = list(getattr(opinion, "missing_information", []) or [])
        # Part XXV — the richer cognitive state, present when the reasoner returns it.
        primary_hyp = str(getattr(opinion, "primary_hypothesis", "") or "")
        alternatives = list(getattr(opinion, "alternative_hypotheses", []) or []) or competing
        wcm = list(getattr(opinion, "what_would_change_my_mind", []) or [])
        regime = str(getattr(opinion, "regime", "") or "")
        key_uncertainty = str(getattr(opinion, "key_uncertainty", "") or "")
        invalidation_txt = str(getattr(opinion, "invalidation", "") or "")
        opportunity_txt = str(getattr(opinion, "opportunity", "") or "")
        opp_horizon = str(getattr(opinion, "opportunity_horizon", "") or "")
        efe = str(getattr(opinion, "expected_favorable_excursion", "") or "")
        eae = str(getattr(opinion, "expected_adverse_excursion", "") or "")
        ev_txt = str(getattr(opinion, "expected_value", "") or "")
        exec_q = str(getattr(opinion, "execution_quality", "") or "")
        risk_txt = str(getattr(opinion, "risk", "") or "")
        uncertainty = _clamp01(consolidation.get("aggregate_uncertainty", 1.0))
        # Part XXV — multidimensional confidence. Resolve each dimension (absent ⇒
        # the overall confidence) and blend to an EFFECTIVE conviction = the
        # weakest dimension. A strong thesis with poor execution/timing is not an
        # act-now trade; this makes EV, the act gate AND sizing reflect that, while
        # a legacy single-confidence reply is unchanged (every dim == confidence).
        conf_dims, eff_conf, limiting_dim = self._confidence_profile(opinion, confidence)

        # Violation V1 — the Brain is not a pure relay: cross-check the advisor's
        # stated confidence against the evidence picture it synthesised, and
        # attenuate the effective conviction when the advisor is overconfident
        # relative to the evidence (or claims conviction under high uncertainty).
        eff_conf, brain_synthesis = self._synthesize_from_evidence(
            consolidation, confidence, eff_conf)

        supporting, contradicting = self._split_evidence(market_state, direction)
        # Part IX Art 1/7 — a genuine expected value (in units of risk, R) from
        # the win-probability (confidence) and the payoff geometry, replacing the
        # former confidence-as-EV proxy. Part V/VI — carry the
        # competing hypotheses on the decision so cognition is not collapsed to a
        # single direction+confidence pair.
        reward_r, risk_r = reward_risk_from_opinion(opinion, self.reward_r_default)
        # Violation V9 — subtract the round-trip execution cost (spread/slippage,
        # in R) so a theoretical thesis that cannot clear its cost is declined at
        # the EV gate (Part XXIV/XXV). Sourced from fresh EXECUTION_QUALITY
        # evidence when present, else the configured default.
        cost_r = self._execution_cost_r(market_state)
        expected_value = expected_value_r(eff_conf, reward_r, risk_r, cost_r=cost_r)
        hypotheses = self._build_hypotheses(
            direction, eff_conf, reward_r, risk_r,
            primary_hyp or rationale, alternatives, invalidation_txt,
            supporting, contradicting,
        )
        questions = {
            "what_is_happening": primary_hyp or rationale,
            "why_is_it_happening": primary_hyp or rationale,
            "evidence_supports": f"{len(supporting)} evidence items lean {direction}",
            "evidence_contradicts": "; ".join(alternatives) if alternatives
            else f"{len(contradicting)} evidence items oppose",
            "information_missing": "; ".join(missing) if missing
            else (key_uncertainty or "none reported"),
            "what_would_change_my_mind": "; ".join(wcm) if wcm
            else ("; ".join(competing) if competing else "opposing evidence dominates"),
            "expected_value": ev_txt or ("positive" if direction in (LONG, SHORT)
            and eff_conf >= self.min_confidence_to_act else "not established"),
            "downside": eae or "bounded by invalidation conditions",
            "opportunity": opportunity_txt or (direction if direction in (LONG, SHORT) else "none"),
            # Violation V7 — populated with the ACTUAL do-nothing reasoning in the
            # act/no-act branches below (never left as a static "considered").
            "should_i_do_nothing": "evaluated",
        }
        # Violation V1 — record the Brain's cross-check of the advisor against the
        # evidence picture, so the synthesis (and any attenuation) is auditable.
        if brain_synthesis:
            questions["brain_synthesis"] = brain_synthesis
        # Part XXV — record the raw scalar and the confidence PROFILE so the honest
        # multidimensional read (and which dimension limited the trade) is auditable.
        questions["confidence_raw"] = round(confidence, 4)
        questions["confidence_profile"] = (
            f"thesis {conf_dims['thesis']:.2f} / opportunity {conf_dims['opportunity']:.2f} / "
            f"timing {conf_dims['timing']:.2f} / execution {conf_dims['execution']:.2f} "
            f"→ effective {eff_conf:.2f} (weakest: {limiting_dim})"
        )
        # Surface the remaining Part XXV context on the decision record for the
        # dashboard/governance (extra keys are harmless to consumers).
        for _k, _v in (("regime", regime), ("opportunity_horizon", opp_horizon),
                       ("expected_favorable_excursion", efe),
                       ("execution_quality", exec_q), ("risk", risk_txt)):
            if _v:
                questions[_k] = _v
        # Prefer the Brain's explicit invalidation / change-my-mind for the
        # campaign's invalidation conditions; fall back to competing hypotheses.
        invalidation_conditions = (
            ([invalidation_txt] if invalidation_txt else []) + wcm + competing
        )

        # Constitutional gate: only OPEN a campaign when the Brain can answer
        # with sufficient confidence AND uncertainty is acceptable AND there is a
        # direction AND the expected value clears the threshold. Otherwise do
        # nothing — there is no obligation to trade (Part IV Art 7 / Part IX Q35:
        # a good thesis at negative EV is not an executable trade).
        ev_ok = (self.min_expected_value is None) or (expected_value >= self.min_expected_value)
        act = (
            direction in (LONG, SHORT)
            and eff_conf >= self.min_confidence_to_act
            and uncertainty <= self.max_uncertainty_to_act
            and ev_ok
        )
        if not act:
            directional_and_qualified = (
                direction in (LONG, SHORT)
                and eff_conf >= self.min_confidence_to_act
                and uncertainty <= self.max_uncertainty_to_act
            )
            if directional_and_qualified and not ev_ok:
                # Saw a directional opportunity but its expected value does not
                # clear the threshold — decline it (Part IX Q35: cannot execute
                # this opportunity at the current expected value).
                reason_txt = "expected value below threshold — opportunity declined"
                dtype = DecisionType.REJECT_OPPORTUNITY
                do_nothing_txt = (
                    f"evaluated: doing nothing is correct — EV {expected_value:.4f}R "
                    "below threshold"
                )
            elif direction in (LONG, SHORT):
                # Part XXV — when the thesis itself is strong (raw confidence
                # clears the bar) but a WEAK actionability dimension (timing or
                # execution) pulled the effective conviction below it, this is a
                # deliberate WAIT for better conditions, not a rejected thesis.
                if (confidence >= self.min_confidence_to_act
                        and limiting_dim in ("timing", "execution")):
                    reason_txt = (
                        f"{limiting_dim} conditions insufficient — waiting "
                        f"(thesis holds at {confidence:.2f}, "
                        f"{limiting_dim} {conf_dims[limiting_dim]:.2f})"
                    )
                    do_nothing_txt = (
                        f"evaluated: doing nothing is correct — {limiting_dim} "
                        f"insufficient ({conf_dims[limiting_dim]:.2f}), thesis holds "
                        "but timing/execution not ready"
                    )
                elif uncertainty > self.max_uncertainty_to_act:
                    reason_txt = "insufficient confidence/uncertainty for a campaign"
                    do_nothing_txt = (
                        f"evaluated: doing nothing is correct — uncertainty "
                        f"{uncertainty:.2f} too high"
                    )
                else:
                    reason_txt = "insufficient confidence/uncertainty for a campaign"
                    do_nothing_txt = (
                        f"evaluated: doing nothing is correct — confidence "
                        f"{eff_conf:.2f} below threshold {self.min_confidence_to_act}"
                    )
                dtype = DecisionType.CONTINUE_OBSERVING
            else:
                reason_txt = "no exploitable directional opportunity"
                dtype = (DecisionType.REJECT_OPPORTUNITY
                         if eff_conf >= self.min_confidence_to_act
                         else DecisionType.CONTINUE_OBSERVING)
                do_nothing_txt = (
                    "evaluated: doing nothing is correct — no directional edge (FLAT)"
                )
            questions["should_i_do_nothing"] = do_nothing_txt
            decision = DecisionPackage(
                symbol=symbol, decision_type=dtype, thesis=rationale or reason_txt,
                supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
                confidence=eff_conf, uncertainty=uncertainty,
                expected_value=expected_value, campaign_recommendation="observe",
                risk_rationale=reason_txt, invalidation_conditions=invalidation_conditions or competing,
                hypotheses=hypotheses,
                questions_answered=questions, do_nothing_considered=True,
                reasoner=self.reasoner_name,
            )
            return self._record(BrainOutput(decision=decision, direction=FLAT))

        # Violation V7 — doing nothing was a real candidate action that lost to a
        # justified opportunity: record the EV it would forfeit.
        questions["should_i_do_nothing"] = (
            f"evaluated: doing nothing forfeits EV of {expected_value:.4f}R — "
            "opportunity justified"
        )
        decision = DecisionPackage(
            symbol=symbol, decision_type=DecisionType.OPEN_CAMPAIGN,
            thesis=rationale or f"{direction} opportunity",
            supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
            confidence=eff_conf, uncertainty=uncertainty,
            expected_value=expected_value,
            campaign_recommendation=f"open {direction}",
            risk_rationale="expected value positive on synthesised evidence",
            invalidation_conditions=invalidation_conditions or [f"{direction} thesis contradicted by dominant opposing evidence"],
            hypotheses=hypotheses,
            questions_answered=questions, do_nothing_considered=True,
            reasoner=self.reasoner_name,
        )
        campaign = CampaignSpecification(
            symbol=symbol, thesis=decision.thesis, direction=direction,
            desired_exposure=round(eff_conf, 4),
            initial_execution_intent={"kind": "market", "confidence": round(eff_conf, 4)},
            supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
            confidence=eff_conf, invalidation_conditions=decision.invalidation_conditions,
            objectives=[f"harvest {direction} opportunity while EV positive"],
            decision_id=decision.decision_id,
        )
        return self._record(BrainOutput(decision=decision, campaign=campaign, direction=direction))

    # ── Management (Phase F — Brain drives the open campaign) ─────────────

    def manage(self, position: "PositionView", market_state: MarketState, *,
               now: Optional[float] = None) -> BrainOutput:
        """Decide the management action for an OPEN position/campaign. Never raises.

        Reasons over the current evidence + the held direction and emits a
        management ``DecisionPackage`` (HOLD / SCALE_IN / TIGHTEN_RISK / EXIT /
        REVERSE). Fallback = HOLD (do nothing) when the reasoner is unavailable.
        """
        symbol = getattr(position, "symbol", "") or getattr(market_state, "symbol", "") or ""
        want = str(getattr(position, "direction", "") or "").upper()
        try:
            consolidation = market_state.consolidation(now)
            uncertainty = _clamp01(consolidation.get("aggregate_uncertainty", 1.0))
            if not self.available:
                return self._record_management(self._manage_pkg(
                    symbol, want, DecisionType.HOLD, 0.0, uncertainty,
                    "reasoner unavailable — hold (no change)"))
            opinion = self._manage_reason(
                symbol, self._evidence_payload(market_state, consolidation), now)
            if opinion is None:
                return self._record_management(self._manage_pkg(
                    symbol, want, DecisionType.HOLD, 0.0, uncertainty, "no opinion — hold"))
            odir = str(getattr(opinion, "direction", FLAT) or FLAT).upper()
            conf = _clamp01(getattr(opinion, "confidence", 0.0))
            # Part XXV — resolve the confidence PROFILE. Risk-ADDING actions
            # (SCALE_IN) require the effective conviction (the weakest dimension),
            # so the Brain never adds into weak execution/timing. Risk-REDUCING
            # actions (EXIT / TIGHTEN / REVERSE) keep using the raw scalar — a poor
            # execution read must never make it HARDER to cut a position.
            conf_dims, eff_conf, limiting_dim = self._confidence_profile(opinion, conf)
            rationale = str(getattr(opinion, "rationale", "") or "")
            aligned = odir == want and want in (LONG, SHORT)
            opposite = odir in (LONG, SHORT) and odir != want and want in (LONG, SHORT)
            # Part XXV Art 11 — management re-reasons over the SAME non-collapsed
            # cognitive state: the opportunity's persistence and expected value
            # govern the exit, not only whether the direction label still matches.
            # Empty fields (a minimal/legacy opinion) never force an exit — these
            # overrides fire only on an EXPLICIT signal, so behaviour is preserved.
            opportunity = str(getattr(opinion, "opportunity", "") or "").strip().lower()
            ev_txt = str(getattr(opinion, "expected_value", "") or "").strip().lower()
            opportunity_gone = opportunity in ("none", "no", "n/a", "gone", "expired")
            ev_negative = ev_txt.startswith("negative")
            thesis_deteriorated = opportunity_gone or ev_negative

            if aligned and conf >= self.min_confidence_to_act \
                    and uncertainty <= self.max_uncertainty_to_act and not thesis_deteriorated:
                in_profit = (getattr(position, "profit_r", None) or 0.0) > 0
                if self.allow_scale_in and in_profit and eff_conf >= self.reverse_confidence:
                    action, why = DecisionType.SCALE_IN, "thesis strengthening + in profit — add"
                elif (self.allow_scale_in and in_profit
                      and conf >= self.reverse_confidence
                      and eff_conf < self.reverse_confidence):
                    # Would add, but a weak actionability dimension (execution /
                    # timing) says now is not the moment to increase risk — hold.
                    action, why = DecisionType.HOLD, (
                        f"thesis strong but {limiting_dim} weak "
                        f"({conf_dims[limiting_dim]:.2f}) — hold, not adding"
                    )
                else:
                    action, why = DecisionType.HOLD, "thesis intact — hold"
            elif aligned and thesis_deteriorated:
                action, why = DecisionType.EXIT, "opportunity gone / EV no longer positive — exit"
            elif aligned and conf >= self.exit_floor:
                action, why = DecisionType.TIGHTEN_RISK, "supporting thesis weakening — tighten risk"
            elif opposite and conf >= self.reverse_confidence:
                action, why = DecisionType.REVERSE, "strong contrary evidence — reverse"
            elif opposite and conf >= self.min_confidence_to_act:
                action, why = DecisionType.EXIT, "contrary evidence dominant — exit"
            else:
                action, why = DecisionType.EXIT, "evidence no longer supports the position — exit"
            return self._record_management(self._manage_pkg(
                symbol, want, action, eff_conf, uncertainty, rationale or why, opinion=opinion))
        except Exception as exc:  # noqa: BLE001 — management reasoning must never break a cycle
            logger.debug("[brain] manage(%s) ignored a fault: %s", symbol, exc)
            with self._lock:
                self._faults += 1
            return self._record_management(self._manage_pkg(
                symbol, want, DecisionType.HOLD, 0.0, 1.0, f"manage fault: {exc}"))

    def _manage_pkg(self, symbol: str, held_dir: str, action: "DecisionType",
                    confidence: float, uncertainty: float, reason: str,
                    *, opinion: Any = None) -> BrainOutput:
        # Part XXV Art 11 — carry the SAME rich cognitive state onto the
        # management record as an entry decision, so an open position is
        # governed by re-reasoned hypotheses / invalidation / opportunity, not a
        # bare direction label. Falls back to the flat record for a minimal
        # opinion (or none), preserving prior behaviour.
        invalidation_conditions: list = []
        # Violation V7 — the management path's do-nothing (HOLD) is a real
        # evaluated candidate: when HOLD is chosen, doing nothing IS the action;
        # otherwise doing nothing lost to the chosen management action.
        if action == DecisionType.HOLD:
            do_nothing_txt = f"evaluated: doing nothing (hold) is correct — {reason}"
        else:
            do_nothing_txt = (
                f"evaluated: doing nothing rejected — {action.value} chosen: {reason}"
            )
        if opinion is not None:
            wcm = list(getattr(opinion, "what_would_change_my_mind", []) or [])
            inv = str(getattr(opinion, "invalidation", "") or "")
            invalidation_conditions = ([inv] if inv else []) + wcm
            primary = str(getattr(opinion, "primary_hypothesis", "") or reason)
            questions = {
                "what_is_happening": primary,
                "why_is_it_happening": primary,
                "evidence_supports": "; ".join(list(getattr(opinion, "supporting_evidence", []) or [])) or reason,
                "evidence_contradicts": "; ".join(list(getattr(opinion, "contradicting_evidence", []) or [])) or reason,
                "information_missing": "; ".join(list(getattr(opinion, "missing_information", []) or []))
                or (str(getattr(opinion, "key_uncertainty", "") or "") or "none reported"),
                "what_would_change_my_mind": "; ".join(wcm) if wcm else reason,
                "expected_value": str(getattr(opinion, "expected_value", "") or reason),
                "downside": str(getattr(opinion, "expected_adverse_excursion", "") or "bounded by invalidation"),
                "opportunity": str(getattr(opinion, "opportunity", "") or held_dir),
                "should_i_do_nothing": do_nothing_txt,
            }
            for _k, _v in (("regime", str(getattr(opinion, "regime", "") or "")),
                           ("opportunity_horizon", str(getattr(opinion, "opportunity_horizon", "") or "")),
                           ("execution_quality", str(getattr(opinion, "execution_quality", "") or "")),
                           ("risk", str(getattr(opinion, "risk", "") or ""))):
                if _v:
                    questions[_k] = _v
            _raw = _clamp01(getattr(opinion, "confidence", 0.0))
            _dims, _eff, _lim = self._confidence_profile(opinion, _raw)
            questions["confidence_raw"] = round(_raw, 4)
            questions["confidence_profile"] = (
                f"thesis {_dims['thesis']:.2f} / opportunity {_dims['opportunity']:.2f} / "
                f"timing {_dims['timing']:.2f} / execution {_dims['execution']:.2f} "
                f"→ effective {_eff:.2f} (weakest: {_lim})"
            )
        else:
            questions = {q: (do_nothing_txt if q == "should_i_do_nothing" else reason)
                         for q in REQUIRED_QUESTIONS}
        decision = DecisionPackage(
            symbol=symbol, decision_type=action, thesis=reason,
            confidence=confidence, uncertainty=uncertainty,
            campaign_recommendation=action.value, risk_rationale=reason,
            invalidation_conditions=invalidation_conditions,
            questions_answered=questions,
            do_nothing_considered=True, reasoner=self.reasoner_name,
        )
        out_dir = _opp(held_dir) if action == DecisionType.REVERSE else held_dir
        return BrainOutput(decision=decision, direction=out_dir)

    def _record_management(self, output: BrainOutput) -> BrainOutput:
        output.decided_at_epoch = time.time()
        with self._lock:
            self._managed += 1
            self._last_management[output.decision.symbol] = output
        return output

    def latest_management(self, symbol: str) -> Optional[BrainOutput]:
        with self._lock:
            return self._last_management.get(str(symbol or ""))

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _evidence_payload(market_state: MarketState, consolidation: dict) -> dict:
        fresh = market_state.fresh_evidence()
        # Part XXV — the council's per-advisor REASONING evidence is appended LAST,
        # so a tight cap would drop exactly the advisors' full analysis. Keep a
        # generous slice so every advisor's complete cognition reaches the Brain.
        return {
            "consolidation": consolidation,
            "evidence": [e.to_dict() for e in fresh[:96]],
        }

    @staticmethod
    def _confidence_profile(opinion: Any, base: float) -> "tuple[dict, float, str]":
        """Resolve the multidimensional confidence (Part XXV): each dimension, the
        effective conviction (the weakest), and which dimension limited it.

        A dimension absent on the opinion resolves to ``base`` (the overall
        ``confidence``), so a legacy single-scalar opinion yields every dimension
        equal to it and an effective conviction identical to the scalar (zero
        behaviour change). Never raises.
        """
        def _rd(attr: str) -> float:
            v = getattr(opinion, attr, None)
            if v is None:
                return base
            try:
                return min(1.0, max(0.0, float(v)))
            except (TypeError, ValueError):
                return base
        dims = {
            "thesis": _rd("thesis_confidence"),
            "opportunity": _rd("opportunity_confidence"),
            "timing": _rd("timing_confidence"),
            "execution": _rd("execution_confidence"),
        }
        eff = min(dims.values())
        limiting = min(dims, key=dims.get)
        return dims, eff, limiting

    def _execution_cost_r(self, market_state: MarketState) -> float:
        """Round-trip execution cost in R for the Brain's EV (Violation V9).

        Reads the freshest EXECUTION_QUALITY evidence's ``estimated_total_cost_r``
        measurement (emitted by the consolidator's execution-cost source) so the
        strategic EV is net of spread/slippage — a theoretical thesis that cannot
        clear its cost is correctly declined at the EV gate. Falls back to
        ``self.default_cost_r`` when no execution-quality evidence is present.
        Never raises.
        """
        try:
            best: Optional[float] = None
            for e in market_state.fresh_evidence():
                if getattr(e, "domain", None) != EvidenceDomain.EXECUTION_QUALITY:
                    continue
                m = getattr(e, "measurements", None)
                if not isinstance(m, dict):
                    continue
                if "estimated_total_cost_r" in m:
                    best = _clamp01(m.get("estimated_total_cost_r"))
                elif "estimated_spread_r" in m or "estimated_slippage_r" in m:
                    best = _clamp01(
                        _clamp01(m.get("estimated_spread_r"))
                        + _clamp01(m.get("estimated_slippage_r"))
                    )
            if best is not None:
                return best
        except Exception:  # noqa: BLE001 — cost estimation must never break reasoning
            pass
        return self.default_cost_r

    def _synthesize_from_evidence(
        self, consolidation: dict, advisor_confidence: float, effective_confidence: float,
    ) -> "tuple[float, str]":
        """Cross-check the advisor opinion against the evidence picture (V1).

        Structural strengthening of the Brain toward a native reasoner: before
        trusting the LLM advisor's stated confidence, the Brain inspects the
        consolidated evidence it synthesised (how much fresh evidence exists, its
        mean confidence, the aggregate uncertainty) and compares the advisor's
        conviction against it. When the advisor is markedly more confident than
        the evidence supports ("advisor overconfidence"), or claims conviction
        while evidence uncertainty is high ("confidence-evidence mismatch"), the
        effective conviction is ATTENUATED toward the evidence's own mean. This
        keeps the Brain from being a pure relay of the advisor's output. Returns
        the (possibly attenuated) effective confidence and a synthesis note.
        Never raises; when there is no fresh evidence to cross-check against, the
        effective confidence is returned unchanged.
        """
        try:
            n_fresh = int(consolidation.get("evidence_fresh", 0) or 0)
            if n_fresh <= 0:
                return effective_confidence, "no fresh evidence to cross-check advisor against"
            ev_mean = _clamp01(consolidation.get("mean_confidence", 0.0))
            ev_unc = _clamp01(consolidation.get("aggregate_uncertainty", 1.0))
            gap = advisor_confidence - ev_mean
            # Advisor markedly more confident than the evidence's own mean.
            if gap > 0.15:
                attenuated = _clamp01(0.5 * effective_confidence + 0.5 * ev_mean)
                return attenuated, (
                    f"LLM advisor confidence {advisor_confidence:.2f} vs evidence mean "
                    f"confidence {ev_mean:.2f} — attenuated effective confidence to "
                    f"{attenuated:.2f}"
                )
            # Advisor claims conviction while the evidence picture is very unsure.
            if advisor_confidence >= self.min_confidence_to_act and ev_unc > 0.6:
                attenuated = _clamp01(effective_confidence * (1.0 - ev_unc))
                return attenuated, (
                    f"confidence-evidence mismatch: advisor confidence "
                    f"{advisor_confidence:.2f} but evidence uncertainty {ev_unc:.2f} — "
                    f"attenuated effective confidence to {attenuated:.2f}"
                )
            return effective_confidence, (
                f"LLM advisor confidence {advisor_confidence:.2f} consistent with "
                f"evidence mean confidence {ev_mean:.2f} (uncertainty {ev_unc:.2f})"
            )
        except Exception:  # noqa: BLE001 — synthesis must never break reasoning
            return effective_confidence, ""

    @staticmethod
    def _split_evidence(market_state: MarketState, direction: str) -> "tuple[list[str], list[str]]":
        # Part XXV — evidence carries no directional reading, so supporting vs
        # contradicting can no longer be inferred from a polarity sign. Record
        # every fresh evidence id as provenance for what the Brain considered;
        # the qualitative supporting/contradicting split is the Brain's own
        # (opinion.supporting_evidence / contradicting_evidence), surfaced in the
        # decision's questions_answered.
        considered = [e.evidence_id for e in market_state.fresh_evidence()
                      if getattr(e, "evidence_id", "")]
        return considered, []

    @staticmethod
    def _build_hypotheses(
        direction: str, confidence: float, reward_r: float, risk_r: float,
        primary_statement: str, alternatives: "list", invalidation_txt: str,
        supporting: "list[str]", contradicting: "list[str]",
    ) -> "list[Hypothesis]":
        """Represent the Brain's primary + competing explanations simultaneously.

        Part V/VI: the decision must not collapse to a single direction. It
        carries the leading hypothesis (with its payoff geometry + invalidation)
        alongside the alternatives the Brain weighed, so contradictory readings
        survive into the record instead of being voted away. The residual
        probability mass (1 − confidence) is spread across the alternatives.
        """
        primary = Hypothesis(
            statement=(primary_statement or (
                f"{direction} opportunity" if direction in (LONG, SHORT)
                else "no directional edge"))[:200],
            probability=confidence,
            direction=direction if direction in (LONG, SHORT) else "",
            expected_reward_r=reward_r, expected_risk_r=risk_r,
            invalidation=invalidation_txt,
            supporting_evidence_ids=list(supporting or []),
            contradicting_evidence_ids=list(contradicting or []),
        )
        hyps = [primary]
        alts = [str(a).strip() for a in (alternatives or []) if str(a).strip()]
        if alts:
            residual = max(0.0, 1.0 - confidence)
            share = round(residual / len(alts), 4)
            for a in alts:
                # An opposing scenario's upside is the primary's downside, so its
                # reward/risk geometry is the primary's mirrored.
                hyps.append(Hypothesis(
                    statement=a[:200], probability=share, direction="",
                    expected_reward_r=risk_r, expected_risk_r=reward_r,
                ))
        return hyps

    def _observe(self, symbol: str, reason_txt: str, consolidation: dict) -> BrainOutput:
        # Violation V7 — doing nothing is an actual evaluated candidate, not a
        # static "considered": record why observing is the correct action.
        do_nothing_txt = f"evaluated: doing nothing is correct — {reason_txt}"
        decision = DecisionPackage(
            symbol=symbol, decision_type=DecisionType.CONTINUE_OBSERVING,
            thesis="no action", confidence=0.0,
            uncertainty=_clamp01(consolidation.get("aggregate_uncertainty", 1.0)) if consolidation else 1.0,
            campaign_recommendation="observe", risk_rationale=reason_txt,
            questions_answered={q: (do_nothing_txt if q == "should_i_do_nothing" else "insufficient")
                                for q in REQUIRED_QUESTIONS},
            do_nothing_considered=True, reasoner=self.reasoner_name,
        )
        return self._record(BrainOutput(decision=decision, direction=FLAT))

    def _reasoner_unavailable(self, symbol: str, reason_txt: str, consolidation: dict) -> BrainOutput:
        """Emit the distinct provider-unavailable state (Part XVIII Art 5 / Q78/Q80).

        This is NOT a market conclusion: the Brain has no usable reasoner, so it
        cannot form a view. Downstream must treat this as 'unknown / infrastructure
        down', never as a FLAT/observe read of the market — so origination and
        management both stand down without recording a market opinion.
        """
        decision = DecisionPackage(
            symbol=symbol, decision_type=DecisionType.REASONER_UNAVAILABLE,
            thesis="reasoner unavailable — no market view formed", confidence=0.0,
            uncertainty=_clamp01(consolidation.get("aggregate_uncertainty", 1.0)) if consolidation else 1.0,
            campaign_recommendation="stand_down", risk_rationale=reason_txt,
            questions_answered={q: (
                "evaluated: doing nothing is mandatory — reasoner unavailable "
                "(infrastructure, not market view)"
                if q == "should_i_do_nothing" else "reasoner unavailable")
                for q in REQUIRED_QUESTIONS},
            do_nothing_considered=True, reasoner=self.reasoner_name,
        )
        return self._record(BrainOutput(decision=decision, direction=FLAT))

    def _record(self, output: BrainOutput) -> BrainOutput:
        output.decided_at_epoch = time.time()
        with self._lock:
            self._decisions += 1
            if output.decision.decision_type == DecisionType.OPEN_CAMPAIGN:
                self._campaigns_opened += 1
            else:
                self._observed += 1
            self._last[output.decision.symbol] = output
        return output

    def latest(self, symbol: str) -> Optional[BrainOutput]:
        with self._lock:
            return self._last.get(str(symbol or ""))

    def get_status(self) -> dict:
        with self._lock:
            recent = {s: o.decision.decision_type.value for s, o in list(self._last.items())[-25:]}
            recent_mgmt = {s: o.decision.decision_type.value
                           for s, o in list(self._last_management.items())[-25:]}
            return {
                "available": self.available,
                "reasoner": self.reasoner_name,
                "min_confidence_to_act": self.min_confidence_to_act,
                "max_uncertainty_to_act": self.max_uncertainty_to_act,
                "allow_scale_in": self.allow_scale_in,
                "decisions": self._decisions,
                "campaigns_opened": self._campaigns_opened,
                "observed": self._observed,
                "managed": self._managed,
                "faults": self._faults,
                "recent_decisions": recent,
                "recent_management": recent_mgmt,
            }


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return min(1.0, max(0.0, f))


def _opp(direction: str) -> str:
    d = str(direction or "").upper()
    return SHORT if d == LONG else (LONG if d == SHORT else FLAT)


__all__ = ["CognitiveBrain", "BrainOutput", "PositionView", "LONG", "SHORT", "FLAT"]
