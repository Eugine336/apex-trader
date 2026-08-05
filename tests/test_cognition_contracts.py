"""Tests for the constitutional cognitive contracts (Parts II, III, IV, VI)."""

import pytest

from cognition.contracts import (
    REQUIRED_QUESTIONS,
    CampaignSpecification,
    DecisionPackage,
    DecisionType,
    Evidence,
    EvidenceDomain,
    MarketState,
    domain_from,
)


# ── Evidence (Part III, Article 4 — no signals, provenance, bounded) ──────────

def test_evidence_defaults_and_clamping():
    e = Evidence(source_module="structure", domain="momentum",
                 confidence=2.0, uncertainty=-1.0, polarity=5.0)
    assert e.evidence_id and e.timestamp_iso and e.timestamp_epoch > 0
    assert e.domain == EvidenceDomain.MOMENTUM
    assert e.confidence == 1.0          # clamped to [0,1]
    assert e.uncertainty == 0.0         # clamped to [0,1]
    assert e.polarity == 1.0            # clamped to [-1,1]


def test_domain_from_unknown_falls_back_to_other():
    assert domain_from("not-a-domain") == EvidenceDomain.OTHER
    assert domain_from(EvidenceDomain.LIQUIDITY) == EvidenceDomain.LIQUIDITY


def test_evidence_freshness():
    e = Evidence(source_module="x", relevance_horizon_seconds=100.0,
                 timestamp_epoch=1000.0)
    assert e.is_fresh(now=1050.0) is True
    assert e.is_fresh(now=1200.0) is False
    unbounded = Evidence(source_module="x", timestamp_epoch=1000.0)
    assert unbounded.is_fresh(now=10_000.0) is True


def test_evidence_to_dict_shape():
    d = Evidence(source_module="liq", domain="liquidity",
                 observation="pool above", confidence=0.7).to_dict()
    assert d["source_module"] == "liq" and d["domain"] == "liquidity"
    assert d["confidence"] == 0.7 and "evidence_id" in d


# ── MarketState (Part IV, Article 2 — missing evidence increases uncertainty) ──

def test_empty_market_state_is_maximally_uncertain():
    ms = MarketState(symbol="EURUSD")
    c = ms.consolidation()
    assert c["evidence_fresh"] == 0
    assert c["aggregate_uncertainty"] == 1.0


def test_market_state_filters_stale_and_reports_conflict():
    ms = MarketState(symbol="EURUSD")
    ms.add(Evidence(source_module="a", polarity=0.8, confidence=0.9,
                    timestamp_epoch=1000.0, relevance_horizon_seconds=100.0))
    ms.add(Evidence(source_module="b", polarity=-0.8, confidence=0.9,
                    timestamp_epoch=1000.0, relevance_horizon_seconds=100.0))
    ms.add(Evidence(source_module="c", polarity=0.5, confidence=0.5,
                    timestamp_epoch=1.0, relevance_horizon_seconds=10.0))  # stale
    c = ms.consolidation(now=1050.0)
    assert c["evidence_total"] == 3
    assert c["evidence_fresh"] == 2                 # stale one filtered
    assert c["conflict_ratio"] == pytest.approx(1.0)  # one bull, one bear


# ── DecisionPackage (Part II, Article 7 + Article 3 required questions) ────────

def test_decision_type_coercion_and_authorisation():
    d = DecisionPackage(symbol="EURUSD", decision_type="open_campaign")
    assert d.decision_type == DecisionType.OPEN_CAMPAIGN
    assert d.authorises_action is True
    assert DecisionPackage(symbol="X", decision_type=DecisionType.CONTINUE_OBSERVING).authorises_action is False
    assert DecisionPackage(symbol="X", decision_type=DecisionType.REJECT_OPPORTUNITY).authorises_action is False


def test_unanswered_required_questions_tracked():
    d = DecisionPackage(symbol="EURUSD", questions_answered={"expected_value": "0.4R"})
    missing = d.unanswered_questions()
    assert "expected_value" not in missing
    assert "should_i_do_nothing" in missing
    assert len(REQUIRED_QUESTIONS) == 10


def test_decision_package_to_dict_has_provenance():
    d = DecisionPackage(symbol="EURUSD", decision_type="open_campaign",
                        supporting_evidence_ids=["e1"], reasoner="ai_brain")
    out = d.to_dict()
    assert out["decision_type"] == "open_campaign"
    assert out["supporting_evidence_ids"] == ["e1"]
    assert out["reasoner"] == "ai_brain"
    assert out["authorises_action"] is True


# ── CampaignSpecification (Part IV, Article 8 / Part VI, Article 2) ────────────

def test_campaign_specification_direction_normalised():
    c = CampaignSpecification(symbol="EURUSD", direction="long", confidence=0.8)
    assert c.direction == "LONG" and c.campaign_id
    assert CampaignSpecification(symbol="X", direction="weird").direction == "FLAT"


def test_campaign_specification_to_dict():
    c = CampaignSpecification(symbol="EURUSD", direction="SHORT", thesis="distribution",
                              invalidation_conditions=["reclaim VAH"], decision_id="d1")
    out = c.to_dict()
    assert out["direction"] == "SHORT" and out["decision_id"] == "d1"
    assert out["invalidation_conditions"] == ["reclaim VAH"]
