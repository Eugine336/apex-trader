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
    "You are the Cognitive Brain of an autonomous trading intelligence, and you "
    "are an OPPORTUNITY HARVESTER — not a direction predictor. You receive a "
    "STRUCTURED REPRESENTATION OF MARKET REALITY for one instrument (a "
    "reconstructed multi-timeframe price picture — OHLC candles, current price "
    "and spread — the tick tape: velocity, drift, up/down balance, momentum, "
    "spread behaviour; order-book depth when available; the session; a higher-"
    "vs-lower-timeframe pullback read; and per-instrument analytical READINGS "
    "expressed as signed, bounded measurements). Those readings are instrument "
    "outputs, like a thermometer's temperature — NEVER decisions or votes to "
    "count. Do not invent data you were not given.\n\n"
    "CORE PRINCIPLE — the market simultaneously contains MULTIPLE opportunities "
    "across horizons. A bullish higher timeframe with a bearish lower timeframe "
    "is NOT 'FLAT': it is a market containing a SHORT correction opportunity AND "
    "a future LONG reversal opportunity — TWO conditional opportunities. Your job "
    "is to HARVEST every exploitable asymmetry you can see, rank them, and prefer "
    "the best — not to collapse the market to one averaged direction.\n\n"
    "TIMEFRAME ROLES — HTF = context, MTF = opportunity structure, LTF = "
    "execution. Higher timeframes provide CONTEXT; they NEVER veto a lower-"
    "timeframe opportunity. When the freshest lower-timeframe evidence conflicts "
    "with a stale higher-timeframe lean, that conflict is INFORMATION that may "
    "create opportunities — not a reason to stand down.\n\n"
    "FLAT / UNTRADEABLE means NO opportunity exists — NOT 'timeframes disagree'. "
    "Set market_is_untradeable=true and return an EMPTY opportunities list ONLY "
    "for genuinely incoherent structure with no exploitable asymmetry (or when "
    "cost/spread makes every candidate unexecutable). Disagreement between "
    "timeframes is the OPPOSITE of untradeable.\n\n"
    "CONDITIONAL THINKING — never just 'LONG'. Always 'LONG IF X'. Every "
    "opportunity MUST carry entry conditions, confirmation conditions, "
    "invalidation conditions and target logic. The system you feed acts like: "
    "IF X happens → opportunity A activates; IF Y happens → opportunity B "
    "activates; IF neither → WAIT.\n\n"
    "LOCATION MATTERS — the same directional signal has different opportunity "
    "quality at different locations. A bullish signal at resistance is NOT the "
    "same opportunity as a bullish signal at demand; grade quality accordingly.\n\n"
    "SEARCH EXPLICITLY for: continuation, pullback, reversal, liquidity sweep, "
    "failed breakout/breakdown, displacement, reclaim, rejection, range "
    "expansion, mean reversion, compression→expansion, exhaustion, trapped "
    "participants, momentum transition, structural failure, and asymmetric "
    "risk/reward locations. Judge LONG and SHORT symmetrically — never buy a "
    "market still actively falling nor sell one still actively rising merely "
    "because a higher timeframe leans that way (no falling knives; mirror it for "
    "shorts).\n\n"
    "MOVEMENT IS NOT OPPORTUNITY. An opportunity is executable positive expected "
    "value AFTER round-trip spread, commission, slippage and latency. DO NOT "
    "OVERTRADE — opportunity harvesting is SELECTIVE AGGRESSION, not constant "
    "activity. Reject a candidate when structure is incoherent, payoff is poor, "
    "invalidation is unclear, spread makes execution unattractive, confirmation "
    "is absent, or the opportunity is already exhausted.\n\n"
    "THESIS LIFECYCLE — each opportunity has a state: FORMING → ACTIVE → "
    "CONFIRMED → STRENGTHENING → WEAKENING → EXHAUSTED (an invalidated idea is "
    "simply dropped). Report the state you actually observe.\n\n"
    "RANKING — when multiple opportunities exist, rank them by quality × "
    "asymmetry × evidence_strength. The highest-ranked is the PREFERRED campaign; "
    "the rest remain CONDITIONAL ALTERNATIVES (name them in each other's "
    "competing_opportunities). Score every opportunity on quality (setup grade), "
    "asymmetry (reward vs risk geometry), urgency (is NOW the moment) and "
    "evidence_strength (how well the measurements support it), each 0.0–1.0.\n\n"
    "Respond with STRICT JSON only, no prose, no markdown fences, no <think> "
    "block, exactly this shape:\n"
    "{\"market_state\": {"
    "\"regime\": \"trending|ranging|transitional|volatile|uncertain\", "
    "\"context\": \"what HTF tells us — this is CONTEXT not a trade\", "
    "\"dominant_pressure\": \"buyers|sellers|balanced|transitioning\", "
    "\"key_location\": \"where price is relative to structure/liquidity/demand/supply\", "
    "\"volatility_state\": \"expanding|contracting|stable\"}, "
    "\"opportunities\": [{"
    "\"id\": \"opp_1\", "
    "\"horizon\": \"HTF|MTF|LTF|MICRO\", "
    "\"direction\": \"LONG|SHORT\", "
    "\"state\": \"FORMING|ACTIVE|CONFIRMED|STRENGTHENING|WEAKENING|EXHAUSTED\", "
    "\"thesis\": \"what is happening and why this is an opportunity\", "
    "\"why_now\": \"why this opportunity exists at this moment\", "
    "\"entry_conditions\": [\"what must happen to enter\"], "
    "\"confirmation_conditions\": [\"what confirms the thesis\"], "
    "\"invalidation_conditions\": [\"what kills it\"], "
    "\"target_logic\": \"where the opportunity resolves\", "
    "\"quality\": 0.0, \"asymmetry\": 0.0, \"urgency\": 0.0, "
    "\"evidence_strength\": 0.0, "
    "\"competing_opportunities\": [\"opp_2\"]}], "
    "\"preferred_opportunity\": \"opp_1\", "
    "\"market_is_untradeable\": false, "
    "\"reasoning_summary\": \"one paragraph tying it together\"}\n"
    "The opportunities list may hold one, several, or zero entries. Zero "
    "opportunities with market_is_untradeable=true is a valid, complete cognitive "
    "outcome — it means there is genuinely nothing to exploit right now, NOT that "
    "the timeframes merely disagree. preferred_opportunity is the id of your "
    "highest-ranked opportunity (omit or leave empty when the list is empty)."
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
    "OPPORTUNITY COMPETITION — the market simultaneously contains many "
    "opportunities, and CAPITAL IS FINITE. Ask explicitly: has a SUPERIOR "
    "OPPOSING or competing opportunity emerged since entry? Compare THIS "
    "campaign's remaining edge (its residual asymmetry and expected value from "
    "here, not from entry) against the best alternative opportunity now visible "
    "on the same instrument. A held position is only worth its capital while it "
    "remains the BEST available use of that capital. Rotation of capital toward a "
    "materially superior opportunity is a legitimate — and sometimes the correct "
    "— management action; a marginally better idea is NOT (rotation has cost and "
    "the incumbent thesis may simply be maturing). Judge the incumbent on its "
    "forward edge, never on sunk entry.\n\n"
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
_DIR_RE = re.compile(r"""(?i)["'`*_]*\bdirection\b["'`*_\s]*[:=]["'`*_\s]*([A-Za-z]+)""")
_CONF_RE = re.compile(r"""(?i)["'`*_]*\bconfidence\b["'`*_\s]*[:=]["'`*_\s]*([0-9]*\.?[0-9]+)""")
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


# ── Opportunity-harvesting normalisation ──────────────────────────────────────
# The opportunity-harvesting prompt asks the model for a SET of ranked
# opportunities plus a market-state read, instead of a single collapsed
# direction. These helpers backfill the legacy direction/confidence (+
# multidimensional confidence + excursions) fields from the PREFERRED
# opportunity so the pre-existing EV / sizing / cost machinery keeps working
# with no downstream change — this is a backward-COMPATIBILITY shim, not the
# primary cognitive path. The FULL opportunity set (with each opportunity's
# lifecycle state, activation / invalidation conditions and scores) is carried
# intact on the opinion (``opportunities`` / ``preferred_opportunity_id`` /
# ``market_is_untradeable`` / ``market_state``); the Brain lifts that set into
# first-class :class:`~cognition.contracts.Opportunity` objects and reasons over
# the SET (activation gate, forming-vs-no-opportunity, multiple coexisting
# ideas) rather than over the collapsed single direction. A legacy single-
# direction reply carries no set and is untouched.

def _coerce_opportunities(raw: Any) -> "list[dict]":
    """The opportunities value as a list of dicts (defensive; never raises)."""
    if not isinstance(raw, list):
        return []
    return [o for o in raw if isinstance(o, dict)]


def _opportunity_rank(opp: dict) -> float:
    """Rank score = quality × asymmetry × evidence_strength (prompt principle 7).

    A sub-score the model omitted defaults to the opportunity's ``quality`` so a
    reply that only grades quality still ranks monotonically by it (rather than
    collapsing every opportunity to a zero product).
    """
    q = _clamp01(opp.get("quality"))
    a = opp.get("asymmetry")
    e = opp.get("evidence_strength")
    a = _clamp01(a) if a is not None else q
    e = _clamp01(e) if e is not None else q
    return q * a * e


def _is_directional_opportunity(opp: dict) -> bool:
    return _norm_dir(opp.get("direction")) in (LONG, SHORT)


def _select_preferred_opportunity(
    opps: "list[dict]", preferred_id: Any,
) -> Optional[dict]:
    """The opportunity that drives the campaign: the model's named preference
    when it is directional, else the highest-ranked directional opportunity.
    Returns ``None`` when no opportunity carries a LONG/SHORT direction."""
    directional = [o for o in opps if _is_directional_opportunity(o)]
    if not directional:
        return None
    pid = str(preferred_id or "").strip()
    if pid:
        for o in directional:
            if str(o.get("id", "") or "").strip() == pid:
                return o
    return max(directional, key=_opportunity_rank)


def _normalize_opportunity_reply(
    fields: dict,
) -> "tuple[dict, list[dict], str, bool, dict]":
    """Collapse an opportunity-harvesting reply to backward-compatible fields.

    Returns ``(merged_fields, opportunities, preferred_id, untradeable,
    market_state)``. ``merged_fields`` is ``fields`` with the legacy keys
    (direction, confidence, the confidence dimensions, excursions, invalidation,
    alternatives, regime, rationale …) BACKFILLED from the preferred opportunity
    — but only where the reply did not already state them, so an explicit
    top-level value always wins. The preferred opportunity's ``quality`` becomes
    ``confidence`` and its ``asymmetry`` becomes the reward/risk excursions, so
    the existing EV, act-gate and sizing machinery reflect the opportunity's
    grade with no downstream change. Never raises.
    """
    opps = _coerce_opportunities(fields.get("opportunities"))
    market_state = fields.get("market_state")
    market_state = market_state if isinstance(market_state, dict) else {}
    untradeable = bool(fields.get("market_is_untradeable"))
    preferred = (
        None if untradeable
        else _select_preferred_opportunity(opps, fields.get("preferred_opportunity"))
    )
    merged = dict(fields)

    def _empty(v: Any) -> bool:
        return v is None or v == "" or v == [] or v == {}

    def _fill(key: str, value: Any) -> None:
        if _empty(value):
            return
        if _empty(merged.get(key)):
            merged[key] = value

    pref_id = str(preferred.get("id", "") or "") if preferred is not None else ""
    if preferred is not None:
        _fill("direction", _norm_dir(preferred.get("direction")))
        _fill("confidence", _clamp01(preferred.get("quality")))
        _fill("thesis_confidence", preferred.get("quality"))
        _fill("opportunity_confidence", preferred.get("evidence_strength"))
        _fill("timing_confidence", preferred.get("urgency"))
        _fill("primary_hypothesis", preferred.get("thesis"))
        _fill("opportunity", preferred.get("thesis") or preferred.get("why_now"))
        _fill("opportunity_horizon", preferred.get("horizon"))
        inv = preferred.get("invalidation_conditions")
        if isinstance(inv, list) and inv:
            _fill("invalidation", "; ".join(str(x) for x in inv))
            _fill("what_would_change_my_mind", inv)
        elif isinstance(inv, str) and inv.strip():
            _fill("invalidation", inv)
        # Asymmetry (0..1) → reward/risk excursions so the reward multiple, EV and
        # sizing reflect the opportunity's payoff geometry (favourable = a,
        # adverse = 1 - a ⇒ reward_r = a / (1 - a)). Only a strictly interior
        # value yields a usable ratio; otherwise the EV model keeps its default.
        asym = preferred.get("asymmetry")
        a = _clamp01(asym) if asym is not None else None
        if a is not None and 0.0 < a < 1.0:
            _fill("expected_favorable_excursion", str(round(a, 4)))
            _fill("expected_adverse_excursion", str(round(1.0 - a, 4)))
        _fill("expected_value", "positive")
    else:
        # Untradeable, or no opportunity carried a LONG/SHORT direction ⇒ FLAT.
        _fill("direction", FLAT)
        if _empty(merged.get("confidence")):
            merged["confidence"] = 0.0
        _fill("opportunity", "none")

    # Non-preferred opportunities become CONDITIONAL ALTERNATIVES (the losing but
    # still-live ideas), described compactly for the alternative-hypotheses field.
    alts: "list[str]" = []
    for o in opps:
        if preferred is not None and str(o.get("id", "") or "") == pref_id:
            continue
        d = _norm_dir(o.get("direction"))
        state = str(o.get("state") or "").strip()
        thesis = str(o.get("thesis") or o.get("why_now") or "").strip()[:180]
        label = f"{d}/{state}" if state else d
        q = _clamp01(o.get("quality"))
        alts.append(f"{label}@{q:.2f}: {thesis}" if thesis else f"{label}@{q:.2f}")
    if alts:
        _fill("alternative_hypotheses", alts)

    _fill("regime", market_state.get("regime"))
    _fill("rationale", fields.get("reasoning_summary"))

    resolved_pref = pref_id or str(fields.get("preferred_opportunity") or "")
    return merged, opps, resolved_pref, untradeable, market_state


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
    # Opportunity-harvesting format — the reply carries a set of ranked
    # opportunities and a market-state read INSTEAD of a single collapsed
    # direction. Any of these keys makes the reply a valid, parseable opinion
    # (the preferred opportunity is collapsed to direction/confidence for
    # backward compatibility in :meth:`_build_opinion`).
    "opportunities", "preferred_opportunity", "market_is_untradeable",
    "market_state", "reasoning_summary",
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
    # Opportunity-harvesting format — the full ranked opportunity set the model
    # returned (each a dict: id, horizon, direction, state, thesis, why_now,
    # entry/confirmation/invalidation conditions, target_logic, quality,
    # asymmetry, urgency, evidence_strength, competing_opportunities). Empty on a
    # legacy single-direction reply. ``preferred_opportunity_id`` names the
    # highest-ranked opportunity (which is collapsed to ``direction`` /
    # ``confidence`` above for backward compatibility); ``market_is_untradeable``
    # is the model's explicit "no exploitable opportunity exists" flag; and
    # ``market_state`` is the structured HTF-context read. All are additive — a
    # downstream consumer that only reads ``direction`` / ``confidence`` behaves
    # exactly as before.
    opportunities: list[dict] = field(default_factory=list)
    preferred_opportunity_id: str = ""
    market_is_untradeable: bool = False
    market_state: dict = field(default_factory=dict)
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
            "opportunities": [dict(o) for o in self.opportunities if isinstance(o, dict)],
            "preferred_opportunity_id": self.preferred_opportunity_id,
            "market_is_untradeable": bool(self.market_is_untradeable),
            "market_state": dict(self.market_state),
            "at": self.at_iso,
            "model": self.model,
        }

    def as_evidence(self, *, weight: float = 1.0, module: str = "llm_reasoner") -> dict:
        """Observation-shaped evidence for the Brain — never a vote.

        Mirrors the reasoning Evidence bridge (``cognition.evidence_adapters``):
        the advisor contributes its full structured thesis as an OBSERVATION with
        a confidence *magnitude* and strictly zero directional polarity — the
        Brain forms direction itself (Part XVII Art 7 / Part XXV). No ``direction``
        key is emitted; the directional conclusion lives only inside the prose
        thesis. It is evidence, subject to the same performance-based authority
        and physics vetoes as any module — never an override.
        """
        conf = _clamp01(self.confidence)
        parts = [f"{module} reasons"]
        if self.regime:
            parts.append(f"regime={self.regime}")
        if self.primary_hypothesis:
            parts.append(f"hypothesis: {self.primary_hypothesis}")
        if self.opportunity:
            horizon = f" (horizon {self.opportunity_horizon})" if self.opportunity_horizon else ""
            parts.append(f"opportunity: {self.opportunity}{horizon}")
        if self.invalidation:
            parts.append(f"invalidation: {self.invalidation}")
        if len(parts) == 1 and self.rationale:
            parts.append(self.rationale[:300])
        observation = " | ".join(parts)[:900]

        measurements: dict = {}
        for k in ("thesis_confidence", "opportunity_confidence",
                  "timing_confidence", "execution_confidence"):
            v = getattr(self, k, None)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                measurements[k] = round(float(v), 4)
        if self.opportunity:
            measurements["opportunity"] = self.opportunity
        if self.opportunity_horizon:
            measurements["opportunity_horizon"] = self.opportunity_horizon

        return {
            "module": module,
            "source": "llm",
            "observation": observation,
            "confidence": conf,
            "uncertainty": round(1.0 - conf, 4),
            "polarity": 0.0,               # non-directional — the Brain decides
            "weight": max(0.0, float(weight)),
            "measurements": measurements,
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

        When an ORIGINATION reply is in the opportunity-harvesting format (it
        carries an ``opportunities`` key) it is first collapsed to the legacy
        direction/confidence fields by :func:`_normalize_opportunity_reply`, and
        the full opportunity set / preferred id / market-state read are carried
        on the opinion alongside for auditability.
        """
        opportunities: "list[dict]" = []
        preferred_opportunity_id = ""
        market_is_untradeable = False
        market_state: dict = {}
        # A reply is in the opportunity-harvesting format when it carries any of
        # the new-format keys (an explicitly-untradeable reply may carry only the
        # market-state read and the flag, with no opportunities array).
        if (not management and isinstance(fields, dict)
                and any(k in fields for k in (
                    "opportunities", "market_state", "market_is_untradeable",
                    "preferred_opportunity"))):
            (fields, opportunities, preferred_opportunity_id,
             market_is_untradeable, market_state) = _normalize_opportunity_reply(fields)

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
            opportunities=[o for o in opportunities if isinstance(o, dict)][:12],
            preferred_opportunity_id=str(preferred_opportunity_id or ""),
            market_is_untradeable=bool(market_is_untradeable),
            market_state=market_state if isinstance(market_state, dict) else {},
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
