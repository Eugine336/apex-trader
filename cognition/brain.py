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

from cognition.brain_reasoning import (
    BrainHypothesis,
    BrainReasoner,
    EvidenceAnalysis,
)
from cognition.contracts import (
    REQUIRED_QUESTIONS,
    CampaignSpecification,
    DecisionPackage,
    DecisionType,
    EvidenceDomain,
    Hypothesis,
    MarketState,
    Opportunity,
)
from cognition.expected_value import expected_value_r, reward_risk_from_opinion

logger = logging.getLogger("apex.cognition.brain")

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"


@dataclass
class BrainOutput:
    """The Brain's complete output for one reasoning pass.

    V-016 / Part XVIII Art 5 — ``direction`` is the Brain's directional MARKET
    read (LONG/SHORT/FLAT). It is ``None`` ONLY when the Brain formed no cognitive
    view at all because its reasoner/provider was unavailable: provider
    unavailability is an INFRASTRUCTURE state, never a FLAT market assessment.
    Consumers and logging must check ``decision_type`` / ``provider_unavailable``
    FIRST and never read ``FLAT`` (or ``None``) direction as "the market is flat".
    """

    decision: DecisionPackage
    campaign: Optional[CampaignSpecification] = None
    direction: Optional[str] = FLAT
    decided_at_epoch: float = 0.0

    @property
    def decision_type(self) -> str:
        """The decision's type as a string (e.g. ``open_campaign``,
        ``reasoner_unavailable``) — the primary status consumers/logs read BEFORE
        interpreting ``direction``."""
        dt = getattr(self.decision, "decision_type", None)
        return getattr(dt, "value", str(dt)) if dt is not None else ""

    @property
    def provider_unavailable(self) -> bool:
        """True when this output reflects an unavailable reasoner/provider
        (infrastructure down), NOT a market read. ``direction`` is ``None`` here
        so it can never be mistaken for a FLAT market conclusion."""
        return getattr(self.decision, "decision_type", None) == DecisionType.REASONER_UNAVAILABLE

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.to_dict(),
            "campaign": self.campaign.to_dict() if self.campaign is not None else None,
            "direction": self.direction,
            "decision_type": self.decision_type,
            "provider_unavailable": self.provider_unavailable,
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
        ev_primary_gate: bool = False,
        ev_action_threshold_r: float = 0.0,
        reasoner_name: str = "ai_brain",
        allow_scale_in: bool = False,
        reverse_confidence: float = 0.7,
        exit_floor: float = 0.3,
        reward_r_default: float = 2.0,
        min_expected_value: Optional[float] = None,
        manage_min_interval_seconds: Optional[float] = None,
        calibration: Optional[Any] = None,
        min_advisors_for_action: int = 2,
        min_evidence_domains: int = 2,
        degraded_confidence_multiplier: float = 0.7,
        degrade_on_unknown_council: bool = False,
        default_cost_r: float = 0.0,
        agreement_boost: float = 1.1,
        disagreement_penalty: float = 0.8,
        independent_min_confidence: float = 0.7,
        independent_max_uncertainty: float = 0.5,
        independent_sizing_multiplier: float = 0.5,
        pattern_tracker: Optional[Any] = None,
        max_correlated_positions: int = 3,
        max_total_exposure: float = 3.0,
    ) -> None:
        self._reasoner = reasoner
        self.min_confidence_to_act = min(1.0, max(0.0, float(min_confidence_to_act)))
        self.max_uncertainty_to_act = min(1.0, max(0.0, float(max_uncertainty_to_act)))
        # V-01 (§I direction is a CONSEQUENCE of cognition; §VI Q13/Q16/Q17;
        # §VIII FLAT = "no exploitable opportunity after EV/risk/execution", NOT
        # "the indicators disagree"). When ``ev_primary_gate`` is on, a campaign
        # opens on a positive EXPECTED VALUE (net of cost, over flat) rather than
        # a raw confidence floor — so a genuine positive-expectancy opportunity is
        # no longer vetoed merely because mixed evidence pulled confidence below a
        # threshold (the forbidden "conflicting evidence ⇒ FLAT" shortcut).
        # Confidence/uncertainty still feed the EV and the position sizing either
        # way. Default False ⇒ the legacy confidence-floor gate is unchanged.
        self.ev_primary_gate = bool(ev_primary_gate)
        self.ev_action_threshold_r = float(ev_action_threshold_r)
        self.reasoner_name = str(reasoner_name or "ai_brain")
        self.allow_scale_in = bool(allow_scale_in)
        self.reverse_confidence = min(1.0, max(0.0, float(reverse_confidence)))
        self.exit_floor = min(1.0, max(0.0, float(exit_floor)))
        # Article XX — advisor quorum: the Council must not become fake diversity.
        # A campaign must be backed by at least this many advisors that actually
        # contributed an opinion; a single advisor's read is insufficient
        # cognitive coverage for action.
        self.min_advisors_for_action = max(0, int(min_advisors_for_action))
        # Article XXXIV — minimum evidence domains: "cannot determine whether an
        # opportunity exists" ≠ "no opportunity". The Brain refuses to act on too
        # thin an evidence picture (fewer than this many domains present).
        self.min_evidence_domains = max(0, int(min_evidence_domains))
        # Article XXI / V-013 — degraded cognition must be recognised AND
        # enforced. When the council under-delivers (most available advisors did
        # not respond) the Brain attenuates its effective conviction by this
        # multiplier — degradation is no longer observation-only. ``degrade_on_
        # unknown_council`` extends the same penalty to the case where NO council
        # metadata exists at all (coverage unknown); it is off by default at this
        # (library) layer so a Brain deliberately wired without a council is not
        # blanket-penalised, but the deployed system opts in via config, and the
        # unknown state is ALWAYS logged and recorded (never a silent pass).
        self.degraded_confidence_multiplier = min(1.0, max(0.0, float(degraded_confidence_multiplier)))
        self._degrade_on_unknown_council = bool(degrade_on_unknown_council)
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
        # Articles II / IV / IX / X — the Brain's NATIVE reasoner. It reads the
        # evidence into its own structured analysis, forms its own hypotheses
        # (with counter-hypotheses), and self-criticises them WITHOUT any LLM
        # call. Advisors INFORM this reasoning; they are not the reasoning. The
        # Brain can even originate an opportunity from this layer alone when no
        # advisor is available (Article X — discovery without explicit
        # programming). ``independent_sizing_multiplier`` sizes such advisor-less
        # originations conservatively.
        self._native = BrainReasoner(
            agreement_boost=agreement_boost,
            disagreement_penalty=disagreement_penalty,
            independent_min_confidence=independent_min_confidence,
            independent_max_uncertainty=independent_max_uncertainty,
        )
        self.independent_sizing_multiplier = min(1.0, max(0.0, float(independent_sizing_multiplier)))
        self._independent_originations = 0
        # Article X — pattern EVOLUTION. An optional
        # :class:`~cognition.pattern_evolution.PatternOutcomeTracker` learns which
        # detected structural patterns preceded winning vs losing campaigns and
        # returns a bounded per-pattern confidence modifier. When wired, the Brain
        # multiplies its effective conviction by the average modifier of the
        # leading hypothesis's supporting patterns (Decision → Outcome →
        # Attribution → future reasoning). None ⇒ patterns are treated as neutral
        # (modifier 1.0), preserving prior behaviour.
        self._pattern_tracker = pattern_tracker
        # Articles XXVII / XXVIII — portfolio-level reasoning. The Brain reasons
        # over the WHOLE book simultaneously (not only per-symbol): it caps how
        # many correlated same-direction positions it will hold in one asset
        # class and bounds total open exposure, demoting the lower-EV
        # opportunities when either limit is breached.
        self.max_correlated_positions = max(1, int(max_correlated_positions))
        self.max_total_exposure = max(0.0, float(max_total_exposure))
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
        # Q40 — the MANAGEMENT reasoning entrypoint. A reasoner may expose a
        # dedicated ``reason_management`` (Part XXV Art 11 — a thesis-evaluation
        # prompt for an OPEN campaign, distinct from the origination prompt). When
        # present it is preferred on the management path; a legacy reasoner
        # without it falls back to plain ``reason`` (direction-based) and the
        # Brain then uses its backward-compatible management tree.
        self._manage_supports_management = callable(
            getattr(reasoner, "reason_management", None)
        )
        self._manage_supports_override = self._fn_supports_override(
            getattr(reasoner, "reason_management", None)
        )
        # Part VIII / Art XXXI — the Brain's own confidence is corrected by its
        # demonstrated calibration (Decision → Outcome → Attribution →
        # Calibration → Future reasoning). Observational until wired; a None
        # tracker (or one below its sample floor) leaves confidence untouched.
        self._calibration = calibration
        self._decisions = 0
        self._campaigns_opened = 0
        self._observed = 0
        self._faults = 0
        self._managed = 0
        self._last: dict[str, BrainOutput] = {}
        self._last_management: dict[str, BrainOutput] = {}
        # Part XXV / item 8 — the latest opportunity SET the Brain saw per symbol
        # at origination, so MANAGEMENT can compare a live campaign against the
        # currently visible opportunities (has a superior competing opportunity
        # emerged since entry?). Keyed by symbol → list[Opportunity]. Best-effort
        # (only present after the symbol has been reasoned in harvesting format).
        self._last_opportunities: dict[str, list[Opportunity]] = {}
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
        return CognitiveBrain._fn_supports_override(getattr(reasoner, "reason", None))

    @staticmethod
    def _fn_supports_override(fn: Optional[Any]) -> bool:
        """True when ``fn`` accepts the ``min_interval`` / ``throttle_key``
        overrides (or **kwargs). Fail-safe: a missing/duck-typed callable without
        them is treated as unsupported."""
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
        raising the global (origination + council) rate.

        Prefers a dedicated ``reason_management`` entrypoint (Part XXV Art 11 —
        the thesis-evaluation prompt for an open campaign) when the reasoner
        exposes one; otherwise falls back to plain ``reason`` (the legacy
        direction-based path). Both honour the management throttle override when
        supported, and degrade safely for a duck-typed stub without it."""
        throttle = f"{symbol}\x00manage"
        if self._manage_supports_management:
            fn = self._reasoner.reason_management
            if self._manage_supports_override and self.manage_min_interval_seconds is not None:
                return fn(symbol, payload, now=now,
                          min_interval=self.manage_min_interval_seconds,
                          throttle_key=throttle)
            return fn(symbol, payload, now=now)
        if self._reason_supports_override and self.manage_min_interval_seconds is not None:
            return self._reasoner.reason(
                symbol, payload, now=now,
                min_interval=self.manage_min_interval_seconds,
                throttle_key=throttle,
            )
        return self._reasoner.reason(symbol, payload, now=now)

    # ── Reasoning ─────────────────────────────────────────────────────────

    def reason(self, market_state: MarketState, *, now: Optional[float] = None) -> BrainOutput:
        """Reason over consolidated evidence into a decision. Never raises."""
        symbol = getattr(market_state, "symbol", "") or ""
        try:
            consolidation = market_state.consolidation(now)
            # Articles II / IV / IX — the Brain ALWAYS reads the evidence into its
            # own structured analysis and forms its own (self-criticised)
            # hypotheses BEFORE any advisor is consulted. This is the Brain's
            # native view; the advisor opinion later INFORMS it, never replaces it.
            analysis, hypotheses = self._native_reasoning(market_state, consolidation, symbol)
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
                # Article X — the Brain can still DISCOVER an opportunity from its
                # native analysis when no advisor is available. Only when the
                # native view is not strong enough does it stand down.
                independent = self._originate_independent(
                    symbol, consolidation, analysis, hypotheses,
                    reason_txt="reasoner unavailable — provider down (not a market view)",
                )
                if independent is not None:
                    return independent
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
                    independent = self._originate_independent(
                        symbol, consolidation, analysis, hypotheses,
                        reason_txt=("reasoner returned no usable opinion — provider "
                                    "failure / unparsable output (not a market view)"),
                    )
                    if independent is not None:
                        return independent
                    return self._reasoner_unavailable(
                        symbol,
                        "reasoner returned no usable opinion — provider failure / "
                        "unparsable output (not a market view)",
                        consolidation,
                    )
                return self._observe(
                    symbol, "no reasoner opinion this cycle — do nothing", consolidation,
                )
            return self._from_opinion(
                symbol, market_state, consolidation, opinion, now,
                analysis=analysis, brain_hypotheses=hypotheses,
            )
        except Exception as exc:  # noqa: BLE001 — reasoning must never break a cycle
            logger.debug("[brain] reason(%s) ignored a fault: %s", symbol, exc)
            with self._lock:
                self._faults += 1
            return self._observe(symbol, f"reasoning fault: {exc}", {})

    def _from_opinion(
        self, symbol: str, market_state: MarketState, consolidation: dict,
        opinion: Any, now: Optional[float],
        *, analysis: Optional[EvidenceAnalysis] = None,
        brain_hypotheses: Optional["list[BrainHypothesis]"] = None,
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

        # Article XIX — the Council is graded on reasoning QUALITY, not counted.
        # A shallow advisor reply (bare direction + confidence) is trusted far
        # less than a deep one that states its hypothesis, invalidation, expected
        # excursions and alternatives. The quality score (0.1–1.0) pulls a shallow
        # opinion's conviction toward the act threshold for SIZING, so a thin read
        # cannot claim full size even if its stated confidence is high; a fully
        # reasoned opinion keeps its stated conviction.
        quality_score, quality_note = self._evaluate_reasoning_quality(opinion)

        # Art XXXI — apply the Brain's demonstrated calibration correction to the
        # effective confidence. If the Brain has been over-predicting, this
        # attenuates conviction (factor < 1.0); if under-predicting, it amplifies
        # (factor > 1.0). Neutral (1.0) until the tracker has enough samples.
        calib_factor = 1.0
        calib_note = "not active (insufficient samples or not wired)"
        if self._calibration is not None:
            try:
                calib_factor = float(self._calibration.calibration_adjustment())
            except Exception as exc:  # noqa: BLE001 — calibration must never break reasoning
                logger.debug("[brain] calibration fault (%s): %s", symbol, exc)
                calib_factor = 1.0
            if calib_factor != 1.0:
                eff_conf = _clamp01(eff_conf * calib_factor)
                try:
                    _m = self._calibration.metrics()
                    calib_note = (
                        f"applied: factor {calib_factor:.3f} "
                        f"(reliability gap {_m.get('reliability_gap', 0.0):.3f}, "
                        f"Brier {_m.get('brier', 0.0):.3f}, "
                        f"{int(_m.get('samples', 0))} samples)"
                    )
                except Exception:  # noqa: BLE001
                    calib_note = f"applied: factor {calib_factor:.3f}"
        # Article XX / XXI / V-013 — advisory-council coverage. Read how much of
        # the council actually contributed. This is a first-class health signal:
        #  * council_unknown — NO council metadata at all (single reasoner, or a
        #    Brain wired without an orchestrator). Historically this SILENTLY
        #    passed the quorum gate; it must not. Unknown coverage is itself a
        #    degraded state — the Brain cannot know whether one advisor or several
        #    backed this read.
        #  * council_partial — metadata present but most AVAILABLE advisors did
        #    not respond (a wired council genuinely under-delivering).
        advisors_responded, advisors_available, advisors_total = (
            self._extract_advisor_counts(market_state)
        )
        council_unknown = advisors_responded is None
        council_partial = (
            advisors_responded is not None
            and advisors_available is not None
            and advisors_available > 0
            and advisors_responded < advisors_available * 0.5
        )
        # Enforcement (V-013) — degraded cognition ATTENUATES the effective
        # conviction below (it no longer merely "observes"). A genuine council
        # under-response always penalises; an UNKNOWN council penalises only when
        # the operator opted in (``degrade_on_unknown_council``) — but is ALWAYS
        # logged and recorded so the pass is never silent.
        penalise_degraded = council_partial or (
            council_unknown and self._degrade_on_unknown_council
        )
        if council_partial:
            logger.warning(
                "[brain] DEGRADED COGNITION: only %s/%s advisors responded — "
                "attenuating conviction x%.2f",
                advisors_responded, advisors_available,
                self.degraded_confidence_multiplier,
            )
        elif council_unknown:
            logger.warning(
                "[brain] DEGRADED COGNITION: council coverage UNKNOWN (no council "
                "metadata) — cannot confirm advisor count; %s",
                ("attenuating conviction x%.2f" % self.degraded_confidence_multiplier)
                if self._degrade_on_unknown_council else "recorded, not penalised",
            )
        # Violation V1 — the Brain is not a pure relay: cross-check the advisor's
        # stated confidence against the evidence picture it synthesised, and
        # attenuate the effective conviction when the advisor is overconfident
        # relative to the evidence (or claims conviction under high uncertainty).
        eff_conf, brain_synthesis = self._synthesize_from_evidence(
            consolidation, confidence, eff_conf)

        # Articles II / IV / IX — INTEGRATE the Brain's own hypotheses with the
        # advisor opinion (not a relay). Agreement with the Brain's leading
        # hypothesis boosts conviction; disagreement attenuates it. This runs
        # BEFORE EV/sizing so the whole decision reflects the synthesis, and is a
        # no-op when the Brain formed no independent structural view (so a
        # measurement-thin evidence picture behaves exactly as before).
        brain_lead = self._native.leading(brain_hypotheses or [])
        eff_conf, brain_adv_synthesis = self._integrate_brain(
            direction, eff_conf, brain_lead, analysis)

        # Article X — pattern EVOLUTION. When a pattern tracker is wired and the
        # leading hypothesis rests on detected structural patterns, weight the
        # effective conviction by those patterns' DEMONSTRATED edge: a pattern
        # that keeps losing attenuates conviction, one that keeps winning
        # amplifies it (bounded, and neutral until statistically meaningful).
        eff_conf, pattern_mod_note, lead_pattern_names = self._apply_pattern_modifier(
            eff_conf, brain_lead)

        # V-013 — degraded-cognition attenuation is the FINAL conviction modifier
        # (after synthesis / brain-integration / patterns) so the recorded
        # confidence, the EV, the sizing and the act gate all reflect the reduced
        # council coverage — degradation is enforced, not merely observed.
        degraded_note = ""
        if penalise_degraded:
            _before = eff_conf
            eff_conf = _clamp01(eff_conf * self.degraded_confidence_multiplier)
            degraded_note = (
                f"attenuated {_before:.2f} -> {eff_conf:.2f} "
                f"(x{self.degraded_confidence_multiplier:.2f})"
            )

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
        # Articles II / IV / IX — record the FULL native reasoning chain: the
        # Brain's own evidence analysis, the hypotheses it formed, its
        # self-criticism, and how it synthesised its view with the advisor's.
        if analysis is not None:
            questions["brain_evidence_analysis"] = analysis.summary()
        if brain_hypotheses:
            questions["brain_hypotheses"] = " | ".join(
                f"{(h.direction_implication or 'FLAT')}@{h.confidence:.2f}: {h.statement}"
                for h in brain_hypotheses
            )[:1000]
        if brain_lead is not None and getattr(brain_lead, "criticism", ""):
            questions["brain_self_criticism"] = brain_lead.criticism
        if brain_adv_synthesis:
            questions["brain_advisor_synthesis"] = brain_adv_synthesis
        # Part XXV — record the raw scalar and the confidence PROFILE so the honest
        # multidimensional read (and which dimension limited the trade) is auditable.
        questions["confidence_raw"] = round(confidence, 4)
        questions["confidence_profile"] = (
            f"thesis {conf_dims['thesis']:.2f} / opportunity {conf_dims['opportunity']:.2f} / "
            f"timing {conf_dims['timing']:.2f} / execution {conf_dims['execution']:.2f} "
            f"→ effective {eff_conf:.2f} (weakest: {limiting_dim})"
        )
        questions["calibration_correction"] = calib_note
        # Article XIX — record how the advisor's reasoning quality weighted its
        # confidence contribution (observability + audit).
        questions["advisor_reasoning_quality"] = quality_note
        # Article X — record the pattern-evolution confidence modification and the
        # supporting pattern names (the loop uses the latter to record the
        # origination against each pattern for outcome learning).
        if pattern_mod_note:
            questions["pattern_confidence_modifier"] = pattern_mod_note
        if lead_pattern_names:
            questions["supporting_patterns"] = list(lead_pattern_names)
        # Surface the remaining Part XXV context on the decision record for the
        # dashboard/governance (extra keys are harmless to consumers).
        for _k, _v in (("regime", regime), ("opportunity_horizon", opp_horizon),
                       ("expected_favorable_excursion", efe),
                       ("execution_quality", exec_q), ("risk", risk_txt)):
            if _v:
                questions[_k] = _v
        # Opportunity-harvesting — when the reasoner returned a SET of ranked
        # opportunities (multi-opportunity format), the preferred one already
        # drives this decision (it was collapsed to direction/confidence/EV
        # upstream). Record the FULL set for auditability, name the preferred
        # opportunity, and log the non-preferred ones as CONDITIONAL ALTERNATIVES
        # (the still-live ideas that lost the ranking but would activate on their
        # own conditions). Purely additive: absent on a legacy single-direction
        # reply, and harmless to consumers that ignore the extra keys.
        self._record_opportunity_audit(questions, opinion)
        # Article XXI / V-013 — record cognition-degradation state on the decision
        # so a partial OR unknown council is auditable and NEVER a silent pass.
        if council_partial:
            questions["cognitive_degradation"] = (
                f"OBSERVED: {advisors_responded}/{advisors_available} advisors — "
                "partial council coverage"
                + (f"; {degraded_note}" if degraded_note else "")
            )
        elif council_unknown:
            questions["cognitive_degradation"] = (
                "OBSERVED: council coverage unknown (no council metadata) — "
                + (degraded_note if degraded_note
                   else "recorded as degraded; conviction not penalised (opt-in)")
            )
        elif advisors_available is not None:
            questions["cognitive_degradation"] = (
                f"NONE: {advisors_responded}/{advisors_available} advisors — "
                "full cognitive coverage"
            )
        # Article XX / V-013 — advisor quorum: at least ``min_advisors_for_action``
        # of the council must have contributed. When NO council metadata is present
        # the quorum cannot be verified — this no longer passes SILENTLY: it is
        # recorded as UNCONFIRMED (and logged/attenuated as degraded above).
        quorum_ok = (
            advisors_responded is None
            or advisors_responded >= self.min_advisors_for_action
        )
        if advisors_responded is not None:
            if quorum_ok:
                questions["advisor_quorum"] = (
                    f"MET: {advisors_responded} advisors responded "
                    f"(available: {advisors_available}, total: {advisors_total})"
                )
            else:
                questions["advisor_quorum"] = (
                    f"NOT MET: {advisors_responded} of {self.min_advisors_for_action} "
                    f"required advisors responded (available: {advisors_available}, "
                    f"total: {advisors_total})"
                )
        else:
            questions["advisor_quorum"] = (
                "UNCONFIRMED: no council metadata — quorum cannot be verified "
                f"(required: {self.min_advisors_for_action}); treated as degraded cognition"
            )
        # Article XXXIV — minimum evidence-domain coverage. Unenforced when the
        # consolidation omits the count (a hand-built consolidation dict).
        domain_count = consolidation.get("domain_count")
        domain_ok = (domain_count is None) or (int(domain_count) >= self.min_evidence_domains)
        if domain_count is not None:
            if domain_ok:
                questions["evidence_coverage"] = (
                    f"ADEQUATE: {int(domain_count)} domains present "
                    f"(minimum: {self.min_evidence_domains})"
                )
            else:
                questions["evidence_coverage"] = (
                    f"INSUFFICIENT: {int(domain_count)} domains present, "
                    f"minimum {self.min_evidence_domains} required"
                )
        # Prefer the Brain's explicit invalidation / change-my-mind for the
        # campaign's invalidation conditions; fall back to competing hypotheses.
        # Article IX — the Brain's OWN leading-hypothesis invalidation leads the
        # list (when it formed a directional view), so the campaign is invalidated
        # on the Brain's terms, not only the advisor's.
        brain_invalidation = (
            [brain_lead.invalidation]
            if (brain_lead is not None and brain_lead.is_directional and brain_lead.invalidation)
            else []
        )
        invalidation_conditions = (
            brain_invalidation
            + ([invalidation_txt] if invalidation_txt else []) + wcm + competing
        )

        # Part XXV — lift the reasoner's ranked opportunity SET into first-class
        # Opportunity objects and reason over them directly (not only over the
        # collapsed direction). ``driving_opp`` is the opportunity whose
        # activation this campaign depends on; ``is_harvesting`` marks a multi-
        # opportunity reply (a legacy single-direction reply leaves both empty and
        # every gate below behaves exactly as before).
        opportunity_set, preferred_opp_id, is_harvesting = self._build_opportunity_set(opinion)
        driving_opp = self._select_driving_opportunity(
            opportunity_set, preferred_opp_id, direction)
        # Item 8 — remember the set per symbol so MANAGEMENT can later compare a
        # live campaign against the currently visible opportunities.
        if is_harvesting:
            with self._lock:
                self._last_opportunities[str(symbol or "")] = opportunity_set
        # ACTIVATION GATE (Part XXV / item 7): a harvesting-format reply may only
        # OPEN a campaign when the driving opportunity has ACTIVATED. A FORMING
        # (identified-but-not-yet-triggered) opportunity must WAIT — even at high
        # thesis confidence — because execution requires activation, not mere
        # identification. UNKNOWN state (legacy) counts as actionable, so nothing
        # changes for pre-harvesting replies.
        activation_ok = (
            (not is_harvesting)
            or (driving_opp is None)
            or driving_opp.is_actionable
        )

        # Constitutional gate: only OPEN a campaign when the Brain can answer
        # with sufficient confidence AND uncertainty is acceptable AND there is a
        # direction AND the expected value clears the threshold AND the advisory
        # quorum + evidence-domain coverage are adequate AND (harvesting format)
        # the driving opportunity has actually activated. Otherwise do nothing —
        # there is no obligation to trade (Part IV Art 7 / Part IX Q35: a good
        # thesis at negative EV, thin coverage, a lone advisor, or an unactivated
        # opportunity is not an executable trade).
        ev_ok = (self.min_expected_value is None) or (expected_value >= self.min_expected_value)
        directional = direction in (LONG, SHORT)
        # V-01 — actionability criterion. Legacy (default): a confidence floor +
        # uncertainty ceiling. EV-primary (opt-in): a positive EXPECTED VALUE net
        # of cost, so a genuine positive-expectancy opportunity is taken (and
        # sized by confidence downstream) instead of being vetoed because mixed
        # evidence pulled confidence below a threshold. Confidence still shapes EV
        # and sizing in both modes.
        if self.ev_primary_gate:
            ev_ok = expected_value > self.ev_action_threshold_r
            directional_and_qualified = directional and ev_ok
        else:
            directional_and_qualified = (
                directional
                and eff_conf >= self.min_confidence_to_act
                and uncertainty <= self.max_uncertainty_to_act
            )
        act = (
            directional_and_qualified
            and ev_ok
            and quorum_ok
            and domain_ok
            and activation_ok
        )
        if not act:
            if (is_harvesting and driving_opp is not None
                    and driving_opp.is_directional and not driving_opp.is_actionable):
                # ACTIVATION GATE — a real, directional opportunity exists but has
                # NOT activated. This is fundamentally different from "no
                # opportunity" (Part XXV / item 4): the Brain is armed and
                # tracking it, waiting for its conditions rather than seeing
                # nothing. A FORMING idea waits (OPPORTUNITY_FORMING); a fading
                # one (WEAKENING/EXHAUSTED) offers no fresh edge to originate into
                # (NO_OPPORTUNITY).
                _state = driving_opp.state.value
                _entry = "; ".join(driving_opp.entry_conditions[:3])
                if driving_opp.is_forming:
                    dtype = DecisionType.OPPORTUNITY_FORMING
                    reason_txt = (
                        f"{direction} opportunity identified but NOT yet activated "
                        f"(state {_state})"
                        + (f" — awaiting: {_entry}" if _entry else
                           " — awaiting entry/confirmation conditions")
                    )
                    do_nothing_txt = (
                        "evaluated: doing nothing is correct — opportunity present "
                        f"but not activated (state {_state}); tracking, not executing"
                    )
                else:
                    dtype = DecisionType.NO_OPPORTUNITY
                    reason_txt = (
                        f"{direction} opportunity is {_state} — no fresh edge to "
                        "originate a new campaign into a decaying opportunity"
                    )
                    do_nothing_txt = (
                        "evaluated: doing nothing is correct — the only opportunity "
                        f"is {_state} (fading), not an executable entry"
                    )
            elif directional_and_qualified and ev_ok and not quorum_ok:
                # Article XX — a qualified opportunity backed by too few advisors:
                # cognitive coverage is insufficient to originate a campaign.
                reason_txt = (
                    f"advisor quorum not met: {advisors_responded}/"
                    f"{self.min_advisors_for_action} advisors responded — "
                    "cognitive coverage insufficient for action"
                )
                dtype = DecisionType.CONTINUE_OBSERVING
                do_nothing_txt = (
                    "evaluated: doing nothing is correct — advisor quorum not met "
                    f"({advisors_responded}/{self.min_advisors_for_action})"
                )
            elif directional_and_qualified and ev_ok and not domain_ok:
                # Article XXXIV — a qualified opportunity on too few evidence
                # domains: cannot determine whether an opportunity exists.
                reason_txt = (
                    f"insufficient evidence coverage: {int(domain_count)}/"
                    f"{self.min_evidence_domains} domains — cannot determine "
                    "whether opportunity exists"
                )
                dtype = DecisionType.CONTINUE_OBSERVING
                do_nothing_txt = (
                    "evaluated: doing nothing is correct — insufficient evidence "
                    f"coverage ({int(domain_count)}/{self.min_evidence_domains} domains)"
                )
            elif (directional_and_qualified and not ev_ok) or (
                self.ev_primary_gate and direction in (LONG, SHORT) and not ev_ok
            ):
                # Saw a directional opportunity but its expected value does not
                # clear the threshold — decline it (Part IX Q35: cannot execute
                # this opportunity at the current expected value). Under the
                # EV-primary gate this is the ONLY directional FLAT — a genuine
                # EV/opportunity verdict, never "confidence below a floor".
                reason_txt = "expected value below threshold — opportunity declined"
                dtype = DecisionType.REJECT_OPPORTUNITY
                do_nothing_txt = (
                    f"evaluated: doing nothing is correct — EV {expected_value:.4f}R "
                    "below threshold"
                )
            elif direction in (LONG, SHORT):
                if self.ev_primary_gate:
                    # EV-primary: EV cleared the bar (else the branch above fired)
                    # but the opportunity is not yet actionable — a genuine WAIT,
                    # not a confidence-floor rejection.
                    reason_txt = (
                        "positive-EV opportunity not yet actionable — awaiting conditions"
                    )
                    do_nothing_txt = (
                        "evaluated: doing nothing is correct — EV positive but the "
                        "opportunity is not yet actionable"
                    )
                # Part XXV — when the thesis itself is strong (raw confidence
                # clears the bar) but a WEAK actionability dimension (timing or
                # execution) pulled the effective conviction below it, this is a
                # deliberate WAIT for better conditions, not a rejected thesis.
                elif (confidence >= self.min_confidence_to_act
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
            elif is_harvesting:
                # Harvesting format, no directional edge and no forming
                # opportunity: the market was understood and offers nothing to
                # exploit right now (untradeable / all non-directional). This is a
                # genuine NO_OPPORTUNITY conclusion — distinct from the "waiting
                # for activation" state above (Part XXV / item 4).
                reason_txt = (
                    "no exploitable opportunity — the market offers nothing to "
                    "harvest right now"
                )
                dtype = DecisionType.NO_OPPORTUNITY
                do_nothing_txt = (
                    "evaluated: doing nothing is correct — no opportunity exists "
                    "(nothing to harvest, not merely 'timeframes disagree')"
                )
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
                opportunities=opportunity_set, preferred_opportunity_id=preferred_opp_id,
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
            opportunities=opportunity_set, preferred_opportunity_id=preferred_opp_id,
            questions_answered=questions, do_nothing_considered=True,
            reasoner=self.reasoner_name,
        )
        # Part XXVIII — capital is competitive: sizing reflects EXPECTED VALUE,
        # not confidence alone. Normalise EV against the reward multiple and take
        # the LESSER of confidence and EV-proportional sizing — a high-confidence
        # trade with poor payoff geometry is sized down; an excellent-payoff
        # trade is sized to (never above) its confidence, for risk control.
        ev_normalized = min(1.0, max(0.0, expected_value / max(0.01, self.reward_r_default)))
        # Article XIX — a shallow advisor reply is sized down: the effective
        # conviction is pulled toward the act threshold in proportion to how thin
        # its reasoning was. A fully reasoned opinion (quality 1.0) is unaffected.
        weighted_confidence = _clamp01(
            eff_conf * quality_score + (1.0 - quality_score) * self.min_confidence_to_act)
        desired_exposure = round(min(weighted_confidence, ev_normalized), 4)
        questions["sizing_rationale"] = (
            f"exposure {desired_exposure:.4f} = min(quality-weighted confidence "
            f"{weighted_confidence:.2f}, ev_normalized {ev_normalized:.2f}) — "
            f"effective confidence {eff_conf:.2f} x reasoning quality {quality_score:.2f}, "
            f"EV {expected_value:.4f}R on {reward_r:.1f}R reward"
        )
        campaign = CampaignSpecification(
            symbol=symbol, thesis=decision.thesis, direction=direction,
            desired_exposure=desired_exposure,
            expected_value=expected_value,
            initial_execution_intent={"kind": "market", "confidence": round(eff_conf, 4)},
            supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
            confidence=eff_conf, invalidation_conditions=decision.invalidation_conditions,
            objectives=[f"harvest {direction} opportunity while EV positive"],
            opportunity_id=(driving_opp.opportunity_id if driving_opp is not None else ""),
            opportunity_state=(driving_opp.state.value if driving_opp is not None else ""),
            decision_id=decision.decision_id,
        )
        return self._record(BrainOutput(decision=decision, campaign=campaign, direction=direction))

    # ── Portfolio-level reasoning (Articles XXVII / XXVIII) ───────────────

    def reason_portfolio(
        self, all_outputs: "list[BrainOutput]", existing_positions: "list",
    ) -> "list[BrainOutput]":
        """Reason across the WHOLE book at once, after per-symbol reasoning.

        Receives this cycle's OPEN_CAMPAIGN outputs and the current open
        positions, then ranks by EV, enforces correlated-concentration and total-
        exposure limits (demoting the lower-EV opportunities to CONTINUE_OBSERVING
        when a limit is breached), and annotates opportunities that dominate a
        weaker existing position in the same asset class. Returns the (possibly
        modified) outputs ranked by EV. Never raises."""
        try:
            outputs = [o for o in (all_outputs or []) if o is not None]
            positions = [p for p in (existing_positions or []) if p is not None]
            opens = [o for o in outputs if self._is_open_output(o)]
            # 1) Rank the opportunities by expected value (highest first).
            opens.sort(key=self._output_ev, reverse=True)

            # 2) Correlated concentration: at most ``max_correlated_positions``
            # same-direction opportunities per asset class this cycle. Keep the
            # highest-EV ones (opens are already EV-sorted), demote the rest.
            class_dir_counts: dict = {}
            for o in opens:
                if not self._is_open_output(o):
                    continue
                ac = self._asset_class(o.decision.symbol)
                d = str(getattr(o, "direction", FLAT) or FLAT).upper()
                if not ac or d not in (LONG, SHORT):
                    continue
                key = (ac, d)
                count = class_dir_counts.get(key, 0)
                if count >= self.max_correlated_positions:
                    self._demote_output(o, (
                        f"portfolio concentration: {count + 1} correlated {ac} "
                        "positions — deferring lower-EV opportunity"))
                    continue
                class_dir_counts[key] = count + 1

            # 3) Total open exposure cap. Demote the LOWEST-EV survivors until the
            # book-wide exposure (this cycle's opportunities + existing positions)
            # is within budget.
            existing_exposure = sum(self._position_exposure(p) for p in positions)
            survivors = [o for o in opens if self._is_open_output(o)]
            total = existing_exposure + sum(self._output_exposure(o) for o in survivors)
            for o in reversed(survivors):  # lowest EV first
                if total <= self.max_total_exposure:
                    break
                exp = self._output_exposure(o)
                self._demote_output(o, (
                    f"portfolio exposure cap: total {total:.2f} exceeds "
                    f"{self.max_total_exposure:.2f} — deferring lowest-EV opportunity"))
                total -= exp

            # 4) Rebalance hint: an opportunity that dominates a weaker existing
            # position in the same asset class suggests trimming that position.
            for o in opens:
                if not self._is_open_output(o):
                    continue
                ac = self._asset_class(o.decision.symbol)
                if not ac:
                    continue
                new_ev = self._output_ev(o)
                weak = [
                    self._position_symbol(p) for p in positions
                    if self._asset_class(self._position_symbol(p)) == ac
                    and self._position_ev(p) is not None
                    and self._position_ev(p) < new_ev
                ]
                if weak:
                    try:
                        o.decision.questions_answered["portfolio_rebalance"] = (
                            f"higher-EV {ac} opportunity ({new_ev:.4f}R) dominates weaker "
                            f"existing position(s) {', '.join(sorted(set(weak)))} — "
                            "consider trimming to free capital")
                    except Exception:  # noqa: BLE001
                        pass

            # Return every output ranked by EV (demoted ones retain their EV).
            return sorted(outputs, key=self._output_ev, reverse=True)
        except Exception as exc:  # noqa: BLE001 — portfolio reasoning must never break a cycle
            logger.debug("[brain] reason_portfolio ignored a fault: %s", exc)
            return list(all_outputs or [])

    @staticmethod
    def _is_open_output(output: Any) -> bool:
        try:
            return (getattr(output, "campaign", None) is not None
                    and output.decision.decision_type == DecisionType.OPEN_CAMPAIGN)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _output_ev(output: Any) -> float:
        try:
            return float(getattr(output.decision, "expected_value", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _output_exposure(output: Any) -> float:
        try:
            camp = getattr(output, "campaign", None)
            if camp is None:
                return 0.0
            return max(0.0, float(getattr(camp, "desired_exposure", 0.0) or 0.0))
        except Exception:  # noqa: BLE001
            return 0.0

    def _demote_output(self, output: Any, rationale: str) -> None:
        """Turn an OPEN_CAMPAIGN output into CONTINUE_OBSERVING (Art XXVII/XXVIII)."""
        try:
            output.decision.decision_type = DecisionType.CONTINUE_OBSERVING
            output.decision.campaign_recommendation = "observe"
            output.decision.risk_rationale = rationale
            try:
                output.decision.questions_answered["portfolio_decision"] = rationale
            except Exception:  # noqa: BLE001
                pass
            output.campaign = None
            output.direction = FLAT
        except Exception as exc:  # noqa: BLE001
            logger.debug("[brain] demote_output fault: %s", exc)

    @staticmethod
    def _asset_class(symbol: str) -> str:
        """Coarse asset-class grouping for correlation (Art XXVII). '' if unknown."""
        s = str(symbol or "").upper()
        if any(x in s for x in ("BTC", "ETH", "SOL", "XRP")):
            return "crypto"
        if any(x in s for x in ("XAU", "XAG")):
            return "metals"
        if any(x in s for x in ("SPX", "NDX", "DJI", "RUT")):
            return "equity_index"
        if any(x in s for x in ("EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF")):
            return "forex"
        if any(x in s for x in ("CL", "NG")):
            return "energy"
        return ""

    @staticmethod
    def _position_symbol(pos: Any) -> str:
        try:
            v = getattr(pos, "symbol", None)
            if v is None and isinstance(pos, dict):
                v = pos.get("symbol")
            return str(v or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _position_exposure(pos: Any) -> float:
        try:
            for attr in ("desired_exposure", "size", "exposure"):
                v = getattr(pos, attr, None)
                if v is None and isinstance(pos, dict):
                    v = pos.get(attr)
                if v is not None:
                    return max(0.0, float(v))
        except Exception:  # noqa: BLE001
            pass
        return 0.0

    @staticmethod
    def _position_ev(pos: Any) -> Optional[float]:
        try:
            for attr in ("expected_value", "ev"):
                v = getattr(pos, attr, None)
                if v is None and isinstance(pos, dict):
                    v = pos.get(attr)
                if v is not None:
                    return float(v)
        except Exception:  # noqa: BLE001
            pass
        return None

    # ── Management (Phase F — Brain drives the open campaign) ─────────────

    def manage(self, position: "PositionView", market_state: MarketState, *,
               now: Optional[float] = None) -> BrainOutput:
        """Decide the management action for an OPEN position/campaign. Never raises.

        Part XXV Art 11 — management asks a DIFFERENT question from origination:
        "given everything that has happened since this campaign was opened, what
        should Apex do with THIS campaign now?" — never "does the council still
        say LONG?". The decision is THESIS-BASED, not a direction comparison:
        temporary uncertainty, a FLAT read, low confidence or conflicting
        evidence NEVER liquidate a live campaign. An EXIT requires a reasoned
        determination that the thesis is invalidated, has materially deteriorated
        beyond its risk/EV boundary, has been superseded, or must be terminated
        for an independent risk constraint. LONG and SHORT are treated with
        perfect symmetry. Fail-safe: any fault ⇒ HOLD (never EXIT on error).
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
            # Article XX / Flaw 4 — read council coverage up front so a DEGRADED
            # council cannot authorise a state change (EXIT / REVERSE / SCALE_IN).
            # Cognitive uncertainty (too few advisors) is distinct from market
            # uncertainty (the thesis genuinely deteriorating): the former holds,
            # it never liquidates. None ⇒ no council metadata (gate unenforced).
            advisors_responded, advisors_available, advisors_total = (
                self._extract_advisor_counts(market_state)
            )
            # Change 4 — the management payload carries the CAMPAIGN CONTEXT (held
            # side, entry, P&L, duration, original thesis/invalidation) so the
            # reasoner evaluates the existing position, not the market in the
            # abstract.
            payload = self._management_evidence_payload(
                symbol, market_state, consolidation, position)
            opinion = self._manage_reason(symbol, payload, now)
            if opinion is None:
                return self._record_management(self._manage_pkg(
                    symbol, want, DecisionType.HOLD, 0.0, uncertainty, "no opinion — hold"))
            conf = _clamp01(getattr(opinion, "confidence", 0.0))
            # Part XXV — resolve the confidence PROFILE. Risk-ADDING actions
            # (SCALE_IN) require the effective conviction (the weakest dimension),
            # so the Brain never adds into weak execution/timing. Risk-REDUCING
            # actions keep using the raw scalar — a poor execution read must never
            # make it HARDER to cut/reduce a position.
            conf_dims, eff_conf, limiting_dim = self._confidence_profile(opinion, conf)
            rationale = str(getattr(opinion, "rationale", "") or "")
            # A management opinion (thesis_state / management_action present) is
            # decided by the thesis-based tree; a legacy direction-based opinion
            # falls back to the backward-compatible tree (with FLAT ⇒ HOLD, never
            # EXIT). Both are directionally symmetric.
            mgmt_action = str(getattr(opinion, "management_action", "") or "").strip()
            thesis_state = str(getattr(opinion, "thesis_state", "") or "").strip()
            if mgmt_action or thesis_state:
                action, why = self._decide_management_thesis(
                    position, opinion, conf, conf_dims, eff_conf, limiting_dim)
            else:
                action, why = self._decide_management_legacy(
                    position, opinion, want, uncertainty, conf,
                    conf_dims, eff_conf, limiting_dim)
            # Flaw 4 — council quorum guard: below quorum, state-changing actions
            # degrade to HOLD; risk-reducing actions (TIGHTEN_RISK / PROTECT_PROFIT
            # / SCALE_OUT) are always permitted.
            pre_quorum_action = action
            action, why = self._apply_management_quorum(
                symbol, action, why, advisors_responded, advisors_available)
            # When the guard OVERRODE the model's action, the record must explain
            # the override (why we held), not echo the model's original rationale.
            reason_txt = why if action != pre_quorum_action else (rationale or why)
            logger.info(
                "[brain] MANAGE %s held=%s → %s | thesis=%s conf=%.2f (council %s/%s) — %s",
                symbol, want or "?", action.value,
                (thesis_state.lower() or "legacy"), eff_conf,
                ("?" if advisors_responded is None else advisors_responded),
                ("?" if advisors_available is None else advisors_available),
                reason_txt[:120],
            )
            return self._record_management(self._manage_pkg(
                symbol, want, action, eff_conf, uncertainty, reason_txt, opinion=opinion))
        except Exception as exc:  # noqa: BLE001 — management reasoning must never break a cycle
            logger.debug("[brain] manage(%s) ignored a fault: %s", symbol, exc)
            with self._lock:
                self._faults += 1
            return self._record_management(self._manage_pkg(
                symbol, want, DecisionType.HOLD, 0.0, 1.0, f"manage fault: {exc}"))

    def _decide_management_legacy(
        self, position: "PositionView", opinion: Any, want: str, uncertainty: float,
        conf: float, conf_dims: dict, eff_conf: float, limiting_dim: str,
    ) -> "tuple[DecisionType, str]":
        """Backward-compatible management decision for a legacy DIRECTION-based
        opinion (one carrying no thesis_state / management_action).

        Identical to the historical tree EXCEPT the catch-all no longer exits:
        Flaw 1 — a FLAT / uncertain / ambiguous read is NOT an invalidation, so
        it HOLDS the campaign. An EXIT still fires on an EXPLICIT deterioration
        (opportunity gone / EV negative) or dominant high-confidence contrary
        evidence; REVERSE only on very-high-confidence contrary evidence. LONG
        and SHORT are handled symmetrically (``aligned`` / ``opposite`` are
        computed identically for both sides)."""
        odir = str(getattr(opinion, "direction", FLAT) or FLAT).upper()
        if odir not in (LONG, SHORT, FLAT):
            odir = FLAT
        aligned = odir == want and want in (LONG, SHORT)
        opposite = odir in (LONG, SHORT) and odir != want and want in (LONG, SHORT)
        # Empty fields (a minimal/legacy opinion) never force an exit — these
        # overrides fire only on an EXPLICIT signal, so prior behaviour is kept.
        opportunity = str(getattr(opinion, "opportunity", "") or "").strip().lower()
        ev_txt = str(getattr(opinion, "expected_value", "") or "").strip().lower()
        opportunity_gone = opportunity in ("none", "no", "n/a", "gone", "expired")
        ev_negative = ev_txt.startswith("negative")
        thesis_deteriorated = opportunity_gone or ev_negative
        in_profit = (getattr(position, "profit_r", None) or 0.0) > 0

        if aligned and conf >= self.min_confidence_to_act \
                and uncertainty <= self.max_uncertainty_to_act and not thesis_deteriorated:
            if self.allow_scale_in and in_profit and eff_conf >= self.reverse_confidence:
                return DecisionType.SCALE_IN, "thesis strengthening + in profit — add"
            if (self.allow_scale_in and in_profit
                    and conf >= self.reverse_confidence
                    and eff_conf < self.reverse_confidence):
                # Would add, but a weak actionability dimension (execution /
                # timing) says now is not the moment to increase risk — hold.
                return DecisionType.HOLD, (
                    f"thesis strong but {limiting_dim} weak "
                    f"({conf_dims[limiting_dim]:.2f}) — hold, not adding")
            return DecisionType.HOLD, "thesis intact — hold"
        if aligned and thesis_deteriorated:
            return DecisionType.EXIT, "opportunity gone / EV no longer positive — exit"
        if aligned and conf >= self.exit_floor:
            return DecisionType.TIGHTEN_RISK, "supporting thesis weakening — tighten risk"
        if opposite and conf >= self.reverse_confidence:
            return DecisionType.REVERSE, "strong contrary evidence — reverse"
        if opposite and conf >= self.min_confidence_to_act:
            return DecisionType.EXIT, "contrary evidence dominant — exit"
        # Flaw 1 — FLAT / low-confidence / ambiguous is temporary uncertainty, not
        # invalidation. Hold the campaign; an exit needs a reasoned justification.
        return DecisionType.HOLD, (
            "no directional edge / uncertain — thesis not invalidated, holding")

    def _decide_management_thesis(
        self, position: "PositionView", opinion: Any, conf: float,
        conf_dims: dict, eff_conf: float, limiting_dim: str,
    ) -> "tuple[DecisionType, str]":
        """THESIS-based management decision (Part XXV Art 11) for a management
        opinion carrying a thesis_state and/or a management_action.

        No direction comparison anywhere — LONG and SHORT are perfectly
        symmetric. Defaults to HOLD on uncertainty/ambiguity. EXIT only on an
        explicit invalidation, opportunity gone, negative EV, or a confident
        contrary EXIT recommendation; REVERSE only on very-high-confidence
        contrary evidence; TIGHTEN_RISK when the thesis is weakening; SCALE_IN
        only when the thesis is strengthening, in profit and every actionability
        dimension is strong."""
        action_hint = str(getattr(opinion, "management_action", "") or "").strip().upper()
        thesis = str(getattr(opinion, "thesis_state", "") or "").strip().lower()
        opp_status = str(getattr(opinion, "opportunity_status", "") or "").strip().lower()
        opportunity = str(getattr(opinion, "opportunity", "") or "").strip().lower()
        ev_txt = str(getattr(opinion, "expected_value", "") or "").strip().lower()
        ev_negative = ev_txt.startswith("negative")
        in_profit = (getattr(position, "profit_r", None) or 0.0) > 0
        # A "very high" bar for flipping the book — at least the reverse gate and
        # never below a hard 0.7 floor, symmetric for both sides.
        very_high = max(self.reverse_confidence, 0.7)

        thesis_invalidated = (thesis == "invalidated") or (opp_status in ("invalidated", "replaced"))
        opportunity_gone = (
            opp_status in ("invalidated", "replaced")
            or opportunity in ("none", "no", "n/a", "gone", "expired")
        )

        # 1) Explicit, reasoned invalidation → EXIT.
        if thesis_invalidated:
            return DecisionType.EXIT, "thesis invalidated — the opportunity no longer exists"

        # 2) Honour an explicit action, cross-checked so uncertainty never
        #    liquidates a live campaign.
        if action_hint == "REVERSE":
            if eff_conf >= very_high and conf >= self.reverse_confidence:
                return DecisionType.REVERSE, "very high-confidence contrary evidence — reverse"
            # Not conclusive enough to flip: de-risk, do not liquidate on doubt.
            return DecisionType.TIGHTEN_RISK, (
                "contrary evidence not conclusive enough to reverse — tighten risk")
        if action_hint == "EXIT":
            if ev_negative or opportunity_gone or conf >= self.min_confidence_to_act:
                return DecisionType.EXIT, "thesis no longer supports the position — exit"
            # Model wants out but has not justified full liquidation → reduce risk.
            return DecisionType.TIGHTEN_RISK, (
                "exit not sufficiently justified — tighten risk instead")
        if action_hint == "SCALE_IN":
            if (self.allow_scale_in and in_profit and eff_conf >= self.reverse_confidence
                    and thesis in ("intact", "strengthening", "")):
                return DecisionType.SCALE_IN, "thesis strengthening + in profit — add"
            if (self.allow_scale_in and in_profit and conf >= self.reverse_confidence
                    and eff_conf < self.reverse_confidence):
                return DecisionType.HOLD, (
                    f"thesis strong but {limiting_dim} weak "
                    f"({conf_dims[limiting_dim]:.2f}) — hold, not adding")
            return DecisionType.HOLD, "scale-in conditions not met — hold"
        if action_hint == "SCALE_OUT":
            return DecisionType.SCALE_OUT, "bank partial — reduce exposure while the thesis clarifies"
        if action_hint == "PROTECT_PROFIT":
            return DecisionType.PROTECT_PROFIT, "protect profit — move stop to secure gains"
        if action_hint == "TIGHTEN_RISK":
            return DecisionType.TIGHTEN_RISK, "reduce risk while the thesis is in question"
        if action_hint == "HOLD":
            return DecisionType.HOLD, "thesis holds — hold"

        # 3) No explicit action → derive from the thesis / opportunity state.
        if ev_negative or opportunity_gone:
            return DecisionType.EXIT, "expected value negative / opportunity gone — exit"
        if thesis == "weakening":
            return DecisionType.TIGHTEN_RISK, "thesis weakening but not invalidated — tighten risk"
        if thesis == "evolved":
            return DecisionType.TIGHTEN_RISK, (
                "opportunity evolving / superseded — tighten risk while reassessing")
        if thesis in ("intact", "strengthening"):
            if (self.allow_scale_in and in_profit and thesis == "strengthening"
                    and eff_conf >= self.reverse_confidence):
                return DecisionType.SCALE_IN, "thesis strengthening + in profit — add"
            return DecisionType.HOLD, "thesis intact — hold"

        # 4) Uncertain / temporarily obscured / ambiguous → HOLD (never auto-exit).
        return DecisionType.HOLD, (
            "uncertain / temporarily obscured — thesis not invalidated, holding")

    def _apply_management_quorum(
        self, symbol: str, action: "DecisionType", why: str,
        responded: Optional[int], available: Optional[int],
    ) -> "tuple[DecisionType, str]":
        """Flaw 4 — a DEGRADED council must not authorise a state change.

        Below ``min_advisors_for_action`` responding advisors, the state-changing
        actions (EXIT / REVERSE / SCALE_IN) degrade to HOLD: too little cognition
        to justify liquidating, flipping or adding to a live campaign. Risk-
        reducing actions (TIGHTEN_RISK / PROTECT_PROFIT / SCALE_OUT) are always
        permitted — de-risking must never be blocked. Unenforced when no council
        metadata is present (``responded is None`` ⇒ fail-safe, prior behaviour)."""
        if responded is None or responded >= self.min_advisors_for_action:
            return action, why
        state_changing = {DecisionType.EXIT, DecisionType.REVERSE, DecisionType.SCALE_IN}
        if action in state_changing:
            logger.info(
                "[brain] MANAGE %s degraded council (%s/%s advisors) — insufficient "
                "cognition for state change (%s blocked), holding",
                symbol, responded,
                ("?" if available is None else available), action.value,
            )
            return DecisionType.HOLD, (
                f"degraded council ({responded}/{self.min_advisors_for_action} "
                f"required advisors) — insufficient cognition for {action.value}, holding")
        return action, why

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
            # Part XXV Art 11 — record the management thesis state / recommended
            # action / opportunity status when present (a management opinion), so
            # the reasoning behind HOLD-vs-change is auditable on the record.
            for _k, _v in (
                ("thesis_state", str(getattr(opinion, "thesis_state", "") or "")),
                ("management_action", str(getattr(opinion, "management_action", "") or "")),
                ("opportunity_status", str(getattr(opinion, "opportunity_status", "") or "")),
            ):
                if _v:
                    questions[_k] = _v
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

    def _management_evidence_payload(
        self, symbol: str, market_state: MarketState, consolidation: dict,
        position: "PositionView",
    ) -> dict:
        """Change 4 — the management payload = the origination evidence PLUS the
        CAMPAIGN CONTEXT the reasoner needs to evaluate an EXISTING position:
        the held side, current P&L (R), how long it has been open, its size, and
        — when the Brain originated it — the ORIGINAL thesis and invalidation
        conditions recorded at entry. Without this the model would answer "what
        direction do I see?" instead of "should Apex keep THIS campaign?".
        Fail-safe: any fault falls back to the plain origination payload."""
        payload = self._evidence_payload(market_state, consolidation)
        try:
            campaign: dict[str, Any] = {
                "held_direction": str(getattr(position, "direction", "") or "").upper(),
                "profit_r": getattr(position, "profit_r", None),
                "hold_seconds": round(float(getattr(position, "hold_seconds", 0.0) or 0.0), 2),
                "size": getattr(position, "size", 0.0),
                "campaign_id": str(getattr(position, "campaign_id", "") or ""),
                "entry_confidence": getattr(position, "entry_confidence", None),
            }
            # Original thesis / invalidation from the Brain's origination record
            # for this symbol, when available (a position opened elsewhere simply
            # has no prior record — the context is still valid, just thinner).
            prior = self._last.get(str(symbol or ""))
            prior_decision = getattr(prior, "decision", None) if prior is not None else None
            if prior_decision is not None:
                thesis = str(getattr(prior_decision, "thesis", "") or "")
                inval = list(getattr(prior_decision, "invalidation_conditions", []) or [])
                if thesis:
                    campaign["original_thesis"] = thesis[:500]
                if inval:
                    campaign["invalidation_conditions"] = [str(i)[:200] for i in inval][:6]
                dtype = getattr(prior_decision, "decision_type", None)
                dval = getattr(dtype, "value", None)
                if dval:
                    campaign["entry_decision_type"] = dval
            # Item 8 — thread the CURRENTLY VISIBLE opportunity set for this
            # symbol into the management payload so the reasoner can compare the
            # live campaign against newly emerging opportunities (the management
            # prompt already asks whether a SUPERIOR OPPOSING opportunity has
            # appeared; previously no set was tracked to answer it). Highlight
            # any opposing opportunity as an explicit rotation candidate.
            held = campaign["held_direction"]
            with self._lock:
                tracked = list(self._last_opportunities.get(str(symbol or ""), []))
            if tracked:
                payload["current_opportunities"] = [o.to_dict() for o in tracked]
                opposing = [
                    o for o in tracked
                    if o.is_directional and held in (LONG, SHORT) and o.direction != held
                ]
                if opposing:
                    best = max(opposing, key=lambda o: o.rank_score)
                    campaign["competing_opposing_opportunity"] = {
                        "id": best.opportunity_id,
                        "direction": best.direction,
                        "state": best.state.value,
                        "quality": round(best.quality, 4),
                        "asymmetry": round(best.asymmetry, 4),
                        "rank_score": round(best.rank_score, 4),
                        "thesis": best.thesis[:240],
                    }
            payload["campaign"] = campaign
            payload["management"] = True
        except Exception as exc:  # noqa: BLE001 — payload enrichment must never break management
            logger.debug("[brain] management payload(%s) ignored a fault: %s", symbol, exc)
        return payload

    @staticmethod
    def _extract_advisor_counts(
        market_state: MarketState,
    ) -> "tuple[Optional[int], Optional[int], Optional[int]]":
        """Read the advisory-council coverage from the reasoning Evidence.

        Article XX — the ``ReasoningOrchestrator`` records how many advisors were
        asked and how many actually replied; ``evidence_from_reasoning`` surfaces
        that on each REASONING Evidence's measurements. Returns
        ``(responded, available, total)`` taken from the reasoning Evidence
        carrying the counts (the maximum ``responded`` seen), or ``(None, None,
        None)`` when no advisor metadata is present — in which case the quorum /
        degraded-cognition gates are left unenforced (fail-safe, preserves the
        behaviour of a Brain wired without a council). Never raises.
        """
        resp: Optional[int] = None
        avail: Optional[int] = None
        total: Optional[int] = None
        try:
            for e in getattr(market_state, "evidence", []) or []:
                if getattr(e, "domain", None) != EvidenceDomain.REASONING:
                    continue
                m = getattr(e, "measurements", None)
                if not isinstance(m, dict) or "advisors_responded" not in m:
                    continue
                r = int(m.get("advisors_responded", 0) or 0)
                if resp is None or r > resp:
                    resp = r
                    avail = int(m.get("advisors_available", 0) or 0)
                    total = int(m.get("advisors_total", 0) or 0)
        except Exception:  # noqa: BLE001 — a coverage probe must never break reasoning
            return None, None, None
        return resp, avail, total

    @staticmethod
    def _build_opportunity_set(
        opinion: Any,
    ) -> "tuple[list[Opportunity], str, bool]":
        """Build the first-class Opportunity SET from a reasoner opinion (Part XXV).

        The opportunity-harvesting reasoner returns a SET of ranked opportunities
        (plus a market-state read / untradeable flag) instead of a single
        collapsed direction. This lifts that raw set into first-class
        :class:`~cognition.contracts.Opportunity` objects the Brain reasons over
        directly — each with its own lifecycle state, activation / invalidation
        conditions and scores — rather than reading only the collapsed
        direction+confidence.

        Returns ``(opportunities, preferred_id, is_harvesting)``. ``is_harvesting``
        is True when the reply was in the multi-opportunity format (so the
        activation gate applies); False for a legacy single-direction reply, whose
        behaviour is then entirely unchanged. Never raises.
        """
        try:
            raw = list(getattr(opinion, "opportunities", []) or [])
            untradeable = bool(getattr(opinion, "market_is_untradeable", False))
            market_state = getattr(opinion, "market_state", {}) or {}
            is_harvesting = bool(raw) or untradeable or bool(market_state)
            if not is_harvesting:
                return [], "", False
            preferred_id = str(getattr(opinion, "preferred_opportunity_id", "") or "")
            opps = [
                Opportunity.from_reply(o, preferred_id=preferred_id)
                for o in raw if isinstance(o, dict)
            ]
            return opps, preferred_id, True
        except Exception:  # noqa: BLE001 — building the set must never break reasoning
            return [], "", False

    @staticmethod
    def _select_driving_opportunity(
        opps: "list[Opportunity]", preferred_id: str, direction: str,
    ) -> "Optional[Opportunity]":
        """The opportunity that produced the collapsed direction — the one whose
        activation the campaign depends on.

        Prefers the model's named preference when it is directional; otherwise the
        highest-ranked directional opportunity matching the collapsed ``direction``
        (falling back to the highest-ranked directional overall). Returns ``None``
        when no opportunity carries a LONG/SHORT direction. Never raises.
        """
        try:
            directional = [o for o in opps if o.is_directional]
            if not directional:
                return None
            pid = str(preferred_id or "")
            if pid:
                for o in directional:
                    if o.opportunity_id == pid:
                        return o
            d = str(direction or "").upper()
            matching = [o for o in directional if o.direction == d] if d in (LONG, SHORT) else []
            pool = matching or directional
            return max(pool, key=lambda o: o.rank_score)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _record_opportunity_audit(questions: dict, opinion: Any) -> None:
        """Record the reasoner's ranked opportunity SET on ``questions_answered``.

        Opportunity-harvesting (multi-opportunity) format only: when the opinion
        carries an ``opportunities`` list, store the full set, name the preferred
        opportunity that drove the decision, and log the non-preferred ones as
        CONDITIONAL ALTERNATIVES plus the structured market-state read. A no-op
        for a legacy single-direction opinion (no ``opportunities``). Never raises.
        """
        try:
            opps = list(getattr(opinion, "opportunities", []) or [])
            if not opps:
                return
            preferred_id = str(getattr(opinion, "preferred_opportunity_id", "") or "")

            def _summ(o: dict) -> dict:
                def _f(k: str) -> float:
                    return round(_clamp01(o.get(k)), 4)

                def _lst(k: str) -> "list[str]":
                    v = o.get(k)
                    return [str(x)[:160] for x in v][:5] if isinstance(v, list) else []

                return {
                    "id": str(o.get("id", "") or ""),
                    "horizon": str(o.get("horizon", "") or "")[:16],
                    "direction": str(o.get("direction", "") or "").upper()[:8],
                    "state": str(o.get("state", "") or "")[:24],
                    "thesis": str(o.get("thesis", "") or o.get("why_now", "") or "")[:240],
                    "why_now": str(o.get("why_now", "") or "")[:180],
                    "entry_conditions": _lst("entry_conditions"),
                    "confirmation_conditions": _lst("confirmation_conditions"),
                    "invalidation_conditions": _lst("invalidation_conditions"),
                    "target_logic": str(o.get("target_logic", "") or "")[:180],
                    "quality": _f("quality"),
                    "asymmetry": _f("asymmetry"),
                    "urgency": _f("urgency"),
                    "evidence_strength": _f("evidence_strength"),
                    "preferred": str(o.get("id", "") or "") == preferred_id,
                }

            summarised = [_summ(o) for o in opps if isinstance(o, dict)]
            questions["opportunity_set"] = summarised
            questions["preferred_opportunity"] = preferred_id or "(highest-ranked)"
            alternatives = [
                f"{s['direction'] or 'FLAT'}"
                f"{('/' + s['state']) if s['state'] else ''}"
                f"@q{s['quality']:.2f}/a{s['asymmetry']:.2f}: {s['thesis']}".strip()
                for s in summarised if not s["preferred"]
            ]
            questions["conditional_alternatives"] = (
                "; ".join(a for a in alternatives if a)
                if alternatives else "none (single opportunity)"
            )
            ms = getattr(opinion, "market_state", {}) or {}
            if isinstance(ms, dict) and ms:
                questions["market_state"] = {
                    str(k): str(v)[:200] for k, v in ms.items()
                }
            if bool(getattr(opinion, "market_is_untradeable", False)):
                questions["market_is_untradeable"] = True
        except Exception:  # noqa: BLE001 — audit recording must never break reasoning
            pass

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

    # ── Native reasoning (Articles II / IV / IX / X) ──────────────────────

    def _analyze_evidence(
        self, market_state: MarketState, consolidation: Optional[dict] = None,
    ) -> EvidenceAnalysis:
        """The Brain's own structured reading of the evidence (no LLM). Never raises."""
        return self._native.analyze_evidence(market_state, consolidation)

    def _generate_hypotheses(
        self, analysis: EvidenceAnalysis, symbol: str,
    ) -> "list[BrainHypothesis]":
        """The Brain's own hypotheses formed from its evidence analysis (Article IX)."""
        return self._native.generate_hypotheses(analysis, symbol)

    def _self_criticize(
        self, hypotheses: "list[BrainHypothesis]", analysis: EvidenceAnalysis,
    ) -> "list[BrainHypothesis]":
        """Challenge each hypothesis and adjust confidence up/down (Article IV)."""
        return self._native.self_criticize(hypotheses, analysis)

    def _compare_opportunities(
        self, new_opportunity: BrainHypothesis, symbol: str, existing_positions: Any,
    ) -> str:
        """Rank a discovered opportunity against the book (Article XXVIII)."""
        return self._native.compare_opportunities(new_opportunity, symbol, existing_positions)

    def _native_reasoning(
        self, market_state: MarketState, consolidation: dict, symbol: str,
    ) -> "tuple[EvidenceAnalysis, list[BrainHypothesis]]":
        """Run the full native reasoning pass: analyse → hypothesise → self-criticise.

        Fail-safe: on any fault returns a minimal empty analysis and no
        hypotheses, so the LLM path is entirely unaffected.
        """
        try:
            analysis = self._native.analyze_evidence(market_state, consolidation)
            hypotheses = self._native.generate_hypotheses(analysis, symbol)
            hypotheses = self._native.self_criticize(hypotheses, analysis)
            return analysis, hypotheses
        except Exception as exc:  # noqa: BLE001 — native reasoning must never break a cycle
            logger.debug("[brain] native reasoning(%s) ignored a fault: %s", symbol, exc)
            return EvidenceAnalysis(), []

    def _integrate_brain(
        self, llm_direction: str, eff_conf: float,
        brain_lead: Optional[BrainHypothesis], analysis: Optional[EvidenceAnalysis],
    ) -> "tuple[float, str]":
        """Integrate the Brain's leading hypothesis with the advisor's direction.

        Agreement boosts the effective conviction, disagreement attenuates it,
        and when the Brain formed no directional view it defers to the advisor.
        A no-op (returns ``eff_conf`` unchanged, empty note) when the Brain has
        no leading hypothesis or no analysis — so a measurement-thin evidence
        picture behaves exactly as before. Never raises.
        """
        if brain_lead is None or analysis is None:
            return eff_conf, ""
        try:
            brain_dir = brain_lead.direction_implication or FLAT
            brain_conf = _clamp01(brain_lead.confidence)
            if brain_dir in (LONG, SHORT) and llm_direction in (LONG, SHORT):
                if brain_dir == llm_direction:
                    new_conf = _clamp01(max(brain_conf, eff_conf) * self._native.agreement_boost)
                    outcome, note = "agreement", (
                        f"agreement (both {brain_dir}) → confidence "
                        f"{eff_conf:.2f}→{new_conf:.2f}")
                    eff_conf = new_conf
                else:
                    new_conf = _clamp01(min(brain_conf, eff_conf) * self._native.disagreement_penalty)
                    outcome, note = "disagreement", (
                        f"disagreement (brain {brain_dir} vs advisor {llm_direction}) → "
                        f"confidence {eff_conf:.2f}→{new_conf:.2f}")
                    eff_conf = new_conf
            elif brain_dir not in (LONG, SHORT):
                if analysis.detected_patterns:
                    # The Brain saw non-directional structure (e.g. compression,
                    # degraded execution): defer to the advisor but attenuate by
                    # the Brain's own evidence uncertainty.
                    new_conf = _clamp01(eff_conf * (1.0 - 0.5 * analysis.evidence_uncertainty))
                    outcome, note = "defer_attenuated", (
                        f"brain saw non-directional structure — deferring to advisor, "
                        f"attenuated by evidence uncertainty {analysis.evidence_uncertainty:.2f} "
                        f"→ {new_conf:.2f}")
                    eff_conf = new_conf
                else:
                    outcome, note = "defer", (
                        "brain formed no independent directional hypothesis — "
                        "deferring to advisor")
            else:
                outcome, note = "brain_only", (
                    f"brain holds a {brain_dir} view but the advisor is FLAT — "
                    "no confidence change")
            synthesis = (
                f"Brain hypothesis [{brain_dir} @ {brain_conf:.2f}: "
                f"{brain_lead.statement[:80]}] + advisor opinion [{llm_direction}] → "
                f"[{outcome}] → {note}"
            )
            return eff_conf, synthesis
        except Exception:  # noqa: BLE001 — integration must never break reasoning
            return eff_conf, ""

    @staticmethod
    def _evaluate_reasoning_quality(opinion: Any) -> "tuple[float, str]":
        """Score an advisor opinion's reasoning DEPTH 0.1–1.0 (Article XIX).

        The Council is weighted by reasoning quality, not merely counted for
        quorum. Seven structural signals (a stated hypothesis, its invalidation,
        expected favourable/adverse excursions, alternative hypotheses, the key
        uncertainty, and what-would-change-my-mind) each contribute, plus a bonus
        when the full multidimensional confidence profile is supplied. A bare
        direction+confidence reply floors at 0.1; a fully reasoned opinion
        approaches 1.0. Returns ``(score, explanation)``. Never raises.
        """
        try:
            def _present(attr: str) -> bool:
                v = getattr(opinion, attr, None)
                return bool(str(v).strip()) if v is not None else False

            def _list_has(attr: str) -> bool:
                v = getattr(opinion, attr, None)
                try:
                    return len(list(v or [])) >= 1
                except TypeError:
                    return False

            raw = 0.0
            signals: list[str] = []
            if _present("primary_hypothesis"):
                raw += 0.15
                signals.append("hypothesis")
            if _present("invalidation"):
                raw += 0.15
                signals.append("invalidation")
            if _present("expected_favorable_excursion"):
                raw += 0.10
                signals.append("efe")
            if _present("expected_adverse_excursion"):
                raw += 0.10
                signals.append("eae")
            if _list_has("alternative_hypotheses"):
                raw += 0.10
                signals.append("alternatives")
            if _present("key_uncertainty"):
                raw += 0.10
                signals.append("key_uncertainty")
            if _list_has("what_would_change_my_mind"):
                raw += 0.10
                signals.append("wcm")
            dims = ("thesis_confidence", "opportunity_confidence",
                    "timing_confidence", "execution_confidence")
            if all(getattr(opinion, d, None) is not None for d in dims):
                raw += 0.20
                signals.append("confidence_dims")
            score = min(1.0, max(0.1, raw))
            note = (
                f"reasoning quality {score:.2f} from {len(signals)} signal(s): "
                f"{', '.join(signals) or 'none (bare direction+confidence)'}"
            )
            return score, note
        except Exception:  # noqa: BLE001 — quality scoring must never break reasoning
            return 1.0, "reasoning quality not evaluated (fault)"

    def _apply_pattern_modifier(
        self, eff_conf: float, brain_lead: Optional[BrainHypothesis],
    ) -> "tuple[float, str, list[str]]":
        """Weight conviction by the leading hypothesis's patterns' demonstrated
        edge (Article X). No-op (returns ``eff_conf`` unchanged) when no tracker
        is wired or the hypothesis rests on no detected pattern. Never raises."""
        names: list[str] = []
        try:
            if brain_lead is not None:
                from cognition.pattern_rules import pattern_name
                names = [pattern_name(p)
                         for p in (getattr(brain_lead, "supporting_patterns", []) or [])]
                names = [n for n in names if n]
        except Exception:  # noqa: BLE001
            names = []
        if self._pattern_tracker is None or not names:
            return eff_conf, "", names
        try:
            mods = [float(self._pattern_tracker.confidence_modifier(n)) for n in names]
            avg = (sum(mods) / len(mods)) if mods else 1.0
            new_conf = _clamp01(eff_conf * avg)
            note = (
                f"pattern edge modifier {avg:.3f} (avg over {len(mods)} pattern(s): "
                f"{', '.join(names)}) → confidence {eff_conf:.2f}→{new_conf:.2f}"
            )
            return new_conf, note, names
        except Exception:  # noqa: BLE001 — pattern weighting must never break reasoning
            return eff_conf, "", names

    def _originate_independent(
        self, symbol: str, consolidation: dict,
        analysis: Optional[EvidenceAnalysis], hypotheses: "list[BrainHypothesis]",
        *, reason_txt: str,
    ) -> Optional[BrainOutput]:
        """Discover an opportunity from native analysis when no advisor is available.

        Article X — APEX must be able to discover opportunities that were never
        explicitly programmed, even when every LLM provider is down. When the
        Brain's leading hypothesis is confident (>= threshold), directional and
        the evidence uncertainty is acceptable, the Brain originates a campaign
        WITHOUT any advisor, sized conservatively. Otherwise returns ``None`` and
        the caller stands down (REASONER_UNAVAILABLE). Never raises.
        """
        if analysis is None or not hypotheses:
            return None
        try:
            lead = self._native.leading(hypotheses)
            if lead is None or not lead.is_directional:
                return None
            if lead.confidence < self._native.independent_min_confidence:
                return None
            if analysis.evidence_uncertainty >= self._native.independent_max_uncertainty:
                return None
            direction = lead.direction_implication
            uncertainty = _clamp01(consolidation.get("aggregate_uncertainty", 1.0)) \
                if consolidation else analysis.evidence_uncertainty
            # Article X — weight the lead confidence by its patterns' demonstrated
            # edge before computing EV (neutral when no tracker is wired).
            lead_conf, pattern_mod_note, lead_pattern_names = self._apply_pattern_modifier(
                _clamp01(lead.confidence), lead)
            reward_r, risk_r = self.reward_r_default, 1.0
            cost_r = self.default_cost_r
            expected_value = expected_value_r(lead_conf, reward_r, risk_r, cost_r=cost_r)
            # EV must still be positive to justify capital when acting alone.
            if self.min_expected_value is not None and expected_value < self.min_expected_value:
                return None
            if expected_value <= 0.0:
                return None
            origination_note = (
                f"Brain discovered opportunity from native analysis without advisor "
                f"confirmation — {lead.statement}")
            considered = self._compare_opportunities(lead, symbol, [])
            questions = {
                "what_is_happening": lead.statement,
                "why_is_it_happening": "; ".join(analysis.observations) or lead.statement,
                "evidence_supports": "; ".join(
                    p for p in lead.supporting_patterns) or "native structural patterns",
                "evidence_contradicts": "; ".join(analysis.contradictions) or "none detected",
                "information_missing": "; ".join(analysis.missing_information) or "none",
                "what_would_change_my_mind": lead.invalidation,
                "expected_value": f"{expected_value:.4f}R (native)",
                "downside": "bounded by invalidation conditions",
                "opportunity": lead.opportunity_implication or direction,
                "should_i_do_nothing": (
                    f"evaluated: acting alone is justified — native leading hypothesis "
                    f"at {lead.confidence:.2f} with EV {expected_value:.4f}R"),
                "brain_independent_origination": origination_note,
                "brain_evidence_analysis": analysis.summary(),
                "brain_hypotheses": " | ".join(
                    f"{(h.direction_implication or 'FLAT')}@{h.confidence:.2f}: {h.statement}"
                    for h in hypotheses)[:1000],
                "brain_self_criticism": lead.criticism or "no material criticism",
                "brain_book_comparison": considered,
                "reasoner_state": reason_txt,
            }
            if pattern_mod_note:
                questions["pattern_confidence_modifier"] = pattern_mod_note
            if lead_pattern_names:
                questions["supporting_patterns"] = list(lead_pattern_names)
            base_exposure = min(lead_conf,
                                min(1.0, max(0.0, expected_value / max(0.01, self.reward_r_default))))
            desired_exposure = round(self.independent_sizing_multiplier * base_exposure, 4)
            questions["sizing_rationale"] = (
                f"independent origination sized {desired_exposure:.4f} = "
                f"{self.independent_sizing_multiplier:.2f}x base {base_exposure:.4f} "
                "(conservative — no advisor confirmation)")
            invalidation_conditions = (
                ([lead.invalidation] if lead.invalidation else [])
                + [h.statement for h in hypotheses if h is not lead][:3]
            )
            brain_hyps = self._build_hypotheses(
                direction, lead_conf, reward_r, risk_r,
                lead.statement, [h.statement for h in hypotheses if h is not lead],
                lead.invalidation, [], [],
            )
            decision = DecisionPackage(
                symbol=symbol, decision_type=DecisionType.OPEN_CAMPAIGN,
                thesis=lead.statement,
                confidence=lead_conf, uncertainty=uncertainty,
                expected_value=expected_value,
                campaign_recommendation=f"open {direction}",
                risk_rationale="native opportunity — originated without advisor (Article X)",
                invalidation_conditions=invalidation_conditions,
                hypotheses=brain_hyps,
                questions_answered=questions, do_nothing_considered=True,
                reasoner=self.reasoner_name,
            )
            campaign = CampaignSpecification(
                symbol=symbol, thesis=lead.statement, direction=direction,
                desired_exposure=desired_exposure, expected_value=expected_value,
                initial_execution_intent={"kind": "market",
                                          "confidence": round(lead_conf, 4),
                                          "independent_origination": True},
                confidence=lead_conf,
                invalidation_conditions=invalidation_conditions,
                objectives=[f"harvest {direction} opportunity discovered from native analysis"],
                decision_id=decision.decision_id,
            )
            with self._lock:
                self._independent_originations += 1
            logger.info(
                "[brain] INDEPENDENT ORIGINATION (%s): %s %s @ conf %.2f, EV %.4fR — no advisor",
                symbol, direction, lead.statement[:60], lead_conf, expected_value,
            )
            return self._record(BrainOutput(
                decision=decision, campaign=campaign, direction=direction))
        except Exception as exc:  # noqa: BLE001 — independent origination must never break a cycle
            logger.debug("[brain] independent origination(%s) ignored a fault: %s", symbol, exc)
            return None


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
        # V-016 — direction=None (NOT FLAT): this is an infrastructure state, not
        # a market read. Downstream/logging distinguish it via decision_type /
        # provider_unavailable and must never render it as a FLAT conclusion.
        return self._record(BrainOutput(decision=decision, direction=None))

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
                "ev_primary_gate": self.ev_primary_gate,
                "ev_action_threshold_r": self.ev_action_threshold_r,
                "min_advisors_for_action": self.min_advisors_for_action,
                "min_evidence_domains": self.min_evidence_domains,
                "degraded_confidence_multiplier": self.degraded_confidence_multiplier,
                "degrade_on_unknown_council": self._degrade_on_unknown_council,
                "allow_scale_in": self.allow_scale_in,
                "decisions": self._decisions,
                "campaigns_opened": self._campaigns_opened,
                "independent_originations": self._independent_originations,
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
