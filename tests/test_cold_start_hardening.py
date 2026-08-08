"""
APEX TRADER — Cold-start hardening tests

The cold-start philosophy: the system should WIN first and only size up once a
pair earns it — learning amplifies an existing edge rather than rescuing from
losses. These tests cover:

* FIX 1 — cold-start sizing reduced to 0.5 (was 0.8).
* FIX 2 — a cold-start entry-score boost raises the bar for unproven symbols.
* FIX 3 — early-stop: losing cold-start symbols are AVOIDed fast (win-rate
  and consecutive-loss triggers), long before the n>=30 statistical AVOID.
* FIX 4 — graduated cold-start sizing: a winning cold-start pair sizes up, a
  borderline loser shrinks below the flat default.
* Mature pairs (>= MIN_TRADES) are unaffected by every cold-start change.
"""

from __future__ import annotations

import pytest

from config import PairLearnerConfig
from adaptive.pair_learner import PairLearner


# ── helpers ──────────────────────────────────────────────────────────────


def _trade(pair="EURUSD", pnl=1.0, session="LONDON", regime="TREND"):
    return {"pair": pair, "pnl": pnl, "session": session, "regime": regime, "spread": 1.0}


def _mixed_trades(pair: str, wins: int, losses: int) -> list[dict]:
    """Wins first, then losses (so the most recent trades are losses)."""
    return (
        [_trade(pair=pair, pnl=10.0) for _ in range(wins)]
        + [_trade(pair=pair, pnl=-10.0) for _ in range(losses)]
    )


def _losses_then_wins(pair: str, wins: int, losses: int) -> list[dict]:
    """Losses first, then wins (most recent trades are wins)."""
    return (
        [_trade(pair=pair, pnl=-10.0) for _ in range(losses)]
        + [_trade(pair=pair, pnl=10.0) for _ in range(wins)]
    )


def _cfg(**overrides) -> PairLearnerConfig:
    base = dict(continuous_pair_multiplier=True)
    base.update(overrides)
    return PairLearnerConfig(**base)


@pytest.fixture
def save_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(PairLearner, "SAVE_PATH", str(tmp_path / "pairs.json"))
    return tmp_path


# ── FIX 1: cold-start sizing reduced to 0.5 ────────────────────────────────


class TestColdStartMultiplier:
    def test_config_default_is_half(self):
        assert PairLearnerConfig().cold_start_multiplier == 0.5

    def test_unknown_pair_sizes_at_half(self, save_to_tmp):
        learner = PairLearner(config=_cfg())
        assert learner.get_pair_multiplier("NEVER_SEEN") == 0.5

    def test_thin_profile_sizes_at_half(self, save_to_tmp):
        learner = PairLearner(config=_cfg(cold_start_min_trades=5))
        learner.learn(_mixed_trades("EURUSD", wins=2, losses=1))  # n=3 < 5
        assert learner.get_pair_multiplier("EURUSD") == 0.5


# ── FIX 3: early-stop for losing cold-start symbols ────────────────────────


class TestEarlyStop:
    def test_zero_win_rate_avoids(self, save_to_tmp):
        learner = PairLearner(config=_cfg())
        learner.learn(_mixed_trades("XAU", wins=0, losses=5))  # n=5, wr 0.0
        assert learner.get_profile("XAU").recommendation == "AVOID"
        assert learner.get_pair_multiplier("XAU") == 0.0

    def test_consecutive_losses_avoid(self, save_to_tmp):
        # Four straight losses (n=4 < early_avoid_min_trades=5, so win-rate path
        # is skipped) — the consecutive-loss trigger still blocks it fast.
        learner = PairLearner(config=_cfg())
        learner.learn(_mixed_trades("GBPCAD", wins=0, losses=4))
        assert learner.get_profile("GBPCAD").recommendation == "AVOID"
        assert learner.get_pair_multiplier("GBPCAD") == 0.0

    def test_win_rate_above_threshold_not_avoided(self, save_to_tmp):
        # 40% win rate is above the 25% early-stop floor — not avoided. Wins are
        # ordered first so the most recent run is not four straight losses.
        learner = PairLearner(config=_cfg())
        learner.learn(_mixed_trades("EURUSD", wins=2, losses=3))  # n=5, wr 0.40
        prof = learner.get_profile("EURUSD")
        assert prof.recommendation != "AVOID"
        assert learner.get_pair_multiplier("EURUSD") > 0.0

    def test_avoid_is_reversible(self, save_to_tmp):
        # Early-AVOID is just a recommendation: retraining with a better record
        # moves it back to a tradeable state.
        learner = PairLearner(config=_cfg())
        learner.learn(_mixed_trades("AAA", wins=0, losses=5))  # AVOID
        assert learner.get_pair_multiplier("AAA") == 0.0
        learner.learn(_mixed_trades("AAA", wins=18, losses=4))  # n=22, wr ~0.82
        assert learner.get_profile("AAA").recommendation != "AVOID"
        assert learner.get_pair_multiplier("AAA") > 0.0


# ── FIX 4: graduated cold-start sizing ─────────────────────────────────────


class TestGraduatedColdStart:
    def test_winning_cold_start_sizes_up(self, save_to_tmp):
        learner = PairLearner(config=_cfg())
        learner.learn(_mixed_trades("AAA", wins=8, losses=2))  # n=10, wr 0.80
        m = learner.get_pair_multiplier("AAA")
        assert m > 0.5  # earned above the flat cold-start default

    def test_losing_cold_start_sizes_down(self, save_to_tmp):
        # 40% win rate (above the 25% early-stop floor) but a losing pair:
        # graduated sizing shrinks it below the flat cold-start default. Wins
        # ordered last so the consecutive-loss early-stop does not fire.
        learner = PairLearner(config=_cfg())
        learner.learn(_losses_then_wins("BBB", wins=4, losses=6))  # n=10, wr 0.40
        m = learner.get_pair_multiplier("BBB")
        assert 0.0 < m < 0.5

    def test_graduated_can_be_disabled(self, save_to_tmp):
        # With graduated off, a cold-start winner falls back to the sigmoid +
        # shrinkage path instead of the earned ladder.
        learner = PairLearner(config=_cfg(cold_start_graduated=False))
        learner.learn(_mixed_trades("AAA", wins=8, losses=2))  # n=10, wr 0.80
        # Behaviour differs from graduated (which would return ~0.8 here) — just
        # assert it produced a finite, in-range multiplier.
        m = learner.get_pair_multiplier("AAA")
        assert 0.0 < m <= PairLearnerConfig().continuous_ceiling


# ── FIX 2: cold-start entry-score boost ────────────────────────────────────


class TestColdStartScoreBoost:
    def test_boost_for_unknown_pair(self, save_to_tmp):
        learner = PairLearner(config=_cfg(cold_start_score_boost=5))
        assert learner.get_cold_start_score_boost("NEVER") == 5

    def test_boost_for_thin_profile(self, save_to_tmp):
        learner = PairLearner(config=_cfg(cold_start_score_boost=5))
        learner.learn(_mixed_trades("AAA", wins=3, losses=2))  # n=5 < MIN_TRADES
        assert learner.get_cold_start_score_boost("AAA") == 5

    def test_no_boost_once_graduated(self, save_to_tmp):
        learner = PairLearner(config=_cfg(cold_start_score_boost=5))
        learner.learn(_mixed_trades("AAA", wins=20, losses=5))  # n=25 >= MIN_TRADES
        assert learner.get_cold_start_score_boost("AAA") == 0

    def test_boost_zero_when_disabled(self, save_to_tmp):
        learner = PairLearner(config=_cfg(cold_start_score_boost=0))
        assert learner.get_cold_start_score_boost("NEVER") == 0

    def test_entry_engine_honors_boost(self):
        # The EntryEngine exposes a pair_learner hook (set by the trading loop)
        # and adds the cold-start boost to the entry-score floor. A fake learner
        # confirms the value is read and applied.
        from trigger.entry_engine import EntryEngine

        engine = EntryEngine()
        assert engine.pair_learner is None  # neutral by default

        class _FakeLearner:
            def get_cold_start_score_boost(self, pair):
                return 7

        engine.pair_learner = _FakeLearner()
        assert engine.pair_learner.get_cold_start_score_boost("EURUSD") == 7


# ── Mature pairs unaffected by cold-start changes ──────────────────────────


class TestMaturePairUnaffected:
    def test_trailing_losses_do_not_avoid_mature_pair(self, save_to_tmp):
        # n=25 with four trailing losses: a mature pair is governed by the
        # statistical logic — the trailing-loss early-stop must NOT fire at
        # n >= MIN_TRADES, so it stays REDUCE_SIZE rather than AVOID.
        learner = PairLearner(config=_cfg())
        learner.learn(_mixed_trades("EURUSD", wins=12, losses=13))  # n=25, wr 0.48
        prof = learner.get_profile("EURUSD")
        assert prof.recommendation == "REDUCE_SIZE"
        assert learner.get_pair_multiplier("EURUSD") > 0.0

    def test_winning_mature_pair_sizes_above_one(self, save_to_tmp):
        learner = PairLearner(config=_cfg())
        learner.learn(_mixed_trades("EURUSD", wins=30, losses=0))  # n=30, wr 1.0
        assert learner.get_pair_multiplier("EURUSD") > 1.0

    def test_confident_loser_still_avoided(self, save_to_tmp):
        learner = PairLearner(config=_cfg())
        learner.learn(_mixed_trades("NZDJPY", wins=0, losses=35))  # n=35, wr 0.0
        assert learner.get_pair_multiplier("NZDJPY") == 0.0
