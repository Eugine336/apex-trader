"""Tests for Phase E evidence adapters — Constitution Part III (full evidence)."""

from cognition.contracts import EvidenceDomain
from cognition.evidence_adapters import (
    classify_domain,
    evidence_from_analogues,
    evidence_from_developing_bias,
    evidence_from_knowledge,
    evidence_from_reasoning,
    evidence_from_thesis_status,
)


# ── Domain classification ─────────────────────────────────────────────────────

def test_classify_domain_maps_known_modules():
    assert classify_domain("structure_engine") == EvidenceDomain.STRUCTURE
    assert classify_domain("liquidity_mapper") == EvidenceDomain.LIQUIDITY
    assert classify_domain("order_block") == EvidenceDomain.ORDER_FLOW
    assert classify_domain("momentum_divergence") == EvidenceDomain.MOMENTUM
    assert classify_domain("atr_percentile") == EvidenceDomain.VOLATILITY
    assert classify_domain("volume_analyzer") == EvidenceDomain.VOLUME
    assert classify_domain("currency_strength") == EvidenceDomain.CORRELATION
    assert classify_domain("session_engine") == EvidenceDomain.SESSION
    assert classify_domain("news_impact") == EvidenceDomain.MACRO
    assert classify_domain("world_model") == EvidenceDomain.MULTI_TIMEFRAME
    assert classify_domain("instrument_stats") == EvidenceDomain.HISTORICAL_ANALOGUE


def test_classify_domain_unknown_is_other():
    assert classify_domain("totally_unknown") == EvidenceDomain.OTHER
    assert classify_domain("") == EvidenceDomain.OTHER


# ── evidence_from_thesis_status ───────────────────────────────────────────────

def _status(dominant="LONG", supporting=None, opposing=None, long_conf=0.8, short_conf=0.1):
    long_t = {"confidence": long_conf,
              "supporting_modules": supporting if dominant == "LONG" else [],
              "opposing_modules": opposing if dominant == "LONG" else []}
    short_t = {"confidence": short_conf,
               "supporting_modules": supporting if dominant == "SHORT" else [],
               "opposing_modules": opposing if dominant == "SHORT" else []}
    return {"theses": {"EURUSD": {
        "long": long_t, "short": short_t, "flat": {"confidence": 0.2},
        "effective": {"dominant": dominant, "long_ev": 0.6, "short_ev": -0.2, "flat_ev": 0.0},
    }}}


def test_thesis_status_aggregate_only_without_modules():
    ev = evidence_from_thesis_status(_status("LONG"), "EURUSD")
    assert len(ev) == 1
    assert ev[0].source_module == "brain.thesis_engine"
    assert ev[0].polarity == 0.0  # Part XXV — no directional reading
    assert "LONG" not in ev[0].observation and "lean" not in ev[0].observation


def test_thesis_status_emits_per_module_evidence_with_domains():
    ev = evidence_from_thesis_status(
        _status("LONG", supporting=["structure_engine", "liquidity_mapper", "momentum_divergence"],
                opposing=["volume_analyzer"]),
        "EURUSD",
    )
    # aggregate + 3 supporting + 1 opposing
    assert len(ev) == 5
    domains = {e.domain for e in ev}
    assert EvidenceDomain.STRUCTURE in domains
    assert EvidenceDomain.LIQUIDITY in domains
    assert EvidenceDomain.MOMENTUM in domains
    # Part XXV — every module is a non-directional instrument reading; neither
    # supporting nor opposing modules carry a lean, and none names a direction.
    sup = next(e for e in ev if e.source_module == "structure_engine")
    opp = next(e for e in ev if e.source_module == "volume_analyzer")
    assert sup.polarity == 0.0
    assert opp.polarity == 0.0
    assert "LONG" not in sup.observation and "SHORT" not in opp.observation


def test_thesis_status_short_dominant_supporting_is_negative():
    ev = evidence_from_thesis_status(
        _status("SHORT", supporting=["order_block"], short_conf=0.7), "EURUSD")
    sup = next(e for e in ev if e.source_module == "order_block")
    assert sup.polarity == 0.0
    assert sup.domain == EvidenceDomain.ORDER_FLOW


def test_thesis_status_missing_symbol_is_empty():
    assert evidence_from_thesis_status(_status("LONG"), "GBPUSD") == []
    assert evidence_from_thesis_status({}, "EURUSD") == []


# ── V-004 — the vote-source adapter API has been removed ──────────────────────

def test_vote_source_adapter_api_is_removed():
    # V-004: a vote panel is a pre-collapsed direction+confidence read, so it must
    # not be wireable into the MarketState as evidence. The adapter is gone.
    import cognition.evidence_adapters as ea
    assert not hasattr(ea, "evidence_from_votes")


# ── evidence_from_developing_bias (forming-bar read) ──────────────────────────

def test_developing_bias_confidence_is_structural_not_directional():
    # V-004 — the Evidence confidence must come ONLY from the non-directional
    # structural clarity (alignment / trend strength, eroded by conflict), NEVER
    # from a directional conviction. Here a high directional conviction rides
    # alongside a moderate structural clarity: the evidence weight tracks the
    # structure, not the conviction.
    ev = evidence_from_developing_bias("XAUUSD", {
        "direction": "LONG", "confidence": 0.9, "conflict_score": 0.2,
        "long_probability": 0.9, "short_probability": 0.1, "score": 0.8,
        "alignment_degree": 0.6, "trend_strength": 0.5,
        "strength": "STRONG", "tradeable": True,
    })
    assert len(ev) == 1
    e = ev[0]
    assert e.domain == EvidenceDomain.MULTI_TIMEFRAME
    assert e.source_module == "world_model.developing"
    # Part XXV / V-004 — the developing read is non-directional: no lean.
    assert e.polarity == 0.0
    # confidence = clarity * (1 - conflict) = max(0.6, 0.5) * (1 - 0.2) = 0.48,
    # derived from structure — NOT from the 0.9 directional conviction.
    assert abs(e.confidence - 0.48) < 1e-6
    assert e.measurements["developing"] is True
    assert e.measurements["tradeable"] is True
    # Directional keys never ride along as measurements.
    assert "long_probability" not in e.measurements
    assert "direction" not in e.measurements and "score" not in e.measurements
    assert e.relevance_horizon_seconds == 60.0
    assert "developing-candle structural reading" in e.observation
    assert "votes" not in e.observation and "LONG" not in e.observation


def test_developing_bias_short_is_non_directional():
    ev = evidence_from_developing_bias("XAUUSD", {"direction": "SHORT", "confidence": 0.6})
    assert ev[0].polarity == 0.0


def test_developing_bias_directional_conviction_does_not_leak():
    # V-004 — a read carrying ONLY a directional conviction (no structural
    # clarity) must NOT let that conviction become the evidence weight.
    ev = evidence_from_developing_bias("XAUUSD", {
        "direction": "LONG", "confidence": 0.9,
        "long_probability": 0.7, "short_probability": 0.3,
    })
    assert len(ev) == 1
    assert ev[0].confidence == 0.0  # no structural clarity ⇒ no evidence weight
    assert ev[0].polarity == 0.0


def test_developing_bias_empty_and_fault_safe():
    assert evidence_from_developing_bias("XAUUSD", None) == []
    assert evidence_from_developing_bias("XAUUSD", {}) == []
    assert evidence_from_developing_bias("XAUUSD", "not-a-dict") == []


# ── evidence_from_knowledge (Composio Operational Intelligence, Part IX v3.0) ──

def test_knowledge_research_items_are_macro_context():
    ev = evidence_from_knowledge("XAUUSD", {"items": [
        {"title": "Gold rallies", "snippet": "safe-haven bid", "source": "news",
         "sentiment": "bullish"},
        {"title": "DXY firm", "snippet": "dollar strength", "sentiment": "bearish"},
    ]})
    assert len(ev) == 2
    assert all(e.domain == EvidenceDomain.MACRO for e in ev)
    bull = next(e for e in ev if "Gold rallies" in e.observation)
    bear = next(e for e in ev if "DXY firm" in e.observation)
    # Part XXV — news is external context, not a directional reading: the Brain
    # reads the headline text and infers direction itself. No sentiment lean.
    assert bull.polarity == 0.0 and bear.polarity == 0.0
    assert bull.measurements["external"] is True


def test_knowledge_advisor_answer_is_reasoning_domain():
    ev = evidence_from_knowledge("XAUUSD", {
        "answer": "Range-bound; wait for a sweep.", "direction": "neutral",
        "confidence": 0.5, "advisor": "gpt-advisor",
    })
    assert len(ev) == 1
    assert ev[0].domain == EvidenceDomain.REASONING
    assert ev[0].measurements.get("advisor") is True
    assert abs(ev[0].polarity) < 1e-9  # neutral ⇒ directionless


def test_knowledge_is_bounded_and_fault_safe():
    items = [{"title": f"h{i}", "snippet": "s"} for i in range(50)]
    ev = evidence_from_knowledge("XAUUSD", {"items": items}, max_items=3)
    assert len(ev) == 3
    assert evidence_from_knowledge("XAUUSD", None) == []
    assert evidence_from_knowledge("XAUUSD", {}) == []
    assert evidence_from_knowledge("XAUUSD", {"items": [42, "x"]}) == []  # non-dict items skipped


# ── Historical analogues (Phase H — Part VII memory) ──────────────────────────

def test_evidence_from_analogues_won_long_leans_long():
    analogues = [
        {"direction": "LONG", "similarity": 0.9, "outcome_won": True, "reasoning_quality": 0.8},
        {"direction": "LONG", "similarity": 0.7, "outcome_won": True, "reasoning_quality": 0.7},
    ]
    ev = evidence_from_analogues("EURUSD", analogues)
    assert len(ev) == 1
    assert ev[0].domain == EvidenceDomain.HISTORICAL_ANALOGUE
    assert ev[0].polarity == 0.0  # Part XXV — win/loss stats, no directional lean
    assert ev[0].measurements["wins"] == 2


def test_evidence_from_analogues_lost_long_is_non_directional():
    analogues = [
        {"direction": "LONG", "similarity": 0.9, "outcome_won": False, "reasoning_quality": 0.8},
    ]
    ev = evidence_from_analogues("EURUSD", analogues)
    assert ev[0].polarity == 0.0
    assert ev[0].measurements["losses"] == 1


def test_evidence_from_analogues_empty_and_fault_safe():
    assert evidence_from_analogues("EURUSD", None) == []
    assert evidence_from_analogues("EURUSD", []) == []
    assert evidence_from_analogues("EURUSD", [object()]) == []  # non-dict → skipped


# ── Multi-model reasoning opinions → Evidence (Part XVII, Art 7) ──────────────

from types import SimpleNamespace


def _consult(*opinions):
    return SimpleNamespace(opinions=[
        SimpleNamespace(engine=e, direction=d, confidence=c, rationale="r", latency_ms=12.0)
        for (e, d, c) in opinions
    ])


def test_evidence_from_reasoning_one_per_engine_not_a_vote():
    # Two engines agree LONG, one SHORT — each becomes its OWN evidence item;
    # the adapter never collapses them into a majority (Art 7).
    ev = evidence_from_reasoning("EURUSD", _consult(
        ("openai", "LONG", 0.8), ("claude", "LONG", 0.7), ("deepseek", "SHORT", 0.6)))
    assert len(ev) == 3
    assert all(e.domain == EvidenceDomain.REASONING for e in ev)
    srcs = {e.source_module for e in ev}
    assert srcs == {"reasoning_engine.openai", "reasoning_engine.claude",
                    "reasoning_engine.deepseek"}
    # Part XXV — each engine is its own evidence and none carries a directional
    # lean; the Brain synthesises their rationales, it does not count votes.
    assert all(e.polarity == 0.0 for e in ev)
    assert all("LONG" not in e.observation and "SHORT" not in e.observation for e in ev)


def test_evidence_from_reasoning_classify_domain():
    assert classify_domain("reasoning_engine.openai") == EvidenceDomain.REASONING


def test_evidence_from_reasoning_empty_and_fault_safe():
    assert evidence_from_reasoning("EURUSD", None) == []
    assert evidence_from_reasoning("EURUSD", SimpleNamespace(opinions=[])) == []
    assert evidence_from_reasoning("EURUSD", object()) == []
