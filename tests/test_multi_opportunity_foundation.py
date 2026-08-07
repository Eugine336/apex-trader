"""
Tests for the multi-opportunity foundation (Session 1).

Covers the new candidate models (``brain/candidate_models.py``), the
non-destructive multi-candidate decision entry point
(``brain.decision_core.decide_candidates`` / ``select_top_candidate``), and that
the ranker-EV sizing dimension is enabled by default.

Dependency-light — pure vote math, no torch/pandas required.
"""

import pytest

from brain.directional_consensus import Vote
from brain.opportunity_ranker import SCALP, SWING, Opportunity, rank_opportunities
from brain.candidate_models import (
    Allocation,
    Candidate,
    CandidateEntryDecision,
    CandidatePosition,
    new_candidate_id,
)
from brain.decision_core import decide_candidates, select_top_candidate


def _vote(module, direction, conf=0.8, weight=1.0, tf=""):
    return Vote(module=module, direction=direction, confidence=conf, weight=weight, timeframe=tf)


# ── Candidate dataclass ────────────────────────────────────────────────────

class TestCandidate:
    def test_frozen_immutable(self):
        c = Candidate(direction="LONG", timeframe_class=SWING, score=0.7)
        with pytest.raises(Exception):
            c.direction = "SHORT"  # type: ignore[misc]

    def test_unique_candidate_id(self):
        a = Candidate(direction="LONG", timeframe_class=SWING, score=0.5)
        b = Candidate(direction="LONG", timeframe_class=SWING, score=0.5)
        assert a.candidate_id and b.candidate_id
        assert a.candidate_id != b.candidate_id
        assert len(new_candidate_id()) == 12

    def test_provenance_properties(self):
        votes = [
            _vote("structure", "LONG", tf="H4"),
            _vote("wyckoff", "LONG", tf="D1"),
            _vote("fvg", "LONG", tf="H4"),  # duplicate TF must collapse
        ]
        c = Candidate(
            direction="LONG", timeframe_class=SWING, score=0.8,
            contributing_votes=votes, vote_count=len(votes),
        )
        assert c.contributing_modules == ["structure", "wyckoff", "fvg"]
        assert c.contributing_timeframes == ["H4", "D1"]

    def test_from_opportunity_maps_fields(self):
        votes = [_vote("structure", "SHORT", conf=0.9, tf="M5")]
        opp = Opportunity(
            direction="SHORT",
            timeframe_class=SCALP,
            expected_value=1.25,
            confidence=0.9,
            coherence=1.0,
            net_score=-0.9,
            reward_risk=1.5,
            win_prob=0.6,
            contributors=["structure"],
            votes=votes,
            timeframes=["M5"],
        )
        c = Candidate.from_opportunity(opp, regime_context="trending")
        assert c.direction == "SHORT"
        assert c.timeframe_class == SCALP
        assert c.score == pytest.approx(0.9)
        assert c.ev_estimate == pytest.approx(1.25)
        assert c.vote_count == 1
        assert c.regime_context == "trending"
        assert c.contributing_modules == ["structure"]
        assert c.candidate_id


# ── decide_candidates() — the non-destructive "rewire" ──────────────────────

class TestDecideCandidates:
    def test_empty_votes_returns_empty(self):
        assert decide_candidates([]) == []

    def test_multiple_independent_candidates_coexist(self):
        # Fast modules see a SHORT scalp; slow modules see a LONG swing.
        votes = [
            _vote("momentum", "SHORT", conf=0.9, tf="M5"),
            _vote("volume", "SHORT", conf=0.8, tf="M5"),
            _vote("structure", "LONG", conf=0.8, tf="H4"),
            _vote("wyckoff", "LONG", conf=0.7, tf="D1"),
        ]
        candidates = decide_candidates(votes, regime_context="trending")
        assert len(candidates) == 2
        directions = {(c.direction, c.timeframe_class) for c in candidates}
        assert ("SHORT", SCALP) in directions
        assert ("LONG", SWING) in directions
        # Every candidate carries provenance + a unique id.
        ids = [c.candidate_id for c in candidates]
        assert len(set(ids)) == len(ids)
        for c in candidates:
            assert c.regime_context == "trending"
            assert c.vote_count >= 1
            assert c.contributing_votes

    def test_min_vote_count_filter(self):
        votes = [
            _vote("momentum", "SHORT", tf="M5"),
            _vote("structure", "LONG", tf="H4"),
            _vote("wyckoff", "LONG", tf="D1"),
        ]
        # Require at least 2 votes per candidate — the lone SHORT scalp drops.
        candidates = decide_candidates(votes, min_vote_count=2)
        assert all(c.vote_count >= 2 for c in candidates)
        assert all(c.direction == "LONG" for c in candidates)

    def test_min_candidate_score_filter(self):
        votes = [_vote("structure", "LONG", conf=0.1, tf="H4")]
        # A score floor above the weak cluster confidence removes it.
        assert decide_candidates(votes, min_candidate_score=0.5) == []

    def test_matches_ranker_directions(self):
        votes = [
            _vote("momentum", "SHORT", tf="M5"),
            _vote("structure", "LONG", tf="H4"),
        ]
        ranked = rank_opportunities(votes)
        candidates = decide_candidates(votes)
        assert len(candidates) == len(ranked)
        assert {c.direction for c in candidates} == {o.direction for o in ranked}


# ── select_top_candidate() — legacy single-direction convenience ────────────

class TestSelectTopCandidate:
    def test_none_when_empty(self):
        assert select_top_candidate([]) is None

    def test_returns_best_first(self):
        votes = [
            _vote("momentum", "SHORT", conf=0.5, tf="M5"),
            _vote("structure", "LONG", conf=0.95, tf="H4"),
            _vote("wyckoff", "LONG", conf=0.9, tf="D1"),
        ]
        candidates = decide_candidates(votes)
        top = select_top_candidate(candidates)
        assert top is candidates[0]
        # Best-first ordering => the top candidate has the max EV.
        assert top.ev_estimate == max(c.ev_estimate for c in candidates)


# ── other foundation dataclasses ────────────────────────────────────────────

class TestFoundationModels:
    def test_candidate_entry_decision(self):
        c = Candidate(direction="LONG", timeframe_class=SWING, score=0.7)
        d = CandidateEntryDecision(symbol="EURUSD", candidate=c, source="zone", sl=1.08, tp=1.09)
        assert d.symbol == "EURUSD"
        assert d.candidate is c
        assert d.source == "zone"

    def test_candidate_position(self):
        p = CandidatePosition(
            symbol="EURUSD", direction="LONG", candidate_id="abc123",
            timeframe_class=SWING, contributing_modules=["structure"],
            contributing_timeframes=["H4"], entry_regime="trending",
        )
        assert p.candidate_id == "abc123"
        assert p.contributing_modules == ["structure"]

    def test_allocation(self):
        a = Allocation(approved=True, max_risk_pct=1.0, reason="approved")
        assert a.approved is True
        assert a.max_risk_pct == 1.0
        assert a.conflicts == []


# ── ranker-EV sizing dimension is enabled by default ────────────────────────

class TestRankerEvEnabled:
    def test_use_ranker_ev_default_true(self):
        from config import OrchestratorConfig

        assert OrchestratorConfig().use_ranker_ev is True
