"""
Tests for the RL checkpoint ↔ contract pipeline.

Covers:
  (a) save() → load() round-trips when contract matches
  (b) load() raises when obs_schema_hash differs
  (c) load() raises when obs_contract_version differs
  (d) H6 dimension guard raises when n_features != OBS_FEATURES
  (e) H6 dimension guard raises when context_dim != N_CONTEXT_FEATURES
"""

from __future__ import annotations

import tempfile
import os
from pathlib import Path

import pytest
import torch

from rl.contracts import (
    OBS_CONTRACT_VERSION,
    OBS_FEATURES,
    N_CONTEXT_FEATURES,
    schema_hash,
    schema,
)
from rl.network import ApexRLAgent
from rl.shadow import ShadowEngine


def _make_checkpoint(
    tmp_dir: str,
    *,
    n_features: int = OBS_FEATURES,
    context_dim: int = N_CONTEXT_FEATURES,
    n_symbols: int = 0,
    version: str = OBS_CONTRACT_VERSION,
    hash_val: str | None = None,
    omit_meta: bool = False,
) -> str:
    """Build a minimal checkpoint file and return its path."""
    agent = ApexRLAgent(
        n_features=n_features,
        n_actions=4,
        context_dim=context_dim,
        n_symbols=n_symbols,
    )
    meta = {
        "obs_contract_version": version,
        "obs_schema_hash": hash_val if hash_val is not None else schema_hash(),
        "obs_schema": schema(),
        "n_features": n_features,
        "context_dim": context_dim,
        "n_symbols": n_symbols,
    }
    ckpt = {
        "agent": agent.state_dict(),
        "step": 0,
        "meta": meta if not omit_meta else {},
    }
    path = os.path.join(tmp_dir, "test_ckpt.pt")
    torch.save(ckpt, path)
    return path


class TestRoundTrip:
    """(a) save() → load() succeeds when contract matches."""

    def test_valid_checkpoint_loads(self, tmp_path):
        path = _make_checkpoint(str(tmp_path))
        db_path = str(tmp_path / "shadow.db")
        engine = ShadowEngine(path, db_path=db_path)
        assert engine.agent is not None
        assert engine._meta["obs_contract_version"] == OBS_CONTRACT_VERSION
        assert engine._meta["obs_schema_hash"] == schema_hash()
        assert engine._meta["n_features"] == OBS_FEATURES
        assert engine._meta["context_dim"] == N_CONTEXT_FEATURES

    def test_round_trip_preserves_weights(self, tmp_path):
        agent = ApexRLAgent(
            n_features=OBS_FEATURES,
            n_actions=4,
            context_dim=N_CONTEXT_FEATURES,
        )
        original_weight = agent.encoder.proj[0].weight.clone()

        meta = {
            "obs_contract_version": OBS_CONTRACT_VERSION,
            "obs_schema_hash": schema_hash(),
            "n_features": OBS_FEATURES,
            "context_dim": N_CONTEXT_FEATURES,
            "n_symbols": 0,
        }
        path = str(tmp_path / "ckpt.pt")
        torch.save({"agent": agent.state_dict(), "step": 0, "meta": meta}, path)

        db_path = str(tmp_path / "shadow.db")
        engine = ShadowEngine(path, db_path=db_path)
        loaded_weight = engine.agent.encoder.proj[0].weight
        assert torch.allclose(original_weight, loaded_weight)


class TestSchemaHashMismatch:
    """(b) load() raises when obs_schema_hash differs."""

    def test_wrong_hash_raises(self, tmp_path):
        path = _make_checkpoint(str(tmp_path), hash_val="deadbeef00000000")
        db_path = str(tmp_path / "shadow.db")
        with pytest.raises(ValueError, match="obs_schema_hash"):
            ShadowEngine(path, db_path=db_path)


class TestVersionMismatch:
    """(c) load() raises when obs_contract_version differs."""

    def test_wrong_version_raises(self, tmp_path):
        path = _make_checkpoint(str(tmp_path), version="stale-v0")
        db_path = str(tmp_path / "shadow.db")
        with pytest.raises(ValueError, match="obs_contract_version"):
            ShadowEngine(path, db_path=db_path)

    def test_missing_meta_raises(self, tmp_path):
        path = _make_checkpoint(str(tmp_path), omit_meta=True)
        db_path = str(tmp_path / "shadow.db")
        with pytest.raises(ValueError, match="obs_contract_version"):
            ShadowEngine(path, db_path=db_path)


class TestDimensionGuard:
    """(d)+(e) H6 — checkpoint dim != production contract → loud failure."""

    def test_wrong_n_features_raises(self, tmp_path):
        path = _make_checkpoint(str(tmp_path), n_features=12)
        db_path = str(tmp_path / "shadow.db")
        with pytest.raises(ValueError, match="n_features=12.*OBS_FEATURES=48"):
            ShadowEngine(path, db_path=db_path)

    def test_wrong_context_dim_raises(self, tmp_path):
        path = _make_checkpoint(str(tmp_path), context_dim=0)
        db_path = str(tmp_path / "shadow.db")
        with pytest.raises(ValueError, match="context_dim=0.*N_CONTEXT_FEATURES=8"):
            ShadowEngine(path, db_path=db_path)

    def test_both_wrong_fails_on_first(self, tmp_path):
        path = _make_checkpoint(str(tmp_path), n_features=12, context_dim=0)
        db_path = str(tmp_path / "shadow.db")
        with pytest.raises(ValueError, match="n_features"):
            ShadowEngine(path, db_path=db_path)


class TestTrainerSaveMeta:
    """Verify trainer.save() now persists contract metadata."""

    def test_trainer_save_includes_meta(self, tmp_path):
        from rl.trainer import PPOTrainer, PPOConfig

        csv_path = str(tmp_path / "dummy.csv")
        import pandas as pd
        import numpy as np

        n = 200
        df = pd.DataFrame({
            "time": pd.date_range("2024-01-01", periods=n, freq="h"),
            "open": np.random.randn(n).cumsum() + 100,
            "high": np.random.randn(n).cumsum() + 101,
            "low": np.random.randn(n).cumsum() + 99,
            "close": np.random.randn(n).cumsum() + 100,
            "volume": np.abs(np.random.randn(n) * 1000),
        })
        df.to_csv(csv_path, index=False)

        cfg = PPOConfig(
            save_dir=str(tmp_path / "ckpts"),
            total_steps=1,
            rollout_steps=64,
        )
        trainer = PPOTrainer(cfg, csv_path)
        trainer.save("test")

        ckpt_path = tmp_path / "ckpts" / "apex_rl_test.pt"
        assert ckpt_path.exists()

        ckpt = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
        assert "meta" in ckpt
        meta = ckpt["meta"]
        assert meta["obs_contract_version"] == OBS_CONTRACT_VERSION
        assert meta["obs_schema_hash"] == schema_hash()
        assert isinstance(meta["n_features"], int)
        assert isinstance(meta["context_dim"], int)
