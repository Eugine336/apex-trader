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

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("apex.cognition.contracts")


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


# Part XXXV (Art) — stale information must not masquerade as current reality.
# Evidence that omits an explicit relevance horizon is NOT fresh forever: it
# ages out after this default (15 minutes — a reasonable ceiling for most market
# evidence). Sources whose reading has a genuinely different lifetime set their
# own ``relevance_horizon_seconds`` explicitly; only an explicit non-positive
# horizon means "never expires".
DEFAULT_EVIDENCE_HORIZON_SECONDS = 900.0


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
    # Opportunity-harvesting pre-trade states (Part XXV). Both are non-action —
    # neither authorises execution — but they are DISTINCT market conclusions
    # that the old single CONTINUE_OBSERVING/FLAT collapse could not express:
    #   NO_OPPORTUNITY: no exploitable asymmetry exists right now (the market is
    #     untradeable / there is genuinely nothing to harvest). The market was
    #     understood and there is nothing to do.
    #   OPPORTUNITY_FORMING: one or more real opportunities EXIST and are being
    #     tracked, but none has ACTIVATED yet — their entry/confirmation
    #     conditions have not triggered. The Brain is armed and WAITING to
    #     pounce, which is fundamentally different from seeing nothing.
    NO_OPPORTUNITY = "no_opportunity"
    OPPORTUNITY_FORMING = "opportunity_forming"
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


class OpportunityState(Enum):
    """The lifecycle state of a single opportunity (Part XXV — harvesting).

    An opportunity is not born tradeable. It is first IDENTIFIED (FORMING),
    then ACTIVATES when its entry conditions trigger, may be CONFIRMED by
    follow-through, can keep STRENGTHENING, then eventually WEAKEN and become
    EXHAUSTED. The distinction between *identified* and *activated* is what lets
    the Brain hold a forming idea without executing it prematurely.
    """

    FORMING = "forming"
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    STRENGTHENING = "strengthening"
    WEAKENING = "weakening"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# States in which a directional opportunity is ELIGIBLE to become a live
# campaign — it has actually activated, not merely been identified. A FORMING
# opportunity has been SEEN but its entry/confirmation conditions have not
# triggered, so it must WAIT rather than execute (Part XXV: identification is
# not activation). WEAKENING / EXHAUSTED are fading and never originate anew.
_ACTIONABLE_OPPORTUNITY_STATES = frozenset({
    OpportunityState.ACTIVE,
    OpportunityState.CONFIRMED,
    OpportunityState.STRENGTHENING,
})

# The model may report a state with a synonym; normalise it to the canonical
# lifecycle token. An unrecognised or missing value stays UNKNOWN, which is NOT
# actionable (Part XXV / V-003): an opportunity with no explicit lifecycle state
# has not been shown to have ACTIVATED, so it must never drive origination — see
# :meth:`Opportunity.is_actionable`.
_OPPORTUNITY_STATE_ALIASES = {
    "FORMING": OpportunityState.FORMING, "PENDING": OpportunityState.FORMING,
    "SETTING_UP": OpportunityState.FORMING, "WATCHING": OpportunityState.FORMING,
    "POTENTIAL": OpportunityState.FORMING,
    "ACTIVE": OpportunityState.ACTIVE, "TRIGGERED": OpportunityState.ACTIVE,
    "READY": OpportunityState.ACTIVE, "LIVE": OpportunityState.ACTIVE,
    "CONFIRMED": OpportunityState.CONFIRMED, "VALIDATED": OpportunityState.CONFIRMED,
    "STRENGTHENING": OpportunityState.STRENGTHENING, "STRONG": OpportunityState.STRENGTHENING,
    "WEAKENING": OpportunityState.WEAKENING, "FADING": OpportunityState.WEAKENING,
    "DETERIORATING": OpportunityState.WEAKENING, "SOFTENING": OpportunityState.WEAKENING,
    "EXHAUSTED": OpportunityState.EXHAUSTED, "SPENT": OpportunityState.EXHAUSTED,
    "DONE": OpportunityState.EXHAUSTED,
}


def opportunity_state_from(
    value: Any, default: "OpportunityState" = OpportunityState.UNKNOWN,
) -> "OpportunityState":
    if isinstance(value, OpportunityState):
        return value
    key = str(value or "").strip().upper().replace(" ", "_")
    if not key:
        return default
    try:
        return OpportunityState(key.lower())
    except ValueError:
        return _OPPORTUNITY_STATE_ALIASES.get(key, default)


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
        """True if within its relevance horizon (Part XXXV).

        Evidence with no explicit ``relevance_horizon_seconds`` is NOT fresh
        forever: it ages out after :data:`DEFAULT_EVIDENCE_HORIZON_SECONDS`, so
        a stale reading that simply never declared a horizon cannot masquerade
        as current reality. Only an explicit non-positive horizon means the
        evidence never expires (e.g. a deliberately timeless fact).
        """
        horizon = self.relevance_horizon_seconds
        if horizon is None:
            horizon = DEFAULT_EVIDENCE_HORIZON_SECONDS
        if horizon <= 0:
            return True
        t = _now_epoch() if now is None else float(now)
        return (t - self.timestamp_epoch) <= horizon

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
#
# V-025 (semantic debt): this denylist is defense-in-depth only. Its continued
# existence proves upstream evidence producers may STILL emit these forbidden
# keys at the Brain's boundary. The goal is to make this sanitizer unnecessary —
# every actual strip is logged (DEBUG) in :func:`scrub_directional` so the
# offending source module can be found and cleaned up, after which this frozenset
# can shrink and eventually be removed.
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

    V-025: when a forbidden key is ACTUALLY stripped, log which source module
    still emitted it (DEBUG) so this sanitizer can be retired once every upstream
    producer stops emitting directional readings.
    """
    try:
        ev.polarity = 0.0
        m = getattr(ev, "measurements", None)
        if isinstance(m, dict) and m:
            stripped = [
                k for k in list(m.keys())
                if str(k).strip().lower() in _DIRECTIONAL_MEASUREMENT_KEYS
            ]
            for k in stripped:
                m.pop(k, None)
            if stripped:
                logger.debug(
                    "scrub_directional: %s/%s stripped forbidden directional "
                    "measurement key(s) %s — upstream producer still emits a "
                    "directional reading (goal: eliminate this sanitizer as "
                    "producers are cleaned up)",
                    getattr(ev, "source_module", "?"),
                    getattr(ev, "symbol", "?"),
                    stripped,
                )
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
class Opportunity:
    """A single exploitable market asymmetry the Brain reasons over (Part XXV).

    The opportunity — not a collapsed ``direction``+``confidence`` scalar — is
    the primary cognitive object of the harvesting architecture. The market can
    contain SEVERAL opportunities simultaneously (e.g. a SHORT correction AND a
    later LONG reversal on the same instrument); the Brain holds the whole SET
    and prefers the best, rather than averaging them into one weak directional
    vote. Each opportunity carries its own lifecycle ``state``, activation /
    confirmation / invalidation conditions, target logic and bounded 0..1
    sub-scores (quality, asymmetry, urgency, evidence_strength). That lets
    execution require ACTIVATION (state ACTIVE/CONFIRMED) instead of firing the
    instant a thesis is merely identified (FORMING). Inert record — like every
    contract here it carries no method that decides.
    """

    opportunity_id: str = ""
    direction: str = "FLAT"                 # LONG | SHORT | FLAT
    state: OpportunityState = OpportunityState.UNKNOWN
    horizon: str = ""                       # HTF | MTF | LTF | MICRO
    thesis: str = ""
    why_now: str = ""
    entry_conditions: list[str] = field(default_factory=list)
    confirmation_conditions: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    target_logic: str = ""
    quality: float = 0.0
    asymmetry: float = 0.0
    urgency: float = 0.0
    evidence_strength: float = 0.0
    competing_opportunities: list[str] = field(default_factory=list)
    is_preferred: bool = False

    def __post_init__(self) -> None:
        d = str(self.direction or "FLAT").upper()
        self.direction = d if d in ("LONG", "SHORT", "FLAT") else "FLAT"
        self.state = opportunity_state_from(self.state)
        self.quality = _clamp01(self.quality)
        self.asymmetry = _clamp01(self.asymmetry)
        self.urgency = _clamp01(self.urgency)
        self.evidence_strength = _clamp01(self.evidence_strength)

    @property
    def is_directional(self) -> bool:
        return self.direction in ("LONG", "SHORT")

    @property
    def is_actionable(self) -> bool:
        """True when this opportunity has ACTIVATED and is eligible to become a
        live campaign: a directional opportunity whose state is ACTIVE /
        CONFIRMED / STRENGTHENING. A FORMING opportunity is deliberately NOT
        actionable — it has been identified but its entry conditions have not
        triggered, so it must wait (the activation gate, Part XXV). An UNKNOWN
        state (a reply that never reported a lifecycle state) is likewise NOT
        actionable (V-003): a missing state is not evidence of activation, so it
        must never authorise origination — direction alone is not an activated
        opportunity."""
        if not self.is_directional:
            return False
        return self.state in _ACTIONABLE_OPPORTUNITY_STATES

    @property
    def is_forming(self) -> bool:
        """Identified but not yet activated — present, tracked, and waiting for
        its entry/confirmation conditions rather than eligible to execute."""
        return self.is_directional and self.state == OpportunityState.FORMING

    @property
    def rank_score(self) -> float:
        """Ranking score = quality × asymmetry × evidence_strength (Part XXV).

        A sub-score of 0 (typically an omitted field) falls back to ``quality``
        so a reply that only graded quality still ranks monotonically by it,
        rather than collapsing every opportunity to a zero product.
        """
        a = self.asymmetry if self.asymmetry > 0.0 else self.quality
        e = self.evidence_strength if self.evidence_strength > 0.0 else self.quality
        return self.quality * a * e

    @classmethod
    def from_reply(cls, data: Any, *, preferred_id: str = "") -> "Opportunity":
        """Build an :class:`Opportunity` from a raw reasoner opportunity dict
        (the harvesting schema emitted by the LLM). Tolerant of missing keys and
        legacy field names; never raises."""
        if not isinstance(data, dict):
            return cls()

        def _lst(key: str) -> "list[str]":
            v = data.get(key)
            if isinstance(v, list):
                return [str(x)[:200] for x in v][:8]
            if isinstance(v, str) and v.strip():
                return [v.strip()[:200]]
            return []

        oid = str(data.get("id", "") or data.get("opportunity_id", "") or "")
        return cls(
            opportunity_id=oid,
            direction=str(data.get("direction", "") or "FLAT"),
            state=opportunity_state_from(data.get("state")),
            horizon=str(data.get("horizon", "") or "")[:16],
            thesis=str(data.get("thesis", "") or data.get("why_now", "") or "")[:400],
            why_now=str(data.get("why_now", "") or "")[:280],
            entry_conditions=_lst("entry_conditions"),
            confirmation_conditions=_lst("confirmation_conditions"),
            invalidation_conditions=_lst("invalidation_conditions"),
            target_logic=str(data.get("target_logic", "") or "")[:280],
            quality=_clamp01(data.get("quality")),
            asymmetry=_clamp01(data.get("asymmetry")),
            urgency=_clamp01(data.get("urgency")),
            evidence_strength=_clamp01(data.get("evidence_strength")),
            competing_opportunities=_lst("competing_opportunities"),
            is_preferred=bool(oid) and oid == str(preferred_id or ""),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.opportunity_id,
            "direction": self.direction,
            "state": self.state.value,
            "horizon": self.horizon,
            "thesis": self.thesis,
            "why_now": self.why_now,
            "entry_conditions": list(self.entry_conditions),
            "confirmation_conditions": list(self.confirmation_conditions),
            "invalidation_conditions": list(self.invalidation_conditions),
            "target_logic": self.target_logic,
            "quality": round(self.quality, 4),
            "asymmetry": round(self.asymmetry, 4),
            "urgency": round(self.urgency, 4),
            "evidence_strength": round(self.evidence_strength, 4),
            "rank_score": round(self.rank_score, 4),
            "competing_opportunities": list(self.competing_opportunities),
            "is_preferred": bool(self.is_preferred),
            "is_actionable": self.is_actionable,
            "is_forming": self.is_forming,
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
    # Part XXV — the FULL ranked opportunity set the Brain reasoned over, as
    # first-class data (not an audit-only log line). The market can hold several
    # opportunities at once; this preserves every one — including FORMING ideas
    # the Brain is tracking but has not yet acted on — so consumers (management,
    # the dashboard, governance) can reason over the SET, not a single collapsed
    # direction. Empty for a legacy single-direction reply. ``preferred_opportunity_id``
    # names the opportunity that drove this decision (when one did).
    opportunities: list["Opportunity"] = field(default_factory=list)
    preferred_opportunity_id: str = ""
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

    @property
    def actionable_opportunities(self) -> list["Opportunity"]:
        """The subset of the tracked set that has ACTIVATED (eligible now)."""
        return [o for o in self.opportunities if getattr(o, "is_actionable", False)]

    @property
    def forming_opportunities(self) -> list["Opportunity"]:
        """The subset identified but not yet activated (tracked, waiting)."""
        return [o for o in self.opportunities if getattr(o, "is_forming", False)]

    @property
    def preferred_opportunity(self) -> Optional["Opportunity"]:
        """The opportunity that drove this decision (by id), or ``None``.

        Part XXV — a decision is anchored to an *opportunity* (thesis, lifecycle
        state, asymmetry), not a bare direction. Consumers and logs should
        describe a decision through this richer object rather than the collapsed
        top-level direction.
        """
        pid = self.preferred_opportunity_id
        if pid:
            for o in self.opportunities:
                if getattr(o, "opportunity_id", "") == pid:
                    return o
        return None

    @property
    def summary(self) -> str:
        """One-line, constitution-aligned decision summary for logs/operators.

        Leads with the decision type and the opportunity + thesis that drove it
        (Part XXV) — e.g. ``decision=open_campaign opp=momentum_breakout
        state=active thesis=…`` — deliberately NOT a ``dir=LONG conf=…`` collapse.
        Direction, when relevant, is carried by the opportunity object as
        secondary context, not surfaced as the headline.
        """
        parts = [f"decision={self.decision_type.value}"]
        opp = self.preferred_opportunity
        thesis = self.thesis
        if opp is not None:
            if opp.opportunity_id:
                parts.append(f"opp={opp.opportunity_id}")
            state = getattr(opp, "state", None)
            if state is not None:
                parts.append(f"state={getattr(state, 'value', state)}")
            thesis = opp.thesis or thesis
        if thesis:
            parts.append(f"thesis={thesis[:80]}")
        return " ".join(parts)

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
            "opportunities": [o.to_dict() for o in self.opportunities],
            "preferred_opportunity_id": self.preferred_opportunity_id,
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
    expected_value: float = 0.0        # EV in R that justified the sizing (Part XXVIII)
    initial_execution_intent: dict = field(default_factory=dict)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    invalidation_conditions: list[str] = field(default_factory=list)
    objectives: list[str] = field(default_factory=list)
    # Part XXV — provenance of the opportunity that activated this campaign. The
    # campaign is still a single directional position, but recording WHICH
    # opportunity (and the state it had activated in) lets management compare the
    # live campaign against the evolving opportunity set. Empty on a legacy reply.
    opportunity_id: str = ""
    opportunity_state: str = ""
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
            "expected_value": round(self.expected_value, 6),
            "initial_execution_intent": dict(self.initial_execution_intent),
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "contradicting_evidence_ids": list(self.contradicting_evidence_ids),
            "confidence": round(self.confidence, 4),
            "invalidation_conditions": list(self.invalidation_conditions),
            "objectives": list(self.objectives),
            "opportunity_id": self.opportunity_id,
            "opportunity_state": self.opportunity_state,
            "decision_id": self.decision_id,
        }


__all__ = [
    "Evidence",
    "EvidenceDomain",
    "DEFAULT_EVIDENCE_HORIZON_SECONDS",
    "domain_from",
    "MarketState",
    "DecisionType",
    "OpportunityState",
    "opportunity_state_from",
    "Hypothesis",
    "Opportunity",
    "DecisionPackage",
    "CampaignSpecification",
    "REQUIRED_QUESTIONS",
]
