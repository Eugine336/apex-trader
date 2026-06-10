"""
Tests for the consensus participation gate and logging.

Imports only brain.directional_consensus and config — no torch, no pandas.
"""

import pytest
from unittest.mock import patch

from brain.directional_consensus import Vote, decide
from config import ConsensusConfig


# ── Helpers ──────────────────────────────────────────────────────────────

_HA = ["structure", "currency_strength"]
_HA_CONF = 0.6


def _decide(votes, min_contributors=2, **kw):
    return decide(
        votes,
        min_net_score=kw.get("min_net_score", 1.5),
        min_agreement=kw.get("min_agreement", 0.6),
        high_authority_modules=kw.get("high_authority_modules", _HA),
        high_authority_oppose_confidence=kw.get("ha_conf", _HA_CONF),
        min_contributors=min_contributors,
    )


# ── Gate behaviour ───────────────────────────────────────────────────────

class TestParticipationGate:
    def test_lone_voter_blocked_with_min_2(self):
        """Structure alone (weight 3.0, conf 1.0) → NEUTRAL when min_contributors=2."""
        votes = [
            Vote("structure", "LONG", 1.0, 3.0),
            Vote("volume", "NEUTRAL", 0.0, 1.0),
            Vote("fvg", "NEUTRAL", 0.0, 1.0),
        ]
        d = _decide(votes, min_contributors=2)
        assert d.direction == "NEUTRAL"

    def test_lone_voter_allowed_with_min_1(self):
        """Same votes pass when min_contributors=1 (gate effectively off)."""
        votes = [
            Vote("structure", "LONG", 1.0, 3.0),
            Vote("volume", "NEUTRAL", 0.0, 1.0),
            Vote("fvg", "NEUTRAL", 0.0, 1.0),
        ]
        d = _decide(votes, min_contributors=1)
        assert d.direction == "LONG"

    def test_two_aligned_voters_pass_with_min_2(self):
        """Two non-neutral voters in the same direction clears the gate."""
        votes = [
            Vote("structure", "LONG", 0.8, 3.0),
            Vote("currency_strength", "LONG", 0.7, 2.0),
            Vote("volume", "NEUTRAL", 0.0, 1.0),
        ]
        d = _decide(votes, min_contributors=2)
        assert d.direction == "LONG"

    def test_two_opposed_voters_pass_gate_but_may_fail_agreement(self):
        """Two voters on opposite sides still clears the participation gate
        (gate counts all non-neutral, regardless of side)."""
        votes = [
            Vote("structure", "LONG", 1.0, 3.0),
            Vote("currency_strength", "SHORT", 0.9, 2.0),
        ]
        d = _decide(votes, min_contributors=2)
        # Gate passes (2 contributors), but high-authority opposition
        # or agreement may force NEUTRAL depending on thresholds
        assert d.net_score != 0.0  # net was computed, gate didn't short-circuit

    def test_all_neutral_returns_neutral(self):
        """All voters NEUTRAL → NEUTRAL regardless of gate."""
        votes = [
            Vote("structure", "NEUTRAL", 0.0, 3.0),
            Vote("volume", "NEUTRAL", 0.0, 1.0),
        ]
        d = _decide(votes, min_contributors=1)
        assert d.direction == "NEUTRAL"
        assert d.agreement == 0.0

    def test_empty_votes_returns_neutral(self):
        d = _decide([], min_contributors=1)
        assert d.direction == "NEUTRAL"

    def test_gate_ordered_after_other_filters(self):
        """If net < min_net_score, that fires before the participation gate."""
        votes = [
            Vote("structure", "LONG", 0.1, 1.0),  # net = 0.1, below 1.5
        ]
        d = _decide(votes, min_contributors=2, min_net_score=1.5)
        assert d.direction == "NEUTRAL"


# ── Logging ──────────────────────────────────────────────────────────────

class TestParticipationLogging:
    def test_participation_logged_on_every_call(self):
        votes = [
            Vote("structure", "LONG", 0.8, 3.0),
            Vote("volume", "NEUTRAL", 0.0, 1.0),
        ]
        with patch("brain.directional_consensus.logger") as mock_log:
            _decide(votes, min_contributors=1)
            log_calls = [
                str(c) for c in mock_log.info.call_args_list
            ]
            participation_logs = [
                c for c in log_calls if "participation:" in c
            ]
            assert len(participation_logs) >= 1

    def test_participation_log_includes_module_names(self):
        votes = [
            Vote("structure", "LONG", 0.8, 3.0),
            Vote("volume", "NEUTRAL", 0.0, 1.0),
            Vote("fvg", "SHORT", 0.5, 1.0),
        ]
        with patch("brain.directional_consensus.logger") as mock_log:
            _decide(votes, min_contributors=1)
            all_args = " ".join(
                str(c) for c in mock_log.info.call_args_list
            )
            assert "structure" in all_args
            assert "fvg" in all_args
            assert "volume" in all_args

    def test_all_neutral_still_logs_participation(self):
        votes = [
            Vote("structure", "NEUTRAL", 0.0, 3.0),
            Vote("volume", "NEUTRAL", 0.0, 1.0),
        ]
        with patch("brain.directional_consensus.logger") as mock_log:
            _decide(votes, min_contributors=1)
            log_calls = [
                str(c) for c in mock_log.info.call_args_list
            ]
            participation_logs = [
                c for c in log_calls if "participation:" in c
            ]
            assert len(participation_logs) >= 1


# ── Config validation ────────────────────────────────────────────────────

class TestConsensusConfigMinContributors:
    def test_default_is_2(self):
        cc = ConsensusConfig()
        assert cc.min_contributors == 2

    def test_valid_value_accepted(self):
        cc = ConsensusConfig(min_contributors=3)
        assert cc.min_contributors == 3

    def test_min_contributors_zero_raises(self):
        with pytest.raises(ValueError, match="min_contributors"):
            ConsensusConfig(min_contributors=0)

    def test_min_contributors_negative_raises(self):
        with pytest.raises(ValueError, match="min_contributors"):
            ConsensusConfig(min_contributors=-1)

    def test_min_contributors_float_raises(self):
        with pytest.raises(ValueError, match="min_contributors"):
            ConsensusConfig(min_contributors=2.5)

    def test_min_contributors_one_accepted(self):
        cc = ConsensusConfig(min_contributors=1)
        assert cc.min_contributors == 1


# ── Default config assertions ────────────────────────────────────────────

class TestConsensusConfigDefaults:
    """Assert the production defaults: equal weights, no structure veto, 0.67 agreement."""

    def test_all_weights_equal_one(self):
        cc = ConsensusConfig()
        for module, weight in cc.weights.items():
            assert weight == 1.0, f"{module} weight should be 1.0, got {weight}"

    def test_nine_modules_present(self):
        cc = ConsensusConfig()
        expected = {
            "structure", "currency_strength", "wyckoff", "volume",
            "order_block", "fvg", "liquidity", "momentum", "vwap",
        }
        assert set(cc.weights.keys()) == expected

    def test_high_authority_is_currency_strength_only(self):
        cc = ConsensusConfig()
        assert cc.high_authority_modules == ["currency_strength"]

    def test_structure_not_in_high_authority(self):
        cc = ConsensusConfig()
        assert "structure" not in cc.high_authority_modules

    def test_min_agreement_is_067(self):
        cc = ConsensusConfig()
        assert cc.min_agreement == 0.67
