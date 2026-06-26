"""
Tests for the directional consensus voting system.

Dependency-light — no torch, no pandas required for consensus math tests.
Only imports pandas for vote-extractor tests that need DataFrames.
"""

import pytest

from brain.directional_consensus import (
    Vote,
    DirectionDecision,
    decide,
    vote_from_structure,
    vote_from_currency_strength,
    vote_from_volume,
    vote_from_wyckoff,
    vote_from_order_blocks,
    vote_from_fvg,
    vote_from_liquidity,
)


# ── decide() core logic ──────────────────────────────────────────────────

class TestDecideClearMajority:
    def test_clear_long_majority(self):
        votes = [
            Vote("structure", "LONG", 0.9, 3.0),
            Vote("currency_strength", "LONG", 0.8, 2.0),
            Vote("volume", "LONG", 0.6, 1.0),
            Vote("wyckoff", "NEUTRAL", 0.0, 1.5),
        ]
        result = decide(votes, min_net_score=1.0, min_agreement=0.5,
                        high_authority_modules=["structure"], high_authority_oppose_confidence=0.6)
        assert result.direction == "LONG"
        assert result.net_score > 0
        assert result.agreement > 0.5
        assert "structure" in result.contributors

    def test_clear_short_majority(self):
        votes = [
            Vote("structure", "SHORT", 0.8, 3.0),
            Vote("currency_strength", "SHORT", 0.7, 2.0),
            Vote("volume", "SHORT", 0.5, 1.0),
        ]
        result = decide(votes, min_net_score=1.0, min_agreement=0.5,
                        high_authority_modules=["structure"], high_authority_oppose_confidence=0.6)
        assert result.direction == "SHORT"
        assert result.net_score < 0


class TestDecideNeutral:
    def test_tie_returns_neutral(self):
        votes = [
            Vote("structure", "LONG", 0.8, 2.0),
            Vote("currency_strength", "SHORT", 0.8, 2.0),
        ]
        result = decide(votes, min_net_score=1.0, min_agreement=0.5,
                        high_authority_modules=[], high_authority_oppose_confidence=0.6)
        assert result.direction == "NEUTRAL"

    def test_below_min_net_score(self):
        votes = [
            Vote("structure", "LONG", 0.3, 1.0),
            Vote("volume", "SHORT", 0.2, 1.0),
        ]
        result = decide(votes, min_net_score=1.0, min_agreement=0.3,
                        high_authority_modules=[], high_authority_oppose_confidence=0.6)
        assert result.direction == "NEUTRAL"

    def test_below_min_agreement(self):
        votes = [
            Vote("structure", "LONG", 0.9, 3.0),
            Vote("currency_strength", "SHORT", 0.85, 2.0),
            Vote("volume", "SHORT", 0.8, 1.0),
            Vote("wyckoff", "SHORT", 0.7, 1.5),
        ]
        result = decide(votes, min_net_score=0.1, min_agreement=0.9,
                        high_authority_modules=[], high_authority_oppose_confidence=0.6)
        assert result.direction == "NEUTRAL"

    def test_all_neutral_returns_neutral(self):
        votes = [
            Vote("structure", "NEUTRAL", 0.0, 3.0),
            Vote("volume", "NEUTRAL", 0.0, 1.0),
        ]
        result = decide(votes, min_net_score=0.5, min_agreement=0.5,
                        high_authority_modules=[], high_authority_oppose_confidence=0.6)
        assert result.direction == "NEUTRAL"
        assert result.net_score == 0.0
        assert result.agreement == 0.0


class TestHighAuthorityVeto:
    def test_structure_opposes_net_forces_neutral(self):
        """Weighted majority says SHORT but structure says LONG with high confidence → NEUTRAL."""
        votes = [
            Vote("structure", "LONG", 0.9, 3.0),        # +2.7
            Vote("currency_strength", "SHORT", 0.8, 2.0),  # -1.6
            Vote("volume", "SHORT", 0.7, 1.0),             # -0.7
            Vote("wyckoff", "SHORT", 0.8, 1.5),            # -1.2
            Vote("fvg", "SHORT", 0.6, 1.0),                # -0.6
            Vote("liquidity", "SHORT", 0.6, 1.0),          # -0.6
        ]
        result = decide(votes, min_net_score=0.5, min_agreement=0.3,
                        high_authority_modules=["structure", "currency_strength"],
                        high_authority_oppose_confidence=0.6)
        assert result.direction == "NEUTRAL"
        assert "structure" in result.opposed_by

    def test_no_veto_when_authority_agrees(self):
        votes = [
            Vote("structure", "LONG", 0.9, 3.0),
            Vote("currency_strength", "LONG", 0.7, 2.0),
            Vote("volume", "SHORT", 0.3, 1.0),
        ]
        result = decide(votes, min_net_score=0.5, min_agreement=0.3,
                        high_authority_modules=["structure", "currency_strength"],
                        high_authority_oppose_confidence=0.6)
        assert result.direction == "LONG"
        assert result.opposed_by == []

    def test_no_veto_when_authority_confidence_below_threshold(self):
        votes = [
            Vote("structure", "LONG", 0.3, 3.0),   # low confidence
            Vote("currency_strength", "SHORT", 0.8, 2.0),
            Vote("volume", "SHORT", 0.7, 1.0),
            Vote("wyckoff", "SHORT", 0.6, 1.5),
        ]
        result = decide(votes, min_net_score=0.5, min_agreement=0.3,
                        high_authority_modules=["structure"],
                        high_authority_oppose_confidence=0.6)
        assert result.direction == "SHORT"
        assert result.opposed_by == []


class TestOldLogicWouldTradeButConsensusKills:
    """The regression the user is paying for: structure says one thing,
    but the weighted majority opposes — consensus flips or kills."""

    def test_structure_long_but_majority_short(self):
        votes = [
            Vote("structure", "LONG", 0.7, 3.0),           # +2.1
            Vote("currency_strength", "SHORT", 0.9, 2.0),  # -1.8
            Vote("volume", "SHORT", 0.8, 1.0),             # -0.8
            Vote("wyckoff", "SHORT", 0.7, 1.5),            # -1.05
            Vote("order_block", "SHORT", 0.6, 1.0),        # -0.6
            Vote("fvg", "SHORT", 0.5, 1.0),                # -0.5
        ]
        result = decide(votes, min_net_score=0.5, min_agreement=0.3,
                        high_authority_modules=["structure", "currency_strength"],
                        high_authority_oppose_confidence=0.6)
        assert result.direction in ("SHORT", "NEUTRAL")
        assert result.direction != "LONG"


# ── ConsensusConfig validation ────────────────────────────────────────────

class TestConsensusConfigValidation:
    def test_default_config_valid(self):
        from config import ConsensusConfig
        cfg = ConsensusConfig()
        assert cfg.enabled is True
        assert cfg.min_agreement > 0
        assert all(w >= 0 for w in cfg.weights.values())

    def test_negative_weight_rejected(self):
        from config import ConsensusConfig
        with pytest.raises(ValueError, match="weights"):
            ConsensusConfig(weights={"structure": -1.0})

    def test_all_zero_weights_rejected(self):
        from config import ConsensusConfig
        with pytest.raises(ValueError, match="at least one"):
            ConsensusConfig(weights={"structure": 0.0, "volume": 0.0})

    def test_bad_min_agreement_rejected(self):
        from config import ConsensusConfig
        with pytest.raises(ValueError, match="min_agreement"):
            ConsensusConfig(min_agreement=0.0)

    def test_bad_min_agreement_over_one(self):
        from config import ConsensusConfig
        with pytest.raises(ValueError, match="min_agreement"):
            ConsensusConfig(min_agreement=1.5)

    def test_bad_high_authority_oppose_confidence(self):
        from config import ConsensusConfig
        with pytest.raises(ValueError, match="high_authority_oppose_confidence"):
            ConsensusConfig(high_authority_oppose_confidence=0.0)

    def test_high_authority_module_not_in_weights(self):
        from config import ConsensusConfig
        with pytest.raises(ValueError, match="not present in weights"):
            ConsensusConfig(
                weights={"structure": 3.0},
                high_authority_modules=["currency_strength"],
            )


# ── Vote extraction helpers ───────────────────────────────────────────────

class TestVoteFromStructure:
    def test_bullish(self):
        d, c = vote_from_structure({"direction": "BULLISH", "confidence": 0.85})
        assert d == "LONG"
        assert 0.0 < c <= 1.0

    def test_bearish(self):
        d, c = vote_from_structure({"direction": "BEARISH", "confidence": 0.7})
        assert d == "SHORT"

    def test_ranging(self):
        d, c = vote_from_structure({"direction": "RANGING", "confidence": 0.5})
        assert d == "NEUTRAL"
        assert c == 0.0


class TestVoteFromVolume:
    def test_bullish_spike(self):
        from types import SimpleNamespace
        va = SimpleNamespace(confirmation_bias="BULLISH", volume_ratio=2.0, has_spike=True)
        d, c = vote_from_volume(va)
        assert d == "LONG"
        assert c > 0

    def test_neutral_no_spike(self):
        from types import SimpleNamespace
        va = SimpleNamespace(confirmation_bias="BULLISH", volume_ratio=2.0, has_spike=False)
        d, c = vote_from_volume(va)
        assert d == "NEUTRAL"


class TestVoteFromWyckoff:
    def test_spring(self):
        from types import SimpleNamespace
        wa = SimpleNamespace(sub_phase="SPRING", phase_confidence=0.8)
        d, c = vote_from_wyckoff(wa)
        assert d == "LONG"

    def test_upthrust(self):
        from types import SimpleNamespace
        wa = SimpleNamespace(sub_phase="UPTHRUST", phase_confidence=0.7)
        d, c = vote_from_wyckoff(wa)
        assert d == "SHORT"

    def test_other_phase(self):
        from types import SimpleNamespace
        wa = SimpleNamespace(sub_phase="ACCUMULATION", phase_confidence=0.9)
        d, c = vote_from_wyckoff(wa)
        assert d == "NEUTRAL"


class TestVoteFromLiquidity:
    def test_reaction_based_vote_returns_valid(self):
        """vote_from_liquidity returns a valid (direction, confidence) tuple."""
        import pandas as pd
        from brain.liquidity_mapper import LiquidityMapper
        rows = []
        for i in range(30):
            c = 1.0500 + i * 0.0003
            rows.append({"open": c, "high": c + 0.0010, "low": c - 0.0010, "close": c, "time": pd.Timestamp.now()})
        df = pd.DataFrame(rows)
        mapper = LiquidityMapper()
        d, c = vote_from_liquidity(mapper, df, pip_size=0.0001)
        assert d in ("LONG", "SHORT", "NEUTRAL")
        assert 0.0 <= c <= 1.0

    def test_fail_closed_on_none(self):
        from brain.liquidity_mapper import LiquidityMapper
        mapper = LiquidityMapper()
        d, c = vote_from_liquidity(mapper, None, pip_size=0.0001)
        assert d == "NEUTRAL"
        assert c == 0.0

    def test_no_sweep_is_reversal_param(self):
        """The static bias flag must be gone from the signature."""
        import inspect
        sig = inspect.signature(vote_from_liquidity)
        assert "sweep_is_reversal" not in sig.parameters


class TestVoteFromOrderBlocks:
    def test_stronger_bullish(self):
        from types import SimpleNamespace
        from brain.order_block import OBStatus
        obs = [
            SimpleNamespace(kind="BULLISH", strength="STRONG", status=OBStatus.FRESH, top=1.0800, bottom=1.0790),
            SimpleNamespace(kind="BEARISH", strength="WEAK", status=OBStatus.TESTED, bottom=1.0850),
        ]
        d, c = vote_from_order_blocks(obs, current_price=1.0820)
        assert d == "LONG"

    def test_no_obs_neutral(self):
        d, c = vote_from_order_blocks([], current_price=1.0820)
        assert d == "NEUTRAL"


class TestVoteFromFVG:
    def test_stronger_bullish_fvg(self):
        from types import SimpleNamespace
        from brain.fvg_detector import FVGStatus
        fvgs = [
            SimpleNamespace(kind="BULLISH", strength="STRONG", status=FVGStatus.OPEN, top=1.0800, bottom=1.0790),
            SimpleNamespace(kind="BEARISH", strength="WEAK", status=FVGStatus.OPEN, top=1.0860, bottom=1.0850),
        ]
        d, c = vote_from_fvg(fvgs, current_price=1.0820, proximity=0.005)
        assert d == "LONG"


class TestVoteFromCurrencyStrength:
    def test_base_stronger_long(self):
        from types import SimpleNamespace
        rankings = [
            SimpleNamespace(currency="EUR", rank=1),
            SimpleNamespace(currency="USD", rank=5),
        ]
        analysis = SimpleNamespace(rankings=rankings)
        d, c = vote_from_currency_strength("EURUSD", analysis, {"EURUSD": ("EUR", "USD")})
        assert d == "LONG"
        assert c > 0

    def test_quote_stronger_short(self):
        from types import SimpleNamespace
        rankings = [
            SimpleNamespace(currency="EUR", rank=6),
            SimpleNamespace(currency="USD", rank=2),
        ]
        analysis = SimpleNamespace(rankings=rankings)
        d, c = vote_from_currency_strength("EURUSD", analysis, {"EURUSD": ("EUR", "USD")})
        assert d == "SHORT"

    def test_small_diff_neutral(self):
        from types import SimpleNamespace
        rankings = [
            SimpleNamespace(currency="EUR", rank=3),
            SimpleNamespace(currency="USD", rank=4),
        ]
        analysis = SimpleNamespace(rankings=rankings)
        d, c = vote_from_currency_strength("EURUSD", analysis, {"EURUSD": ("EUR", "USD")})
        assert d == "NEUTRAL"


# ── Module abstain on exception ───────────────────────────────────────────

class TestAbstainOnException:
    def test_decide_with_all_abstains(self):
        votes = [
            Vote("structure", "NEUTRAL", 0.0, 0.0),
            Vote("volume", "NEUTRAL", 0.0, 0.0),
        ]
        result = decide(votes, min_net_score=0.5, min_agreement=0.5,
                        high_authority_modules=[], high_authority_oppose_confidence=0.6)
        assert result.direction == "NEUTRAL"
        assert result.net_score == 0.0


class TestDecisionSummary:
    def test_summary_format(self):
        result = DirectionDecision(
            direction="LONG", net_score=2.5, agreement=0.8,
            contributors=["structure", "volume"], opposed_by=[],
        )
        s = result.summary
        assert "LONG" in s
        assert "2.50" in s
        assert "80%" in s


class TestVoteSigned:
    def test_long_positive(self):
        v = Vote("test", "LONG", 0.8, 2.0)
        assert v.signed == pytest.approx(1.6)

    def test_short_negative(self):
        v = Vote("test", "SHORT", 0.5, 3.0)
        assert v.signed == pytest.approx(-1.5)

    def test_neutral_zero(self):
        v = Vote("test", "NEUTRAL", 1.0, 5.0)
        assert v.signed == 0.0
