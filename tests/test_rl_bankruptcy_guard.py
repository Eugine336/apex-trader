"""
Tests for the bankruptcy guard in ApexMultiTFTradingEnv.

When balance <= 0 the episode must terminate with done=True and
reward=-1.0 — the agent must never continue trading on a wiped-out
account.
"""

import sys
import types

import numpy as np
from unittest.mock import MagicMock

# Pre-seed torch and heavy rl submodules as mocks before any rl import
for _mod_name in [
    "torch", "torch.nn", "torch.nn.functional",
    "rl.network", "rl.environment", "rl.trainer",
    "rl.shadow", "rl.bridge", "rl.authority", "rl.evaluator",
    "brain.swap_model",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

if "config" not in sys.modules:
    _config_stub = types.ModuleType("config")
    _config_stub.INSTRUMENT_REGISTRY = {}

    class _Info:
        pip_size = 0.0001
        typical_spread_pips = 1.5
        pip_value_per_lot = 10.0
        class category:
            value = "forex"
    _config_stub.INSTRUMENT_REGISTRY["EURUSD"] = _Info()
    sys.modules["config"] = _config_stub


def _make_env(initial_balance=10_000.0, n_bars=500):
    """Build a minimal ApexMultiTFTradingEnv with stubbed data."""
    from rl.mtf_environment import ApexMultiTFTradingEnv

    env = ApexMultiTFTradingEnv.__new__(ApexMultiTFTradingEnv)
    env.instrument = "EURUSD"
    env.initial_bal = initial_balance
    env.max_hold = 200
    env.commission_per_lot = 3.5
    env.slippage_factor = 0.3
    env.swap_rates = {}
    env._reward_shaping = {}
    env.pip_size = 0.0001
    env.typical_spread = 1.5
    env.pip_value = 10.0
    env.category = "forex"

    import pandas as pd
    np.random.seed(42)
    base_price = 1.1000
    closes = base_price + np.cumsum(np.random.randn(n_bars) * 0.0002)
    highs = closes + np.abs(np.random.randn(n_bars) * 0.0003)
    lows = closes - np.abs(np.random.randn(n_bars) * 0.0003)
    opens = closes + np.random.randn(n_bars) * 0.0001

    m5_df = pd.DataFrame({
        "time": pd.date_range("2025-01-01", periods=n_bars, freq="5min"),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.ones(n_bars),
    })

    from rl.contracts import TF_ORDER, build_symbol_vocab
    from rl.multi_tf_obs_builder import MultiTFObservationBuilder, symbol_id_for

    env._m5_raw = m5_df.copy()
    env._m5_feat = ApexMultiTFTradingEnv._build_features(m5_df)
    env._dfs = {}
    for tf in TF_ORDER:
        env._dfs[tf] = m5_df.copy()

    env._obs_builder = MultiTFObservationBuilder()
    env._universe = build_symbol_vocab()
    env._symbol_id = symbol_id_for("EURUSD", env._universe)
    env._profile = {"pip_size": 0.0001, "typical_spread_pips": 1.5, "pip_value": 10.0}
    env.reset()
    return env


class TestBankruptcyGuard:

    def test_done_on_zero_balance(self):
        """When balance is forced to zero, next step returns done=True."""
        env = _make_env()
        env.balance = 0.0
        _, reward, done, info = env.step(0)
        assert done is True, "Episode must end when balance <= 0"
        assert reward == -1.0, "Bankruptcy reward must be -1.0"

    def test_done_on_negative_balance(self):
        """Negative balance also terminates."""
        env = _make_env()
        env.balance = -500.0
        _, reward, done, info = env.step(0)
        assert done is True
        assert reward == -1.0

    def test_balance_resets_after_bankruptcy(self):
        """After bankruptcy, reset() restores initial balance."""
        env = _make_env(initial_balance=5_000.0)
        env.balance = 0.0
        env.step(0)
        env.reset()
        assert env.balance == 5_000.0

    def test_no_trading_on_zero_balance(self):
        """With balance=0, even a buy action cannot open a trade."""
        env = _make_env()
        env.balance = 0.0
        _, _, done, _ = env.step(1)
        assert done is True
        assert len(env.trades_log) == 0

    def test_positive_balance_continues(self):
        """A positive balance with remaining data does NOT trigger done."""
        env = _make_env()
        env.balance = 100.0
        _, _, done, _ = env.step(0)
        assert done is False, "Positive balance should not end the episode"

    def test_end_of_data_still_fires(self):
        """End-of-data done condition is preserved alongside bankruptcy."""
        env = _make_env(n_bars=100)
        env.idx = len(env._m5_feat) - 2
        env.balance = 5_000.0
        _, _, done, _ = env.step(0)
        assert done is True, "End-of-data must still terminate"
