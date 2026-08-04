"""Tests for Phase E evidence adapters — Constitution Part III (full evidence)."""

from cognition.contracts import EvidenceDomain
from cognition.evidence_adapters import (
    classify_domain,
    evidence_from_thesis_status,
    evidence_from_votes,
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
    assert ev[0].polarity > 0


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
    # supporting modules lean with the dominant (LONG ⇒ positive); opposing against.
    sup = next(e for e in ev if e.source_module == "structure_engine")
    opp = next(e for e in ev if e.source_module == "volume_analyzer")
    assert sup.polarity > 0
    assert opp.polarity < 0


def test_thesis_status_short_dominant_supporting_is_negative():
    ev = evidence_from_thesis_status(
        _status("SHORT", supporting=["order_block"], short_conf=0.7), "EURUSD")
    sup = next(e for e in ev if e.source_module == "order_block")
    assert sup.polarity < 0
    assert sup.domain == EvidenceDomain.ORDER_FLOW


def test_thesis_status_missing_symbol_is_empty():
    assert evidence_from_thesis_status(_status("LONG"), "GBPUSD") == []
    assert evidence_from_thesis_status({}, "EURUSD") == []


# ── evidence_from_votes ───────────────────────────────────────────────────────

class _Vote:
    def __init__(self, module, direction, confidence, weight=1.0):
        self.module = module
        self.direction = direction
        self.confidence = confidence
        self.weight = weight


def test_evidence_from_votes():
    votes = [_Vote("structure_engine", "LONG", 0.8), _Vote("volume_analyzer", "SHORT", 0.6)]
    ev = evidence_from_votes("EURUSD", votes)
    assert len(ev) == 2
    long_e = next(e for e in ev if e.source_module == "structure_engine")
    short_e = next(e for e in ev if e.source_module == "volume_analyzer")
    assert long_e.polarity > 0 and long_e.domain == EvidenceDomain.STRUCTURE
    assert short_e.polarity < 0 and short_e.domain == EvidenceDomain.VOLUME


def test_evidence_from_votes_empty_and_fault_safe():
    assert evidence_from_votes("EURUSD", None) == []
    assert evidence_from_votes("EURUSD", [object()]) == []  # no module attr → skipped
