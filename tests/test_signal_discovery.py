"""
Tests for the Synthetic Signal Discovery engine (adaptive/signal_discovery.py,
L5c) and its TunerAgent adapter.

Covers per-trade condition extraction (LONG / SHORT / ABSENT), apriori-style
rule mining with the min-support floor, edge computation vs baseline, the
out-of-sample (train/test) qualification guard, the max-conditions cap, periodic
recompute + caching, disabled/empty handling, and the agent adapter.

Deterministic vote panels + temp SQLite DBs.
"""

import os
import tempfile

import pytest

from adaptive.counterfactual import CounterfactualEngine, TradeAttribution
from adaptive.signal_discovery import (
    ABSENT,
    SignalDiscoveryEngine,
    _trade_conditions,
)
from adaptive.tunable import TuneContext, TuneFrequency
from adaptive.tunable_adapters import SignalDiscoveryTunable
from adaptive.tuner_agent import TunerAgent


_THRESHOLDS = {
    "min_net_score": 1.0, "min_agreement": 0.5,
    "high_authority_modules": [], "high_authority_oppose_confidence": 0.6,
    "min_contributors": 1,
}
_NO_RESCUE = {"execute": False, "rescue_neutral_consensus": False}


def _v(m, c=0.9):
    return {"module": m, "direction": "LONG", "confidence": c, "weight": 1.0}


_WIN = [_v("structure"), _v("liquidity")]      # winners: structure + liquidity
_LOSE = [_v("momentum"), _v("volume")]          # losers: momentum + volume


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
    for _ in range(30):  # interleave so OOS train/test both see winners + losers
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
        enabled=True, db_path=os.path.join(d, "sd.db"),
        min_trades=1, min_support=5, max_conditions=3, min_edge_r=0.1,
    )
    params.update(kw)
    return SignalDiscoveryEngine(cf, **params)


# ── Condition extraction ─────────────────────────────────────────────────────

def test_trade_conditions_directional_and_absent():
    win = _attr(0, _WIN, 2.0)
    conds = _trade_conditions(win, {"structure", "liquidity", "momentum"})
    assert ("structure", "LONG") in conds
    assert ("liquidity", "LONG") in conds
    assert ("momentum", ABSENT) in conds  # not in the winner panel


# ── Mining + OOS qualification ───────────────────────────────────────────────

def test_mines_qualifying_winner_rule(seeded_engine):
    eng = _engine(seeded_engine)
    payload = eng.mine()
    assert payload["baseline_expectancy"] == pytest.approx(0.5)
    assert payload["rule_count"] > 0
    assert payload["qualifying_count"] > 0
    quals = [r for r in payload["rules"] if r["qualifies"]]
    # A rule isolating the winners (e.g. liquidity:LONG) has +1.5R edge OOS.
    labels = {r["label"] for r in quals}
    assert any("liquidity:LONG" in lbl for lbl in labels)
    for r in quals:
        assert r["edge"] > 0
        assert r["train_edge"] >= 0.1 and r["test_edge"] >= 0.1


def test_min_support_prunes_rare_rules(seeded_engine):
    eng = _engine(seeded_engine, min_support=1000)  # nothing clears support
    payload = eng.mine()
    assert payload["rule_count"] == 0


def test_max_conditions_capped(seeded_engine):
    eng = _engine(seeded_engine, max_conditions=2)
    payload = eng.mine()
    assert all(r["size"] <= 2 for r in payload["rules"])


def test_losing_pattern_does_not_qualify(seeded_engine):
    eng = _engine(seeded_engine)
    payload = eng.mine()
    # momentum:LONG fires only on losers → negative edge → must not qualify.
    for r in payload["rules"]:
        if r["label"] == "momentum:LONG":
            assert r["qualifies"] is False
            assert r["edge"] < 0


def test_empty_when_thin():
    d = tempfile.mkdtemp()
    cf = CounterfactualEngine(db_path=os.path.join(d, "cf.db"), enabled=True)
    eng = _engine(cf)
    payload = eng.mine()
    assert payload["rule_count"] == 0
    cf.close()


# ── Cadence + caching ────────────────────────────────────────────────────────

def test_maybe_recompute_and_cache(seeded_engine):
    eng = _engine(seeded_engine, interval=100, min_trades=20)
    assert eng.maybe_recompute(5) is None
    first = eng.maybe_recompute(60)
    assert first is not None
    assert eng.maybe_recompute(80) is None  # within interval
    assert eng.get_cached()["rule_count"] == first["rule_count"]


def test_get_state_shape(seeded_engine):
    eng = _engine(seeded_engine)
    eng.compute_and_cache()
    st = eng.get_state()
    assert st["enabled"] is True
    assert "rules" in st and "qualifying_count" in st


# ── Adapter ──────────────────────────────────────────────────────────────────

def test_adapter_metadata_and_run(seeded_engine):
    eng = _engine(seeded_engine)
    adapter = SignalDiscoveryTunable(eng, min_trades=1)
    assert adapter.tunable_name == "signal_discovery"
    assert adapter.frequency == TuneFrequency.ON_TRADE_BATCH
    res = adapter.tune(TuneContext(total_trades=60, force=True))
    assert res.success and not res.skipped


def test_adapter_skips_when_disabled(seeded_engine):
    eng = _engine(seeded_engine)
    eng.enabled = False
    adapter = SignalDiscoveryTunable(eng, min_trades=0)
    res = adapter.tune(TuneContext(total_trades=60, force=True))
    assert res.success and res.skipped
