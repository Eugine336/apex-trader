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


# ── Overfitting protection (L5c hardening) ───────────────────────────────────

def test_welch_p_separation_and_insufficiency():
    from adaptive.signal_discovery import _welch_p
    # Perfectly separated constant groups → significant (p == 0.0).
    assert _welch_p([2.0, 2.0, 2.0], [-1.0, -1.0, -1.0]) == 0.0
    # Identical groups → no evidence of difference (p == 1.0).
    assert _welch_p([1.0, 1.0, 1.0], [1.0, 1.0, 1.0]) == 1.0
    # Too few samples to assess → no evidence (p == 1.0).
    assert _welch_p([1.0], [0.0]) == 1.0
    # A genuine but noisy separation lands strictly inside (0, 1).
    p = _welch_p([1.0, 0.0, 1.0, 0.0, 0.5], [0.0] * 10)
    assert 0.0 < p < 1.0


def test_qualifying_rules_carry_significance_and_retention(seeded_engine):
    eng = _engine(seeded_engine)
    payload = eng.mine()
    assert payload["candidates_tested"] >= 1
    quals = [r for r in payload["rules"] if r["qualifies"]]
    assert quals  # clean +1.5R winner rules survive every gate
    adj_alpha = payload["bonferroni_alpha"] / payload["candidates_tested"]
    for r in quals:
        # Bonferroni-significant AND retains its edge out-of-sample.
        assert r["p_value"] <= adj_alpha
        assert r["wf_ratio"] >= payload["walk_forward_ratio_threshold"]


def test_walk_forward_ratio_hurdle_rejects_in_sample_fluke():
    """A rule with a strong train edge but collapsed test edge fails the OOS
    retention hurdle even though both edges clear min_edge_r."""
    from adaptive.signal_discovery import SignalDiscoveryEngine

    d = tempfile.mkdtemp()
    cf = CounterfactualEngine(db_path=os.path.join(d, "cf.db"), enabled=True)
    eng = SignalDiscoveryEngine(
        cf, enabled=True, db_path=os.path.join(d, "sd.db"),
        min_support=5, min_edge_r=0.1, walk_forward_ratio_threshold=0.6,
    )
    key = ("alpha", "LONG")
    fire = {key}
    rest = {("beta", "LONG")}
    # 8 fire trades + 12 rest. Train half retains 1.5R edge; test half edge is
    # only 0.15R → wf_ratio = 0.1 < 0.6, so the rule must NOT qualify.
    train_fire = [(set(fire), 2.0)] * 5
    test_fire = [(set(fire), 0.65)] * 3
    rest_rows = [(set(rest), 0.5)] * 12
    rl = train_fire + test_fire + rest_rows
    train = train_fire + rest_rows[:8]
    test = test_fire + rest_rows[8:]
    rule = eng._evaluate_rule(
        [key], rl, 0.5,
        train, test,
        0.5, 0.5,
        n_candidates=1,
    )
    assert rule is not None
    assert rule.train_edge >= 0.1 and rule.test_edge >= 0.1
    assert rule.wf_ratio < 0.6
    assert rule.qualifies is False
    cf.close()
    eng.close()


def test_bonferroni_correction_flips_with_candidate_count():
    """The same moderately-significant rule qualifies when few candidates are
    tested but is rejected once the Bonferroni-adjusted level tightens."""
    from adaptive.signal_discovery import SignalDiscoveryEngine, _welch_p

    d = tempfile.mkdtemp()
    cf = CounterfactualEngine(db_path=os.path.join(d, "cf.db"), enabled=True)
    eng = SignalDiscoveryEngine(
        cf, enabled=True, db_path=os.path.join(d, "sd.db"),
        min_support=5, min_edge_r=0.1, walk_forward_ratio_threshold=0.6,
        bonferroni_alpha=0.05,
    )
    key = ("alpha", "LONG")
    fire = {key}
    rest = {("beta", "LONG")}
    fire_vals = [1.0, 0.0, 1.0, 0.0, 0.5, 1.0, 0.0, 0.5]   # noisy +0.5 mean
    rest_vals = [0.0] * 12
    p = _welch_p(fire_vals, rest_vals)
    assert 0.0 < p < 0.05  # in the regime where Bonferroni can flip the verdict

    train_fire = [(set(fire), v) for v in fire_vals[:5]]
    test_fire = [(set(fire), v) for v in fire_vals[5:]]
    rest_rows = [(set(rest), v) for v in rest_vals]
    rl = train_fire + test_fire + rest_rows
    train = train_fire + rest_rows[:8]
    test = test_fire + rest_rows[8:]

    lenient = eng._evaluate_rule([key], rl, 0.0, train, test, 0.0, 0.0, n_candidates=1)
    import math
    strict_n = math.ceil(0.05 / p) + 1   # drives adj_alpha below the rule's p
    strict = eng._evaluate_rule([key], rl, 0.0, train, test, 0.0, 0.0, n_candidates=strict_n)
    assert lenient.qualifies is True      # passes at alpha = 0.05
    assert strict.qualifies is False      # rejected after Bonferroni correction
    cf.close()
    eng.close()


def test_max_active_cap_limits_active_rules(seeded_engine):
    eng = _engine(seeded_engine, max_active_signals=2)
    payload = eng.compute_and_cache()
    active = [r for r in payload["rules"] if r["active"]]
    assert payload["qualifying_count"] >= 2  # the seeded data yields many winners
    assert payload["active_count"] == 2
    assert len(active) == 2
    # Every active rule must itself be a qualifying (validated) rule.
    assert all(r["qualifies"] for r in active)


def test_score_decays_and_grows_on_reconfirmation(seeded_engine):
    eng = _engine(seeded_engine, score_decay_rate=0.05, max_active_signals=5)
    p1 = eng.compute_and_cache()
    q1 = next(r for r in p1["rules"] if r["qualifies"])
    assert q1["score"] == pytest.approx(1.0)
    assert q1["confirmations"] == 1
    p2 = eng.compute_and_cache()
    q2 = next(r for r in p2["rules"] if r["label"] == q1["label"])
    # Re-confirmation: prior score decays by 5% then gains +1.0 → 1.95.
    assert q2["score"] == pytest.approx(1.95)
    assert q2["confirmations"] == 2


# ── Config validation for the new overfitting knobs ──────────────────────────

def test_signal_discovery_config_validates_new_fields():
    from config import SignalDiscoveryConfig

    SignalDiscoveryConfig()  # defaults must be valid
    with pytest.raises(ValueError):
        SignalDiscoveryConfig(bonferroni_alpha=0.0)
    with pytest.raises(ValueError):
        SignalDiscoveryConfig(bonferroni_alpha=1.5)
    with pytest.raises(ValueError):
        SignalDiscoveryConfig(walk_forward_ratio_threshold=1.5)
    with pytest.raises(ValueError):
        SignalDiscoveryConfig(score_decay_rate=-0.1)
    with pytest.raises(ValueError):
        SignalDiscoveryConfig(max_active_signals=-1)

