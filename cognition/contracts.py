"""APEX TRADER — Cognitive contracts (the constitutional data language).

These are the shared data structures mandated by the APEX Constitution. They are
the only vocabulary the cognitive architecture is permitted to speak in:

* :class:`Evidence` — Part III, Article 4. A structured, explainable observation
  with provenance, confidence and uncertainty. Evidence modules produce these
  and nothing else — never a buy/sell/hold/close/reverse signal (Part III,
  Article 2).
* :class:`MarketState` — Part IV, Article 2. The unified, consolidated snapshot
  of all current evidence the Brain reasons over.
* :class:`DecisionPackage` — Part II, Article 7. The Brain's structured output:
  thesis + supporting/contradicting evidence + confidence/uncertainty/EV +
  horizon + invalidation conditions + the required-questions record (Part II,
  Article 3). Execution consumes this and never reinterprets it (Part VI).
* :class:`CampaignSpecification` — Part IV, Article 8 / Part VI, Article 2. The
  Brain-authored campaign the execution layer faithfully realises.

Constitutional properties enforced here:

* **No signals.** Evidence carries a neutral, bounded ``polarity`` describing
  which way the *observation* leans as evidence — it is explicitly NOT a trade
  instruction; only the Brain turns evidence into a decision.
* **Explainability + provenance.** Every Evidence has an id, timestamp and
  source; every decision references the evidence ids that supported and
  contradicted it (Part III, Article 7; Part VII, Article 6).
* **Single reasoner.** Only a :class:`DecisionPackage` / :class:`CampaignSpecification`
  — produced by the one Brain — authorises action. These types carry no method
  that decides; they are inert records.

Pure standard library only (no third-party imports) so the constitutional
vocabulary is trivially importable and testable everywhere.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


def _now_epoch() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return min(1.0, max(0.0, f))


def _clamp_signed(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return min(1.0, max(-1.0, f))


class EvidenceDomain(Enum):
    """The evidence domains named in Part III, Article 3 (extensible)."""

    STRUCTURE = "structure"
    LIQUIDITY = "liquidity"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    VOLUME = "volume"
    ORDER_FLOW = "order_flow"
    MULTI_TIMEFRAME = "multi_timeframe"
    CORRELATION = "correlation"
    SESSION = "session"
    MACRO = "macro"
    EXECUTION_QUALITY = "execution_quality"
    PORTFOLIO = "portfolio"
    HISTORICAL_ANALOGUE = "historical_analogue"
    REASONING = "reasoning"
    OTHER = "other"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


def domain_from(value: Any, default: "EvidenceDomain" = EvidenceDomain.OTHER) -> "EvidenceDomain":
    if isinstance(value, EvidenceDomain):
        return value
    try:
        return EvidenceDomain(str(value or "").strip().lower())
    except ValueError:
        return default


class DecisionType(Enum):
    """The only constitutional pre-trade outcomes (Part IV, Article 7) plus the
    in-campaign management actions the Brain may authorise (Part VI, Article 5)."""

    # Pre-trade (Part IV, Article 7)
    REJECT_OPPORTUNITY = "reject_opportunity"
    OPEN_CAMPAIGN = "open_campaign"
    CONTINUE_OBSERVING = "continue_observing"
    # Infrastructure state (Part XVIII, Art 5): the Brain has no usable reasoner
    # (provider down / unavailable). This is explicitly NOT a market conclusion —
    # it must never be read as a FLAT/observe view of the market.
    REASONER_UNAVAILABLE = "reasoner_unavailable"
    # In-campaign management (Part VI, Article 5)
    HOLD = "hold"
    SCALE_IN = "scale_in"
    SCALE_OUT = "scale_out"
    PROTECT_PROFIT = "protect_profit"
    TIGHTEN_RISK = "tighten_risk"
    EXIT = "exit"
    REVERSE = "reverse"
    TERMINATE_CAMPAIGN = "terminate_campaign"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass
class Evidence:
    """A single structured observation (Part III, Article 4). Never a signal.

    ``polarity`` is a bounded [-1, +1] *evidential lean* (negative = bearish
    evidence, positive = bullish evidence, 0 = neutral/directionless). It is NOT
    an instruction to trade — only the Brain synthesises polarity across evidence
    into a decision. ``confidence`` is how strongly the source believes the
    observation; ``uncertainty`` is the source's own doubt about it.
    """

    source_module: str
    domain: EvidenceDomain = EvidenceDomain.OTHER
    observation: str = ""
    confidence: float = 0.0
    uncertainty: float = 0.0
    polarity: float = 0.0
    measurements: dict = field(default_factory=dict)
    symbol: str = ""
    relevance_horizon_seconds: Optional[float] = None
    evidence_id: str = ""
    timestamp_iso: str = ""
    timestamp_epoch: float = 0.0

    def __post_init__(self) -> None:
        if not self.evidence_id:
            self.evidence_id = uuid.uuid4().hex[:12]
        if not self.timestamp_epoch:
            self.timestamp_epoch = _now_epoch()
        if not self.timestamp_iso:
            self.timestamp_iso = _now_iso()
        self.domain = domain_from(self.domain)
        self.confidence = _clamp01(self.confidence)
        self.uncertainty = _clamp01(self.uncertainty)
        self.polarity = _clamp_signed(self.polarity)

    def is_fresh(self, now: Optional[float] = None) -> bool:
        """True if within its relevance horizon (always True when unbounded)."""
        if self.relevance_horizon_seconds is None or self.relevance_horizon_seconds <= 0:
            return True
        t = _now_epoch() if now is None else float(now)
        return (t - self.timestamp_epoch) <= self.relevance_horizon_seconds

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "timestamp": self.timestamp_iso,
            "source_module": self.source_module,
            "domain": self.domain.value,
            "symbol": self.symbol,
            "observation": self.observation,
            "confidence": round(self.confidence, 4),
            "uncertainty": round(self.uncertainty, 4),
            "polarity": round(self.polarity, 4),
            "measurements": dict(self.measurements),
            "relevance_horizon_seconds": self.relevance_horizon_seconds,
        }


# Part XXV — the Brain must never receive a directional reading from any module.
# These measurement keys encode a precomputed direction/lean/probability and are
# stripped from every Evidence as it enters a MarketState (see MarketState.add).
_DIRECTIONAL_MEASUREMENT_KEYS = frozenset({
    "directional_lean", "lean", "direction", "bias", "sentiment",
    "long_probability", "short_probability", "long_ev", "short_ev", "flat_ev",
    "dominant", "score", "signed_score", "vote", "signal",
})


def scrub_directional(ev: "Evidence") -> "Evidence":
    """Force an Evidence to be non-directional (Part XXV Art 2/3, in place).

    Sets ``polarity`` to 0 (no directional lean) and removes any measurement key
    that encodes a precomputed direction/probability. The Brain forms direction
    itself from raw measurements + the price picture; nothing upstream may hand
    it a directional reading. Fail-safe — never raises.
    """
    try:
        ev.polarity = 0.0
        m = getattr(ev, "measurements", None)
        if isinstance(m, dict) and m:
            for k in list(m.keys()):
                if str(k).strip().lower() in _DIRECTIONAL_MEASUREMENT_KEYS:
                    m.pop(k, None)
    except Exception:  # noqa: BLE001 — sanitising must never break consolidation
        pass
    return ev


@dataclass
class MarketState:
    """Unified consolidated evidence snapshot the Brain reasons over (Part IV, Art 2)."""

    symbol: str
    evidence: list[Evidence] = field(default_factory=list)
    created_iso: str = ""
    created_epoch: float = 0.0
    # Part VIII — optional per-source influence weights (source_module → weight).
    # When set (by the consolidator from the InfluenceLedger), consolidation
    # weights each evidence's contribution by its source's learned influence.
    # ``None`` ⇒ unweighted (every source counts equally) — the default.
    influence_weights: Optional[dict] = None

    def __post_init__(self) -> None:
        if not self.created_epoch:
            self.created_epoch = _now_epoch()
        if not self.created_iso:
            self.created_iso = _now_iso()

    def add(self, ev: Evidence) -> None:
        if isinstance(ev, Evidence):
            # Part XXV — enforce the non-directional contract at the single choke
            # point every adapter and injected evidence passes through, so no
            # directional reading can reach the Brain (or the advisors, which
            # read this evidence mid-consolidation) from anywhere upstream.
            self.evidence.append(scrub_directional(ev))

    def fresh_evidence(self, now: Optional[float] = None) -> list[Evidence]:
        return [e for e in self.evidence if e.is_fresh(now)]

    def consolidation(self, now: Optional[float] = None) -> dict:
        """Freshness / completeness / conflict / uncertainty summary (Part IV, Art 2).

        ``missing evidence increases uncertainty`` — an empty or stale state
        reports high uncertainty so the Brain treats it as inadequate.
        """
        fresh = self.fresh_evidence(now)
        total = len(self.evidence)
        n = len(fresh)
        domains = {e.domain.value for e in fresh}
        weights = self.influence_weights
        if not weights:
            # Unweighted (default) — every source counts equally.
            mean_conf = sum(e.confidence for e in fresh) / n if n else 0.0
            mean_unc = sum(e.uncertainty for e in fresh) / n if n else 1.0
        else:
            # Part VIII — weight each source's contribution by learned influence.
            def _w(e: Evidence) -> float:
                try:
                    return max(0.0, float(weights.get(e.source_module, 1.0)))
                except Exception:  # noqa: BLE001
                    return 1.0
            wsum = sum(_w(e) for e in fresh)
            mean_conf = (sum(_w(e) * e.confidence for e in fresh) / wsum) if wsum else 0.0
            mean_unc = (sum(_w(e) * e.uncertainty for e in fresh) / wsum) if wsum else 1.0
        # Part XXV — evidence carries no directional reading, so there is no
        # directional "conflict" to measure before the Brain has reasoned.
        # Uncertainty derives from the sources' own doubt, their confidence, and
        # how little of the market picture is present (missing evidence ⇒ doubt).
        conflict = 0.0
        coverage_gap = 1.0 - min(1.0, len(domains) / 6.0)
        # No evidence ⇒ maximal uncertainty (Part IV, Article 2).
        aggregate_uncertainty = 1.0 if n == 0 else _clamp01(
            0.5 * mean_unc + 0.3 * (1.0 - mean_conf) + 0.2 * coverage_gap
        )
        return {
            "symbol": self.symbol,
            "evidence_total": total,
            "evidence_fresh": n,
            "domains_present": sorted(domains),
            "domain_count": len(domains),
            "conflict_ratio": round(conflict, 4),
            "mean_confidence": round(mean_conf, 4),
            "mean_uncertainty": round(mean_unc, 4),
            "aggregate_uncertainty": round(aggregate_uncertainty, 4),
            "influence_weighted": bool(weights),
        }

    def to_dict(self, *, include_evidence: bool = True) -> dict:
        out = {
            "symbol": self.symbol,
            "created": self.created_iso,
            "consolidation": self.consolidation(),
        }
        if include_evidence:
            out["evidence"] = [e.to_dict() for e in self.evidence]
        return out


# The ten required questions the Brain must be able to answer (Part II, Article 3).
REQUIRED_QUESTIONS = (
    "what_is_happening",
    "why_is_it_happening",
    "evidence_supports",
    "evidence_contradicts",
    "information_missing",
    "what_would_change_my_mind",
    "expected_value",
    "downside",
    "opportunity",
    "should_i_do_nothing",
)


@dataclass
class Hypothesis:
    """One candidate explanation the Brain holds about the market (Part V / Part VI).

    The constitution forbids collapsing cognition prematurely into a single
    LONG/SHORT/FLAT verdict. A :class:`DecisionPackage` therefore carries the
    Brain's *competing* hypotheses simultaneously — each with its own
    probability, directional interpretation, payoff geometry (in units of risk,
    R) and invalidation — so the richer reasoning survives into the decision
    record instead of being reduced to one direction+confidence pair. The
    ``direction`` here is an execution hint for a hypothesis, never an authority
    of its own; only a :class:`DecisionPackage` / :class:`CampaignSpecification`
    authorises action.
    """

    statement: str
    probability: float = 0.0          # Brain's P(this hypothesis is correct), [0, 1]
    direction: str = ""               # LONG | SHORT | FLAT | "" (unknown)
    expected_reward_r: float = 0.0    # favorable excursion, in units of risk (R)
    expected_risk_r: float = 1.0      # adverse excursion, in units of risk (R)
    invalidation: str = ""
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    horizon_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        self.probability = _clamp01(self.probability)
        d = str(self.direction or "").upper()
        self.direction = d if d in ("LONG", "SHORT", "FLAT") else ""

    def to_dict(self) -> dict:
        return {
            "statement": self.statement,
            "probability": round(self.probability, 4),
            "direction": self.direction,
            "expected_reward_r": round(self.expected_reward_r, 4),
            "expected_risk_r": round(self.expected_risk_r, 4),
            "invalidation": self.invalidation,
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "contradicting_evidence_ids": list(self.contradicting_evidence_ids),
            "horizon_seconds": self.horizon_seconds,
        }


@dataclass
class DecisionPackage:
    """The Brain's structured decision output (Part II, Article 7). Inert record.

    It carries no method that decides — it is the *result* of the one Brain's
    reasoning, consumed (never reinterpreted) by execution.
    """

    symbol: str
    decision_type: DecisionType = DecisionType.CONTINUE_OBSERVING
    thesis: str = ""
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    uncertainty: float = 0.0
    expected_value: float = 0.0
    expected_holding_horizon_seconds: Optional[float] = None
    campaign_recommendation: str = ""
    risk_rationale: str = ""
    invalidation_conditions: list[str] = field(default_factory=list)
    hypotheses: list["Hypothesis"] = field(default_factory=list)
    questions_answered: dict = field(default_factory=dict)
    do_nothing_considered: bool = False
    reasoner: str = ""                 # which Brain produced it (provenance)
    decision_id: str = ""
    created_iso: str = ""

    def __post_init__(self) -> None:
        if not self.decision_id:
            self.decision_id = uuid.uuid4().hex[:12]
        if not self.created_iso:
            self.created_iso = _now_iso()
        if isinstance(self.decision_type, str):
            try:
                self.decision_type = DecisionType(self.decision_type)
            except ValueError:
                self.decision_type = DecisionType.CONTINUE_OBSERVING
        self.confidence = _clamp01(self.confidence)
        self.uncertainty = _clamp01(self.uncertainty)

    @property
    def authorises_action(self) -> bool:
        """True only for decision types that actually direct execution."""
        return self.decision_type in (
            DecisionType.OPEN_CAMPAIGN, DecisionType.SCALE_IN,
            DecisionType.SCALE_OUT, DecisionType.PROTECT_PROFIT,
            DecisionType.TIGHTEN_RISK, DecisionType.EXIT, DecisionType.REVERSE,
            DecisionType.TERMINATE_CAMPAIGN,
        )

    def unanswered_questions(self) -> list[str]:
        """Required questions (Part II, Art 3) without a recorded answer."""
        return [q for q in REQUIRED_QUESTIONS if not str(self.questions_answered.get(q, "")).strip()]

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "created": self.created_iso,
            "symbol": self.symbol,
            "decision_type": self.decision_type.value,
            "thesis": self.thesis,
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "contradicting_evidence_ids": list(self.contradicting_evidence_ids),
            "confidence": round(self.confidence, 4),
            "uncertainty": round(self.uncertainty, 4),
            "expected_value": round(self.expected_value, 6),
            "expected_holding_horizon_seconds": self.expected_holding_horizon_seconds,
            "campaign_recommendation": self.campaign_recommendation,
            "risk_rationale": self.risk_rationale,
            "invalidation_conditions": list(self.invalidation_conditions),
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "questions_answered": dict(self.questions_answered),
            "unanswered_questions": self.unanswered_questions(),
            "do_nothing_considered": bool(self.do_nothing_considered),
            "reasoner": self.reasoner,
            "authorises_action": self.authorises_action,
        }


@dataclass
class CampaignSpecification:
    """A Brain-authored campaign the execution layer faithfully realises.

    Part IV, Article 8 (campaign initialization) and Part VI, Article 2
    (campaign translation). Execution consumes this without altering its market
    thesis.
    """

    symbol: str
    thesis: str = ""
    direction: str = "FLAT"            # LONG | SHORT | FLAT
    desired_exposure: float = 0.0      # normalised target exposure the Brain wants
    initial_execution_intent: dict = field(default_factory=dict)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    invalidation_conditions: list[str] = field(default_factory=list)
    objectives: list[str] = field(default_factory=list)
    decision_id: str = ""              # link back to the DecisionPackage
    campaign_id: str = ""
    created_iso: str = ""

    def __post_init__(self) -> None:
        if not self.campaign_id:
            self.campaign_id = uuid.uuid4().hex[:12]
        if not self.created_iso:
            self.created_iso = _now_iso()
        d = str(self.direction or "FLAT").upper()
        self.direction = d if d in ("LONG", "SHORT", "FLAT") else "FLAT"
        self.confidence = _clamp01(self.confidence)

    def to_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "created": self.created_iso,
            "symbol": self.symbol,
            "thesis": self.thesis,
            "direction": self.direction,
            "desired_exposure": round(self.desired_exposure, 6),
            "initial_execution_intent": dict(self.initial_execution_intent),
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "contradicting_evidence_ids": list(self.contradicting_evidence_ids),
            "confidence": round(self.confidence, 4),
            "invalidation_conditions": list(self.invalidation_conditions),
            "objectives": list(self.objectives),
            "decision_id": self.decision_id,
        }


__all__ = [
    "Evidence",
    "EvidenceDomain",
    "domain_from",
    "MarketState",
    "DecisionType",
    "Hypothesis",
    "DecisionPackage",
    "CampaignSpecification",
    "REQUIRED_QUESTIONS",
]
