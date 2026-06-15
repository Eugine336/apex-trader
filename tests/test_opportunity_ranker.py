"""
Tests for the opportunity ranker — dependency-light (no torch, no pandas).
"""

from __future__ import annotations

from brain.directional_consensus import Vote
from brain.opportunity_ranker import (
    Opportunity,
    SCALP,
    SWING,
    MIXED,
    cluster_votes,
    rank_opportunities,
    score_opportunity,
    timeframe_class,
)


def _v(module, direction, conf, weight=1.0):
    return Vote(module, direction, conf, weight)


# ── timeframe classification ───────────────────────────────────────────────

class TestTimeframeClass:
    def test_only_fast_is_scalp(self):
        assert timeframe_class(["momentum", "vwap", "fvg"]) == SCALP

    def test_only_slow_is_swing(self):
        assert timeframe_class(["structure", "currency_strength"]) == SWING

    def test_mixed(self):
        assert timeframe_class(["structure", "momentum"]) == MIXED


# ── clustering ───────────────────────────────────────────────────────────

class TestClusterVotes:
    def test_neutral_and_zero_votes_excluded(self):
        votes = [
            _v("momentum", "NEUTRAL", 0.0),
            _v("vwap", "LONG", 0.0),       # zero confidence dropped
            _v("fvg", "LONG", 0.5, 0.0),   # zero weight dropped
        ]
        assert cluster_votes(votes) == []

    def test_opposing_horizons_form_separate_clusters(self):
        # Slow modules LONG (swing), fast modules SHORT (scalp) — must NOT merge.
        votes = [
            _v("structure", "LONG", 0.8),
            _v("currency_strength", "LONG", 0.7),
            _v("momentum", "SHORT", 0.8),
            _v("vwap", "SHORT", 0.7),
        ]
        clusters = cluster_votes(votes)
        assert len(clusters) == 2
        dirs = {(c[0].direction, timeframe_class([v.module for v in c])) for c in clusters}
        assert ("LONG", SWING) in dirs
        assert ("SHORT", SCALP) in dirs


# ── scoring ────────────────────────────────────────────────────────────────

class TestScoreOpportunity:
    def _score(self, cluster):
        return score_opportunity(
            cluster,
            scalp_target_rr=2.0, swing_target_rr=3.0, mixed_target_rr=2.5,
            win_prob_floor=0.30, win_prob_scale=0.40,
            ev_weight=1.0, net_weight=0.25,
        )

    def test_higher_confidence_higher_ev(self):
        low = self._score([_v("momentum", "LONG", 0.2)])
        high = self._score([_v("momentum", "LONG", 0.95)])
        assert high.expected_value > low.expected_value
        assert high.score > low.score

    def test_direction_preserved(self):
        opp = self._score([_v("vwap", "SHORT", 0.8)])
        assert opp.direction == "SHORT"
        assert opp.horizon == SCALP

    def test_mixed_direction_cluster_raises(self):
        import pytest
        with pytest.raises(ValueError):
            self._score([_v("momentum", "LONG", 0.5), _v("vwap", "SHORT", 0.5)])

    def test_empty_cluster_raises(self):
        import pytest
        with pytest.raises(ValueError):
            self._score([])


# ── ranking + edge cases ────────────────────────────────────────────────────

class TestRankOpportunities:
    def test_empty_votes(self):
        assert rank_opportunities([]) == []

    def test_all_neutral(self):
        assert rank_opportunities([_v("momentum", "NEUTRAL", 0.0)]) == []

    def test_ranked_by_score_desc(self):
        votes = [
            _v("structure", "LONG", 0.9),
            _v("currency_strength", "LONG", 0.9),
            _v("momentum", "SHORT", 0.5),
        ]
        ranked = rank_opportunities(votes, min_cluster_confidence=0.0, min_cluster_net=0.0)
        assert len(ranked) == 2
        assert ranked[0].score >= ranked[1].score

    def test_opposing_valid_clusters_both_returned(self):
        votes = [
            _v("structure", "LONG", 0.9),
            _v("currency_strength", "LONG", 0.85),
            _v("momentum", "SHORT", 0.9),
            _v("vwap", "SHORT", 0.85),
            _v("fvg", "SHORT", 0.8),
        ]
        ranked = rank_opportunities(votes)
        dirs = {o.direction for o in ranked}
        assert dirs == {"LONG", "SHORT"}

    def test_all_negative_ev_dropped(self):
        # Weak lone vote → low win_prob → negative EV → dropped when required.
        ranked = rank_opportunities(
            [_v("momentum", "LONG", 0.05)],
            min_cluster_net=0.0,
            min_cluster_confidence=0.0,
            require_positive_ev=True,
        )
        assert ranked == []

    def test_negative_ev_kept_when_not_required(self):
        ranked = rank_opportunities(
            [_v("momentum", "LONG", 0.05)],
            min_cluster_net=0.0,
            min_cluster_confidence=0.0,
            require_positive_ev=False,
        )
        assert len(ranked) == 1

    def test_below_net_floor_dropped(self):
        ranked = rank_opportunities(
            [_v("momentum", "LONG", 0.5, weight=0.1)],  # net = 0.05
            min_cluster_net=0.5,
            min_cluster_confidence=0.0,
        )
        assert ranked == []
