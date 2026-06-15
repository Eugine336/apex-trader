"""
Tests for the opportunity ranker.

Dependency-light — pure vote math, no torch/pandas required.
Verifies clustering by direction × timeframe, EV scoring, ranking order, and
the edge cases that distinguish "graded open-ended answer" from the old scalar
hand-raising collapse.
"""

import pytest

from brain.directional_consensus import Vote, decide_opportunities
from brain.opportunity_ranker import (
    SCALP,
    SWING,
    Opportunity,
    classify_timeframe,
    cluster_votes,
    rank_opportunities,
)


# ── classification & clustering ───────────────────────────────────────────

class TestClassifyAndCluster:
    def test_classify_defaults(self):
        assert classify_timeframe("momentum", ["momentum"], ["structure"]) == SCALP
        assert classify_timeframe("structure", ["momentum"], ["structure"]) == SWING
        # Unknown modules default to the conservative SWING horizon.
        assert classify_timeframe("mystery", ["momentum"], ["structure"]) == SWING

    def test_neutral_and_zero_votes_dropped(self):
        votes = [
            Vote("structure", "NEUTRAL", 0.0, 1.0),
            Vote("momentum", "LONG", 0.0, 1.0),   # zero confidence
            Vote("vwap", "SHORT", 0.5, 0.0),       # zero weight
            Vote("volume", "LONG", 0.7, 1.0),      # kept
        ]
        clusters = cluster_votes(votes)
        assert list(clusters.keys()) == [("LONG", SCALP)]

    def test_two_independent_clusters_form(self):
        # Fast modules agree SHORT (scalp); slow modules agree LONG (swing).
        votes = [
            Vote("momentum", "SHORT", 0.8, 1.0),
            Vote("volume", "SHORT", 0.7, 1.0),
            Vote("structure", "LONG", 0.9, 1.0),
            Vote("currency_strength", "LONG", 0.6, 1.0),
        ]
        clusters = cluster_votes(votes)
        assert ("SHORT", SCALP) in clusters
        assert ("LONG", SWING) in clusters
        assert len(clusters) == 2


# ── EV scoring & ranking ──────────────────────────────────────────────────

class TestRanking:
    def test_empty_votes_yields_no_opportunity(self):
        assert rank_opportunities([]) == []

    def test_all_neutral_yields_nothing(self):
        votes = [Vote("structure", "NEUTRAL", 0.0, 1.0), Vote("vwap", "NEUTRAL", 0.0, 1.0)]
        assert rank_opportunities(votes) == []

    def test_conflicting_timeframes_both_survive(self):
        # The old scalar would cancel these to ~NEUTRAL. Here both are kept.
        votes = [
            Vote("momentum", "SHORT", 0.9, 1.0),
            Vote("volume", "SHORT", 0.8, 1.0),
            Vote("structure", "LONG", 0.9, 1.0),
            Vote("currency_strength", "LONG", 0.8, 1.0),
        ]
        opps = rank_opportunities(votes)
        dirs = {(o.direction, o.timeframe_class) for o in opps}
        assert ("SHORT", SCALP) in dirs
        assert ("LONG", SWING) in dirs

    def test_sorted_by_expected_value_desc(self):
        # Same timeframe class (scalp) so reward:risk is equal and the EV
        # ordering is driven by confidence/coherence. A strong, multi-module
        # SHORT scalp should out-EV a weak lone LONG scalp.
        votes = [
            Vote("momentum", "SHORT", 0.95, 1.0),
            Vote("volume", "SHORT", 0.9, 1.0),
            Vote("vwap", "SHORT", 0.85, 1.0),
            Vote("liquidity", "LONG", 0.2, 1.0),
        ]
        opps = rank_opportunities(votes)
        assert len(opps) >= 2
        evs = [o.expected_value for o in opps]
        assert evs == sorted(evs, reverse=True)
        assert opps[0].direction == "SHORT"

    def test_swing_has_higher_rr_than_scalp(self):
        long_swing = [Vote("structure", "LONG", 0.8, 1.0)]
        long_scalp = [Vote("momentum", "LONG", 0.8, 1.0)]
        swing = rank_opportunities(long_swing)[0]
        scalp = rank_opportunities(long_scalp)[0]
        assert swing.reward_risk > scalp.reward_risk

    def test_min_ev_filters_weak_ideas(self):
        votes = [Vote("momentum", "LONG", 0.05, 1.0)]
        # With a high EV floor nothing should clear it.
        assert rank_opportunities(votes, min_expected_value=5.0) == []

    def test_min_contributors_filters(self):
        votes = [Vote("momentum", "LONG", 0.8, 1.0)]
        assert rank_opportunities(votes, min_cluster_contributors=2) == []

    def test_coherence_penalises_same_tf_opposition(self):
        # Two SHORT scalps with an opposing LONG scalp → lower coherence than
        # the same SHORT scalp uncontested on its horizon.
        contested = [
            Vote("momentum", "SHORT", 0.8, 1.0),
            Vote("volume", "SHORT", 0.8, 1.0),
            Vote("vwap", "LONG", 0.8, 1.0),
        ]
        clean = [
            Vote("momentum", "SHORT", 0.8, 1.0),
            Vote("volume", "SHORT", 0.8, 1.0),
        ]
        c_short = next(o for o in rank_opportunities(contested) if o.direction == "SHORT")
        clean_short = next(o for o in rank_opportunities(clean) if o.direction == "SHORT")
        assert c_short.coherence < clean_short.coherence
        assert c_short.expected_value < clean_short.expected_value

    def test_decide_opportunities_bridge_matches(self):
        votes = [
            Vote("momentum", "LONG", 0.8, 1.0),
            Vote("volume", "LONG", 0.7, 1.0),
        ]
        bridged = decide_opportunities(votes)
        direct = rank_opportunities(votes)
        assert len(bridged) == len(direct) == 1
        assert bridged[0].direction == direct[0].direction
        assert bridged[0].expected_value == pytest.approx(direct[0].expected_value)

    def test_opportunity_summary_renders(self):
        opp = rank_opportunities([Vote("momentum", "LONG", 0.8, 1.0)])[0]
        assert isinstance(opp, Opportunity)
        assert "LONG" in opp.summary and "EV=" in opp.summary
