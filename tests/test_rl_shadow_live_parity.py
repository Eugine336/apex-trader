"""
Tests for RL shadow trading live-engine parity.

The RL agent decides WHEN to trade (its neural net), but WHERE the stop/target
go and HOW risk is measured must mirror the live engine:
  - structure-aware stop from recent swing extremes (ATR fallback otherwise)
  - InstrumentProfile-driven minimum-risk floor
  - live config reward:risk for the target
  - session gating for session-gated (FX) instruments

Also covers the authority cold-start fix (an empty shadow book must not surface
a phantom 100% drawdown failure).
"""
from __future__ import annotations

import os

import pytest

torch = pytest.importorskip("torch")

from rl.contracts import (
    N_CONTEXT_FEATURES,
    OBS_CONTRACT_VERSION,
    OBS_FEATURES,
    build_symbol_vocab,
    schema_hash,
)
from rl.network import ApexRLAgent
from rl.shadow import RLSignal, ShadowEngine
from rl.authority import AuthorityManager, STAGE_MAP


def _make_checkpoint(path: str, **meta_overrides) -> str:
    vocab = build_symbol_vocab()
    agent = ApexRLAgent(
        n_features=OBS_FEATURES, n_actions=4,
        context_dim=N_CONTEXT_FEATURES, n_symbols=len(vocab),
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


def _engine(tmp_path) -> ShadowEngine:
    ckpt = str(tmp_path / "ckpt.pt")
    _make_checkpoint(ckpt)
    eng = ShadowEngine(ckpt, db_path=str(tmp_path / "shadow.db"))
    eng.session_filter_enabled = False  # deterministic by default
    return eng


def _signal(pair: str, action: int = 1) -> RLSignal:
    return RLSignal(
        pair=pair, timestamp="2026-01-01T00:00:00Z",
        action=action, action_label="BUY" if action == 1 else "SELL",
        confidence=0.8, expected_r=1.5, latent_repr=[], authority="SHADOW",
    )


# ── Structure-aware SL ─────────────────────────────────────────────────────────


class TestStructureStop:
    def test_long_stop_anchors_below_recent_swing_low(self, tmp_path):
        eng = _engine(tmp_path)
        pair = "EURUSD"
        # Feed a recent window with a clear swing low at 1.0950.
        for h, lo in [(1.1010, 1.0980), (1.1005, 1.0950), (1.1020, 1.0990),
                     (1.1015, 1.0985), (1.1025, 1.0995), (1.1030, 1.1000),
                     (1.1028, 1.0998), (1.1035, 1.1002)]:
            eng.update_price(pair, h, lo, (h + lo) / 2, 0.0010, None)

        eng.open_shadow_trade(pair, _signal(pair, 1), close=1.1000, atr=0.0010, pip_size=0.0001)
        t = eng.open_trades[pair]
        assert t.direction == 1
        # Stop sits below the swing low (1.0950) minus the FX buffer (2 pips).
        assert t.sl < 1.0950
        assert t.sl == pytest.approx(1.0950 - 2.0 * 0.0001, abs=1e-7)
        # TP is risk * config RR (default 3.0) beyond entry.
        risk = t.entry - t.sl
        assert t.tp == pytest.approx(t.entry + risk * eng._tp_rr, abs=1e-7)

    def test_short_stop_anchors_above_recent_swing_high(self, tmp_path):
        eng = _engine(tmp_path)
        pair = "EURUSD"
        for h, lo in [(1.1050, 1.1000), (1.1080, 1.1010), (1.1040, 1.1005),
                     (1.1045, 1.1008), (1.1035, 1.1002), (1.1030, 1.1000),
                     (1.1032, 1.1001), (1.1028, 1.0999)]:
            eng.update_price(pair, h, lo, (h + lo) / 2, 0.0010, None)

        eng.open_shadow_trade(pair, _signal(pair, 2), close=1.1010, atr=0.0010, pip_size=0.0001)
        t = eng.open_trades[pair]
        assert t.direction == -1
        assert t.sl > 1.1080  # above swing high
        assert t.sl == pytest.approx(1.1080 + 2.0 * 0.0001, abs=1e-7)

    def test_atr_fallback_when_no_structure(self, tmp_path):
        eng = _engine(tmp_path)
        pair = "EURUSD"
        # No update_price calls → empty buffer → ATR fallback.
        eng.open_shadow_trade(pair, _signal(pair, 1), close=1.1000, atr=0.0020, pip_size=0.0001)
        t = eng.open_trades[pair]
        # ATR stop uses the config SL ATR multiple.
        assert t.sl == pytest.approx(t.entry - eng._sl_atr_mult * 0.0020, abs=1e-7)

    def test_min_risk_floor_widens_tight_structure_stop(self, tmp_path):
        eng = _engine(tmp_path)
        pair = "EURUSD"
        # Swing low only ~0.5 pip below entry — tighter than the 5-pip FX floor.
        for _ in range(eng.MIN_STRUCT_BARS):
            eng.update_price(pair, 1.10006, 1.09995, 1.10000, 0.0010, None)
        eng.open_shadow_trade(pair, _signal(pair, 1), close=1.1000, atr=0.0010, pip_size=0.0001)
        t = eng.open_trades[pair]
        # Floored to min_risk_pips (5) * pip_size.
        assert (t.entry - t.sl) == pytest.approx(5.0 * 0.0001, abs=1e-7)


# ── InstrumentProfile-driven floor ─────────────────────────────────────────────


class TestMinRiskDistance:
    def test_forex_floor_uses_min_risk_pips(self):
        from brain.instrument_profile import get_profile
        prof = get_profile("EURUSD")
        d = ShadowEngine._min_risk_distance(1.1000, 0.0001, prof, "forex")
        assert d == pytest.approx(prof.min_risk_pips * 0.0001)

    def test_synthetic_floor_is_percentage(self):
        d = ShadowEngine._min_risk_distance(1000.0, 0.01, None, "synthetic")
        assert d == pytest.approx(1000.0 * 0.003)

    def test_crypto_floor_is_percentage(self):
        d = ShadowEngine._min_risk_distance(50000.0, 1.0, None, "crypto")
        assert d == pytest.approx(50000.0 * 0.0015)


# ── Session gating ─────────────────────────────────────────────────────────────


class TestSessionGate:
    def test_fx_is_session_gated(self):
        assert ShadowEngine._is_session_gated("EURUSD") is True

    def test_synthetic_not_session_gated(self):
        assert ShadowEngine._is_session_gated("V10_1S") is False

    def test_unknown_not_session_gated(self):
        assert ShadowEngine._is_session_gated("PAIR0") is False

    def test_untradeable_session_skips_fx_entry(self, tmp_path):
        eng = _engine(tmp_path)
        eng.session_filter_enabled = True

        class _DeadSession:
            def get_status(self, now=None):
                from types import SimpleNamespace
                return SimpleNamespace(is_tradeable=False)

        eng._session_engine = _DeadSession()
        eng.open_shadow_trade("EURUSD", _signal("EURUSD", 1), 1.1000, 0.0020, 0.0001)
        assert "EURUSD" not in eng.open_trades

    def test_tradeable_session_allows_fx_entry(self, tmp_path):
        eng = _engine(tmp_path)
        eng.session_filter_enabled = True

        class _LiveSession:
            def get_status(self, now=None):
                from types import SimpleNamespace
                return SimpleNamespace(is_tradeable=True)

        eng._session_engine = _LiveSession()
        eng.open_shadow_trade("EURUSD", _signal("EURUSD", 1), 1.1000, 0.0020, 0.0001)
        assert "EURUSD" in eng.open_trades


# ── Authority cold-start ───────────────────────────────────────────────────────


class TestAuthorityColdStart:
    def test_empty_book_no_phantom_drawdown_failure(self, tmp_path):
        auth = AuthorityManager(db_path=str(tmp_path / "auth.db"))
        auth._set_stage(2)
        # Cold start: zero shadow trades, missing/zero metrics.
        result = auth.evaluate({
            "n_shadow_trades": 0, "win_rate": 0.0, "expectancy": 0.0,
            "max_drawdown": 0.0, "n_live_trades": 0, "checkpoint_loaded": True,
        })
        assert result["action"] == "HOLD"
        assert "drawdown" not in result["reason"]
        # The honest blocker — the trade count — is what is reported.
        assert "shadow_trades" in result["reason"]

    def test_drawdown_gate_active_once_book_is_warm(self, tmp_path):
        auth = AuthorityManager(db_path=str(tmp_path / "auth.db"))
        auth._set_stage(2)
        req = auth._check_requirements(
            {"n_shadow_trades": 250, "win_rate": 0.6, "expectancy": 0.4,
             "max_drawdown": 0.9, "n_live_trades": 0},
            STAGE_MAP[3],
        )
        passed, reason = req
        assert passed is False
        assert "drawdown" in reason
