"""
Regression tests for RiskConfig __post_init__ validation.

Ensures invalid money/risk values raise ValueError at construction
and that valid defaults pass unchanged.
"""

import math
import pytest
from config import RiskConfig, AppConfig


# ── Defaults construct cleanly ──────────────────────────────────────────────

def test_default_risk_config_is_valid():
    cfg = RiskConfig()
    assert cfg.risk_per_trade_pct == 0.75


def test_default_app_config_is_valid():
    cfg = AppConfig()
    assert cfg.risk.risk_per_trade_pct == 0.75


# ── Positivity checks ──────────────────────────────────────────────────────

@pytest.mark.parametrize("field_name,bad_value", [
    ("risk_per_trade_pct", -1.0),
    ("risk_per_trade_pct", 0.0),
    ("max_daily_drawdown_pct", -0.5),
    ("max_daily_drawdown_pct", 0.0),
    ("max_weekly_drawdown_pct", 0.0),
    ("min_risk_reward", -1.0),
    ("min_risk_reward", 0.0),
    ("max_spread_multiplier", -2.0),
    ("deriv_min_stake_usd", 0.0),
    ("deriv_min_stake_usd", -0.35),
    ("backtest_starting_balance_usd", 0.0),
    ("backtest_starting_balance_usd", -100.0),
    ("margin_warn_pct", 0.0),
    ("margin_block_entry_pct", -50.0),
    ("margin_flatten_pct", 0.0),
])
def test_non_positive_rejected(field_name, bad_value):
    with pytest.raises(ValueError, match=field_name):
        RiskConfig(**{field_name: bad_value})


# ── Non-negative (>= 0) ────────────────────────────────────────────────────

def test_micro_threshold_zero_allowed():
    cfg = RiskConfig(micro_account_threshold_usd=0.0)
    assert cfg.micro_account_threshold_usd == 0.0


def test_micro_threshold_negative_rejected():
    with pytest.raises(ValueError, match="micro_account_threshold_usd"):
        RiskConfig(micro_account_threshold_usd=-10.0)


# ── NaN / inf rejected ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_inf_rejected_on_balance(bad):
    with pytest.raises(ValueError, match="backtest_starting_balance_usd"):
        RiskConfig(backtest_starting_balance_usd=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nan_inf_rejected_on_risk_pct(bad):
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        RiskConfig(risk_per_trade_pct=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nan_inf_rejected_on_deriv_stake(bad):
    with pytest.raises(ValueError, match="deriv_min_stake_usd"):
        RiskConfig(deriv_min_stake_usd=bad)


# ── risk_per_trade_pct < 100 ceiling ────────────────────────────────────────

def test_risk_per_trade_pct_at_100_rejected():
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        RiskConfig(risk_per_trade_pct=100.0)


def test_risk_per_trade_pct_above_100_rejected():
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        RiskConfig(risk_per_trade_pct=150.0)


def test_risk_per_trade_pct_just_below_100_allowed():
    cfg = RiskConfig(risk_per_trade_pct=99.9)
    assert cfg.risk_per_trade_pct == 99.9


# ── Integer constraints ─────────────────────────────────────────────────────

def test_max_open_trades_zero_rejected():
    with pytest.raises(ValueError, match="max_open_trades"):
        RiskConfig(max_open_trades=0)


def test_max_open_trades_negative_rejected():
    with pytest.raises(ValueError, match="max_open_trades"):
        RiskConfig(max_open_trades=-1)


def test_max_correlated_trades_negative_rejected():
    with pytest.raises(ValueError, match="max_correlated_trades"):
        RiskConfig(max_correlated_trades=-1)


def test_max_correlated_trades_zero_allowed():
    cfg = RiskConfig(max_correlated_trades=0)
    assert cfg.max_correlated_trades == 0


# ── Cross-field: weekly >= daily ────────────────────────────────────────────

def test_weekly_below_daily_rejected():
    with pytest.raises(ValueError, match="max_weekly_drawdown_pct"):
        RiskConfig(max_weekly_drawdown_pct=2.0, max_daily_drawdown_pct=3.0)


def test_weekly_equals_daily_allowed():
    cfg = RiskConfig(max_weekly_drawdown_pct=5.0, max_daily_drawdown_pct=5.0)
    assert cfg.max_weekly_drawdown_pct == cfg.max_daily_drawdown_pct


# ── Cross-field: margin ordering ────────────────────────────────────────────

def test_margin_block_above_warn_rejected():
    with pytest.raises(ValueError, match="margin"):
        RiskConfig(margin_warn_pct=100.0, margin_block_entry_pct=150.0)


def test_margin_flatten_above_block_rejected():
    with pytest.raises(ValueError, match="margin"):
        RiskConfig(
            margin_warn_pct=200.0,
            margin_block_entry_pct=150.0,
            margin_flatten_pct=160.0,
        )


def test_margin_all_equal_allowed():
    cfg = RiskConfig(
        margin_warn_pct=150.0,
        margin_block_entry_pct=150.0,
        margin_flatten_pct=150.0,
    )
    assert cfg.margin_warn_pct == cfg.margin_flatten_pct
