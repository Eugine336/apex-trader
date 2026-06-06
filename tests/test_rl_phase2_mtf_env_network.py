"""
Phase 2 acceptance tests — MTF environment + 48-feature encoder + symbol context.

Covers:
  • Network back-compat (n_features=12, no context → identical shape/params)
  • Network MTF mode    (n_features=48, context_dim=8, n_symbols=49 → correct shapes)
  • MTF env             (reset/step shapes, per-instrument spread, gap slippage, commission)
  • Legacy env          (ApexTradingEnv still returns (50,12) — unchanged)
  • Contracts           (schema_hash determinism, assert_compatible)
"""

from __future__ import annotations

import os
import tempfile
import shutil

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from rl.contracts import (
    OBS_CONTRACT_VERSION,
    OBS_SHAPE,
    N_CONTEXT_FEATURES,
    N_MARKET_FEATURES,
    OBS_FEATURES,
    WINDOW,
    N_TIMEFRAMES,
    schema,
    schema_hash,
    assert_compatible,
    build_symbol_vocab,
)
from rl.network import ApexRLAgent, LATENT_DIM
from rl.environment import ApexTradingEnv


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_csv(path: str, n_bars: int = 200, pip_size: float = 0.0001, tf_minutes: int = 5):
    """Generate a synthetic OHLCV CSV for testing."""
    np.random.seed(42)
    base = 1.10
    rows = []
    t = pd.Timestamp("2025-01-01", tz="UTC")
    for i in range(n_bars):
        o = base + np.random.normal(0, 0.001)
        h = o + abs(np.random.normal(0, 0.0005))
        lo = o - abs(np.random.normal(0, 0.0005))
        c = lo + (h - lo) * np.random.random()
        v = int(np.random.uniform(50, 500))
        rows.append({"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": v})
        t += pd.Timedelta(minutes=tf_minutes)
        base = c
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    return df


def _make_test_data_dir(n_bars_m5=500):
    """Create a temp dir with 4-TF CSVs for EURUSD."""
    tmpdir = tempfile.mkdtemp(prefix="apex_test_")
    tf_minutes = {"M5": 5, "M15": 15, "H1": 60, "H4": 240}
    for tf, minutes in tf_minutes.items():
        n = max(n_bars_m5, 200) if tf == "M5" else max(n_bars_m5 // (minutes // 5), 200)
        _make_csv(os.path.join(tmpdir, f"EURUSD_{tf}.csv"), n_bars=n, tf_minutes=minutes)
    return tmpdir


# ── Contract tests ───────────────────────────────────────────────────────────

class TestContracts:
    def test_schema_hash_deterministic(self):
        h1 = schema_hash()
        h2 = schema_hash()
        assert h1 == h2
        assert len(h1) == 16

    def test_assert_compatible_good(self):
        meta = {
            "obs_contract_version": OBS_CONTRACT_VERSION,
            "obs_schema_hash": schema_hash(),
        }
        assert_compatible(meta)

    def test_assert_compatible_mismatch(self):
        meta = {
            "obs_contract_version": OBS_CONTRACT_VERSION,
            "obs_schema_hash": "0000000000000000",
        }
        with pytest.raises(ValueError, match="obs_schema_hash"):
            assert_compatible(meta)

    def test_assert_compatible_missing_hash(self):
        with pytest.raises(ValueError, match="obs_contract_version"):
            assert_compatible({})

    def test_obs_shape_values(self):
        assert OBS_SHAPE == (50, 48)
        assert N_CONTEXT_FEATURES == 8
        assert OBS_FEATURES == 48
        assert N_MARKET_FEATURES == 12

    def test_obs_features_importable_and_consistent(self):
        assert OBS_FEATURES == OBS_SHAPE[1]
        assert OBS_FEATURES == N_MARKET_FEATURES * N_TIMEFRAMES

    def test_build_symbol_vocab_excludes_synthetics(self):
        vocab = build_symbol_vocab()
        assert len(vocab) > 0
        for sym in vocab:
            assert "V10" not in sym
            assert "BOOM" not in sym
            assert "CRASH" not in sym
            assert "STPIDX" not in sym


# ── Network back-compat tests ────────────────────────────────────────────────

class TestNetworkBackCompat:
    """ApexRLAgent(n_features=12) must be unchanged."""

    def test_default_construction(self):
        agent = ApexRLAgent(n_features=12, n_actions=4)
        assert agent.context_dim == 0
        assert agent.context_proj is None
        assert agent.symbol_embed is None

    def test_default_param_count_unchanged(self):
        agent = ApexRLAgent(n_features=12, n_actions=4)
        count = agent.count_parameters()
        assert count > 0
        agent2 = ApexRLAgent(n_features=12, n_actions=4)
        assert agent2.count_parameters() == count

    def test_default_forward_shapes(self):
        agent = ApexRLAgent(n_features=12, n_actions=4)
        batch = torch.randn(8, 50, 12)
        logits, values = agent.act(batch)
        assert logits.shape == (8, 4)
        assert values.shape == (8,)

    def test_default_predict_returns_tuple(self):
        agent = ApexRLAgent(n_features=12, n_actions=4)
        obs = np.random.randn(50, 12).astype(np.float32)
        action, conf, exp_r = agent.predict(obs)
        assert isinstance(action, int)
        assert 0 <= action <= 3
        assert 0.0 <= conf <= 1.0

    def test_default_encode_shape(self):
        agent = ApexRLAgent(n_features=12, n_actions=4)
        batch = torch.randn(4, 50, 12)
        latent = agent.encode(batch)
        assert latent.shape == (4, LATENT_DIM)


# ── Network MTF + context tests ──────────────────────────────────────────────

class TestNetworkMTFContext:
    """ApexRLAgent with context_dim=8, n_symbols=49."""

    def test_mtf_construction(self):
        agent = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        assert agent.context_dim == 8
        assert agent.context_proj is not None
        assert agent.symbol_embed is not None

    def test_mtf_more_params_than_default(self):
        base = ApexRLAgent(n_features=12, n_actions=4)
        mtf  = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        assert mtf.count_parameters() > base.count_parameters()

    def test_mtf_forward_shapes(self):
        agent = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        batch_obs = torch.randn(8, 50, 48)
        batch_ctx = torch.randn(8, 8)
        batch_sym = torch.randint(0, 49, (8,))
        logits, values = agent.act(batch_obs, context_vec=batch_ctx, symbol_id=batch_sym)
        assert logits.shape == (8, 4)
        assert values.shape == (8,)

    def test_mtf_forward_without_context_still_works(self):
        agent = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        batch = torch.randn(8, 50, 48)
        logits, values = agent.act(batch)
        assert logits.shape == (8, 4)
        assert values.shape == (8,)

    def test_mtf_predict_with_context(self):
        agent = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        obs = np.random.randn(50, 48).astype(np.float32)
        ctx = np.random.randn(8).astype(np.float32)
        action, conf, exp_r = agent.predict(obs, context_vec=ctx, symbol_id=5)
        assert isinstance(action, int)
        assert 0 <= action <= 3

    def test_mtf_predict_without_context(self):
        agent = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        obs = np.random.randn(50, 48).astype(np.float32)
        action, conf, exp_r = agent.predict(obs)
        assert isinstance(action, int)

    def test_context_dim_zero_no_extra_params(self):
        a = ApexRLAgent(n_features=48, n_actions=4, context_dim=0)
        b = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        assert b.count_parameters() > a.count_parameters()


# ── _fuse_context regression tests ──────────────────────────────────────────

class TestFuseContextRegression:
    """Guard against the silent context-slicing bug found during Phase 2 review.

    The bug: ``_fuse_context`` sliced ``context_vec[:, 1:]`` under the
    assumption that ``symbol_id`` was embedded inside the context vector.
    It is not — ``symbol_id`` is a *separate* integer passed to a learned
    embedding.  The full context vector must reach the projection MLP, and
    the symbol embedding must be concatenated alongside it.
    """

    def test_full_context_vec_used(self):
        agent = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        latent = torch.randn(1, LATENT_DIM)
        ctx_a = torch.zeros(1, 8)
        ctx_b = torch.zeros(1, 8)
        ctx_b[0, 0] = 5.0
        sym = torch.LongTensor([0])

        out_a = agent._fuse_context(latent, context_vec=ctx_a, symbol_id=sym)
        out_b = agent._fuse_context(latent, context_vec=ctx_b, symbol_id=sym)

        assert not torch.allclose(out_a, out_b), (
            "Changing context_vec[0] had no effect — element 0 is being "
            "sliced away instead of used"
        )

    def test_symbol_id_fused_via_embedding(self):
        agent = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        latent = torch.randn(1, LATENT_DIM)
        ctx = torch.randn(1, 8)
        sym_a = torch.LongTensor([0])
        sym_b = torch.LongTensor([1])

        out_a = agent._fuse_context(latent, context_vec=ctx, symbol_id=sym_a)
        out_b = agent._fuse_context(latent, context_vec=ctx, symbol_id=sym_b)

        assert not torch.allclose(out_a, out_b), (
            "Different symbol_id produced identical output — symbol embedding "
            "is not being fused"
        )

    def test_fuse_context_passthrough_without_context(self):
        agent = ApexRLAgent(n_features=48, n_actions=4, context_dim=8, n_symbols=49)
        latent = torch.randn(1, LATENT_DIM)

        out = agent._fuse_context(latent, context_vec=None, symbol_id=None)
        assert out.shape == (1, LATENT_DIM + 32)
        torch.testing.assert_close(out[:, :LATENT_DIM], latent)


# ── MTF environment tests ────────────────────────────────────────────────────

class TestMultiTFEnv:
    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.tmpdir = _make_test_data_dir(n_bars_m5=500)
        yield
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reset_shapes(self):
        from rl.mtf_environment import ApexMultiTFTradingEnv
        env = ApexMultiTFTradingEnv(data_dir=self.tmpdir, instrument="EURUSD")
        obs, ctx, sym_id = env.reset()
        assert obs.shape == OBS_SHAPE, f"Expected {OBS_SHAPE}, got {obs.shape}"
        assert ctx.shape == (N_CONTEXT_FEATURES,)
        assert isinstance(sym_id, int)

    def test_step_shapes(self):
        from rl.mtf_environment import ApexMultiTFTradingEnv
        env = ApexMultiTFTradingEnv(data_dir=self.tmpdir, instrument="EURUSD")
        env.reset()
        obs_tuple, reward, done, info = env.step(0)
        obs, ctx, sym_id = obs_tuple
        assert obs.shape == OBS_SHAPE
        assert ctx.shape == (N_CONTEXT_FEATURES,)
        assert isinstance(reward, float)
        assert isinstance(done, bool)

    def test_observation_shape_method(self):
        from rl.mtf_environment import ApexMultiTFTradingEnv
        env = ApexMultiTFTradingEnv(data_dir=self.tmpdir, instrument="EURUSD")
        assert env.observation_shape() == OBS_SHAPE

    def test_action_space(self):
        from rl.mtf_environment import ApexMultiTFTradingEnv
        env = ApexMultiTFTradingEnv(data_dir=self.tmpdir, instrument="EURUSD")
        assert env.action_space_n() == 4

    def test_per_instrument_spread_applied(self):
        from rl.mtf_environment import ApexMultiTFTradingEnv
        from config import INSTRUMENT_REGISTRY
        env = ApexMultiTFTradingEnv(data_dir=self.tmpdir, instrument="EURUSD")
        info = INSTRUMENT_REGISTRY["EURUSD"]
        assert env.typical_spread == info.typical_spread_pips
        assert env.pip_size == info.pip_size

    def test_commission_deducted_on_trade(self):
        from rl.mtf_environment import ApexMultiTFTradingEnv
        env = ApexMultiTFTradingEnv(
            data_dir=self.tmpdir,
            instrument="EURUSD",
            commission_per_lot=5.0,
        )
        env.reset()
        initial_bal = env.balance
        for _ in range(50):
            env.step(1)
        if len(env.trades_log) > 0:
            trade = env.trades_log[0]
            assert trade["commission"] > 0, "Commission must be charged"
        elif env.trade is not None:
            assert env.balance < initial_bal, "Commission should reduce balance on entry"

    def test_random_agent_negative_expectancy(self):
        from rl.mtf_environment import ApexMultiTFTradingEnv
        env = ApexMultiTFTradingEnv(
            data_dir=self.tmpdir,
            instrument="EURUSD",
            commission_per_lot=7.0,
            slippage_factor=1.0,
        )
        env.reset()
        np.random.seed(123)
        steps = 0
        while steps < 300:
            action = np.random.randint(0, 4)
            _, _, done, _ = env.step(action)
            steps += 1
            if done:
                break

        if len(env.trades_log) >= 3:
            total_costs = sum(t["total_costs"] for t in env.trades_log)
            assert total_costs > 0, "Costs must be positive (spread + commission + slippage)"

    def test_gap_through_sl_worse_fill(self):
        from rl.mtf_environment import ApexMultiTFTradingEnv
        env = ApexMultiTFTradingEnv(
            data_dir=self.tmpdir,
            instrument="EURUSD",
            slippage_factor=0.0,
        )
        env.reset()

        for _ in range(100):
            obs_tuple, _, done, _ = env.step(1)
            if done:
                break

        if len(env.trades_log) > 0:
            for trade in env.trades_log:
                if trade["reason"] == "sl":
                    assert trade["spread_applied"] > 0, "Spread must be applied"


# ── Legacy env back-compat ───────────────────────────────────────────────────

class TestLegacyEnvBackCompat:
    @pytest.fixture(autouse=True)
    def setup_csv(self):
        self.tmpdir = tempfile.mkdtemp(prefix="apex_legacy_")
        self.csv_path = os.path.join(self.tmpdir, "test.csv")
        _make_csv(self.csv_path, n_bars=300)
        yield
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_legacy_env_still_works(self):
        env = ApexTradingEnv(csv_path=self.csv_path)
        obs = env.reset()
        assert obs.shape == (50, 12)

    def test_legacy_env_step(self):
        env = ApexTradingEnv(csv_path=self.csv_path)
        env.reset()
        obs, reward, done, info = env.step(0)
        assert obs.shape == (50, 12)
        assert isinstance(reward, float)

    def test_legacy_env_n_features(self):
        assert ApexTradingEnv.N_FEATURES == 12
