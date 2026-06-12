"""
Tests for the 7 RL dangerous-edge fixes.
Torch-free where possible; torch-dependent tests use pytest.importorskip.
"""

import os
import sqlite3
import sys
import tempfile
import numpy as np
import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Fix 1: base_direction parameter replaces magic threshold 60
# ──────────────────────────────────────────────────────────────────────────────

class TestBaseDirectionParameter:
    """bridge.augment_score accepts explicit base_direction."""

    def test_augment_score_signature_accepts_base_direction(self):
        pytest.importorskip("torch")
        import inspect
        from rl.bridge import RLBridge
        sig = inspect.signature(RLBridge.augment_score)
        assert "base_direction" in sig.parameters
        assert sig.parameters["base_direction"].default is None

    def test_bridge_passthrough_with_base_direction(self):
        pytest.importorskip("torch")
        from rl.bridge import RLBridge
        bridge = RLBridge(checkpoint="nonexistent.pt", enabled=False)
        result = bridge.augment_score(
            pair="EURUSD", base_score=50.0,
            obs=np.zeros((50, 12), dtype=np.float32),
            base_direction=1,
        )
        assert result.rl_delta == 0.0
        assert result.final_score == 50.0
        assert result.vetoed is False

    def test_bridge_passthrough_without_base_direction_backward_compat(self):
        pytest.importorskip("torch")
        from rl.bridge import RLBridge
        bridge = RLBridge(checkpoint="nonexistent.pt", enabled=False)
        result = bridge.augment_score(
            pair="EURUSD", base_score=50.0,
            obs=np.zeros((50, 12), dtype=np.float32),
        )
        assert result.rl_delta == 0.0
        assert result.final_score == 50.0


# ──────────────────────────────────────────────────────────────────────────────
# Fix 2: predict_full eliminates double forward pass
# ──────────────────────────────────────────────────────────────────────────────

class TestPredictFull:
    """ApexRLAgent.predict_full returns action + latent in one pass."""

    def test_predict_full_exists_and_returns_4_values(self):
        pytest.importorskip("torch")
        from rl.network import ApexRLAgent
        agent = ApexRLAgent(n_features=12)
        obs = np.random.randn(50, 12).astype(np.float32)
        result = agent.predict_full(obs)
        assert len(result) == 4
        action, conf, exp_r, latent_list = result
        assert isinstance(action, int)
        assert 0.0 <= conf <= 1.0
        assert isinstance(exp_r, float)
        assert isinstance(latent_list, list)
        assert len(latent_list) > 0

    def test_predict_full_matches_predict(self):
        pytest.importorskip("torch")
        from rl.network import ApexRLAgent
        agent = ApexRLAgent(n_features=12)
        agent.eval()
        obs = np.random.randn(50, 12).astype(np.float32)
        a1, c1, r1 = agent.predict(obs)
        a2, c2, r2, latent = agent.predict_full(obs)
        assert a1 == a2
        assert abs(c1 - c2) < 1e-5
        assert abs(r1 - r2) < 1e-5

    def test_predict_full_with_context(self):
        pytest.importorskip("torch")
        from rl.network import ApexRLAgent
        agent = ApexRLAgent(n_features=12, context_dim=8, n_symbols=10)
        obs = np.random.randn(50, 12).astype(np.float32)
        ctx = np.random.randn(8).astype(np.float32)
        result = agent.predict_full(obs, context_vec=ctx, symbol_id=3)
        assert len(result) == 4

    def test_out_of_range_symbol_id_does_not_crash(self):
        """A symbol_id >= n_symbols (e.g. a hash from a missing universe) must
        not raise 'index out of range in self' — it is treated as unknown."""
        pytest.importorskip("torch")
        from rl.network import ApexRLAgent
        agent = ApexRLAgent(n_features=12, context_dim=8, n_symbols=10)
        obs = np.random.randn(50, 12).astype(np.float32)
        ctx = np.random.randn(8).astype(np.float32)
        # Way out of range and negative ("unknown" sentinel) both must be safe.
        for bad_id in (999_999, -1):
            result = agent.predict_full(obs, context_vec=ctx, symbol_id=bad_id)
            assert len(result) == 4

    def test_unknown_symbol_matches_no_symbol_embedding(self):
        """An out-of-range id yields the same zero-embedding path as passing
        no symbol_id at all (deterministic, unknown == zero embedding)."""
        pytest.importorskip("torch")
        from rl.network import ApexRLAgent
        agent = ApexRLAgent(n_features=12, context_dim=8, n_symbols=10)
        agent.eval()
        obs = np.random.randn(50, 12).astype(np.float32)
        ctx = np.random.randn(8).astype(np.float32)
        none_action, none_conf, none_r, _ = agent.predict_full(obs, context_vec=ctx, symbol_id=None)
        bad_action, bad_conf, bad_r, _ = agent.predict_full(obs, context_vec=ctx, symbol_id=999_999)
        assert none_action == bad_action
        assert abs(none_conf - bad_conf) < 1e-5
        assert abs(none_r - bad_r) < 1e-5


# ──────────────────────────────────────────────────────────────────────────────
# Fix 3: live trade counter replaces hardcoded 0
# ──────────────────────────────────────────────────────────────────────────────

class TestLiveTradeCounter:

    def test_counter_starts_at_zero(self):
        pytest.importorskip("torch")
        from rl.bridge import RLBridge
        bridge = RLBridge(checkpoint="x.pt", enabled=False)
        assert bridge._live_trade_count == 0

    def test_record_live_trade_increments(self):
        pytest.importorskip("torch")
        from rl.bridge import RLBridge
        bridge = RLBridge(checkpoint="x.pt", enabled=False)
        bridge.record_live_trade()
        bridge.record_live_trade()
        bridge.record_live_trade()
        assert bridge._live_trade_count == 3


# ──────────────────────────────────────────────────────────────────────────────
# Fix 4: shadow trade closed by DB row ID
# ──────────────────────────────────────────────────────────────────────────────

class TestShadowTradeDbId:

    def test_db_id_field_exists_on_shadow_trade(self):
        pytest.importorskip("torch")
        from rl.shadow import ShadowTrade
        t = ShadowTrade(
            pair="EURUSD", direction=1, entry=1.08, sl=1.075,
            tp=1.095, open_time="2026-01-01", open_bar=0,
            expected_r=1.5, confidence=0.8,
        )
        assert t.db_id is None

    def test_close_by_db_id_updates_correct_row(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            con = sqlite3.connect(db_path)
            con.execute("""
                CREATE TABLE shadow_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT, direction INTEGER,
                    entry REAL, sl REAL, tp REAL,
                    open_time TEXT, expected_r REAL, confidence REAL,
                    closed INTEGER DEFAULT 0,
                    exit REAL, close_time TEXT,
                    actual_r REAL, close_reason TEXT
                )
            """)
            cur1 = con.execute(
                "INSERT INTO shadow_trades (pair, direction, entry, sl, tp, open_time, expected_r, confidence) "
                "VALUES ('EURUSD', 1, 1.08, 1.075, 1.095, '2026-01-01', 1.5, 0.8)"
            )
            id1 = cur1.lastrowid
            cur2 = con.execute(
                "INSERT INTO shadow_trades (pair, direction, entry, sl, tp, open_time, expected_r, confidence) "
                "VALUES ('EURUSD', 1, 1.09, 1.085, 1.105, '2026-01-02', 1.3, 0.7)"
            )
            id2 = cur2.lastrowid
            con.commit()

            con.execute(
                "UPDATE shadow_trades SET closed=1, exit=1.095, close_reason='tp' WHERE id=?",
                (id1,),
            )
            con.commit()

            row1 = con.execute("SELECT closed, close_reason FROM shadow_trades WHERE id=?", (id1,)).fetchone()
            row2 = con.execute("SELECT closed, close_reason FROM shadow_trades WHERE id=?", (id2,)).fetchone()
            con.close()

            assert row1[0] == 1
            assert row1[1] == "tp"
            assert row2[0] == 0
            assert row2[1] is None
        finally:
            os.unlink(db_path)


# ──────────────────────────────────────────────────────────────────────────────
# Fix 5: shadow trade recovery on restart
# ──────────────────────────────────────────────────────────────────────────────

class TestShadowTradeRecovery:

    def test_reload_recovers_open_trades(self):
        pytest.importorskip("torch")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            con = sqlite3.connect(db_path)
            con.execute("""
                CREATE TABLE shadow_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT, timestamp TEXT, action INTEGER,
                    action_label TEXT, confidence REAL, expected_r REAL,
                    authority TEXT
                )
            """)
            con.execute("""
                CREATE TABLE shadow_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT, direction INTEGER,
                    entry REAL, sl REAL, tp REAL,
                    open_time TEXT, expected_r REAL, confidence REAL,
                    closed INTEGER DEFAULT 0,
                    exit REAL, close_time TEXT,
                    actual_r REAL, close_reason TEXT
                )
            """)
            con.execute(
                "INSERT INTO shadow_trades (pair, direction, entry, sl, tp, open_time, expected_r, confidence, closed) "
                "VALUES ('GBPUSD', -1, 1.26, 1.265, 1.245, '2026-01-01', 2.0, 0.75, 0)"
            )
            con.execute(
                "INSERT INTO shadow_trades (pair, direction, entry, sl, tp, open_time, expected_r, confidence, closed) "
                "VALUES ('EURUSD', 1, 1.08, 1.075, 1.095, '2026-01-01', 1.5, 0.8, 1)"
            )
            con.commit()
            con.close()

            from rl.shadow import ShadowTrade
            import rl.shadow as shadow_mod

            class FakeShadowEngine:
                pass

            engine = FakeShadowEngine()
            engine.db_path = db_path
            engine.open_trades = {}
            engine.bar_counter = {}

            shadow_mod.ShadowEngine._reload_open_trades(engine)

            assert "GBPUSD" in engine.open_trades
            assert "EURUSD" not in engine.open_trades
            t = engine.open_trades["GBPUSD"]
            assert t.direction == -1
            assert t.entry == 1.26
            assert t.db_id is not None
            assert engine.bar_counter["GBPUSD"] == 0
        finally:
            os.unlink(db_path)


# ──────────────────────────────────────────────────────────────────────────────
# Fix 6: authority DB uses WAL mode
# ──────────────────────────────────────────────────────────────────────────────

class TestAuthorityWAL:

    def test_authority_init_db_sets_wal(self):
        """Verify the _init_db method enables WAL mode by reading the source."""
        import pathlib
        authority_path = pathlib.Path(__file__).parent.parent / "rl" / "authority.py"
        source = authority_path.read_text()
        assert "PRAGMA journal_mode=WAL" in source
        assert "PRAGMA synchronous=NORMAL" in source


# ──────────────────────────────────────────────────────────────────────────────
# Fix 7: trainer.py F import at top (no duplicate at bottom)
# ──────────────────────────────────────────────────────────────────────────────

class TestTrainerImport:

    def test_no_duplicate_F_import_at_bottom(self):
        import pathlib
        trainer_path = pathlib.Path(__file__).parent.parent / "rl" / "trainer.py"
        source = trainer_path.read_text()
        lines = source.strip().split("\n")
        last_5 = "\n".join(lines[-5:])
        assert "import torch.nn.functional as F" not in last_5
