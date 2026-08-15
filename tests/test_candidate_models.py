"""Tests for the multi-opportunity foundation types (brain/candidate_models.py).

Locks the constitutional identity of a :class:`Candidate`: it tracks one
discovered *opportunity* (thesis, lifecycle state, expected value, invalidation)
and records provenance as *evidence sources* — never as a cluster of directional
votes or a vote count.
"""

from dataclasses import dataclass, field
from typing import List

from brain.candidate_models import Candidate, new_candidate_id


@dataclass
class _EvidenceStub:
    module: str = ""
    source: str = ""
    timeframe: str = ""


@dataclass
class _OpportunityStub:
    """A constitutional opportunity-like object (no votes, no confidence proxy)."""

    direction: str = "LONG"
    horizon: str = "SWING"
    thesis: str = "liquidity sweep absorbed, displacement up"
    opportunity_id: str = "op-123"
    state: str = "ACTIVE"
    quality: float = 0.72
    expected_value: float = 1.8
    invalidation_conditions: List[str] = field(default_factory=list)
    evidence_sources: List[object] = field(default_factory=list)


def test_candidate_has_no_vote_count_attribute():
    c = Candidate(direction="LONG", timeframe_class="SWING", score=0.5)
    assert not hasattr(c, "vote_count")


def test_from_opportunity_tracks_thesis_lifecycle_ev_invalidation():
    opp = _OpportunityStub(
        invalidation_conditions=["close back below the sweep low"],
        evidence_sources=[
            _EvidenceStub(module="structure", timeframe="H1"),
            _EvidenceStub(module="liquidity", timeframe="M15"),
        ],
    )
    c = Candidate.from_opportunity(opp, regime_context="trending")

    # Identity is the opportunity's thesis + lifecycle + EV + invalidation.
    assert c.opportunity_id == "op-123"
    assert c.thesis.startswith("liquidity sweep")
    assert c.state == "ACTIVE"
    assert c.ev_estimate == 1.8
    assert c.invalidation == "close back below the sweep low"
    assert c.direction == "LONG"
    assert c.timeframe_class == "SWING"
    # Conviction is the opportunity's own quality, not a vote tally.
    assert c.score == 0.72


def test_from_opportunity_records_evidence_sources_not_votes():
    opp = _OpportunityStub(
        evidence_sources=[
            _EvidenceStub(module="structure", timeframe="H1"),
            _EvidenceStub(module="momentum", timeframe="M5"),
        ],
    )
    c = Candidate.from_opportunity(opp)
    assert c.evidence_sources == ["structure", "momentum"]
    assert c.contributing_timeframes == ["H1", "M5"]
    # No vote-derived provenance leaks in.
    assert not hasattr(c, "vote_count")
    assert not hasattr(c, "contributing_modules")


def test_from_opportunity_ignores_votes_collection():
    # A legacy object that still exposes ``votes`` must NOT be mined for them:
    # provenance comes only from evidence_sources (here: none).
    @dataclass
    class _LegacyWithVotes:
        direction: str = "SHORT"
        timeframe_class: str = "SCALP"
        quality: float = 0.4
        votes: List[object] = field(default_factory=lambda: [_EvidenceStub(module="vwap")])

    c = Candidate.from_opportunity(_LegacyWithVotes())
    assert c.evidence_sources == []
    assert c.direction == "SHORT"


def test_summary_reads_evidence_sources():
    c = Candidate(
        direction="LONG", timeframe_class="SWING", score=0.6, ev_estimate=2.0,
        evidence_sources=["structure", "liquidity"],
    )
    s = c.summary
    assert "evidence: structure, liquidity" in s
    assert "LONG SWING" in s


def test_new_candidate_id_is_unique():
    assert new_candidate_id() != new_candidate_id()
