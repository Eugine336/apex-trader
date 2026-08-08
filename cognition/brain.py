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
                return self._observe(symbol, "no reasoner opinion — do nothing", consolidation)
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

        supporting, contradicting = self._split_evidence(market_state, direction)
        # Part IX Art 1/7 — a genuine expected value (in units of risk, R) from
        # the win-probability (confidence) and the payoff geometry, replacing the
        # former confidence-as-EV proxy. Part V/VI — carry the
        # competing hypotheses on the decision so cognition is not collapsed to a
        # single direction+confidence pair.
        reward_r, risk_r = reward_risk_from_opinion(opinion, self.reward_r_default)
        expected_value = expected_value_r(confidence, reward_r, risk_r)
        hypotheses = self._build_hypotheses(
            direction, confidence, reward_r, risk_r,
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
            and confidence >= self.min_confidence_to_act else "not established"),
            "downside": eae or "bounded by invalidation conditions",
            "opportunity": opportunity_txt or (direction if direction in (LONG, SHORT) else "none"),
            "should_i_do_nothing": "considered",
        }
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
            and confidence >= self.min_confidence_to_act
            and uncertainty <= self.max_uncertainty_to_act
            and ev_ok
        )
        if not act:
            directional_and_qualified = (
                direction in (LONG, SHORT)
                and confidence >= self.min_confidence_to_act
                and uncertainty <= self.max_uncertainty_to_act
            )
            if directional_and_qualified and not ev_ok:
                # Saw a directional opportunity but its expected value does not
                # clear the threshold — decline it (Part IX Q35: cannot execute
                # this opportunity at the current expected value).
                reason_txt = "expected value below threshold — opportunity declined"
                dtype = DecisionType.REJECT_OPPORTUNITY
            elif direction in (LONG, SHORT):
                reason_txt = "insufficient confidence/uncertainty for a campaign"
                dtype = DecisionType.CONTINUE_OBSERVING
            else:
                reason_txt = "no exploitable directional opportunity"
                dtype = (DecisionType.REJECT_OPPORTUNITY
                         if confidence >= self.min_confidence_to_act
                         else DecisionType.CONTINUE_OBSERVING)
            decision = DecisionPackage(
                symbol=symbol, decision_type=dtype, thesis=rationale or reason_txt,
                supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
                confidence=confidence, uncertainty=uncertainty,
                expected_value=expected_value, campaign_recommendation="observe",
                risk_rationale=reason_txt, invalidation_conditions=invalidation_conditions or competing,
                hypotheses=hypotheses,
                questions_answered=questions, do_nothing_considered=True,
                reasoner=self.reasoner_name,
            )
            return self._record(BrainOutput(decision=decision, direction=FLAT))

        decision = DecisionPackage(
            symbol=symbol, decision_type=DecisionType.OPEN_CAMPAIGN,
            thesis=rationale or f"{direction} opportunity",
            supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
            confidence=confidence, uncertainty=uncertainty,
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
            desired_exposure=round(confidence, 4),
            initial_execution_intent={"kind": "market", "confidence": round(confidence, 4)},
            supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
            confidence=confidence, invalidation_conditions=decision.invalidation_conditions,
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
            opinion = self._reasoner.reason(
                symbol, self._evidence_payload(market_state, consolidation), now=now)
            if opinion is None:
                return self._record_management(self._manage_pkg(
                    symbol, want, DecisionType.HOLD, 0.0, uncertainty, "no opinion — hold"))
            odir = str(getattr(opinion, "direction", FLAT) or FLAT).upper()
            conf = _clamp01(getattr(opinion, "confidence", 0.0))
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
                if self.allow_scale_in and conf >= self.reverse_confidence \
                        and (getattr(position, "profit_r", None) or 0.0) > 0:
                    action, why = DecisionType.SCALE_IN, "thesis strengthening + in profit — add"
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
                symbol, want, action, conf, uncertainty, rationale or why, opinion=opinion))
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
                "should_i_do_nothing": "considered",
            }
            for _k, _v in (("regime", str(getattr(opinion, "regime", "") or "")),
                           ("opportunity_horizon", str(getattr(opinion, "opportunity_horizon", "") or "")),
                           ("execution_quality", str(getattr(opinion, "execution_quality", "") or "")),
                           ("risk", str(getattr(opinion, "risk", "") or ""))):
                if _v:
                    questions[_k] = _v
        else:
            questions = {q: ("considered" if q == "should_i_do_nothing" else reason)
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
        return {
            "consolidation": consolidation,
            "evidence": [e.to_dict() for e in fresh[:64]],
        }

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
        decision = DecisionPackage(
            symbol=symbol, decision_type=DecisionType.CONTINUE_OBSERVING,
            thesis="no action", confidence=0.0,
            uncertainty=_clamp01(consolidation.get("aggregate_uncertainty", 1.0)) if consolidation else 1.0,
            campaign_recommendation="observe", risk_rationale=reason_txt,
            questions_answered={q: ("considered" if q == "should_i_do_nothing" else "insufficient")
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
            questions_answered={q: ("considered" if q == "should_i_do_nothing"
                                    else "reasoner unavailable") for q in REQUIRED_QUESTIONS},
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
