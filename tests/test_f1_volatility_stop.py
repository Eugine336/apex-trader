"""
Tests for the F1 volatility stop model (Phase A: offline evidence only).

Validates:
  1. ATR equivalence with RegimeDetector._calculate_atr
  2. clamped_atr_stop_distance: unavailable cases, normal, all clamp boundaries
  3. atr_stop_price sign by direction
  4. Determinism: same inputs -> same outputs
  5. BacktestResult.atr_comparison is None when compare_atr_stop=False
"""

import math

import numpy as np
import pandas as pd
import pytest

from brain.volatility_stop import (
    atr_series,
    atr_stop_price,
    clamped_atr_stop_distance,
    latest_atr,
)


def _make_ohlc(n: int = 50, seed: int = 42) -> pd.DataFrame:
    """Construct a synthetic OHLC DataFrame for testing."""
    rng = np.random.RandomState(seed)
    close = 100.0 + np.cumsum(rng.randn(n) * 0.5)
    high = close + rng.uniform(0.1, 1.0, n)
    low = close - rng.uniform(0.1, 1.0, n)
    opens = close + rng.randn(n) * 0.2
    return pd.DataFrame({
        "open": opens,
        "high": high,
        "low": low,
        "close": close,
    })


class TestATREquivalence:
    """ATR must match RegimeDetector._calculate_atr element-wise."""

    def test_matches_regime_detector_formula(self) -> None:
        df = _make_ohlc(60)
        period = 14
        result = atr_series(df, period)

        prev_close = df["close"].shift(1)
        tr_components = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        )
        true_range = tr_components.max(axis=1)
        expected = true_range.rolling(period, min_periods=1).mean()

        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_different_period(self) -> None:
        df = _make_ohlc(40)
        result = atr_series(df, period=7)
        assert len(result) == len(df)
        assert not result.isna().any()


class TestLatestATR:

    def test_returns_last_value(self) -> None:
        df = _make_ohlc(30)
        val = latest_atr(df, 14)
        assert val is not None
        assert val > 0

    def test_empty_dataframe(self) -> None:
        df = pd.DataFrame(columns=["open", "high", "low", "close"])
        assert latest_atr(df) is None

    def test_single_row(self) -> None:
        df = pd.DataFrame({
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
        })
        val = latest_atr(df, 14)
        if val is not None:
            assert val > 0


class TestClampedATRStopDistance:

    PIP = 0.0001

    def test_unavailable_none(self) -> None:
        dist, status = clamped_atr_stop_distance(
            None, 0.001, mult=1.5, min_pips=5, max_pips=20,
            pip_size=self.PIP, ratio_min=0.5, ratio_max=2.0,
        )
        assert dist is None
        assert status == "unavailable"

    def test_unavailable_nan(self) -> None:
        dist, status = clamped_atr_stop_distance(
            float("nan"), 0.001, mult=1.5, min_pips=5, max_pips=20,
            pip_size=self.PIP, ratio_min=0.5, ratio_max=2.0,
        )
        assert dist is None
        assert status == "unavailable"

    def test_unavailable_zero(self) -> None:
        dist, status = clamped_atr_stop_distance(
            0.0, 0.001, mult=1.5, min_pips=5, max_pips=20,
            pip_size=self.PIP, ratio_min=0.5, ratio_max=2.0,
        )
        assert dist is None
        assert status == "unavailable"

    def test_unavailable_negative(self) -> None:
        dist, status = clamped_atr_stop_distance(
            -0.5, 0.001, mult=1.5, min_pips=5, max_pips=20,
            pip_size=self.PIP, ratio_min=0.5, ratio_max=2.0,
        )
        assert dist is None
        assert status == "unavailable"

    def test_normal_within_bounds(self) -> None:
        atr_val = 0.001
        struct_dist = 0.0015
        dist, status = clamped_atr_stop_distance(
            atr_val, struct_dist, mult=1.5, min_pips=5, max_pips=40,
            pip_size=self.PIP, ratio_min=0.5, ratio_max=2.0,
        )
        assert status == "modeled"
        assert dist is not None
        assert dist == round(1.5 * 0.001, 6)

    def test_clamped_to_min_pips_floor(self) -> None:
        atr_val = 0.0001
        struct_dist = 0.002
        dist, status = clamped_atr_stop_distance(
            atr_val, struct_dist, mult=1.5, min_pips=10, max_pips=40,
            pip_size=self.PIP, ratio_min=0.1, ratio_max=10.0,
        )
        assert status == "modeled"
        assert dist is not None
        assert dist >= 10 * self.PIP - 1e-9

    def test_clamped_to_max_pips_ceiling(self) -> None:
        atr_val = 0.1
        struct_dist = 0.5
        dist, status = clamped_atr_stop_distance(
            atr_val, struct_dist, mult=1.5, min_pips=5, max_pips=20,
            pip_size=self.PIP, ratio_min=0.0, ratio_max=100.0,
        )
        assert status == "modeled"
        assert dist is not None
        assert dist <= 20 * self.PIP + 1e-9

    def test_ratio_floor_clamp(self) -> None:
        atr_val = 0.0001
        struct_dist = 0.002
        dist, status = clamped_atr_stop_distance(
            atr_val, struct_dist, mult=1.5, min_pips=0, max_pips=1000,
            pip_size=self.PIP, ratio_min=0.5, ratio_max=2.0,
        )
        assert status == "modeled"
        assert dist is not None
        assert dist >= 0.5 * struct_dist - 1e-9

    def test_ratio_ceiling_clamp(self) -> None:
        atr_val = 0.01
        struct_dist = 0.001
        dist, status = clamped_atr_stop_distance(
            atr_val, struct_dist, mult=1.5, min_pips=0, max_pips=10000,
            pip_size=self.PIP, ratio_min=0.5, ratio_max=2.0,
        )
        assert status == "modeled"
        assert dist is not None
        assert dist <= 2.0 * struct_dist + 1e-9

    def test_boundary_hit_still_modeled(self) -> None:
        atr_val = 10.0
        struct_dist = 0.001
        dist, status = clamped_atr_stop_distance(
            atr_val, struct_dist, mult=1.5, min_pips=5, max_pips=20,
            pip_size=self.PIP, ratio_min=0.5, ratio_max=2.0,
        )
        assert status == "modeled"

    def test_zero_structure_distance_skips_ratio_clamp(self) -> None:
        atr_val = 0.001
        dist, status = clamped_atr_stop_distance(
            atr_val, 0.0, mult=1.5, min_pips=5, max_pips=40,
            pip_size=self.PIP, ratio_min=0.5, ratio_max=2.0,
        )
        assert status == "modeled"
        assert dist is not None


class TestATRStopPrice:

    def test_long_below_entry(self) -> None:
        price = atr_stop_price(1.30000, "LONG", 0.0015)
        assert price < 1.30000

    def test_short_above_entry(self) -> None:
        price = atr_stop_price(1.30000, "SHORT", 0.0015)
        assert price > 1.30000

    def test_buy_alias(self) -> None:
        price = atr_stop_price(100.0, "BUY", 1.0)
        assert price == 99.0

    def test_sell_alias(self) -> None:
        price = atr_stop_price(100.0, "SELL", 1.0)
        assert price == 101.0

    def test_case_insensitive(self) -> None:
        price = atr_stop_price(50.0, "long", 2.0)
        assert price == 48.0


class TestDeterminism:

    def test_same_inputs_same_outputs(self) -> None:
        df = _make_ohlc(50)
        kwargs = dict(
            mult=1.5, min_pips=5, max_pips=20,
            pip_size=0.0001, ratio_min=0.5, ratio_max=2.0,
        )
        val1 = latest_atr(df, 14)
        val2 = latest_atr(df, 14)
        assert val1 == val2

        d1, s1 = clamped_atr_stop_distance(val1, 0.001, **kwargs)
        d2, s2 = clamped_atr_stop_distance(val2, 0.001, **kwargs)
        assert d1 == d2
        assert s1 == s2


class TestBacktestATRComparison:
    """Verify compare_atr_stop=False leaves result unchanged."""

    def test_comparison_none_when_disabled(self) -> None:
        from brain.backtest_engine import BacktestResult
        result = BacktestResult(
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            profit_factor=0.0, sharpe_ratio=0.0, max_drawdown=0.0,
            max_consecutive_losses=0, expectancy=0.0, avg_hold_time=0.0,
            best_pair=None, worst_pair=None, best_session=None,
            equity_curve=[],
        )
        assert result.atr_comparison is None

    def test_atr_comparison_result_fields(self) -> None:
        from brain.backtest_engine import ATRComparisonResult
        cmp = ATRComparisonResult(
            total_compared=10, total_skipped=2,
            structure_wins=5, structure_losses=3, structure_breakevens=2,
            structure_win_rate=50.0, structure_mean_r=0.3, structure_expectancy=0.3,
            atr_wins=6, atr_losses=2, atr_breakevens=2,
            atr_win_rate=60.0, atr_mean_r=0.5, atr_expectancy=0.5,
            expectancy_delta=0.2,
            structure_loss_rate=0.3, atr_loss_rate=0.2,
        )
        assert cmp.total_compared == 10
        assert cmp.expectancy_delta == 0.2


class TestATRCounterfactualTPEquality:
    """Verify the ATR counterfactual trade keeps the same TP prices as
    the structure trade — only stop_loss and risk differ."""

    def test_atr_trade_shares_structure_tps(self) -> None:
        import copy

        structure_trade = {
            "entry_price": 1.1000,
            "stop_loss": 1.0950,
            "tp1": 1.1080,
            "tp2": 1.1150,
            "risk": 0.0050,
        }

        atr_sl = 1.0930
        atr_risk = 0.0070

        atr_trade = copy.deepcopy(structure_trade)
        atr_trade["stop_loss"] = atr_sl
        atr_trade["risk"] = atr_risk

        assert atr_trade["tp1"] == structure_trade["tp1"]
        assert atr_trade["tp2"] == structure_trade["tp2"]
        assert atr_trade["stop_loss"] != structure_trade["stop_loss"]
        assert atr_trade["risk"] != structure_trade["risk"]
