"""
PR-A5 — Backtest realism fixes.

Tests for:
  A. Real multi-fold walk-forward (anchored/expanding-window, non-overlapping
     OOS segments, min_history respected, n_folds=1 backward-compat).
  B. Intrabar pessimism in _evaluate_trade (no same-candle TP1→TP2 fall-
     through; BE checked before TP2 in post-TP1 block; symmetric LONG/SHORT).

NOTE: pytest CANNOT be executed in this coding session because numpy and
loguru are not installed in the sandbox. These tests are written for CI —
the PR's CI pipeline is the runtime authority.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from dataclasses import dataclass

import pytest


# ---------------------------------------------------------------------------
# Lightweight stand-ins so the module can be imported without numpy/pandas
# in the test description; actual execution requires numpy+pandas (CI).
# ---------------------------------------------------------------------------


@dataclass
class _FakeSetup:
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    score: int = 80
    confluences: list = None
    regime: str = "TRENDING"

    def __post_init__(self):
        if self.confluences is None:
            self.confluences = []


def _make_trade(direction, entry, stop, tp1, tp2, risk=None,
                tp1_hit=False, realized_r=0.0):
    """Build the dict that _evaluate_trade expects."""
    if risk is None:
        risk = abs(entry - stop)
    return {
        "setup": _FakeSetup(
            direction=direction, entry_price=entry,
            stop_loss=stop, tp1=tp1, tp2=tp2,
        ),
        "entry_time": datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        "entry_price": entry,
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "tp1_hit": tp1_hit,
        "realized_r": realized_r,
        "session": "LONDON",
        "entry_type": "MARKET",
        "slippage_cost": 0.0,
    }


def _candle(time_offset_min, open_, high, low, close):
    """Return a dict that quacks like a pd.Series row."""
    import pandas as pd
    t = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=time_offset_min)
    return pd.Series({
        "time": pd.Timestamp(t),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    })


# ===================================================================
#  SECTION A — walk_forward multi-fold
# ===================================================================


class TestWalkForwardMultiFold:
    """Tests that exercise walk_forward's fold geometry without needing
    a real orchestrator (we mock self.run to capture index ranges)."""

    def _make_engine(self, min_history=10):
        """Return a BacktestEngine with a mocked run() that records calls."""
        from brain.backtest_engine import BacktestEngine, BacktestResult

        engine = BacktestEngine.__new__(BacktestEngine)
        engine.min_history = min_history
        engine.orchestrator = MagicMock()
        engine.journal = None
        engine.starting_balance = 10_000.0
        engine.risk_per_trade = 0.02
        engine.pip_size = 0.0001
        engine.slippage_pips = 1.0
        engine.commission_per_lot = 3.5
        engine.broker_loader = MagicMock()

        dummy = BacktestResult(
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            profit_factor=0.0, sharpe_ratio=0.0, max_drawdown=0.0,
            max_consecutive_losses=0, expectancy=0.0, avg_hold_time=0.0,
            best_pair=None, worst_pair=None, best_session=None,
            equity_curve=[10_000.0],
        )
        engine._run_calls: list[tuple[int, int]] = []


        def _tracking_run(pair, data_by_timeframe, start_index=None, end_index=None):
            engine._run_calls.append((start_index, end_index))
            return dummy

        engine.run = _tracking_run
        return engine

    def _m1_data(self, n_bars):
        import pandas as pd
        times = pd.date_range("2025-01-01", periods=n_bars, freq="min", tz="UTC")
        return {"M1": pd.DataFrame({
            "time": times,
            "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0,
        })}

    # --- n_folds >= 2: multi-fold geometry ---

    def test_five_fold_produces_four_oos_segments(self):
        engine = self._make_engine(min_history=10)
        data = self._m1_data(510)
        result = engine.walk_forward("TEST", data, n_folds=5)

        assert len(result["folds"]) == 4  # n_folds - 1
        assert "aggregate" in result

    def test_oos_segments_are_non_overlapping(self):
        engine = self._make_engine(min_history=10)
        data = self._m1_data(510)
        result = engine.walk_forward("TEST", data, n_folds=5)

        calls = engine._run_calls
        test_ranges = []
        for i in range(len(result["folds"])):
            fold_calls_idx = i * 2 + 1  # each fold: train call, test call
            test_ranges.append(calls[fold_calls_idx])

        for i in range(len(test_ranges) - 1):
            _, prev_end = test_ranges[i]
            next_start, _ = test_ranges[i + 1]
            assert next_start > prev_end, (
                f"OOS segments overlap: fold {i} ends at {prev_end}, "
                f"fold {i+1} starts at {next_start}"
            )

    def test_train_windows_expand(self):
        engine = self._make_engine(min_history=10)
        data = self._m1_data(510)
        engine.walk_forward("TEST", data, n_folds=5)

        calls = engine._run_calls
        train_ends = []
        for i in range(4):  # 4 folds
            train_start, train_end = calls[i * 2]
            assert train_start == 10  # anchored at min_history
            train_ends.append(train_end)

        for i in range(len(train_ends) - 1):
            assert train_ends[i + 1] > train_ends[i], (
                f"Train window did not expand: fold {i} end={train_ends[i]}, "
                f"fold {i+1} end={train_ends[i+1]}"
            )

    def test_all_indices_respect_min_history(self):
        engine = self._make_engine(min_history=50)
        data = self._m1_data(500)
        engine.walk_forward("TEST", data, n_folds=5)

        for start, end in engine._run_calls:
            assert start >= 50, f"Index {start} < min_history 50"

    def test_aggregate_spans_all_oos(self):
        engine = self._make_engine(min_history=10)
        data = self._m1_data(510)
        engine.walk_forward("TEST", data, n_folds=5)

        calls = engine._run_calls
        first_test_start = calls[1][0]   # fold 0 test
        last_test_end = calls[7][1]      # fold 3 test
        agg_call = calls[-1]             # aggregate is the last call
        assert agg_call == (first_test_start, last_test_end)

    # --- n_folds == 1: legacy backward-compat ---

    def test_single_fold_backward_compat(self):
        engine = self._make_engine(min_history=10)
        data = self._m1_data(100)
        result = engine.walk_forward("TEST", data, n_folds=1, train_ratio=0.7)

        assert len(result["folds"]) == 1
        assert result["folds"][0]["fold"] == 0
        assert "train" in result["folds"][0]
        assert "test" in result["folds"][0]

        calls = engine._run_calls
        assert len(calls) == 2  # train + test only (aggregate = test)

        train_start, train_end = calls[0]
        test_start, test_end = calls[1]
        assert train_start == 10
        split = int(100 * 0.7)
        assert train_end == max(split - 1, 10)
        assert test_start == max(split, 10)
        assert test_end == 99

    # --- edge cases ---

    def test_n_folds_zero_raises(self):
        engine = self._make_engine(min_history=10)
        data = self._m1_data(100)
        with pytest.raises(ValueError, match="n_folds must be >= 1"):
            engine.walk_forward("TEST", data, n_folds=0)

    def test_too_few_bars_raises(self):
        engine = self._make_engine(min_history=90)
        data = self._m1_data(100)  # only 10 usable bars
        with pytest.raises(ValueError, match="Not enough bars"):
            engine.walk_forward("TEST", data, n_folds=5)

    def test_two_fold_produces_one_oos(self):
        engine = self._make_engine(min_history=10)
        data = self._m1_data(210)
        result = engine.walk_forward("TEST", data, n_folds=2)
        assert len(result["folds"]) == 1

        calls = engine._run_calls
        assert len(calls) == 3  # 1 train + 1 test + 1 aggregate


# ===================================================================
#  SECTION B — _evaluate_trade intrabar pessimism
# ===================================================================


class TestEvaluateTradeIntrabarPessimism:
    """Tests that _evaluate_trade is strictly more conservative after the
    pessimism fix."""

    def _engine(self):
        from brain.backtest_engine import BacktestEngine
        engine = BacktestEngine.__new__(BacktestEngine)
        engine.min_history = 120
        engine.pip_size = 0.0001
        return engine

    # --- LONG direction ---

    def test_long_tp1_same_candle_does_not_evaluate_tp2(self):
        """When TP1 is hit on a candle, _evaluate_trade must return None
        (trade stays open) rather than falling through to test TP2."""
        engine = self._engine()
        trade = _make_trade("LONG", entry=1.0, stop=0.99, tp1=1.01, tp2=1.02)

        # Candle whose high reaches BOTH tp1 (1.01) and tp2 (1.02)
        c = _candle(5, open_=1.005, high=1.025, low=1.004, close=1.02)
        result = engine._evaluate_trade(trade, c)

        assert result is None, (
            "TP1+TP2 same candle must return None (defer TP2 to next candle)"
        )
        assert trade["tp1_hit"] is True
        assert trade["stop_loss"] == trade["entry_price"]  # moved to BE
        assert trade["realized_r"] > 0  # partial accrued

    def test_long_post_tp1_be_before_tp2(self):
        """Post-TP1, a candle spanning both BE-stop and TP2 must book
        BREAKEVEN, not WIN."""
        engine = self._engine()
        risk = 0.01
        partial_r = 0.5 * ((1.01 - 1.0) / risk)
        trade = _make_trade(
            "LONG", entry=1.0, stop=1.0, tp1=1.01, tp2=1.02,
            risk=risk, tp1_hit=True, realized_r=partial_r,
        )

        # Candle low touches BE (1.0) AND high touches TP2 (1.02)
        c = _candle(10, open_=1.01, high=1.025, low=0.999, close=1.015)
        result = engine._evaluate_trade(trade, c)

        assert result is not None
        assert result["outcome"] == "BREAKEVEN", (
            f"Expected BREAKEVEN but got {result['outcome']}"
        )

    def test_long_post_tp1_tp2_win_when_be_not_touched(self):
        """Post-TP1, if only TP2 is hit (BE-stop NOT touched), it should
        still correctly book a WIN."""
        engine = self._engine()
        risk = 0.01
        partial_r = 0.5 * ((1.01 - 1.0) / risk)
        trade = _make_trade(
            "LONG", entry=1.0, stop=1.0, tp1=1.01, tp2=1.02,
            risk=risk, tp1_hit=True, realized_r=partial_r,
        )

        # Candle high hits TP2 but low stays above BE (1.0)
        c = _candle(10, open_=1.015, high=1.025, low=1.005, close=1.02)
        result = engine._evaluate_trade(trade, c)

        assert result is not None
        assert result["outcome"] == "WIN"

    def test_long_pre_tp1_loss_unchanged(self):
        """Pre-TP1 full-loss logic must be unchanged (stop-first)."""
        engine = self._engine()
        trade = _make_trade("LONG", entry=1.0, stop=0.99, tp1=1.01, tp2=1.02)

        c = _candle(5, open_=1.0, high=1.005, low=0.988, close=0.99)
        result = engine._evaluate_trade(trade, c)

        assert result is not None
        assert result["outcome"] == "LOSS"
        assert result["pnl_r"] == -1.0

    # --- SHORT direction (symmetric) ---

    def test_short_tp1_same_candle_does_not_evaluate_tp2(self):
        """SHORT: When TP1 is hit on a candle, must return None."""
        engine = self._engine()
        trade = _make_trade("SHORT", entry=1.0, stop=1.01, tp1=0.99, tp2=0.98)

        # Candle low reaches BOTH tp1 (0.99) and tp2 (0.98)
        c = _candle(5, open_=0.995, high=0.996, low=0.975, close=0.98)
        result = engine._evaluate_trade(trade, c)

        assert result is None, (
            "SHORT TP1+TP2 same candle must return None"
        )
        assert trade["tp1_hit"] is True
        assert trade["stop_loss"] == trade["entry_price"]

    def test_short_post_tp1_be_before_tp2(self):
        """SHORT: Post-TP1, a candle spanning both BE-stop and TP2 must
        book BREAKEVEN, not WIN."""
        engine = self._engine()
        risk = 0.01
        partial_r = 0.5 * ((1.0 - 0.99) / risk)
        trade = _make_trade(
            "SHORT", entry=1.0, stop=1.0, tp1=0.99, tp2=0.98,
            risk=risk, tp1_hit=True, realized_r=partial_r,
        )

        # Candle high touches BE (1.0) AND low touches TP2 (0.98)
        c = _candle(10, open_=0.99, high=1.001, low=0.975, close=0.985)
        result = engine._evaluate_trade(trade, c)

        assert result is not None
        assert result["outcome"] == "BREAKEVEN", (
            f"Expected BREAKEVEN but got {result['outcome']}"
        )

    def test_short_post_tp1_tp2_win_when_be_not_touched(self):
        """SHORT: Post-TP1, TP2 hit without BE-stop → WIN."""
        engine = self._engine()
        risk = 0.01
        partial_r = 0.5 * ((1.0 - 0.99) / risk)
        trade = _make_trade(
            "SHORT", entry=1.0, stop=1.0, tp1=0.99, tp2=0.98,
            risk=risk, tp1_hit=True, realized_r=partial_r,
        )

        # Low hits TP2 but high stays below BE (1.0)
        c = _candle(10, open_=0.985, high=0.995, low=0.975, close=0.98)
        result = engine._evaluate_trade(trade, c)

        assert result is not None
        assert result["outcome"] == "WIN"

    def test_short_pre_tp1_loss_unchanged(self):
        """SHORT: Pre-TP1 full-loss logic unchanged."""
        engine = self._engine()
        trade = _make_trade("SHORT", entry=1.0, stop=1.01, tp1=0.99, tp2=0.98)

        c = _candle(5, open_=1.0, high=1.015, low=0.998, close=1.01)
        result = engine._evaluate_trade(trade, c)

        assert result is not None
        assert result["outcome"] == "LOSS"
        assert result["pnl_r"] == -1.0

    # --- Additional edge cases ---

    def test_tp1_candle_accrues_partial_r(self):
        """On the TP1-hit candle, realized_r must be updated before
        returning None."""
        engine = self._engine()
        risk = 0.01
        trade = _make_trade("LONG", entry=1.0, stop=0.99, tp1=1.01, tp2=1.02, risk=risk)

        c = _candle(5, open_=1.005, high=1.015, low=1.004, close=1.01)
        engine._evaluate_trade(trade, c)

        expected_partial = 0.5 * ((1.01 - 1.0) / risk)
        assert abs(trade["realized_r"] - expected_partial) < 1e-10

    def test_no_hit_returns_none(self):
        """A candle that touches neither stop nor TP returns None."""
        engine = self._engine()
        trade = _make_trade("LONG", entry=1.0, stop=0.99, tp1=1.01, tp2=1.02)

        c = _candle(5, open_=1.001, high=1.005, low=1.0, close=1.003)
        result = engine._evaluate_trade(trade, c)
        assert result is None

    def test_time_close_unchanged(self):
        """The 180-minute time-close path must still work."""
        engine = self._engine()
        trade = _make_trade("LONG", entry=1.0, stop=0.99, tp1=1.01, tp2=1.02)

        c = _candle(185, open_=1.002, high=1.005, low=1.0, close=1.003)
        result = engine._evaluate_trade(trade, c)

        assert result is not None
        assert result["hold_minutes"] >= 180
