"""
APEX TRADER — Adaptive Optimizer Tests
Verifies trade analysis, score optimisation, regime/pair/session learning,
and the master optimizer's adjustment pipeline.
"""

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
import numpy as np

from adaptive.trade_analyzer import TradeAnalyzer, PerformanceProfile
from adaptive.score_optimizer import ScoreOptimizer, ScoringWeights
from adaptive.regime_learner import RegimeLearner, RegimeStrategy
from adaptive.pair_learner import PairLearner, PairProfile
from adaptive.session_learner import SessionLearner, SessionProfile
from adaptive.optimizer import AdaptiveOptimizer, TradeAdjustments, OptimizationReport


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _make_trade(
    pair: str = "EURUSD",
    pnl: float = 10.0,
    session: str = "LONDON",
    regime: str = "TRENDING_STRONG",
    entry_type: str = "FVG",
    score: int = 90,
    spread: float = 1.2,
    time_to_exit: float = 25.0,
    day_of_week: str = "Tuesday",
    confluences_tags: list[str] | None = None,
) -> dict:
    return {
        "pair": pair,
        "pnl": pnl,
        "session": session,
        "regime": regime,
        "entry_type": entry_type,
        "score": score,
        "spread": spread,
        "time_to_exit": time_to_exit,
        "day_of_week": day_of_week,
        "confluences_tags": confluences_tags or ["structure", "fvg", "session"],
    }


def _generate_trades(n: int = 100, win_rate: float = 0.75) -> list[dict]:
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
    sessions = ["LONDON", "NEW_YORK", "TOKYO", "OVERLAP_LONDON_NY"]
    regimes = ["TRENDING_STRONG", "TRENDING_WEAK", "RANGING"]
    entry_types = ["FVG", "OB", "SWEEP", "CHOCH"]
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    all_tags = ["structure", "order_block", "fvg", "mtf_confluence", "session", "news", "currency_strength", "m1_trigger", "liquidity_sweep"]

    trades: list[dict] = []
    for _ in range(n):
        is_win = random.random() < win_rate
        pnl = round(random.uniform(5, 40), 2) if is_win else round(random.uniform(-30, -3), 2)
        tags = random.sample(all_tags, k=random.randint(3, 6))
        trades.append(_make_trade(
            pair=random.choice(pairs),
            pnl=pnl,
            session=random.choice(sessions),
            regime=random.choice(regimes),
            entry_type=random.choice(entry_types),
            score=random.randint(80, 100),
            spread=round(random.uniform(0.5, 3.0), 2),
            time_to_exit=round(random.uniform(5, 120), 2),
            day_of_week=random.choice(days),
            confluences_tags=tags,
        ))
    return trades


# ──────────────────────────────────────────────────────────────────────────
# TradeAnalyzer
# ──────────────────────────────────────────────────────────────────────────

class TestTradeAnalyzer:
    def test_empty_trades(self):
        profile = TradeAnalyzer().analyze_all([])
        assert profile.total_trades == 0
        assert profile.win_rate == 0.0

    def test_all_winners(self):
        trades = [_make_trade(pnl=10) for _ in range(20)]
        profile = TradeAnalyzer().analyze_all(trades)
        assert profile.win_rate == 1.0
        assert profile.profit_factor == float("inf")
        assert profile.max_consecutive_wins == 20
        assert profile.max_consecutive_losses == 0

    def test_mixed_trades(self):
        trades = [_make_trade(pnl=15)] * 8 + [_make_trade(pnl=-10)] * 2
        profile = TradeAnalyzer().analyze_all(trades)
        assert profile.total_trades == 10
        assert profile.win_rate == 0.8
        assert profile.avg_winner_pips == 15.0
        assert profile.avg_loser_pips == 10.0
        assert profile.profit_factor > 0

    def test_analyze_by_pair(self):
        trades = (
            [_make_trade(pair="EURUSD", pnl=20)] * 5
            + [_make_trade(pair="GBPUSD", pnl=-5)] * 5
        )
        by_pair = TradeAnalyzer().analyze_by_pair(trades)
        assert "EURUSD" in by_pair
        assert "GBPUSD" in by_pair
        assert by_pair["EURUSD"].win_rate == 1.0
        assert by_pair["GBPUSD"].win_rate == 0.0

    def test_analyze_by_session(self):
        trades = [_make_trade(session="LONDON", pnl=10)] * 5
        by_session = TradeAnalyzer().analyze_by_session(trades)
        assert "LONDON" in by_session

    def test_analyze_by_regime(self):
        trades = [_make_trade(regime="RANGING", pnl=-5)] * 10
        by_regime = TradeAnalyzer().analyze_by_regime(trades)
        assert by_regime["RANGING"].win_rate == 0.0

    def test_analyze_by_entry_type(self):
        trades = [_make_trade(entry_type="FVG", pnl=15)] * 10
        by_type = TradeAnalyzer().analyze_by_entry_type(trades)
        assert by_type["FVG"].win_rate == 1.0

    def test_analyze_by_day(self):
        trades = [_make_trade(day_of_week="Monday", pnl=10)] * 5
        by_day = TradeAnalyzer().analyze_by_day_of_week(trades)
        assert "Monday" in by_day

    def test_losing_patterns(self):
        trades = (
            [_make_trade(pair="NZDJPY", session="TOKYO", pnl=-8)] * 15
            + [_make_trade(pair="EURUSD", session="LONDON", pnl=12)] * 15
        )
        patterns = TradeAnalyzer().get_losing_patterns(trades)
        assert len(patterns) >= 1
        assert patterns[0]["win_rate"] < 0.45

    def test_sharpe_calculation(self):
        profile = TradeAnalyzer().analyze_all(
            [_make_trade(pnl=10)] * 5 + [_make_trade(pnl=-3)] * 2
        )
        assert profile.sharpe_ratio != 0.0

    def test_streaks(self):
        trades = (
            [_make_trade(pnl=10)] * 4
            + [_make_trade(pnl=-5)] * 3
            + [_make_trade(pnl=8)] * 2
        )
        profile = TradeAnalyzer().analyze_all(trades)
        assert profile.max_consecutive_wins == 4
        assert profile.max_consecutive_losses == 3


# ──────────────────────────────────────────────────────────────────────────
# ScoreOptimizer
# ──────────────────────────────────────────────────────────────────────────

class TestScoreOptimizer:
    def test_default_weights_sum_to_100(self):
        w = ScoringWeights()
        assert w.total == 100

    def test_optimize_below_min_trades_returns_defaults(self):
        opt = ScoreOptimizer()
        trades = _generate_trades(n=10)
        result = opt.optimize(trades, min_trades=50)
        assert result.total == 100

    def test_optimize_produces_valid_weights(self):
        random.seed(42)
        opt = ScoreOptimizer()
        trades = _generate_trades(n=120, win_rate=0.7)
        result = opt.optimize(trades, min_trades=50)
        assert result.total == 100
        d = result.as_dict()
        assert all(v >= opt.MIN_WEIGHT for v in d.values())

    def test_gradual_changes(self):
        random.seed(42)
        opt = ScoreOptimizer()
        default = ScoringWeights()
        trades = _generate_trades(n=120, win_rate=0.7)
        result = opt.optimize(trades, min_trades=50)

        dd = default.as_dict()
        rd = result.as_dict()
        for key in dd:
            diff = abs(dd[key] - rd[key])
            assert diff <= 10, f"{key} shifted by {diff} — too aggressive"

    def test_factor_effectiveness(self):
        trades = _generate_trades(n=100)
        eff = ScoreOptimizer().get_factor_effectiveness(trades)
        assert "structure" in eff
        assert "fvg" in eff
        assert "lift" in eff["structure"]

    def test_save_and_load_weights(self, tmp_path):
        opt = ScoreOptimizer()
        w = ScoringWeights(structure_weight=22, order_block_weight=16,
                           fvg_weight=11, mtf_confluence_weight=13,
                           session_weight=10, news_weight=7,
                           currency_strength_weight=8,
                           m1_trigger_weight=7, liquidity_sweep_weight=6)
        fp = str(tmp_path / "weights.json")
        opt.save_weights(w, fp)
        loaded = opt.load_weights(fp)
        assert loaded.structure_weight == 22
        assert loaded.total == 100

    def test_load_missing_file_returns_defaults(self, tmp_path):
        loaded = ScoreOptimizer().load_weights(str(tmp_path / "nonexistent.json"))
        assert loaded.total == 100


# ──────────────────────────────────────────────────────────────────────────
# RegimeLearner
# ──────────────────────────────────────────────────────────────────────────

class TestRegimeLearner:
    def test_learn_returns_strategies(self):
        trades = _generate_trades(n=100)
        strategies = RegimeLearner().learn(trades)
        assert len(strategies) > 0
        for strat in strategies.values():
            assert isinstance(strat, RegimeStrategy)
            assert 0.0 <= strat.win_rate <= 1.0

    def test_default_strategy_for_unknown(self):
        learner = RegimeLearner()
        strat = learner.get_strategy("ALIEN_REGIME")
        assert strat.regime == "ALIEN_REGIME"
        assert strat.optimal_score_threshold == 85

    def test_should_trade_losing_regime(self):
        trades = [_make_trade(regime="CHOPPY", pnl=-10)] * 60
        learner = RegimeLearner()
        learner.learn(trades)
        should, reason = learner.should_trade_regime("CHOPPY")
        assert should is False
        assert "avoiding" in reason.lower() or "40%" in reason

    def test_should_trade_winning_regime(self):
        trades = [_make_trade(regime="TRENDING_STRONG", pnl=15)] * 50
        learner = RegimeLearner()
        learner.learn(trades)
        should, _ = learner.should_trade_regime("TRENDING_STRONG")
        assert should is True


# ──────────────────────────────────────────────────────────────────────────
# PairLearner
# ──────────────────────────────────────────────────────────────────────────

class TestPairLearner:
    def test_learn_returns_profiles(self):
        trades = _generate_trades(n=100)
        profiles = PairLearner().learn(trades)
        assert len(profiles) > 0
        for prof in profiles.values():
            assert isinstance(prof, PairProfile)

    def test_pair_multiplier_good_pair(self):
        trades = [_make_trade(pair="EURUSD", pnl=15)] * 30
        learner = PairLearner()
        learner.learn(trades)
        assert learner.get_pair_multiplier("EURUSD") == 1.0

    def test_pair_multiplier_bad_pair(self):
        trades = [_make_trade(pair="NZDJPY", pnl=-8)] * 35
        learner = PairLearner()
        learner.learn(trades)
        assert learner.get_pair_multiplier("NZDJPY") == 0.0

    def test_pair_multiplier_unknown(self):
        learner = PairLearner()
        assert learner.get_pair_multiplier("UNKNOWN") == 0.8

    def test_recommended_pairs(self):
        trades = (
            [_make_trade(pair="EURUSD", pnl=15)] * 25
            + [_make_trade(pair="GBPUSD", pnl=12)] * 25
        )
        learner = PairLearner()
        learner.learn(trades)
        recommended = learner.get_recommended_pairs()
        assert "EURUSD" in recommended


# ──────────────────────────────────────────────────────────────────────────
# SessionLearner
# ──────────────────────────────────────────────────────────────────────────

class TestSessionLearner:
    def test_learn_returns_profiles(self):
        trades = _generate_trades(n=100)
        profiles = SessionLearner().learn(trades)
        assert len(profiles) > 0
        for prof in profiles.values():
            assert isinstance(prof, SessionProfile)

    def test_aggression_for_winning_session(self):
        trades = [_make_trade(session="OVERLAP_LONDON_NY", pnl=20)] * 20
        learner = SessionLearner()
        learner.learn(trades)
        assert learner.get_session_aggression("OVERLAP_LONDON_NY") == "AGGRESSIVE"

    def test_aggression_for_losing_session(self):
        trades = [_make_trade(session="TOKYO", pnl=-8)] * 20
        learner = SessionLearner()
        learner.learn(trades)
        assert learner.get_session_aggression("TOKYO") == "AVOID"

    def test_aggression_unknown_session(self):
        learner = SessionLearner()
        assert learner.get_session_aggression("UNKNOWN") == "NORMAL"


# ──────────────────────────────────────────────────────────────────────────
# AdaptiveOptimizer (master controller)
# ──────────────────────────────────────────────────────────────────────────

class TestAdaptiveOptimizer:
    def test_run_optimization(self):
        random.seed(42)
        trades = _generate_trades(n=120, win_rate=0.75)
        adapter = AdaptiveOptimizer()
        report = adapter.run_optimization(trades)

        assert isinstance(report, OptimizationReport)
        assert report.trades_analyzed == 120
        assert report.new_scoring_weights.total == 100
        assert report.overall_performance is not None
        assert report.overall_performance.total_trades == 120

    def test_get_trade_adjustments_defaults(self):
        adapter = AdaptiveOptimizer()
        adj = adapter.get_trade_adjustments("EURUSD", "TRENDING_STRONG", "LONDON")
        assert isinstance(adj, TradeAdjustments)
        assert adj.should_trade is True
        assert adj.position_size_multiplier > 0

    def test_adjustments_after_learning(self):
        random.seed(42)
        trades = (
            [_make_trade(pair="EURUSD", regime="TRENDING_STRONG",
                         session="LONDON", pnl=15)] * 40
            + [_make_trade(pair="EURUSD", regime="TRENDING_STRONG",
                           session="LONDON", pnl=-8)] * 10
        )
        adapter = AdaptiveOptimizer()
        adapter.run_optimization(trades)
        adj = adapter.get_trade_adjustments("EURUSD", "TRENDING_STRONG", "LONDON")
        assert adj.should_trade is True
        assert adj.confidence > 0

    def test_adjustments_block_bad_regime(self):
        trades = [_make_trade(regime="CHOPPY", pnl=-10)] * 60
        adapter = AdaptiveOptimizer()
        adapter.run_optimization(trades)
        adj = adapter.get_trade_adjustments("EURUSD", "CHOPPY", "LONDON")
        assert adj.should_trade is False

    def test_adjustments_block_bad_pair(self):
        trades = [_make_trade(pair="NZDJPY", pnl=-8)] * 40
        adapter = AdaptiveOptimizer()
        adapter.run_optimization(trades)
        adj = adapter.get_trade_adjustments("NZDJPY", "TRENDING_STRONG", "LONDON")
        assert adj.should_trade is False

    def test_should_retrain_initially(self):
        adapter = AdaptiveOptimizer()
        assert adapter.should_retrain() is True

    def test_should_retrain_after_trades(self):
        adapter = AdaptiveOptimizer()
        adapter._last_train_time = datetime.now(timezone.utc)
        adapter._trades_since_train = 51
        assert adapter.should_retrain() is True

    def test_no_retrain_when_fresh(self):
        adapter = AdaptiveOptimizer()
        adapter._last_train_time = datetime.now(timezone.utc)
        adapter._trades_since_train = 5
        assert adapter.should_retrain() is False

    def test_register_new_trade(self):
        adapter = AdaptiveOptimizer()
        adapter.register_new_trade()
        adapter.register_new_trade()
        assert adapter._trades_since_train == 2

    def test_recommendations_generated(self):
        random.seed(42)
        trades = _generate_trades(n=120, win_rate=0.75)
        adapter = AdaptiveOptimizer()
        report = adapter.run_optimization(trades)
        assert len(report.recommendations) > 0

    def test_insufficient_data_safe_defaults(self):
        adapter = AdaptiveOptimizer()
        adj = adapter.get_trade_adjustments("UNKNOWN", "UNKNOWN", "UNKNOWN")
        assert adj.should_trade is True
        assert adj.position_size_multiplier == 0.8

    def test_persistence_after_optimization(self, tmp_path):
        import adaptive.score_optimizer as so_mod
        import adaptive.pair_learner as pl_mod
        import adaptive.regime_learner as rl_mod
        import adaptive.session_learner as sl_mod

        old_so = so_mod.ScoreOptimizer.DEFAULT_PATH
        old_pl = pl_mod.PairLearner.SAVE_PATH
        old_rl = rl_mod.RegimeLearner.SAVE_PATH
        old_sl = sl_mod.SessionLearner.SAVE_PATH

        so_mod.ScoreOptimizer.DEFAULT_PATH = str(tmp_path / "scoring_weights.json")
        pl_mod.PairLearner.SAVE_PATH = str(tmp_path / "ml_pair_profiles.json")
        rl_mod.RegimeLearner.SAVE_PATH = str(tmp_path / "ml_regime_strategies.json")
        sl_mod.SessionLearner.SAVE_PATH = str(tmp_path / "ml_session_profiles.json")

        try:
            random.seed(42)
            trades = _generate_trades(n=120, win_rate=0.75)
            adapter = AdaptiveOptimizer()
            adapter.run_optimization(trades)

            assert Path(so_mod.ScoreOptimizer.DEFAULT_PATH).exists()
            assert Path(pl_mod.PairLearner.SAVE_PATH).exists()
            assert Path(rl_mod.RegimeLearner.SAVE_PATH).exists()
            assert Path(sl_mod.SessionLearner.SAVE_PATH).exists()
        finally:
            so_mod.ScoreOptimizer.DEFAULT_PATH = old_so
            pl_mod.PairLearner.SAVE_PATH = old_pl
            rl_mod.RegimeLearner.SAVE_PATH = old_rl
            sl_mod.SessionLearner.SAVE_PATH = old_sl
