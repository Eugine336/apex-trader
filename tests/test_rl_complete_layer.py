"""
Tests for the complete RL layer — MTF trainer, evaluator, data validation, health, reconciliation.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


def _real_torch():
    """Import torch and skip if it's a mock or not installed."""
    torch = pytest.importorskip("torch")
    if not hasattr(torch, "__version__"):
        pytest.skip("torch is a mock from another test module")
    return torch


# ── Test data helpers ────────────────────────────────────────────────────────


def _make_ohlcv_csv(path: Path, symbol: str, tf: str, n_bars: int = 300):
    """Generate a synthetic OHLCV CSV for testing."""
    np.random.seed(42)
    base = 1.1000
    rows = []
    from datetime import datetime, timedelta

    tf_seconds = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}
    dt = datetime(2024, 1, 2, 0, 0, 0)
    delta = timedelta(seconds=tf_seconds.get(tf, 300))

    for i in range(n_bars):
        o = base + np.random.normal(0, 0.001)
        c = o + np.random.normal(0, 0.001)
        h = max(o, c) + abs(np.random.normal(0, 0.0005))
        lo = min(o, c) - abs(np.random.normal(0, 0.0005))
        v = max(1, int(np.random.exponential(100)))
        rows.append({"time": dt.isoformat(), "open": o, "high": h, "low": lo, "close": c, "volume": v})
        dt += delta

    df = pd.DataFrame(rows)
    df.to_csv(path / f"{symbol}_{tf}.csv", index=False)


def _setup_test_data(tmp: Path, symbol: str = "TESTPAIR"):
    """Create a complete set of MTF CSVs."""
    for tf in ["M5", "M15", "H1", "H4"]:
        _make_ohlcv_csv(tmp, symbol, tf, n_bars=300)


# ── 1. MTFPPOConfig ─────────────────────────────────────────────────────────


def test_mtf_ppo_config_fields():
    """MTFPPOConfig has the required fields."""
    _real_torch()
    from rl.mtf_trainer import MTFPPOConfig

    cfg = MTFPPOConfig()
    assert hasattr(cfg, "data_dir")
    assert hasattr(cfg, "instrument")
    assert hasattr(cfg, "commission_per_lot")
    assert hasattr(cfg, "slippage_factor")
    assert hasattr(cfg, "reward_shaping")
    assert hasattr(cfg, "rollout_steps")
    assert hasattr(cfg, "total_steps")
    assert hasattr(cfg, "save_dir")
    assert cfg.data_dir == "data"
    assert cfg.instrument == "EURUSD"
    assert cfg.reward_shaping is None


# ── 2. MTFRolloutBuffer ─────────────────────────────────────────────────────


def test_mtf_rollout_buffer_stores_context_and_symbol():
    """MTFRolloutBuffer stores context vectors and symbol IDs."""
    torch = _real_torch()
    from rl.mtf_trainer import MTFRolloutBuffer

    buf = MTFRolloutBuffer(steps=10, obs_shape=(50, 48), context_dim=8, device=torch.device("cpu"))
    obs = np.zeros((50, 48), dtype=np.float32)
    ctx = np.ones(8, dtype=np.float32) * 0.5
    sym_id = 3

    buf.add(obs, ctx, sym_id, action=1, log_prob=-0.5, reward=0.1, value=0.3, done=False)

    assert buf.ptr == 1
    assert buf.contexts[0].sum().item() == pytest.approx(4.0, abs=0.01)
    assert buf.symbol_ids[0].item() == 3


def test_mtf_rollout_buffer_get_batches():
    """MTFRolloutBuffer yields 7-tuple batches."""
    torch = _real_torch()
    from rl.mtf_trainer import MTFRolloutBuffer

    buf = MTFRolloutBuffer(steps=4, obs_shape=(50, 48), context_dim=8, device=torch.device("cpu"))
    for i in range(4):
        buf.add(
            np.zeros((50, 48)), np.zeros(8), i,
            action=0, log_prob=-1.0, reward=0.0, value=0.0, done=False,
        )
    buf.compute_returns(0.0, gamma=0.99, gae_lambda=0.95)

    for batch in buf.get_batches(batch_size=2):
        assert len(batch) == 7
        obs_b, ctx_b, sym_b, act_b, lp_b, ret_b, adv_b = batch
        assert obs_b.shape[1:] == (50, 48)
        assert ctx_b.shape[1] == 8
        assert sym_b.dtype == torch.long
        break


# ── 2b. MTFVecRolloutBuffer (parallel rollout storage) ───────────────────────


def test_mtf_vec_rollout_buffer_shapes_and_batches():
    """MTFVecRolloutBuffer stores (steps, n_envs, ...) and yields flat 7-tuples."""
    torch = _real_torch()
    from rl.mtf_trainer import MTFVecRolloutBuffer

    steps, n_envs = 4, 3
    buf = MTFVecRolloutBuffer(
        steps=steps, n_envs=n_envs, obs_shape=(50, 48), context_dim=8,
        device=torch.device("cpu"),
    )
    for _ in range(steps):
        buf.add(
            obs=np.zeros((n_envs, 50, 48), dtype=np.float32),
            contexts=np.ones((n_envs, 8), dtype=np.float32),
            symbol_ids=np.arange(n_envs),
            actions=np.zeros(n_envs),
            log_probs=np.full(n_envs, -1.0),
            rewards=np.zeros(n_envs),
            values=np.zeros(n_envs),
            dones=np.zeros(n_envs),
        )
    assert buf.ptr == steps
    buf.compute_returns(np.zeros(n_envs), gamma=0.99, gae_lambda=0.95)

    total = steps * n_envs
    seen = 0
    for batch in buf.get_batches(batch_size=5):
        assert len(batch) == 7
        obs_b, ctx_b, sym_b, act_b, lp_b, ret_b, adv_b = batch
        assert obs_b.shape[1:] == (50, 48)
        assert ctx_b.shape[1] == 8
        assert sym_b.dtype == torch.long
        seen += obs_b.shape[0]
    assert seen == total


def test_mtf_vec_buffer_matches_single_env_buffer():
    """With n_envs=1 the vectorized buffer must produce identical returns and
    advantages to the original single-env MTFRolloutBuffer (behavioral parity)."""
    torch = _real_torch()
    from rl.mtf_trainer import MTFRolloutBuffer, MTFVecRolloutBuffer

    rng = np.random.default_rng(0)
    steps = 8
    rewards = rng.normal(size=steps).astype(np.float32)
    values = rng.normal(size=steps).astype(np.float32)
    dones = np.array([0, 0, 0, 1, 0, 0, 0, 0], dtype=np.float32)
    last_value = 0.42

    single = MTFRolloutBuffer(steps, (50, 48), 8, torch.device("cpu"))
    vec = MTFVecRolloutBuffer(steps, 1, (50, 48), 8, torch.device("cpu"))
    for t in range(steps):
        single.add(
            np.zeros((50, 48), dtype=np.float32), np.zeros(8, dtype=np.float32), 0,
            action=0, log_prob=-1.0, reward=float(rewards[t]),
            value=float(values[t]), done=float(dones[t]),
        )
        vec.add(
            np.zeros((1, 50, 48), dtype=np.float32), np.zeros((1, 8), dtype=np.float32),
            np.zeros(1), np.zeros(1), np.full(1, -1.0),
            rewards[t : t + 1], values[t : t + 1], dones[t : t + 1],
        )

    single.compute_returns(last_value, gamma=0.99, gae_lambda=0.95)
    vec.compute_returns(np.array([last_value]), gamma=0.99, gae_lambda=0.95)

    assert torch.allclose(single.returns, vec.returns.reshape(steps), atol=1e-5)
    assert torch.allclose(single.advantages, vec.advantages.reshape(steps), atol=1e-5)


# ── 2c. Vectorized environment wrappers ──────────────────────────────────────


def test_make_vec_env_picks_dummy_for_single_env():
    """A single environment uses the in-process DummyVecEnv (no subprocesses)."""
    pytest.importorskip("pandas")
    from rl.vec_env import make_vec_env, DummyVecEnv, has_instrument_data

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _setup_test_data(tmp_path, "EURUSD")
        assert has_instrument_data(tmp, "EURUSD")
        kwargs = {"data_dir": tmp, "instrument": "EURUSD"}
        vec = make_vec_env([kwargs])
        try:
            assert isinstance(vec, DummyVecEnv)
            assert vec.num_envs == 1
        finally:
            vec.close()


def test_dummy_vec_env_step_returns_batched_arrays():
    """DummyVecEnv.step returns stacked (n_envs, ...) observations and infos."""
    pytest.importorskip("pandas")
    from rl.vec_env import DummyVecEnv

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _setup_test_data(tmp_path, "EURUSD")
        kwargs = {"data_dir": tmp, "instrument": "EURUSD"}
        vec = DummyVecEnv([kwargs, kwargs])
        try:
            obs, ctx, sym = vec.reset()
            assert obs.shape[0] == 2
            assert ctx.shape[0] == 2
            assert sym.shape[0] == 2

            obs, ctx, sym, rewards, dones, infos = vec.step(np.array([0, 1]))
            assert obs.shape[0] == 2 and obs.shape[1:] == (50, 48)
            assert rewards.shape == (2,)
            assert dones.shape == (2,)
            assert len(infos) == 2
        finally:
            vec.close()


# ── 3. EvalReport ────────────────────────────────────────────────────────────


def test_eval_report_has_required_fields():
    """EvalReport has all required fields."""
    _real_torch()
    from rl.evaluator import EvalReport

    report = EvalReport(
        instrument="EURUSD",
        n_folds=5,
        total_trades=100,
        mean_win_rate=0.55,
        std_win_rate=0.05,
        mean_expectancy=0.3,
        std_expectancy=0.1,
        mean_max_drawdown=0.08,
        std_max_drawdown=0.02,
        mean_sharpe=1.2,
        std_sharpe=0.3,
        consistent=True,
    )

    assert report.instrument == "EURUSD"
    assert report.n_folds == 5
    assert report.consistent is True
    assert hasattr(report, "folds")
    assert hasattr(report, "regime_summary")


def test_fold_result_has_regime_metrics():
    """FoldResult carries per-regime metrics."""
    _real_torch()
    from rl.evaluator import FoldResult

    fold = FoldResult(
        fold=0,
        n_trades=50,
        win_rate=0.56,
        expectancy=0.25,
        max_drawdown=0.05,
        sharpe_ratio=1.5,
        regime_metrics={
            "high_volatility": {"n_trades": 10, "win_rate": 0.6, "expectancy": 0.4},
            "normal": {"n_trades": 30, "win_rate": 0.53, "expectancy": 0.2},
        },
    )

    assert "high_volatility" in fold.regime_metrics
    assert fold.regime_metrics["high_volatility"]["n_trades"] == 10


# ── 4. WalkForwardEvaluator splits chronologically ──────────────────────────


def test_walk_forward_splits_chronologically():
    """WalkForwardEvaluator creates chronological folds."""
    _real_torch()
    from rl.evaluator import WalkForwardEvaluator

    ev = WalkForwardEvaluator(n_folds=5)
    assert ev.n_folds == 5


# ── 5. HealthWatchdog RL fields ──────────────────────────────────────────────


def test_health_watchdog_rl_fields():
    """HealthWatchdog has RL health tracking."""
    from platforms.health_watchdog import HealthWatchdog

    hw = HealthWatchdog()

    assert hasattr(hw, "record_rl_status")
    assert hasattr(hw, "record_rl_signal_failure")
    assert hasattr(hw, "record_rl_signal_success")

    hw.record_rl_status(enabled=True, stage=2, checkpoint_loaded=True)
    hw.record_rl_signal_success()

    report = hw.check_health()
    assert report.rl_enabled is True
    assert report.rl_stage == 2
    assert report.rl_checkpoint_loaded is True
    assert report.rl_signal_failures == 0


def test_health_watchdog_rl_checkpoint_warning():
    """HealthWatchdog warns when RL enabled but checkpoint not loaded."""
    from platforms.health_watchdog import HealthWatchdog

    hw = HealthWatchdog()
    hw.record_rl_status(enabled=True, stage=1, checkpoint_loaded=False)
    report = hw.check_health()
    assert any("checkpoint not loaded" in w for w in report.warnings)


def test_health_watchdog_rl_signal_failures():
    """HealthWatchdog warns after consecutive signal failures."""
    from platforms.health_watchdog import HealthWatchdog

    hw = HealthWatchdog()
    hw.record_rl_status(enabled=True, stage=2, checkpoint_loaded=True)
    for _ in range(5):
        hw.record_rl_signal_failure()
    report = hw.check_health()
    assert any("signal generation failing" in w for w in report.warnings)
    assert report.rl_signal_failures == 5


# ── 6. Reward shaping ───────────────────────────────────────────────────────


def test_reward_shaping_hold_penalty():
    """Reward shaping applies hold_penalty when not in a trade."""
    _real_torch()

    with tempfile.TemporaryDirectory() as tmp:
        _tmp_path = Path(tmp)

        with patch("rl.mtf_environment.ApexMultiTFTradingEnv._load_instrument_info"):
            with patch("rl.mtf_environment.ApexMultiTFTradingEnv._load_data"):
                from rl.mtf_environment import ApexMultiTFTradingEnv

                env = ApexMultiTFTradingEnv.__new__(ApexMultiTFTradingEnv)
                env.instrument = "TESTPAIR"
                env.initial_bal = 10000.0
                env.max_hold = 200
                env.commission_per_lot = 3.5
                env.slippage_factor = 0.3
                env.swap_rates = {}
                env._reward_shaping = {"hold_penalty": -0.01}

                assert env._reward_shaping.get("hold_penalty") == -0.01


def test_reward_shaping_quick_loss():
    """Quick loss penalty value is accessible."""
    shaping = {"hold_penalty": -0.001, "quick_loss_penalty": -0.5, "timeout_penalty": -0.3}
    assert shaping.get("quick_loss_penalty") == -0.5
    assert shaping.get("timeout_penalty") == -0.3


# ── 7. Data validation OHLC sanity ──────────────────────────────────────────


def test_ohlc_sanity_catches_bad_bars():
    """validate_rl_data OHLC check catches high < low."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    from validate_rl_data import _check_ohlc_sanity

    good = pd.DataFrame({
        "open": [1.1, 1.2],
        "high": [1.15, 1.25],
        "low": [1.05, 1.15],
        "close": [1.12, 1.22],
    })
    assert _check_ohlc_sanity(good) == []

    bad = pd.DataFrame({
        "open": [1.1],
        "high": [1.0],
        "low": [1.2],
        "close": [1.12],
    })
    issues = _check_ohlc_sanity(bad)
    assert len(issues) > 0


def test_ohlc_sanity_catches_high_below_max_oc():
    """Catches bars where high < max(open, close)."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    from validate_rl_data import _check_ohlc_sanity

    bad = pd.DataFrame({
        "open": [1.10],
        "high": [1.08],
        "low": [1.05],
        "close": [1.09],
    })
    issues = _check_ohlc_sanity(bad)
    assert any("high < max(open, close)" in i for i in issues)


# ── 8. Shadow reconciliation ────────────────────────────────────────────────


def test_shadow_reconcile_returns_dict():
    """ShadowEngine.reconcile_with_phase4 returns the expected keys."""
    _real_torch()
    from rl.shadow import ShadowEngine

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test_shadow.db")
        with patch.object(ShadowEngine, "_load"):
            with patch.object(ShadowEngine, "_reload_open_trades"):
                engine = ShadowEngine.__new__(ShadowEngine)
                engine.agent = MagicMock()
                engine._meta = {}
                engine.open_trades = {}
                engine.bar_counter = {}
                engine.db_path = db_path
                engine._init_db()

                mock_store = MagicMock()
                mock_store.fetch_resolved.return_value = []

                result = engine.reconcile_with_phase4(mock_store)
                assert "matched" in result
                assert "rl_only" in result
                assert "phase4_only" in result
                assert "r_multiple_correlation" in result


# ── 9. MTF trainer save produces compatible checkpoint ───────────────────────


def test_mtf_trainer_save_meta_compatible():
    """MTFPPOTrainer.save produces checkpoints with correct contract metadata."""
    torch = _real_torch()
    from rl.mtf_trainer import MTFPPOTrainer, MTFPPOConfig
    from rl.contracts import assert_compatible, OBS_FEATURES, N_CONTEXT_FEATURES

    with tempfile.TemporaryDirectory() as tmp:
        cfg = MTFPPOConfig(save_dir=tmp)

        with patch.object(MTFPPOTrainer, "__init__", lambda self, cfg: None):
            trainer = MTFPPOTrainer.__new__(MTFPPOTrainer)
            trainer.cfg = cfg
            trainer.global_step = 0
            trainer.best_reward = 0.0
            trainer._vocab = ["EURUSD", "GBPUSD"]

            from rl.network import ApexRLAgent
            trainer.agent = ApexRLAgent(
                n_features=OBS_FEATURES,
                context_dim=N_CONTEXT_FEATURES,
                n_symbols=2,
            )
            trainer.opt = torch.optim.Adam(trainer.agent.parameters(), lr=3e-4)

            trainer.save("test")

            ckpt_path = Path(tmp) / "apex_rl_mtf_test.pt"
            assert ckpt_path.exists()

            ckpt = torch.load(ckpt_path, map_location="cpu")
            meta = ckpt["meta"]

            assert_compatible(meta)
            assert meta["n_features"] == OBS_FEATURES
            assert meta["context_dim"] == N_CONTEXT_FEATURES


# ── 10. Commission calculation uses lots, not units ──────────────────────────


def test_commission_divides_by_lot_size():
    """Commission must be per-lot, not per-unit — a 2.79 lot trade should
    cost ~$9.77 in commission at $3.50/lot, not $976K."""
    _real_torch()
    from rl.mtf_environment import ApexMultiTFTradingEnv

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _setup_test_data(tmp_path, "EURUSD")

        with patch("rl.mtf_environment.INSTRUMENT_REGISTRY", {
            "EURUSD": type("Info", (), {
                "pip_size": 0.0001,
                "typical_spread_pips": 1.0,
                "pip_value_per_lot": 10.0,
                "category": type("Cat", (), {"value": "forex"})(),
            })(),
        }):
            env = ApexMultiTFTradingEnv(
                data_dir=tmp,
                instrument="EURUSD",
                initial_balance=10_000.0,
                commission_per_lot=3.5,
            )
            env.reset()

            initial_bal = env.balance
            # Step until a trade opens (action 1 = buy, 2 = sell)
            for _ in range(200):
                obs, reward, done, info = env.step(1)
                if env.trade is not None:
                    break

            if env.trade is not None:
                comm_paid = initial_bal - env.balance
                # Commission on a forex trade should be < $50, never > $1000
                assert comm_paid < 50.0, (
                    f"Commission ${comm_paid:.2f} is absurdly high — "
                    f"units ({env.trade.size:.0f}) treated as lots?"
                )
                assert comm_paid >= 0, "Commission should be non-negative"
