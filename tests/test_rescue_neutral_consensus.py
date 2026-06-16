"""
Tests for the ranker rescue of a NEUTRAL scalar consensus.

The scalar ``decide`` collapses mixed panels (fast vs slow modules disagreeing
on horizon) to NEUTRAL, which the scanner turns into a non-tradeable WAITING
setup BEFORE the main-loop executor runs. ``rescue_neutral_direction`` promotes
such a NEUTRAL setup to the ranker's best-EV direction at the scan stage so the
coherent opportunity can reach READY and flow through the unchanged pipeline.

Dependency-light — pure vote math + config, no torch/pandas required.
"""

from config import OpportunityRankerConfig
from brain.directional_consensus import Vote, decide_opportunities
from scanner.pair_scanner import rescue_neutral_direction


# Mixed panel: fast modules agree SHORT (scalp), slow modules agree LONG (swing).
# The scalar ``decide`` would cancel these toward NEUTRAL; the ranker keeps both.
_MIXED_VOTES = [
    Vote("momentum", "SHORT", 0.9, 1.0),
    Vote("volume", "SHORT", 0.8, 1.0),
    Vote("structure", "LONG", 0.9, 1.0),
    Vote("currency_strength", "LONG", 0.8, 1.0),
]


def _candidates(votes=_MIXED_VOTES):
    return decide_opportunities(votes)


def _cfg(**overrides):
    cfg = OpportunityRankerConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestRescueNeutralDirection:
    def test_neutral_with_candidates_is_promoted(self):
        candidates = _candidates()
        assert candidates, "fixture must produce ranked candidates"
        direction, opp = rescue_neutral_direction(
            "NEUTRAL", candidates, _cfg(execute=True, rescue_neutral_consensus=True)
        )
        assert direction in ("LONG", "SHORT")
        assert opp is candidates[0]
        # Rescued direction matches the best-EV (top-ranked) candidate.
        assert direction == candidates[0].direction

    def test_neutral_without_candidates_stays_neutral(self):
        direction, opp = rescue_neutral_direction(
            "NEUTRAL", [], _cfg(execute=True, rescue_neutral_consensus=True)
        )
        assert direction == "NEUTRAL"
        assert opp is None

    def test_directional_consensus_not_rescued(self):
        candidates = _candidates()
        direction, opp = rescue_neutral_direction(
            "LONG", candidates, _cfg(execute=True, rescue_neutral_consensus=True)
        )
        # A consensus that already has a direction is left untouched.
        assert direction == "LONG"
        assert opp is None

    def test_flag_disabled_skips_rescue(self):
        candidates = _candidates()
        direction, opp = rescue_neutral_direction(
            "NEUTRAL", candidates, _cfg(execute=True, rescue_neutral_consensus=False)
        )
        assert direction == "NEUTRAL"
        assert opp is None

    def test_execute_off_skips_rescue(self):
        # Shadow mode: the ranker computes candidates but must not drive direction.
        candidates = _candidates()
        direction, opp = rescue_neutral_direction(
            "NEUTRAL", candidates, _cfg(execute=False, rescue_neutral_consensus=True)
        )
        assert direction == "NEUTRAL"
        assert opp is None

    def test_no_config_skips_rescue(self):
        direction, opp = rescue_neutral_direction("NEUTRAL", _candidates(), None)
        assert direction == "NEUTRAL"
        assert opp is None

    def test_neutral_top_candidate_promotes_best_ev(self):
        # The promoted direction is the highest-EV candidate, not an arbitrary one.
        candidates = _candidates()
        evs = [c.expected_value for c in candidates]
        assert evs == sorted(evs, reverse=True)
        direction, opp = rescue_neutral_direction(
            "NEUTRAL", candidates, _cfg(execute=True, rescue_neutral_consensus=True)
        )
        assert opp.expected_value == max(evs)

    def test_default_config_enables_rescue(self):
        # The shipped default is live + rescue-on, so a NEUTRAL panel is rescued.
        direction, opp = rescue_neutral_direction("NEUTRAL", _candidates(), _cfg())
        assert direction in ("LONG", "SHORT")
        assert opp is not None
