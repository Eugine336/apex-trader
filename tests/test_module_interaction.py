"""
Tests for the Module Interaction Discovery engine (adaptive/module_interaction.py,
L5b) and its TunerAgent adapter.

Covers subset replay (retain/drop), single-module effects, the pairwise
interaction term + TOXIC / SYNERGISTIC / REDUNDANT classification, the greedy
optimal-subset search, periodic recompute cadence + caching, disabled/empty
handling, and the agent adapter metadata + skip-when-disabled.

Deterministic vote panels + temp SQLite DBs.
"""

import os
import tempfile

import pytest

from adaptive.counterfactual import CounterfactualEngine, TradeAttribution
from adaptive.module_interaction import (
    TOXIC,
    ModuleInteractionEngine,
    subset_retains_trade,
    subset_total_r,
)
from adaptive.tunable import TuneContext, TuneFrequency
from adaptive.tunable_adapters import ModuleInteractionTunable
from adaptive.tuner_agent import TunerAgent


_THRESHOLDS = {
    "min_net_score": 1.0, "min_agreement": 0.5,
    "high_authority_modules": [], "high_authority_oppose_confidence": 0.6,
    "min_contributors": 1,
}
_NO_RESCUE = {"execute": False, "rescue_neutral_consensus": False}


def _v(m, c=0.9):
    return {"module": m, "direction": "LONG", "confidence": c, "weight": 1.0}


# Winners need structure+liquidity; losers are opened by the momentum+volume pair.
_WIN = [_v("structure"), _v("liquidity")]
_LOSE = [_v("momentum"), _v("volume")]


def _attr(tid, votes, pnl):
    return TradeAttribution(
        trade_id=str(tid), pair="EURUSD", direction="LONG", timestamp_open=float(tid),
        votes=votes, thresholds=dict(_THRESHOLDS), ranker_kwargs=dict(_NO_RESCUE),
        pnl_r=pnl, won=pnl > 0, closed=True,
    )


@pytest.fixture
def seeded_engine():
    d = tempfile.mkdtemp()
    eng = CounterfactualEngine(
        db_path=os.path.join(d, "cf.db"), enabled=True, min_trades_for_attribution=1,
    )
    tid = 0
    for _ in range(15):
        for votes, pnl in ((_WIN, 2.0), (_LOSE, -1.0)):
            a = _attr(tid, votes, pnl)
            eng.record_open(a)
            eng.complete(str(tid), {"pnl_r": pnl, "won": pnl > 0})
            tid += 1
    yield eng
    eng.close()


def _engine(cf, **kw):
    d = tempfile.mkdtemp()
    params = dict(
        enabled=True, db_path=os.path.join(d, "mi.db"),
        min_trades=1, significance_r=1.0,
    )
    params.update(kw)
    return ModuleInteractionEngine(cf, **params)


# ── Subset replay ────────────────────────────────────────────────────────────

def test_subset_retains_and_drops():
    win = _attr(0, _WIN, 2.0)
    # With only liquidity active, the winner's net (0.9) fails min_net_score 1.0.
    assert subset_retains_trade(win, {"structure", "liquidity"}) is True
    assert subset_retains_trade(win, {"liquidity"}) is False


def test_subset_total_r(seeded_engine):
    trades = seeded_engine.get_closed_attributions(500)
    full = subset_total_r(trades, {"structure", "liquidity", "momentum", "volume"})
    # 15 winners (+2) + 15 losers (-1) = +15.
    assert full == pytest.approx(15.0)


# ── Interaction computation ──────────────────────────────────────────────────

def test_toxic_pair_detected(seeded_engine):
    eng = _engine(seeded_engine)
    payload = eng.compute()
    assert payload["baseline_total_r"] == pytest.approx(15.0)
    # momentum & volume each open losers → removing either helps (positive effect).
    eff = payload["module_effects"]
    assert eff["momentum"] > 0 and eff["volume"] > 0
    assert eff["structure"] < 0 and eff["liquidity"] < 0
    toxic = {
        tuple(sorted((p["module_a"], p["module_b"])))
        for p in payload["pairs"] if p["classification"] == TOXIC
    }
    assert ("momentum", "volume") in toxic


def test_greedy_optimal_subset_drops_loser_modules(seeded_engine):
    eng = _engine(seeded_engine)
    payload = eng.compute()
    sub = payload["optimal_subset"]
    # Removing a loser module raises retained R above the +15 baseline.
    assert sub["improvement_r"] > 0
    assert "structure" in sub["kept_modules"] and "liquidity" in sub["kept_modules"]


def test_empty_when_no_trades():
    d = tempfile.mkdtemp()
    cf = CounterfactualEngine(db_path=os.path.join(d, "cf.db"), enabled=True)
    eng = _engine(cf)
    payload = eng.compute()
    assert payload["trades_analyzed"] == 0
    assert payload["pairs"] == []
    cf.close()


# ── Cadence + caching ────────────────────────────────────────────────────────

def test_maybe_recompute_cadence_and_cache(seeded_engine):
    eng = _engine(seeded_engine, interval=50, min_trades=10)
    assert eng.maybe_recompute(5) is None            # below min_trades
    first = eng.maybe_recompute(30)
    assert first is not None                          # first qualifying run
    assert eng.maybe_recompute(40) is None            # within interval
    cached = eng.get_cached()
    assert cached["trades_analyzed"] == first["trades_analyzed"]


def test_get_state_shape(seeded_engine):
    eng = _engine(seeded_engine)
    eng.compute_and_cache()
    st = eng.get_state()
    assert st["enabled"] is True
    assert "pairs" in st and "optimal_subset" in st


# ── Adapter ──────────────────────────────────────────────────────────────────

def test_adapter_metadata_and_run(seeded_engine):
    eng = _engine(seeded_engine)
    adapter = ModuleInteractionTunable(eng, min_trades=1)
    assert adapter.tunable_name == "module_interaction"
    assert adapter.frequency == TuneFrequency.ON_TRADE_BATCH
    res = adapter.tune(TuneContext(total_trades=30, force=True))
    assert res.success and not res.skipped


def test_adapter_skips_when_disabled(seeded_engine):
    eng = _engine(seeded_engine)
    eng.enabled = False
    adapter = ModuleInteractionTunable(eng, min_trades=0)
    res = adapter.tune(TuneContext(total_trades=30, force=True))
    assert res.success and res.skipped


def test_adapter_registers_with_agent(seeded_engine):
    eng = _engine(seeded_engine)
    agent = TunerAgent(enabled=True, audit_db_path=os.path.join(tempfile.mkdtemp(), "a.db"))
    agent.register(ModuleInteractionTunable(eng, min_trades=1))
    assert "module_interaction" in agent.registered_names
    agent.close()
