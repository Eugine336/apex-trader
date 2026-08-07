"""
Tests for RL Phase 4 wiring — checkpoint init, bridge safety,
update_price wiring, and shadow loop closure.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from rl.contracts import (
    N_CONTEXT_FEATURES,
    OBS_CONTRACT_VERSION,
    OBS_FEATURES,
    OBS_SHAPE,
    build_symbol_vocab,
    schema_hash,
)
from rl.network import ApexRLAgent


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_checkpoint(path: str, **meta_overrides) -> str:
    """Create a valid MTF checkpoint at *path*."""
    vocab = build_symbol_vocab()
    agent = ApexRLAgent(
        n_features=OBS_FEATURES,
        n_actions=4,
        context_dim=N_CONTEXT_FEATURES,
        n_symbols=len(vocab),
    )
    meta = {
        "obs_contract_version": OBS_CONTRACT_VERSION,
        "obs_schema_hash": schema_hash(),
        "n_features": OBS_FEATURES,
        "context_dim": N_CONTEXT_FEATURES,
        "n_symbols": len(vocab),
        "symbol_vocab": vocab,
        "initialized_only": True,
    }
    meta.update(meta_overrides)

    ckpt = {"agent": agent.state_dict(), "step": 0, "meta": meta}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    torch.save(ckpt, path)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# A) Checkpoint init script produces a loadable agent
# ══════════════════════════════════════════════════════════════════════════════


class TestCheckpointInit:
    def test_init_script_produces_loadable_checkpoint(self, tmp_path):
        out = str(tmp_path / "test_ckpt.pt")
        _make_checkpoint(out)

        ckpt = torch.load(out, map_location="cpu", weights_only=False)
        meta = ckpt["meta"]
        assert meta["obs_contract_version"] == OBS_CONTRACT_VERSION
        assert meta["obs_schema_hash"] == schema_hash()
        assert meta["n_features"] == OBS_FEATURES
        assert meta["context_dim"] == N_CONTEXT_FEATURES

    def test_agent_dims_match_contract(self, tmp_path):
        out = str(tmp_path / "test_ckpt.pt")
        _make_checkpoint(out)

        ckpt = torch.load(out, map_location="cpu", weights_only=False)
        meta = ckpt["meta"]
        agent = ApexRLAgent(
            n_features=meta["n_features"],
            context_dim=meta["context_dim"],
            n_symbols=meta["n_symbols"],
        )
        agent.load_state_dict(ckpt["agent"])

        obs = np.random.randn(*OBS_SHAPE).astype(np.float32)
        ctx = np.random.randn(N_CONTEXT_FEATURES).astype(np.float32)
        action, conf, exp_r = agent.predict(obs, context_vec=ctx, symbol_id=0)
        assert 0 <= action <= 3
        assert 0.0 <= conf <= 1.0

    def test_checkpoint_not_committed_to_git(self):
        gitignore = Path(__file__).resolve().parent.parent / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "checkpoints/*.pt" in content


# ══════════════════════════════════════════════════════════════════════════════
# B) Bridge safety — no-op when disabled or checkpoint missing
# ══════════════════════════════════════════════════════════════════════════════


class TestBridgeSafety:
    def test_bridge_disabled_is_noop(self, tmp_path):
        from rl.bridge import RLBridge

        bridge = RLBridge(
            checkpoint=str(tmp_path / "nonexistent.pt"),
            authority_db=str(tmp_path / "auth.db"),
            enabled=False,
        )
        assert bridge.enabled is False
        assert bridge.shadow is None

        obs = np.random.randn(*OBS_SHAPE).astype(np.float32)
        result = bridge.augment_score("EURUSD", 80.0, obs)
        assert result.final_score == 80.0
        assert result.rl_delta == 0.0
        assert result.vetoed is False

    def test_bridge_missing_checkpoint_disables_gracefully(self, tmp_path):
        from rl.bridge import RLBridge

        bridge = RLBridge(
            checkpoint=str(tmp_path / "missing.pt"),
            shadow_db=str(tmp_path / "shadow.db"),
            authority_db=str(tmp_path / "auth.db"),
            enabled=True,
        )
        assert bridge.enabled is False

        obs = np.random.randn(*OBS_SHAPE).astype(np.float32)
        result = bridge.augment_score("EURUSD", 75.0, obs)
        assert result.final_score == 75.0
        assert result.rl_delta == 0.0

    def test_bridge_loads_valid_checkpoint(self, tmp_path):
        from rl.bridge import RLBridge

        ckpt_path = str(tmp_path / "ckpt.pt")
        _make_checkpoint(ckpt_path)

        bridge = RLBridge(
            checkpoint=ckpt_path,
            shadow_db=str(tmp_path / "shadow.db"),
            authority_db=str(tmp_path / "auth.db"),
            enabled=True,
        )
        assert bridge.enabled is True
        assert bridge.shadow is not None
        assert bridge.shadow.agent is not None

    def test_bridge_rejects_schema_mismatch(self, tmp_path):
        from rl.bridge import RLBridge

        ckpt_path = str(tmp_path / "bad.pt")
        _make_checkpoint(ckpt_path, obs_contract_version="wrong-v99")

        bridge = RLBridge(
            checkpoint=ckpt_path,
            shadow_db=str(tmp_path / "shadow.db"),
            authority_db=str(tmp_path / "auth.db"),
            enabled=True,
        )
        assert bridge.enabled is False

    def test_update_price_noop_when_disabled(self, tmp_path):
        from rl.bridge import RLBridge

        bridge = RLBridge(
            checkpoint=str(tmp_path / "none.pt"),
            authority_db=str(tmp_path / "auth.db"),
            enabled=False,
        )
        bridge.update_price("EURUSD", 1.10, 1.09, 1.095, 0.0005, None)


# ══════════════════════════════════════════════════════════════════════════════
# C) update_price wiring — shadow trades close
# ══════════════════════════════════════════════════════════════════════════════


class TestUpdatePriceWiring:
    def test_shadow_trades_open_and_close_via_update_price(self, tmp_path):
        from rl.bridge import RLBridge

        ckpt_path = str(tmp_path / "ckpt.pt")
        _make_checkpoint(ckpt_path)

        bridge = RLBridge(
            checkpoint=ckpt_path,
            shadow_db=str(tmp_path / "shadow.db"),
            authority_db=str(tmp_path / "auth.db"),
        )
        assert bridge.enabled

        # Deterministic: this test asserts an FX (session-gated) entry opens
        # regardless of when CI runs, so bypass the live session gate here.
        bridge.shadow.session_filter_enabled = False

        from rl.shadow import RLSignal

        signal = RLSignal(
            pair="EURUSD",
            timestamp="2026-01-01T00:00:00Z",
            action=1,
            action_label="BUY",
            confidence=0.8,
            expected_r=1.5,
            latent_repr=[0.0] * 10,
            authority="SHADOW",
        )
        close_price = 1.1000
        atr = 0.0020
        pip_size = 0.0001
        bridge.shadow.open_shadow_trade("EURUSD", signal, close_price, atr, pip_size)
        assert "EURUSD" in bridge.shadow.open_trades
        assert not bridge.shadow.open_trades["EURUSD"].closed

        trade = bridge.shadow.open_trades["EURUSD"]
        tp_price = trade.tp

        bridge.update_price("EURUSD", tp_price + 0.001, trade.entry, tp_price, atr, None)
        assert bridge.shadow.open_trades["EURUSD"].closed
        assert bridge.shadow.open_trades["EURUSD"].close_reason == "tp"

    def test_shadow_score_populates_after_closed_trades(self, tmp_path):
        from rl.bridge import RLBridge
        from rl.shadow import RLSignal

        ckpt_path = str(tmp_path / "ckpt.pt")
        _make_checkpoint(ckpt_path)

        bridge = RLBridge(
            checkpoint=ckpt_path,
            shadow_db=str(tmp_path / "shadow.db"),
            authority_db=str(tmp_path / "auth.db"),
        )

        for i in range(5):
            pair = f"PAIR{i}"
            signal = RLSignal(
                pair=pair, timestamp="2026-01-01T00:00:00Z",
                action=1, action_label="BUY", confidence=0.8,
                expected_r=1.5, latent_repr=[], authority="SHADOW",
            )
            bridge.shadow.open_shadow_trade(pair, signal, 1.1, 0.002, 0.0001)
            trade = bridge.shadow.open_trades[pair]
            bridge.update_price(pair, trade.tp + 0.01, trade.entry, trade.tp, 0.002, None)

        score = bridge.shadow.shadow_score()
        assert score["n_trades"] == 5


# ══════════════════════════════════════════════════════════════════════════════
# D) Phase 4 shadow contract creation
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase4ShadowContracts:
    def test_rl_shadow_creates_phase4_contract(self, tmp_path):
        from rl.bridge import RLBridge
        from rl.shadow import RLSignal
        from persistence.shadow_store import ShadowStore

        ckpt_path = str(tmp_path / "ckpt.pt")
        _make_checkpoint(ckpt_path)

        shadow_store = ShadowStore(db_path=tmp_path / "test_shadow.db")

        bridge = RLBridge(
            checkpoint=ckpt_path,
            shadow_db=str(tmp_path / "shadow.db"),
            authority_db=str(tmp_path / "auth.db"),
        )
        bridge._shadow_store = shadow_store

        signal = RLSignal(
            pair="EURUSD", timestamp="2026-01-01T00:00:00Z",
            action=1, action_label="BUY", confidence=0.8,
            expected_r=1.5, latent_repr=[], authority="SHADOW",
        )
        bridge._handle_shadow("EURUSD", signal, 1.1000, 0.0020, 0.0001)

        contracts = shadow_store.get_all_contracts(status="PENDING")
        assert len(contracts) >= 1
        c = contracts[0]
        assert c.symbol == "EURUSD"
        assert c.direction == "LONG"
        assert c.rejecting_gate == "rl_shadow"
        assert c.entry_price > 0
        assert c.stop_loss > 0
        assert c.tp1 > 0
        assert c.tp2 > 0

        shadow_store.close()

    def test_no_contract_when_confidence_too_low(self, tmp_path):
        from rl.bridge import RLBridge
        from rl.shadow import RLSignal
        from persistence.shadow_store import ShadowStore

        ckpt_path = str(tmp_path / "ckpt.pt")
        _make_checkpoint(ckpt_path)

        shadow_store = ShadowStore(db_path=tmp_path / "test_shadow.db")

        bridge = RLBridge(
            checkpoint=ckpt_path,
            shadow_db=str(tmp_path / "shadow.db"),
            authority_db=str(tmp_path / "auth.db"),
        )
        bridge._shadow_store = shadow_store

        signal = RLSignal(
            pair="EURUSD", timestamp="2026-01-01T00:00:00Z",
            action=1, action_label="BUY", confidence=0.30,
            expected_r=1.0, latent_repr=[], authority="SHADOW",
        )
        bridge._handle_shadow("EURUSD", signal, 1.1000, 0.0020, 0.0001)

        contracts = shadow_store.get_all_contracts(status="PENDING")
        assert len(contracts) == 0

        shadow_store.close()


# ══════════════════════════════════════════════════════════════════════════════
# E) MTF obs builder integration in augment_score
# ══════════════════════════════════════════════════════════════════════════════


class TestMTFObsIntegration:
    def test_augment_score_with_mtf_obs_and_context(self, tmp_path):
        from rl.bridge import RLBridge

        ckpt_path = str(tmp_path / "ckpt.pt")
        _make_checkpoint(ckpt_path)

        bridge = RLBridge(
            checkpoint=ckpt_path,
            shadow_db=str(tmp_path / "shadow.db"),
            authority_db=str(tmp_path / "auth.db"),
        )
        assert bridge.enabled

        obs = np.random.randn(*OBS_SHAPE).astype(np.float32)
        ctx = np.random.randn(N_CONTEXT_FEATURES).astype(np.float32)

        result = bridge.augment_score(
            pair="EURUSD", base_score=80.0, obs=obs,
            close=1.10, atr=0.002, pip_size=0.0001,
            context_vec=ctx, symbol_id=0,
        )
        assert result.pair == "EURUSD"
        assert result.authority_stage == 1
        assert result.final_score == 80.0


# ══════════════════════════════════════════════════════════════════════════════
# F) Checkpoint resolution is anchored to the repo root (not the cwd)
# ══════════════════════════════════════════════════════════════════════════════


class TestCheckpointResolution:
    def test_resolve_uses_repo_root_not_cwd(self, tmp_path, monkeypatch):
        """A multi-tenant instance runs with cwd set to its isolated workdir;
        the shared checkpoint must still resolve under the repo root so the
        trained model is reachable instead of silently 'missing'."""
        from rl.bridge import resolve_rl_checkpoint

        repo = tmp_path / "code_repo"
        (repo / "checkpoints").mkdir(parents=True)
        monkeypatch.setenv("APEX_REPO_DIR", str(repo))
        # Simulate the per-user cwd being elsewhere (no checkpoints/ here).
        monkeypatch.chdir(tmp_path)

        resolved = Path(resolve_rl_checkpoint())
        assert resolved.parent == (repo / "checkpoints")
        assert resolved.name == "apex_rl_best.pt"

    def test_resolve_prefers_mtf_checkpoint(self, tmp_path, monkeypatch):
        from rl.bridge import resolve_rl_checkpoint

        repo = tmp_path / "code_repo"
        ckpts = repo / "checkpoints"
        ckpts.mkdir(parents=True)
        (ckpts / "apex_rl_mtf_best.pt").write_bytes(b"x")
        monkeypatch.setenv("APEX_REPO_DIR", str(repo))

        resolved = Path(resolve_rl_checkpoint())
        assert resolved == ckpts / "apex_rl_mtf_best.pt"

    def test_resolve_honours_explicit_override(self):
        from rl.bridge import resolve_rl_checkpoint

        assert resolve_rl_checkpoint("/tmp/explicit.pt") == "/tmp/explicit.pt"

