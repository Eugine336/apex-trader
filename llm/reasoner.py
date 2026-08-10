"""APEX TRADER — LLM reasoning subsystem.

The language model is **not** a chatbot bolted onto the side and it is **not**
an oracle that overrides deterministic safeguards. It is an additional reasoning
participant that receives the same *structured evidence* every other module sees
(the competing theses, the vote panel summary, regime, structure) and returns a
structured opinion: a direction, a calibrated confidence, a short rationale, the
competing hypotheses it considered, and the information it feels is missing.

That opinion is emitted as **evidence**, exactly like any analytical module — it
becomes a vote-like object whose influence is governed by the existing
performance-based authority layer (``ModuleGovernor`` / ``VoteCalibrator``) and
can never bypass the physics vetoes. Introduced observationally: default OFF,
and even when enabled it records its opinions for the dashboard/governance and
only feeds the panel once ``drive_decisions`` is set.

Design principles:

* **Fail-safe.** Every public method swallows faults and returns a safe default
  (``None`` / neutral) — a reasoning fault must never break the trading cycle.
* **Bounded / throttled.** LLM calls are expensive; a per-symbol minimum
  interval prevents hammering the provider. The network call itself lives behind
  :class:`llm.client.LLMClient` and is only reached when a real provider is
  configured — so this module is fully testable offline.
* **Secret-safe.** Status output exposes provider/model only, never the key.
* **Latency-aware.** :meth:`reason` performs a blocking provider call, so it must
  be driven from a background/periodic path — never from the hot tick loop.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from llm.json_repair import repair_json

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"

_SYSTEM_PROMPT = (
    "You are the Cognitive Brain of an autonomous trading intelligence. You "
    "receive a STRUCTURED REPRESENTATION OF MARKET REALITY for one instrument — "
    "NOT a set of votes to arbitrate. It may include a reconstructed multi-"
    "timeframe price picture (OHLC candles, current price and spread), the tick "
    "tape (velocity, drift, up/down balance, momentum, spread behaviour), order-"
    "book depth when available, the session, a higher-vs-lower-timeframe pullback "
    "read, and per-instrument analytical READINGS expressed as measurements (each "
    "a signed, bounded 'measured lean' plus secondary values). Those readings are "
    "instrument outputs, like a thermometer's temperature — NEVER decisions or "
    "votes to count. Do not invent data you were not given.\n\n"
    "Observe reality first, then interpret what is actually happening. Form a "
    "PRIMARY hypothesis and at least one ALTERNATIVE, then actively challenge your "
    "primary: why might I be wrong? what evidence contradicts it? am I anchoring "
    "to one timeframe? am I confusing movement with opportunity? has the move "
    "already happened? State the key uncertainty and what information is missing. "
    "You may discover an opportunity nobody pre-programmed — it can run against "
    "the higher-timeframe trend, be very short-lived, or not exist at all.\n\n"
    "MOVEMENT IS NOT OPPORTUNITY. An opportunity is executable positive expected "
    "value after the round-trip spread, commission, slippage and latency. Only act "
    "when the expected favourable move clears those costs and beats the expected "
    "adverse excursion; when the edge does not clear costs, or the picture is "
    "noise, do nothing. Judge LONG and SHORT symmetrically: the higher-timeframe "
    "trend is CONTEXT, never a default — never buy a market still actively falling "
    "(down ticks, negative drift, fresh lower lows) nor sell one still actively "
    "rising merely because a higher timeframe leans that way (no falling knives; "
    "apply the mirror for shorts). When the freshest lower-timeframe evidence "
    "conflicts with a stale higher-timeframe lean, trust the fresh evidence or do "
    "nothing.\n\n"
    "Direction is the CONSEQUENCE of your reasoning, not its container. Reason "
    "first; only then collapse to an execution instruction. Respond with STRICT "
    "JSON only, no prose, no markdown fences, exactly these keys:\n"
    "{\"regime\": \"trending|ranging|transitional|volatile|uncertain\", "
    "\"primary_hypothesis\": \"what is happening and why\", "
    "\"alternative_hypotheses\": [\"competing explanation(s)\"], "
    "\"supporting_evidence\": [\"...\"], \"contradicting_evidence\": [\"...\"], "
    "\"key_uncertainty\": \"the main thing you are unsure of\", "
    "\"missing_information\": [\"...\"], "
    "\"opportunity\": \"the exploitable opportunity, or 'none'\", "
    "\"opportunity_horizon\": \"seconds|minutes|hours|days|none\", "
    "\"expected_favorable_excursion\": \"how far it can go your way\", "
    "\"expected_adverse_excursion\": \"how far it can go against you first\", "
    "\"expected_value\": \"net of costs: positive|negative|unclear\", "
    "\"execution_quality\": \"spread/liquidity/slippage read\", "
    "\"risk\": \"the risk if wrong\", "
    "\"what_would_change_my_mind\": [\"...\"], "
    "\"invalidation\": \"the level/condition that voids the thesis\", "
    "\"direction\": \"LONG|SHORT|FLAT\", \"confidence\": 0.0-1.0, "
    "\"thesis_confidence\": 0.0-1.0, \"opportunity_confidence\": 0.0-1.0, "
    "\"timing_confidence\": 0.0-1.0, \"execution_confidence\": 0.0-1.0, "
    "\"rationale\": \"one or two sentences tying it together\"}\n"
    "FLAT means the evidence does not support acting. 'confidence' is your "
    "calibrated probability that the stated direction is correct; it is NOT a "
    "substitute for the reasoning above. Decompose it into: thesis_confidence "
    "(is your read correct?), opportunity_confidence (is there a real exploitable "
    "edge?), timing_confidence (is NOW the moment, or is it early?) and "
    "execution_confidence (can it be realised after spread/slippage/liquidity?). "
    "If unsure, set each to your overall 'confidence'. A strong thesis with weak "
    "timing or execution is NOT an act-now trade — say so via these fields. You "
    "may answer FLAT with an opportunity of 'none' and that is a valid, complete "
    "cognitive outcome."
)


# Part XXV Art 11 / Q40 — MANAGEMENT reasoning is a DIFFERENT question from
# origination. Origination asks "is there an opportunity, and in which
# direction?". Management asks "given everything that has happened since this
# campaign was opened, what should be done with THIS campaign now?". The model
# is told the position it already holds (direction, entry, P&L, duration and the
# original thesis) and is asked to EVALUATE THE THESIS — never to re-derive a
# direction. A fresh FLAT read, low confidence, conflicting evidence or a
# moment of uncertainty is NOT a reason to liquidate a live campaign; only a
# reasoned determination that the thesis is invalidated, has materially
# deteriorated beyond its risk/EV boundary, has been superseded by a better
# opportunity, or must be terminated for an independent risk/execution
# constraint justifies an exit.
_MANAGEMENT_SYSTEM_PROMPT = (
    "You are the Cognitive Brain of an autonomous trading intelligence, MANAGING "
    "AN EXISTING OPEN CAMPAIGN — not scanning for a new one. The payload includes "
    "a 'campaign' object: the side already held (LONG or SHORT), the entry "
    "context, the current profit/loss (in R multiples where available), how long "
    "the position has been open, and the ORIGINAL thesis and invalidation "
    "conditions recorded when it was opened. It also includes the SAME structured "
    "representation of current market reality you receive at origination "
    "(multi-timeframe price picture, tick tape, order-book depth, session, "
    "analytical readings as measurements). Do not invent data you were not given.\n\n"
    "Your question is NOT 'what direction do I see now?' — it is: 'given "
    "everything that has happened since this campaign was opened, what should be "
    "done with THIS campaign now?'. Re-examine the ORIGINAL thesis against current "
    "reality and decide whether the opportunity that justified the position is: "
    "intact, evolving, temporarily obscured, deteriorating, invalidated, or "
    "replaced by a superior opportunity.\n\n"
    "CRITICAL — an open campaign is NEVER closed merely because the latest read is "
    "FLAT, low-confidence, conflicting, or temporarily uncertain. Temporary "
    "uncertainty is the normal texture of a live trade, not a reason to "
    "liquidate. An EXIT requires a REASONED determination that the existing "
    "opportunity has been (a) invalidated, (b) materially deteriorated beyond its "
    "acceptable risk/expected-value boundary, (c) superseded by a demonstrably "
    "superior opportunity, or (d) must be terminated for an independent "
    "risk/execution constraint. If none of those hold, the correct action is to "
    "HOLD (or to reduce risk while the picture clarifies) — not to exit. Judge "
    "LONG and SHORT campaigns with perfect symmetry: apply the identical standard "
    "to a held long and a held short; there is no directional bias.\n\n"
    "Choose ONE management action:\n"
    "  HOLD — the thesis still holds (or is only temporarily obscured); keep the "
    "position as-is.\n"
    "  TIGHTEN_RISK — the thesis is weakening but not dead; reduce risk (tighten "
    "the stop) while staying in.\n"
    "  PROTECT_PROFIT — the position is in profit and the thesis is maturing; move "
    "the stop to protect gains.\n"
    "  SCALE_IN — the thesis is strengthening and conditions justify adding "
    "(only if genuinely warranted).\n"
    "  SCALE_OUT — bank part of the position (partial de-risk) while keeping "
    "core exposure.\n"
    "  EXIT — the thesis is invalidated / opportunity gone / EV now negative / an "
    "independent risk constraint forces closure.\n"
    "  REVERSE — the evidence now supports the OPPOSITE side with very high "
    "conviction (rare; requires strong, reasoned contrary evidence).\n\n"
    "Respond with STRICT JSON only, no prose, no markdown fences, exactly these "
    "keys:\n"
    "{\"thesis_state\": \"THESIS_INTACT|THESIS_WEAKENING|THESIS_INVALIDATED|"
    "OPPORTUNITY_EVOLVED\", "
    "\"opportunity_status\": \"intact|evolving|temporarily_obscured|deteriorating|"
    "invalidated|replaced\", "
    "\"primary_hypothesis\": \"what is happening to the campaign's thesis now\", "
    "\"alternative_hypotheses\": [\"competing reading(s)\"], "
    "\"supporting_evidence\": [\"what still supports the held side\"], "
    "\"contradicting_evidence\": [\"what now argues against it\"], "
    "\"key_uncertainty\": \"the main thing you are unsure of\", "
    "\"missing_information\": [\"...\"], "
    "\"expected_value\": \"net of costs, for CONTINUING to hold: positive|negative|unclear\", "
    "\"risk\": \"the risk of continuing to hold\", "
    "\"what_would_change_my_mind\": [\"...\"], "
    "\"invalidation\": \"the condition that proves the campaign thesis dead\", "
    "\"action\": \"HOLD|TIGHTEN_RISK|PROTECT_PROFIT|SCALE_IN|SCALE_OUT|EXIT|REVERSE\", "
    "\"confidence\": 0.0-1.0, "
    "\"thesis_confidence\": 0.0-1.0, \"opportunity_confidence\": 0.0-1.0, "
    "\"timing_confidence\": 0.0-1.0, \"execution_confidence\": 0.0-1.0, "
    "\"rationale\": \"one or two sentences tying it together\"}\n"
    "'confidence' is your calibrated probability that the chosen action is the "
    "correct thing to do with this campaign right now. Decompose it into "
    "thesis_confidence (is the original read still correct?), "
    "opportunity_confidence (does a real exploitable edge still exist?), "
    "timing_confidence (is now the moment to take this action?) and "
    "execution_confidence (can it be realised after spread/slippage/liquidity?). "
    "If unsure, default to HOLD with thesis_state THESIS_INTACT — uncertainty "
    "resolves toward holding, never toward liquidation."
)


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return min(1.0, max(0.0, f))


def _norm_dir(d: Any) -> str:
    s = str(d or "").strip().upper()
    return s if s in (LONG, SHORT, FLAT) else FLAT


_DIR_MAP = {
    "LONG": LONG, "BUY": LONG, "BULLISH": LONG, "UP": LONG,
    "SHORT": SHORT, "SELL": SHORT, "BEARISH": SHORT, "DOWN": SHORT,
    "FLAT": FLAT, "HOLD": FLAT, "NEUTRAL": FLAT, "NONE": FLAT, "WAIT": FLAT,
}
_DIR_RE = re.compile(r"""(?i)["']?\bdirection\b["']?\s*[:=]\s*["']?([A-Za-z]+)""")
_CONF_RE = re.compile(r"""(?i)["']?\bconfidence\b["']?\s*[:=]\s*([0-9]*\.?[0-9]+)""")
_RATIONALE_RE = re.compile(r"""(?i)["']?\brationale\b["']?\s*[:=]\s*["']([^"']*)""")
_TOKEN_RE = re.compile(r"\b(LONG|SHORT|FLAT)\b")

# Max chars of the serialised evidence payload handed to a reasoner. The council's
# per-advisor REASONING evidence is appended LAST in the MarketState, so a tight
# cap truncates exactly the advisors' full analysis off the tail — the collapse we
# are removing (Part XXV: the Brain must see each advisor's complete cognition, not
# a summary sentence). Sized for a full panel of rich advisor theses plus the live
# market view; hosted high-context models handle it comfortably.
_MAX_USER_PROMPT_CHARS = 60000


def _map_dir(s: Any) -> str:
    return _DIR_MAP.get(str(s or "").strip().upper(), FLAT)


# Part XXV Art 11 — MANAGEMENT vocabulary. A management reply does NOT carry a
# trade direction; it carries a THESIS STATE and a MANAGEMENT ACTION. These maps
# normalise the model's (possibly synonymous) words into the canonical action /
# thesis / opportunity tokens the Brain's management decision tree understands.
# An unrecognised value maps to "" (empty) so the Brain lawfully falls back to
# HOLD rather than inventing a state-changing action. NOTE: "HOLD" here means
# KEEP THE POSITION — it is NEVER mapped to FLAT (the origination collapse that
# turned "hold" into an exit is precisely what management must not do).
_MGMT_ACTION_MAP = {
    "HOLD": "HOLD", "KEEP": "HOLD", "MAINTAIN": "HOLD", "STAY": "HOLD",
    "CONTINUE": "HOLD", "DO_NOTHING": "HOLD", "NOTHING": "HOLD", "WAIT": "HOLD",
    "NONE": "HOLD",
    "TIGHTEN_RISK": "TIGHTEN_RISK", "TIGHTEN": "TIGHTEN_RISK",
    "REDUCE_RISK": "TIGHTEN_RISK", "TIGHTEN_STOP": "TIGHTEN_RISK",
    "PROTECT_PROFIT": "PROTECT_PROFIT", "PROTECT": "PROTECT_PROFIT",
    "LOCK_IN": "PROTECT_PROFIT", "LOCK_PROFIT": "PROTECT_PROFIT",
    "SECURE": "PROTECT_PROFIT", "SECURE_PROFIT": "PROTECT_PROFIT",
    "SCALE_IN": "SCALE_IN", "ADD": "SCALE_IN", "PYRAMID": "SCALE_IN",
    "INCREASE": "SCALE_IN", "ADD_TO_WINNER": "SCALE_IN",
    "SCALE_OUT": "SCALE_OUT", "TRIM": "SCALE_OUT", "PARTIAL": "SCALE_OUT",
    "PARTIAL_CLOSE": "SCALE_OUT", "REDUCE": "SCALE_OUT", "TAKE_PARTIAL": "SCALE_OUT",
    "EXIT": "EXIT", "CLOSE": "EXIT", "LIQUIDATE": "EXIT", "TERMINATE": "EXIT",
    "SELL_ALL": "EXIT", "CLOSE_ALL": "EXIT",
    "REVERSE": "REVERSE", "FLIP": "REVERSE",
}
_MGMT_THESIS_MAP = {
    "THESIS_INTACT": "intact", "INTACT": "intact", "VALID": "intact",
    "CONFIRMED": "intact", "HOLDING": "intact",
    "THESIS_STRENGTHENING": "strengthening", "STRENGTHENING": "strengthening",
    "THESIS_WEAKENING": "weakening", "WEAKENING": "weakening",
    "DETERIORATING": "weakening", "SOFTENING": "weakening",
    "THESIS_INVALIDATED": "invalidated", "INVALIDATED": "invalidated",
    "VOID": "invalidated", "BROKEN": "invalidated", "DEAD": "invalidated",
    "OPPORTUNITY_EVOLVED": "evolved", "EVOLVED": "evolved",
    "SUPERSEDED": "evolved", "REPLACED": "evolved",
}
_MGMT_OPP_MAP = {
    "INTACT": "intact", "EVOLVING": "evolving",
    "OBSCURED": "obscured", "TEMPORARILY_OBSCURED": "obscured",
    "DETERIORATING": "deteriorating", "DECAYING": "deteriorating",
    "INVALIDATED": "invalidated", "GONE": "invalidated", "EXPIRED": "invalidated",
    "REPLACED": "replaced", "SUPERSEDED": "replaced",
}


def _map_mgmt_action(s: Any) -> str:
    return _MGMT_ACTION_MAP.get(str(s or "").strip().upper().replace(" ", "_"), "")


def _map_mgmt_thesis(s: Any) -> str:
    return _MGMT_THESIS_MAP.get(str(s or "").strip().upper().replace(" ", "_"), "")


def _map_mgmt_opportunity(s: Any) -> str:
    return _MGMT_OPP_MAP.get(str(s or "").strip().upper().replace(" ", "_"), "")


# Part XXV / Violation V2 — the rich cognitive fields that make a reply a valid
# opinion even when it carries NO ``direction``. A missing direction is itself a
# cognitive result ("I understand the market but see no directional edge"), so a
# reply carrying any of these must be kept and parsed (direction ⇒ FLAT), never
# discarded. Only a reply with NONE of these (and no direction) is unparseable.
_OPINION_FIELD_KEYS = frozenset({
    "direction", "confidence", "regime", "primary_hypothesis",
    "alternative_hypotheses", "supporting_evidence", "contradicting_evidence",
    "key_uncertainty", "missing_information", "invalidation", "opportunity",
    "opportunity_horizon", "expected_value", "expected_favorable_excursion",
    "expected_adverse_excursion", "execution_quality", "risk",
    "what_would_change_my_mind", "competing_hypotheses", "rationale",
    "thesis_confidence", "opportunity_confidence", "timing_confidence",
    "execution_confidence",
    # Part XXV Art 11 — management-only fields. A management reply may carry NO
    # direction at all (it answers with a thesis state + action instead), so
    # these must count as recognisable opinion fields or the whole reply is
    # dropped as unparseable.
    "thesis_state", "management_action", "action", "opportunity_status",
})


def _has_opinion_fields(obj: Any) -> bool:
    """True when ``obj`` is a dict carrying at least one recognisable opinion
    field — with or without ``direction`` (Violation V2)."""
    return isinstance(obj, dict) and any(k in obj for k in _OPINION_FIELD_KEYS)


def _regex_opinion_fields(reply: str) -> Optional[dict]:
    """Last-resort extraction of the opinion fields from unparseable text.

    Pulls ``direction`` / ``confidence`` / ``rationale`` out of a reply that no
    JSON layer could recover (e.g. truncated before the value, or prose). Only
    commits to a direction it can defend: an explicit ``direction:`` field, or —
    failing that — a single unambiguous LONG/SHORT/FLAT token. A directional
    read with no recoverable confidence defaults to 0.0 (weak/observe) rather
    than inventing conviction. Returns ``None`` when no direction is present.
    """
    text = str(reply or "")
    m = _DIR_RE.search(text)
    if m:
        direction = _map_dir(m.group(1))
    else:
        tokens = set(_TOKEN_RE.findall(text))
        if len(tokens) != 1:
            return None
        direction = _map_dir(next(iter(tokens)))
    cm = _CONF_RE.search(text)
    rm = _RATIONALE_RE.search(text)
    return {
        "direction": direction,
        "confidence": cm.group(1) if cm else 0.0,
        "rationale": rm.group(1) if rm else "",
    }


def _extract_opinion_fields(reply: str) -> Optional[dict]:
    """Recover the opinion object from a model reply, robustly (Part XXIII).

    Layered so a fenced/prose-wrapped/truncated reply still yields the Brain's
    decision instead of being discarded: (1) lenient JSON recovery
    (:func:`llm.json_repair.repair_json` — fence strip, strict, balanced scan,
    structural repair of a missing brace / open string / trailing comma); then
    (2) regex field extraction as a final fallback. Never raises.
    """
    try:
        obj = repair_json(reply)
        if _has_opinion_fields(obj):
            return obj
        if isinstance(obj, list):
            for x in obj:
                if _has_opinion_fields(x):
                    return x
        return _regex_opinion_fields(reply)
    except Exception:  # noqa: BLE001 — recovery must never raise
        return None


def _reply_was_strict_json(reply: str) -> bool:
    """True when the raw reply is already valid JSON carrying opinion fields (no
    repair needed) — used only for observability (recovered-vs-clean counting)."""
    try:
        raw = json.loads(str(reply or ""))
        return _has_opinion_fields(raw)
    except Exception:  # noqa: BLE001
        return False


@dataclass
class LLMOpinion:
    """The structured cognitive state the Brain returns — emitted as evidence.

    Per Part XXV the state is richer than a direction+confidence: it carries the
    Brain's regime read, a primary hypothesis and alternatives, the supporting
    and contradicting evidence it weighed, its key uncertainty, the opportunity
    and its horizon, expected favourable/adverse excursion and net expected
    value, the execution read, the risk, and what would change its mind.
    ``direction``/``confidence`` are the *consequence* of that reasoning — the
    execution instruction — never a substitute for it. Every rich field defaults
    empty so a legacy minimal reply still parses.
    """

    symbol: str
    direction: str                 # LONG | SHORT | FLAT (execution consequence)
    confidence: float              # 0..1 (overall — the execution consequence)
    rationale: str = ""
    competing_hypotheses: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    # Part XXV — multidimensional confidence. Each is None when the model returned
    # only the overall scalar; it then resolves to ``confidence`` downstream, so a
    # legacy single-confidence reply is unchanged (zero behaviour change).
    thesis_confidence: Optional[float] = None
    opportunity_confidence: Optional[float] = None
    timing_confidence: Optional[float] = None
    execution_confidence: Optional[float] = None
    # Part XXV — non-collapsed cognitive state.
    regime: str = ""
    primary_hypothesis: str = ""
    alternative_hypotheses: list[str] = field(default_factory=list)
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    key_uncertainty: str = ""
    invalidation: str = ""
    opportunity: str = ""
    opportunity_horizon: str = ""
    expected_value: str = ""
    expected_favorable_excursion: str = ""
    expected_adverse_excursion: str = ""
    execution_quality: str = ""
    risk: str = ""
    what_would_change_my_mind: list[str] = field(default_factory=list)
    # Part XXV Art 11 — MANAGEMENT state. Populated only when the opinion came
    # from the management prompt (:meth:`reason_management`); empty on an
    # origination opinion. ``management_action`` is the canonical action
    # (HOLD / TIGHTEN_RISK / PROTECT_PROFIT / SCALE_IN / SCALE_OUT / EXIT /
    # REVERSE); ``thesis_state`` is intact / strengthening / weakening /
    # invalidated / evolved; ``opportunity_status`` is intact / evolving /
    # obscured / deteriorating / invalidated / replaced. Their presence is how
    # the Brain tells a thesis-based management opinion from a legacy
    # direction-based one.
    thesis_state: str = ""
    management_action: str = ""
    opportunity_status: str = ""
    at_iso: str = ""
    model: str = ""

    def to_dict(self) -> dict:
        def _rc(v):
            try:
                return round(self.confidence if v is None else min(1.0, max(0.0, float(v))), 4)
            except (TypeError, ValueError):
                return round(self.confidence, 4)
        thesis_c = _rc(self.thesis_confidence)
        opp_c = _rc(self.opportunity_confidence)
        timing_c = _rc(self.timing_confidence)
        exec_c = _rc(self.execution_confidence)
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "thesis_confidence": thesis_c,
            "opportunity_confidence": opp_c,
            "timing_confidence": timing_c,
            "execution_confidence": exec_c,
            "effective_confidence": round(min(thesis_c, opp_c, timing_c, exec_c), 4),
            "rationale": self.rationale,
            "competing_hypotheses": list(self.competing_hypotheses),
            "missing_information": list(self.missing_information),
            "regime": self.regime,
            "primary_hypothesis": self.primary_hypothesis,
            "alternative_hypotheses": list(self.alternative_hypotheses),
            "supporting_evidence": list(self.supporting_evidence),
            "contradicting_evidence": list(self.contradicting_evidence),
            "key_uncertainty": self.key_uncertainty,
            "invalidation": self.invalidation,
            "opportunity": self.opportunity,
            "opportunity_horizon": self.opportunity_horizon,
            "expected_value": self.expected_value,
            "expected_favorable_excursion": self.expected_favorable_excursion,
            "expected_adverse_excursion": self.expected_adverse_excursion,
            "execution_quality": self.execution_quality,
            "risk": self.risk,
            "what_would_change_my_mind": list(self.what_would_change_my_mind),
            "thesis_state": self.thesis_state,
            "management_action": self.management_action,
            "opportunity_status": self.opportunity_status,
            "at": self.at_iso,
            "model": self.model,
        }

    def as_evidence(self, *, weight: float = 1.0, module: str = "llm_reasoner") -> dict:
        """Vote-like evidence object for the consensus panel (future promotion).

        Shaped like the attributes the panel reads (``module`` / ``direction`` /
        ``confidence`` / ``weight``). It is *evidence*, subject to the same
        performance-based authority and physics vetoes as any module — never an
        override.
        """
        return {
            "module": module,
            "direction": self.direction,
            "confidence": _clamp01(self.confidence),
            "weight": max(0.0, float(weight)),
            "source": "llm",
            "rationale": self.rationale,
        }


class LLMReasoner:
    """Drives the LLM over structured evidence and parses a structured opinion."""

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        enabled: bool = False,
        drive_decisions: bool = False,
        min_interval_seconds: float = 30.0,
        recent_limit: int = 50,
        default_compute_class: str = "",
        default_min_context: int = 0,
    ) -> None:
        self._client = client
        self.enabled = bool(enabled)
        self.drive_decisions = bool(drive_decisions)
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._recent_limit = max(1, int(recent_limit))
        # Compute-class routing (GPU/Compute Constitution §8): the reasoning
        # CLASS this reasoner requests from a class-aware client (ModelManager).
        # "" ⇒ request no class (every model eligible). The Brain's strategic
        # reasoner sets "deep"; a class-tagged model only serves matching
        # requests, but an untagged roster keeps serving everything (no-op).
        self.default_compute_class = str(default_compute_class or "").strip().lower()
        self.default_min_context = max(0, int(default_min_context or 0))
        self._last_call: dict[str, float] = {}
        # Per-symbol liveness of the last NON-throttled reason() attempt:
        # True = produced an opinion, False = failed (provider down/timeout, or
        # an unparsable reply). Lets the Brain tell an infrastructure failure
        # (REASONER_UNAVAILABLE) apart from a genuine market observe (Q78/Q80).
        self._last_reason_ok: dict[str, bool] = {}
        self._recent: list[LLMOpinion] = []
        self._calls = 0
        self._faults = 0
        self._recovered = 0   # opinions recovered from non-strict/truncated JSON
        # Part XX (provider health) — consecutive-failure backoff. A provider
        # that keeps failing is not retried every cycle: each failure lengthens
        # an exponential cooldown (capped at 5 minutes) during which reason()
        # skips the call outright; a single success resets it. Prevents wasting
        # provider budget and flooding logs on a persistently down provider.
        self._consecutive_failures = 0
        self._backoff_until = 0.0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """True when enabled and a usable client is wired."""
        return bool(self.enabled and self._client is not None
                    and getattr(self._client, "usable", False))

    @property
    def client(self) -> Optional[Any]:
        """The underlying completion client (read-only accessor).

        Exposes the client so the background recovery prober can issue a
        direct liveness probe and read ``last_fail_signature`` without
        reaching into private state.
        """
        return self._client

    def last_reason_degraded(self, symbol: str) -> bool:
        """True when the last NON-throttled ``reason(symbol)`` FAILED to yield an
        opinion — provider down/timeout, or an unparsable reply.

        This is a *liveness* signal, distinct from the static :pyattr:`available`
        config flag, so the Brain can tell an INFRASTRUCTURE failure
        (REASONER_UNAVAILABLE) apart from a genuine market ``observe`` when
        ``reason`` returns ``None`` (Part XVIII Art 5; Q78/Q80/Q81/Q106). A
        throttled cycle leaves this state unchanged.
        """
        with self._lock:
            return self._last_reason_ok.get(str(symbol or "")) is False

    def _throttled(self, key: str, now: float, interval: Optional[float] = None) -> bool:
        iv = self.min_interval_seconds if interval is None else max(0.0, float(interval))
        if iv <= 0:
            return False
        last = self._last_call.get(key, 0.0)
        return (now - last) < iv

    def _note_failure(self, now: float) -> None:
        """Record a provider failure and extend the exponential backoff window.

        Backoff = min(300, 2 ** min(consecutive_failures, 8)) seconds — capped at
        5 minutes. Must be called while holding ``self._lock``.
        """
        self._consecutive_failures += 1
        self._backoff_until = now + min(300.0, 2.0 ** min(self._consecutive_failures, 8))

    def _note_success(self, now: float) -> None:
        """Clear the failure counter + backoff after a healthy call. Lock held."""
        self._consecutive_failures = 0
        self._backoff_until = 0.0

    def _complete(self, system: str, user: str) -> Optional[str]:
        """Call the client, requesting a compute class / min context when the
        client is class-aware (the ModelManager). Falls back to the plain
        two-arg call for a simple client that does not accept the kwargs."""
        client = self._client
        if not self.default_compute_class and not self.default_min_context:
            return client.complete(system, user)
        try:
            return client.complete(
                system, user,
                compute_class=(self.default_compute_class or None),
                min_context=(self.default_min_context or None),
            )
        except TypeError:
            return client.complete(system, user)

    def reason(
        self, symbol: str, evidence: dict, *, now: Optional[float] = None,
        min_interval: Optional[float] = None, throttle_key: Optional[str] = None,
    ) -> Optional[LLMOpinion]:
        """Ask the model to reason over ``evidence`` for ``symbol`` (ORIGINATION).

        Returns a parsed :class:`LLMOpinion`, or ``None`` when unavailable,
        throttled, or on any fault. Blocking (provider round-trip) — call from a
        background/periodic path, never the hot loop. Fail-safe.

        ``min_interval`` overrides the per-symbol throttle window for THIS call
        and ``throttle_key`` overrides the throttle bucket, so a caller can run a
        tighter cadence on an independent bucket (e.g. management re-reasoning an
        open position faster than origination) without changing the global rate.
        """
        return self._reason_impl(
            symbol, evidence, now=now, min_interval=min_interval,
            throttle_key=throttle_key, management=False,
        )

    def reason_management(
        self, symbol: str, evidence: dict, *, now: Optional[float] = None,
        min_interval: Optional[float] = None, throttle_key: Optional[str] = None,
    ) -> Optional[LLMOpinion]:
        """Re-reason an OPEN campaign (MANAGEMENT — Part XXV Art 11 / Q40).

        Identical machinery to :meth:`reason` (same throttle/backoff/recovery)
        but driven by :data:`_MANAGEMENT_SYSTEM_PROMPT` and parsed by
        :meth:`_parse_management`, so the reply carries a thesis state + a
        management action rather than a fresh trade direction. ``evidence`` is
        expected to include the campaign context (held side, entry, P&L,
        original thesis) so the model evaluates the EXISTING position, not the
        market in the abstract. Fail-safe.
        """
        return self._reason_impl(
            symbol, evidence, now=now, min_interval=min_interval,
            throttle_key=throttle_key, management=True,
        )

    def _reason_impl(
        self, symbol: str, evidence: dict, *, now: Optional[float],
        min_interval: Optional[float], throttle_key: Optional[str],
        management: bool,
    ) -> Optional[LLMOpinion]:
        """Shared origination/management reasoning body. Fail-safe."""
        if not self.available:
            return None
        t = time.time() if now is None else float(now)
        sym = str(symbol or "")
        key = str(throttle_key or sym)
        iv = self.min_interval_seconds if min_interval is None else max(0.0, float(min_interval))
        try:
            with self._lock:
                if t < self._backoff_until:
                    logger.debug(
                        "[llm] {} backing off {:.1f}s after {} consecutive failure(s) "
                        "— skipping call",
                        sym, max(0.0, self._backoff_until - t), self._consecutive_failures,
                    )
                    return None
                if self._throttled(key, t, iv):
                    logger.debug(
                        "[llm] {} throttled ({}s min interval) — no fresh model call this cycle",
                        sym, iv,
                    )
                    return None
                self._last_call[key] = t
            user = self._build_user_prompt(sym, evidence)
            system = _MANAGEMENT_SYSTEM_PROMPT if management else _SYSTEM_PROMPT
            reply = self._complete(system, user)
            if not reply:
                with self._lock:
                    self._faults += 1
                    self._last_reason_ok[sym] = False
                    self._note_failure(t)
                # No reply this cycle (throttle/quota/offline). The client logs
                # the reason once on its down transition and the council panel
                # summarises who is absent, so keep this per-symbol line at DEBUG
                # to avoid flooding when an advisor is persistently unavailable.
                logger.debug("[llm] {} — no reply from model (fail-safe: no opinion)", sym)
                return None
            opinion = self._parse_management(sym, reply) if management else self._parse(sym, reply)
            with self._lock:
                self._calls += 1
                if opinion is not None:
                    self._recent.append(opinion)
                    if len(self._recent) > self._recent_limit:
                        self._recent = self._recent[-self._recent_limit:]
                    self._last_reason_ok[sym] = True
                    self._note_success(t)
                else:
                    self._faults += 1
                    self._last_reason_ok[sym] = False
                    self._note_failure(t)
            if opinion is not None:
                if management:
                    logger.info(
                        "[llm] {} [{}] MANAGE thesis={} action={} conf={} — {}",
                        sym,
                        str(getattr(opinion, "model", "") or getattr(self._client, "model", "") or "?"),
                        str(getattr(opinion, "thesis_state", "") or "?"),
                        str(getattr(opinion, "management_action", "") or "?"),
                        round(float(getattr(opinion, "confidence", 0.0) or 0.0), 3),
                        str(getattr(opinion, "rationale", "") or "")[:140],
                    )
                else:
                    logger.info(
                        "[llm] {} [{}] dir={} conf={} | regime={} opp={} ({}) — {}",
                        sym,
                        str(getattr(opinion, "model", "") or getattr(self._client, "model", "") or "?"),
                        getattr(opinion, "direction", "?"),
                        round(float(getattr(opinion, "confidence", 0.0) or 0.0), 3),
                        str(getattr(opinion, "regime", "") or "?"),
                        str(getattr(opinion, "opportunity", "") or "n/a")[:60],
                        str(getattr(opinion, "opportunity_horizon", "") or "?"),
                        str(getattr(opinion, "rationale", "") or "")[:140],
                    )
            else:
                # 2xx reply that did not parse into a usable opinion — the
                # single most common "why is the Brain idle?" cause. Surface the
                # raw reply (key-free) so the prompt/format can be corrected.
                logger.warning(
                    "[llm] {} model replied but NO opinion parsed (fail-safe: observe) — reply[:220]={}",
                    sym, str(reply)[:220],
                )
            return opinion
        except Exception as exc:  # noqa: BLE001 — reasoning must never break a cycle
            logger.debug("[llm] reason({}) ignored a fault: {}", symbol, exc)
            with self._lock:
                self._faults += 1
                self._last_reason_ok[str(symbol or "")] = False
                self._note_failure(t)
            return None

    @staticmethod
    def _build_user_prompt(symbol: str, evidence: dict) -> str:
        """Serialise the structured evidence into a compact JSON user prompt."""
        payload = {"symbol": symbol, "evidence": evidence or {}}
        try:
            # Larger cap so the reconstructed multi-timeframe price snapshot
            # (Part XIX Art 2 — the chart) reaches the model alongside the
            # analytical reads rather than being truncated away.
            return json.dumps(payload, default=str)[:_MAX_USER_PROMPT_CHARS]
        except Exception:  # noqa: BLE001
            return json.dumps({"symbol": symbol})

    def _parse(self, symbol: str, reply: str) -> Optional[LLMOpinion]:
        fields = _extract_opinion_fields(reply)
        # Violation V2 — keep the reply whenever it carries ANY recognisable
        # cognitive field, even without ``direction``. A missing direction is a
        # legitimate cognitive result ("I understand the market but see no
        # directional edge") and resolves to FLAT below — discarding the whole
        # rich response (regime, hypotheses, uncertainty, invalidation, …) would
        # throw away valuable reasoning. Only a truly unparseable reply (no JSON,
        # no recognisable field at all) is dropped.
        if not _has_opinion_fields(fields):
            return None
        # Observability: note when the Brain's opinion had to be *recovered* from
        # a fenced / truncated / prose reply rather than clean JSON (Part XXIII).
        if not _reply_was_strict_json(reply):
            with self._lock:
                self._recovered += 1
        return self._build_opinion(symbol, fields)

    def _parse_management(self, symbol: str, reply: str) -> Optional[LLMOpinion]:
        """Parse a MANAGEMENT reply (Part XXV Art 11).

        A management reply answers with a THESIS STATE and a MANAGEMENT ACTION,
        not a trade direction. It is parsed with the SAME robust recovery as
        origination (:func:`_extract_opinion_fields`) but the collapse that maps
        ``HOLD``/``NEUTRAL``/``WAIT`` → FLAT is DELIBERATELY NOT applied: here
        ``HOLD`` means *keep the position*, and a missing/uncertain action
        resolves to HOLD downstream, never to an exit. Never raises.
        """
        fields = _extract_opinion_fields(reply)
        if not _has_opinion_fields(fields):
            return None
        if not _reply_was_strict_json(reply):
            with self._lock:
                self._recovered += 1
        return self._build_opinion(symbol, fields, management=True)

    def _build_opinion(
        self, symbol: str, fields: dict, *, management: bool = False,
    ) -> LLMOpinion:
        """Construct an :class:`LLMOpinion` from an extracted fields dict.

        Shared by origination (:meth:`_parse`) and management
        (:meth:`_parse_management`). The rich cognitive fields are parsed
        identically; ``management`` only governs the management-specific
        fields (thesis_state / management_action / opportunity_status) and
        ensures a management reply is NEVER forced into an exit via the
        direction map.
        """
        ch = fields.get("competing_hypotheses") or []
        mi = fields.get("missing_information") or []
        alt = fields.get("alternative_hypotheses") or []
        wcm = fields.get("what_would_change_my_mind") or []
        sup = fields.get("supporting_evidence") or []
        con = fields.get("contradicting_evidence") or []

        def _list(x, cap_items=5, cap_chars=200):
            return [str(i)[:cap_chars] for i in x][:cap_items] if isinstance(x, list) else []

        def _txt(key, cap=300):
            return str(fields.get(key, "") or "")[:cap]

        def _conf(key):
            v = fields.get(key)
            if v is None:
                return None
            try:
                return min(1.0, max(0.0, float(v)))
            except (TypeError, ValueError):
                return None

        # Backward-compat: a legacy reply carries only competing_hypotheses /
        # missing_information — keep populating them, and cross-fill the Part XXV
        # fields so neither representation is empty when only one was returned.
        alt_l = _list(alt)
        ch_l = _list(ch) or alt_l
        wcm_l = _list(wcm)
        # Part XXV Art 11 — management state. Only populated for a management
        # reply; an origination reply leaves these empty (so the Brain can tell
        # the two apart). A management reply intentionally carries NO trade
        # direction, so ``direction`` stays FLAT and is never used to decide an
        # exit; the management action drives the decision instead.
        mgmt_action = _map_mgmt_action(
            fields.get("management_action") or fields.get("action")
        ) if management else ""
        thesis_state = _map_mgmt_thesis(fields.get("thesis_state")) if management else ""
        opportunity_status = _map_mgmt_opportunity(
            fields.get("opportunity_status") or fields.get("opportunity_state")
        ) if management else ""
        return LLMOpinion(
            symbol=symbol,
            direction=_norm_dir(fields.get("direction")),
            confidence=_clamp01(fields.get("confidence")),
            rationale=str(fields.get("rationale", ""))[:500],
            competing_hypotheses=ch_l,
            missing_information=_list(mi),
            thesis_confidence=_conf("thesis_confidence"),
            opportunity_confidence=_conf("opportunity_confidence"),
            timing_confidence=_conf("timing_confidence"),
            execution_confidence=_conf("execution_confidence"),
            regime=_txt("regime", 48),
            primary_hypothesis=_txt("primary_hypothesis"),
            alternative_hypotheses=alt_l or ch_l,
            supporting_evidence=_list(sup),
            contradicting_evidence=_list(con),
            key_uncertainty=_txt("key_uncertainty"),
            invalidation=_txt("invalidation"),
            opportunity=_txt("opportunity"),
            opportunity_horizon=_txt("opportunity_horizon", 48),
            expected_value=_txt("expected_value", 120),
            expected_favorable_excursion=_txt("expected_favorable_excursion", 120),
            expected_adverse_excursion=_txt("expected_adverse_excursion", 120),
            execution_quality=_txt("execution_quality", 160),
            risk=_txt("risk"),
            what_would_change_my_mind=wcm_l,
            thesis_state=thesis_state,
            management_action=mgmt_action,
            opportunity_status=opportunity_status,
            at_iso=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            model=str(getattr(self._client, "model", "") or ""),
        )

    def latest_opinion(self, symbol: str) -> Optional[LLMOpinion]:
        """Most recent recorded opinion for ``symbol`` (or None). Fail-safe.

        The panel-feeding path (when ``drive_decisions`` is enabled) reads this
        to pull the latest LLM read for a symbol without triggering a call.
        """
        try:
            sym = str(symbol or "")
            with self._lock:
                for op in reversed(self._recent):
                    if op.symbol == sym:
                        return op
            return None
        except Exception:  # noqa: BLE001
            return None

    def get_status(self) -> dict:
        """Secret-safe status for the dashboard / governance."""
        try:
            with self._lock:
                recent = [o.to_dict() for o in self._recent[-10:]]
                calls, faults = self._calls, self._faults
                recovered = self._recovered
                consecutive_failures = self._consecutive_failures
                backoff_remaining = max(0.0, self._backoff_until - time.time())
            client_desc = None
            if self._client is not None and hasattr(self._client, "describe"):
                try:
                    client_desc = self._client.describe()
                except Exception:  # noqa: BLE001
                    client_desc = None
            return {
                "enabled": self.enabled,
                "available": self.available,
                "drive_decisions": self.drive_decisions,
                "min_interval_seconds": self.min_interval_seconds,
                "calls": calls,
                "faults": faults,
                "recovered": recovered,
                "consecutive_failures": consecutive_failures,
                "backoff_seconds_remaining": round(backoff_remaining, 3),
                "client": client_desc,
                "recent_opinions": recent,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[llm] get_status ignored a fault: {}", exc)
            return {"enabled": self.enabled, "available": False}


__all__ = ["LLMReasoner", "LLMOpinion", "LONG", "SHORT", "FLAT"]
