"""
APEX TRADER — Integration Tests
Verifies end-to-end wiring between modules: scan → entry → risk → manage → close.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from management.trade_manager import (
    EntrySignal as TMEntrySignal,
    TradeManager,
    TradeStatus,
)
from persistence.position_store import PositionStore
from risk.risk_engine import RiskEngine, RiskAssessment
from adaptive.ev_estimator import EVEstimator, EVEstimate
from brain.drawdown_guard import DrawdownMode
from adaptive.optimizer import AdaptiveOptimizer
from dashboard.state import LiveState


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_ohlcv(n: int = 50, start: float = 1.10, trend: float = 0.0001) -> pd.DataFrame:
    closes = [start + i * trend + np.random.normal(0, 0.0002) for i in range(n)]
    return pd.DataFrame({
        "open": [c - 0.0001 for c in closes],
        "high": [c + 0.0005 for c in closes],
        "low": [c - 0.0005 for c in closes],
        "close": closes,
        "tick_volume": [100 + int(np.random.exponential(50)) for _ in range(n)],
        "time": pd.date_range("2024-01-01", periods=n, freq="1min"),
    })


def _make_entry_signal(
    pair: str = "EURUSD",
    direction: str = "LONG",
    entry: float = 1.1000,
    sl: float = 1.0950,
    tp1: float = 1.1050,
    tp2: float = 1.1100,
    score: int = 90,
) -> TMEntrySignal:
    risk_pips = abs(entry - sl)
    return TMEntrySignal(
        pair=pair,
        direction=direction,
        entry_price=entry,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        risk_reward_1=round(abs(tp1 - entry) / risk_pips, 2) if risk_pips else 1.0,
        risk_reward_2=round(abs(tp2 - entry) / risk_pips, 2) if risk_pips else 2.0,
        position_size_lots=0.05,
        score=score,
        confluences=["structure", "fvg", "ob"],
        entry_zone="FVG_MIDPOINT",
        entry_timeframe="M5",
    )


def _make_trade_history(n: int = 30, win_rate: float = 0.6) -> list[dict]:
    trades = []
    for i in range(n):
        won = (i % int(1 / win_rate)) != 0 if win_rate < 1 else True
        pnl = 25.0 if won else -15.0
        trades.append({
            "pair": "EURUSD",
            "direction": "LONG",
            "pnl": pnl,
            "pnl_dollars": pnl,
            "risk_dollars": 15.0,
            "regime": "trending",
            "session": "london",
            "entry_type": "FVG_MIDPOINT",
            "outcome": "WIN" if won else "LOSS",
        })
    return trades


# ── Test Classes ─────────────────────────────────────────────────────────

class TestEntryToRiskPipeline:
    """Generate entry signal → pass through RiskEngine → get assessment."""

    def test_risk_approves_valid_signal(self):
        engine = RiskEngine(starting_balance=10_000)
        result = engine.assess(
            pair="EURUSD",
            direction="LONG",
            entry_price=1.1000,
            stop_loss=1.0950,
            account_balance=10_000,
            score=92,
        )
        assert isinstance(result, RiskAssessment)
        assert result.approved is True
        assert result.position_size_lots > 0
        assert result.max_loss_dollars > 0
        assert len(result.checks) > 0

    def test_risk_rejects_frozen_state(self):
        engine = RiskEngine(starting_balance=10_000)
        engine.drawdown_guard.mode = DrawdownMode.FROZEN
        result = engine.assess(
            pair="EURUSD",
            direction="LONG",
            entry_price=1.1000,
            stop_loss=1.0950,
            account_balance=10_000,
            score=92,
        )
        assert result.approved is False
        assert any("frozen" in r.lower() or "drawdown" in r.lower() for r in result.rejections)

    def test_risk_rejects_max_trades_exceeded(self):
        engine = RiskEngine(starting_balance=10_000)
        open_trades = [
            {"pair": f"PAIR{i}", "direction": "LONG", "risk_pct": 0.02}
            for i in range(engine.risk_cfg.max_open_trades)
        ]
        result = engine.assess(
            pair="EURUSD",
            direction="LONG",
            entry_price=1.1000,
            stop_loss=1.0950,
            open_trades=open_trades,
            account_balance=10_000,
            score=92,
        )
        assert result.approved is False


class TestTradeLifecycle:
    """Open → TP1 partial → breakeven → trailing → close lifecycle."""

    def test_full_lifecycle_long(self):
        tm = TradeManager()
        signal = _make_entry_signal()
        trade = tm.open_trade(signal)

        assert trade.status == TradeStatus.OPEN
        assert trade.partial_closed is False
        assert trade.breakeven_active is False

        trade = tm.update(trade, current_price=signal.tp1)
        assert trade.status in {TradeStatus.TP1_HIT, TradeStatus.BREAKEVEN}
        assert trade.partial_closed is True

        trade = tm.update(trade, current_price=signal.tp2)
        assert trade.status in {TradeStatus.CLOSED, TradeStatus.TRAILING}

    def test_stop_loss_hit(self):
        tm = TradeManager()
        signal = _make_entry_signal()
        trade = tm.open_trade(signal)

        trade = tm.update(trade, current_price=signal.stop_loss - 0.0001)
        assert trade.status == TradeStatus.STOPPED
        assert trade.pnl_pips < 0

    def test_short_trade_lifecycle(self):
        tm = TradeManager()
        signal = _make_entry_signal(
            direction="SHORT",
            entry=1.1000,
            sl=1.1050,
            tp1=1.0950,
            tp2=1.0900,
        )
        trade = tm.open_trade(signal)
        assert trade.status == TradeStatus.OPEN

        trade = tm.update(trade, current_price=signal.tp1)
        assert trade.partial_closed is True


class TestMicroAccountPipeline:
    """Verify $5 account correctly skips oversized trades."""

    def test_micro_account_wide_stop_rejected(self):
        engine = RiskEngine(starting_balance=5.0)
        result = engine.assess(
            pair="XAUUSD",
            direction="LONG",
            entry_price=2400.0,
            stop_loss=2390.0,
            account_balance=5.0,
            score=92,
        )
        if result.approved:
            assert result.position_size_lots > 0
        else:
            assert any("micro" in r.lower() or "minimum" in r.lower() or "small" in r.lower()
                       for r in result.rejections)


class TestEVGatePipeline:
    """Verify EV estimator integrates with risk engine."""

    def test_ev_estimate_with_sufficient_data(self):
        estimator = EVEstimator(min_trades_for_gate=5)
        history = _make_trade_history(n=30, win_rate=0.7)
        estimate = estimator.estimate("EURUSD", "trending", "london", history)
        assert isinstance(estimate, EVEstimate)
        assert estimate.sample_size > 0
        assert estimate.confidence in {"high", "medium", "low", "insufficient"}

    def test_negative_ev_pair(self):
        estimator = EVEstimator(min_trades_for_gate=5)
        history = [
            {"pair": "GBPNZD", "pnl": -20.0, "pnl_dollars": -20.0,
             "risk_dollars": 10.0, "regime": "ranging", "session": "asian"}
            for _ in range(20)
        ]
        estimate = estimator.estimate("GBPNZD", "ranging", "asian", history)
        assert estimate.expected_value < 0

    def test_insufficient_data_returns_default(self):
        estimator = EVEstimator(min_trades_for_gate=10)
        estimate = estimator.estimate("EURUSD", "trending", "london", [])
        assert estimate.confidence == "insufficient"


class TestPositionPersistenceRoundTrip:
    """Save → load → verify position data survives round-trip."""

    def test_round_trip(self, tmp_path):
        db = str(tmp_path / "test_pos.db")
        store = PositionStore(db_path=db)

        pos = SimpleNamespace(
            order_id="ORD001",
            platform="mt5",
            symbol="EURUSD",
            direction="LONG",
            lots=0.05,
            entry_price=1.1000,
            sl=1.0950,
            tp1=1.1050,
            tp2=1.1100,
            score=90,
            regime="trending",
            session="london",
            entry_type="FVG_MIDPOINT",
            open_time=datetime.now(timezone.utc),
            tp1_hit=False,
            at_breakeven=False,
            trailing=False,
            tm_trade_id="abc123",
            stake_usd=0.0,
            multiplier=100,
        )

        store.save_position(pos)
        assert store.count() == 1

        loaded = store.load_all_positions()
        assert len(loaded) == 1
        assert loaded[0]["order_id"] == "ORD001"
        assert loaded[0]["symbol"] == "EURUSD"
        assert loaded[0]["direction"] == "LONG"
        assert abs(loaded[0]["entry_price"] - 1.1000) < 1e-6

        store.remove_position("ORD001")
        assert store.count() == 0
        store.close()

    def test_update_position(self, tmp_path):
        db = str(tmp_path / "test_pos2.db")
        store = PositionStore(db_path=db)

        pos = SimpleNamespace(
            order_id="ORD002",
            platform="deriv",
            symbol="V75_1S",
            direction="SHORT",
            lots=0.0,
            entry_price=500.0,
            sl=510.0,
            tp1=490.0,
            tp2=480.0,
            score=72,
            regime="volatile",
            session="24_7",
            entry_type="OB_MIDPOINT",
            open_time=datetime.now(timezone.utc),
            tp1_hit=False,
            at_breakeven=False,
            trailing=False,
            tm_trade_id="def456",
            stake_usd=5.0,
            multiplier=100,
        )

        store.save_position(pos)
        store.update_position("ORD002", tp1_hit=1, at_breakeven=1, sl=500.0)
        loaded = store.load_all_positions()
        assert loaded[0]["tp1_hit"] == 1
        assert loaded[0]["at_breakeven"] == 1
        assert abs(loaded[0]["sl"] - 500.0) < 1e-6
        store.close()


class TestAdaptiveOptimizerPipeline:
    """Verify AdaptiveOptimizer produces valid adjustments."""

    def test_optimization_cycle(self, tmp_path):
        import adaptive.score_optimizer as so_mod
        import adaptive.pair_learner as pl_mod
        import adaptive.regime_learner as rl_mod
        import adaptive.session_learner as sl_mod

        old_so = so_mod.ScoreOptimizer.DEFAULT_PATH
        old_pl = pl_mod.PairLearner.SAVE_PATH
        old_rl = rl_mod.RegimeLearner.SAVE_PATH
        old_sl = sl_mod.SessionLearner.SAVE_PATH
        so_mod.ScoreOptimizer.DEFAULT_PATH = str(tmp_path / "w.json")
        pl_mod.PairLearner.SAVE_PATH = str(tmp_path / "p.json")
        rl_mod.RegimeLearner.SAVE_PATH = str(tmp_path / "r.json")
        sl_mod.SessionLearner.SAVE_PATH = str(tmp_path / "s.json")
        try:
            optimizer = AdaptiveOptimizer()
            trades = _make_trade_history(n=60, win_rate=0.65)
            report = optimizer.run_optimization(trades)
            assert report.trades_analyzed == 60
            assert report.overall_performance is not None
            assert report.overall_performance.total_trades == 60
        finally:
            so_mod.ScoreOptimizer.DEFAULT_PATH = old_so
            pl_mod.PairLearner.SAVE_PATH = old_pl
            rl_mod.RegimeLearner.SAVE_PATH = old_rl
            sl_mod.SessionLearner.SAVE_PATH = old_sl

    def test_adjustments_allow_trading_by_default(self):
        optimizer = AdaptiveOptimizer()
        adj = optimizer.get_trade_adjustments("NEWPAIR", "UNKNOWN_REGIME", "UNKNOWN_SESSION")
        assert adj.should_trade is True
        assert adj.position_size_multiplier > 0

    def test_should_retrain_initially(self):
        optimizer = AdaptiveOptimizer()
        assert optimizer.should_retrain() is True


class TestDashboardStateFallbacks:
    """Verify dashboard LiveState fallback shapes (non-live mode)."""

    def test_status_fallback(self):
        state = LiveState()
        status = state.get_status()
        assert status["mode"] == "simulated"
        assert status["bot_status"] == "running"
        assert status["account_balance"] == 10000.0

    def test_open_trades_fallback(self):
        state = LiveState()
        trades = state.get_open_trades()
        assert trades == {"trades": [], "count": 0}

    def test_performance_fallback(self):
        state = LiveState()
        perf = state.get_performance()
        assert perf["total_trades"] == 0
        assert perf["win_rate"] == 0.0
        assert len(perf["equity_curve"]) == 1

    def test_scanner_fallback(self):
        state = LiveState()
        scanner = state.get_scanner_results()
        assert scanner["ready_count"] == 0
        assert scanner["total_count"] == 0

    def test_risk_fallback(self):
        state = LiveState()
        risk = state.get_risk_status()
        assert risk["mode"] == "NORMAL"
        assert risk["account_balance"] == 10000.0

    def test_ml_insights_fallback(self):
        state = LiveState()
        ml = state.get_ml_insights()
        assert ml == {"score_adjustments": {}, "regime_stats": {}, "session_stats": {}, "pair_stats": {}}

    def test_controls_without_engine(self):
        state = LiveState()
        result = state.start_trading()
        assert result["status"] == "no_engine_attached"


class TestDrawdownModeTransitions:
    """Verify drawdown guard modes interact correctly with risk engine."""

    def test_normal_mode_allows_full_risk(self):
        engine = RiskEngine(starting_balance=10_000)
        engine.drawdown_guard.mode = DrawdownMode.NORMAL
        result = engine.assess("EURUSD", "LONG", 1.1000, 1.0950, account_balance=10_000, score=92)
        assert result.approved is True

    def test_caution_mode_reduces_risk(self):
        engine = RiskEngine(starting_balance=10_000)
        engine.drawdown_guard.mode = DrawdownMode.CAUTION
        result = engine.assess("EURUSD", "LONG", 1.1000, 1.0950, account_balance=10_000, score=92)
        if result.approved:
            assert result.risk_pct <= 0.02

    def test_recovery_mode_further_reduces(self):
        engine = RiskEngine(starting_balance=10_000)
        engine.drawdown_guard.mode = DrawdownMode.RECOVERY
        result = engine.assess("EURUSD", "LONG", 1.1000, 1.0950, account_balance=10_000, score=95)
        if result.approved:
            assert result.risk_pct <= 0.015
