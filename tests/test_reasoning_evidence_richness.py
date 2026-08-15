"""Council cognition must reach the Brain WHOLE (Part XXV).

Previously each advisor was collapsed to `direction + confidence + a 200-char
rationale` before the Brain ever saw it. These tests lock in the fix: the
advisor's FULL structured reasoning (regime, hypotheses, the opportunity + its
horizon, expected excursions, invalidation, uncertainties) is preserved from the
engine, through the Evidence bridge, into the Brain's reasoner payload — and it
stays non-directional (no vote) and independent of how many advisors replied.
"""

import json
from types import SimpleNamespace

from cognition.brain import CognitiveBrain
from cognition.contracts import MarketState
from cognition.evidence_adapters import evidence_from_reasoning
from llm.reasoning_orchestrator import EngineOpinion, ReasoningEngine


_RICH = {
    "regime": "transitional",
    "primary_hypothesis": "Asian-session pullback within a D1 uptrend",
    "opportunity": "short-lived long into liquidity above the range high",
    "opportunity_horizon": "minutes",
    "expected_favorable_excursion": "20 pips",
    "expected_adverse_excursion": "8 pips",
    "invalidation": "loss of the 5m swing low",
    "key_uncertainty": "whether the sweep completes before London",
    "alternative_hypotheses": ["range continuation", "failed breakout reversal"],
    "supporting_evidence": ["liquidity sweep", "absorption of selling"],
    "contradicting_evidence": ["m5 downtrend"],
    "what_would_change_my_mind": ["acceptance below the range"],
    "direction": "LONG",
    "confidence": 0.7,
    "rationale": "sweep-and-reclaim long",
}


class _FakeOp:
    direction = "LONG"
    confidence = 0.7
    rationale = "sweep-and-reclaim long"

    def to_dict(self):
        return dict(_RICH)


class _FakeReasoner:
    available = True

    def reason(self, symbol, evidence, now=None):
        return _FakeOp()

    def last_reason_degraded(self, symbol):
        return False


def _opinion(engine="groq", cognition=None, confidence=0.7):
    return EngineOpinion(engine=engine, direction="LONG", confidence=confidence,
                         rationale="r", latency_ms=12.0,
                         cognition=dict(cognition if cognition is not None else _RICH))


# ── EngineOpinion / engine consult ──────────────────────────────────────────

def test_engine_opinion_to_dict_carries_cognition():
    d = _opinion().to_dict()
    assert d["cognition"]["opportunity"] == _RICH["opportunity"]
    assert d["cognition"]["opportunity_horizon"] == "minutes"


def test_consult_preserves_full_cognition():
    eng = ReasoningEngine("groq", _FakeReasoner())
    op = eng.consult("BTCUSD", {}, now=0.0)
    assert op is not None
    # The engine keeps direction/confidence for observability …
    assert op.direction == "LONG" and abs(op.confidence - 0.7) < 1e-9
    # … but ALSO the advisor's complete cognition (never discarded).
    assert op.cognition.get("opportunity") == _RICH["opportunity"]
    assert op.cognition.get("primary_hypothesis") == _RICH["primary_hypothesis"]


# ── Evidence bridge: full cognition, non-directional ────────────────────────

def test_reasoning_evidence_surfaces_micro_opportunity():
    ev = evidence_from_reasoning("BTCUSD", SimpleNamespace(opinions=[_opinion()]))
    assert len(ev) == 1
    e = ev[0]
    m = e.measurements
    # The micro-opportunity, its horizon and the excursions reach the Brain.
    assert m["opportunity"] == _RICH["opportunity"]
    assert m["opportunity_horizon"] == "minutes"
    assert m["expected_favorable_excursion"] == "20 pips"
    assert m["expected_adverse_excursion"] == "8 pips"
    assert m["primary_hypothesis"] == _RICH["primary_hypothesis"]
    assert m["invalidation"] == _RICH["invalidation"]
    assert m["alternative_hypotheses"] == _RICH["alternative_hypotheses"]
    # Rich, readable thesis observation — not a bare "reasons: <sentence>".
    assert "opportunity:" in e.observation and "hypothesis:" in e.observation
    # Confidence preserved as a magnitude; strictly non-directional (no vote).
    assert abs(e.confidence - 0.7) < 1e-9
    assert e.polarity == 0.0
    assert "direction" not in m and "bias" not in m and "vote" not in m


def test_reasoning_evidence_survives_scrub_into_market_state():
    ms = MarketState(symbol="BTCUSD")
    for e in evidence_from_reasoning("BTCUSD", SimpleNamespace(opinions=[_opinion()])):
        ms.add(e)  # runs contracts.scrub_directional
    reasoning = [e for e in ms.fresh_evidence()
                 if e.source_module == "reasoning_engine.groq"]
    assert len(reasoning) == 1
    m = reasoning[0].measurements
    # The reasoning content survives scrubbing; only directional keys are removed.
    assert m.get("opportunity") == _RICH["opportunity"]
    assert reasoning[0].polarity == 0.0


def test_brain_payload_carries_council_cognition():
    ms = MarketState(symbol="BTCUSD")
    for e in evidence_from_reasoning("BTCUSD", SimpleNamespace(opinions=[_opinion()])):
        ms.add(e)
    payload = CognitiveBrain._evidence_payload(ms, ms.consolidation())
    blob = json.dumps(payload, default=str)
    # The Brain's own reasoner now sees the advisor's full analysis, not a summary.
    assert "short-lived long into liquidity" in blob
    assert "Asian-session pullback" in blob
    assert "opportunity_horizon" in blob


# ── Council size must never weaken an advisor (2/14 is not "weak") ──────────

def test_advisor_strength_is_independent_of_council_size():
    solo = evidence_from_reasoning("BTCUSD", SimpleNamespace(opinions=[_opinion("groq")]))
    full = evidence_from_reasoning("BTCUSD", SimpleNamespace(opinions=[
        _opinion("groq"), _opinion("cohere"), _opinion("cerebras"), _opinion("cloudflare"),
    ]))
    groq_solo = next(e for e in solo if e.source_module == "reasoning_engine.groq")
    groq_full = next(e for e in full if e.source_module == "reasoning_engine.groq")
    # Same advisor, same conviction — presence of fewer/more peers changes nothing.
    assert groq_solo.confidence == groq_full.confidence
    assert groq_solo.measurements["opportunity"] == groq_full.measurements["opportunity"]


# ── V-019: EngineOpinion is opinion-shaped, not a direction+confidence vote ──

def test_engine_opinion_exposes_structured_contribution():
    op = _opinion()
    # The advisor's full thesis is first-class — hypotheses, opportunity, regime.
    assert op.regime == "transitional"
    assert op.opportunity == _RICH["opportunity"]
    assert _RICH["primary_hypothesis"] in op.hypotheses
    assert "range continuation" in op.hypotheses          # alternatives too
    assert op.key_uncertainty == _RICH["key_uncertainty"]
    # A readable thesis summary — never "name DIRECTION(conf)".
    s = op.thesis_summary(200)
    assert "opportunity" in s.lower()
    assert "LONG" not in s and "(0.7" not in s


def test_engine_opinion_to_dict_is_thesis_led():
    d = _opinion().to_dict()
    assert d["thesis"] and "cognition" in d
    # direction/confidence survive ONLY as observability projections.
    assert d["direction"] == "LONG"


def test_panel_log_shows_thesis_not_vote():
    from types import SimpleNamespace as _NS

    from loguru import logger

    from llm.reasoning_orchestrator import ReasoningOrchestrator

    lines: list = []
    sink_id = logger.add(lines.append, level="INFO", format="{message}")
    try:
        result = _NS(symbol="BTCUSD", opinions=[_opinion("groq")],
                     consulted=["groq", "absent-one"])
        ReasoningOrchestrator._log_panel(result)
    finally:
        logger.remove(sink_id)
    blob = "".join(str(x) for x in lines)
    assert "contributing" in blob and "groq:" in blob
    assert "opportunity" in blob.lower()
    # The panel line is thesis-led, not a vote tag.
    assert "LONG(" not in blob and "(0.70)" not in blob
    assert "absent-one" in blob                            # who left the panel

