"""
Tests for the Parameter Evolution engine (adaptive/param_evolution.py, L5a) and
its TunerAgent adapter (adaptive.tunable_adapters.ParameterEvolverTunable).

Covers candidate generation + bounds, the single-pass replay evaluator
(retain/drop semantics, walk-forward + robustness guards), the tournament →
shadow → promotion/rejection lifecycle, the promote-callback application path,
cooldown rate limiting, persistence/get_state, disabled no-op, and the agent
adapter metadata + skip-when-disabled.

Deterministic: builds vote panels directly so consensus outcomes are fixed, and
uses temp SQLite DBs so nothing touches the real data/ dir.
"""

import os
import tempfile

import pytest

from config import ParameterEvolutionConfig
from adaptive.counterfactual import CounterfactualEngine, TradeAttribution
from adaptive.param_evolution import (
    LOC_RANKER,
    LOC_THRESHOLD,
    EvolvableParam,
    ParameterCandidate,
    ParameterEvolver,
    candidate_retains_trade,
    evaluate_candidate,
    generate_candidates,
)
from adaptive.tunable import TuneContext, TuneFrequency
from adaptive.tunable_adapters import ParameterEvolverTunable
from adaptive.tuner_agent import TunerAgent


# ── Fixtures / helpers ─────────────────────────────────────────────────────

# All seeded trades pass these thresholds at entry, so the baseline replay
# retains them — only a *tightening* candidate can drop the marginal losers.
_THRESHOLDS = {
    "min_net_score": 1.0,
    "min_agreement": 0.5,
    "high_authority_modules": [],
    "high_authority_oppose_confidence": 0.6,
    "min_contributors": 1,
}
_NO_RESCUE = {"execute": False, "rescue_neutral_consensus": False}


def _v(module, direction="LONG", confidence=0.9, weight=1.0):
    return {"module": module, "direction": direction, "confidence": confidence, "weight": weight}


# Strong winner: two aligned modules (net 1.8) → survives even a tightened gate.
_WIN_VOTES = [_v("structure"), _v("liquidity")]
# Marginal loser: a single weak vote (net 1.0) → dropped once min_net_score>1.0.
_LOSE_VOTES = [_v("vwap", confidence=1.0)]


def _attr(tid, votes, pnl):
    return TradeAttribution(
        trade_id=str(tid), pair="EURUSD", direction="LONG",
        timestamp_open=float(tid), votes=votes,
        thresholds=dict(_THRESHOLDS), ranker_kwargs=dict(_NO_RESCUE),
        pnl_r=pnl, won=pnl > 0, closed=True,
    )


def _seed(engine, n_pairs=20):
    """Interleave winners (+2R) and marginal losers (-1R) across time so the
    walk-forward split has both classes in train and test."""
    tid = 0
    for _ in range(n_pairs):
        a = _attr(tid, _WIN_VOTES, 2.0)
        engine.record_open(a)
        engine.complete(str(tid), {"pnl_r": 2.0, "won": True})
        tid += 1
        b = _attr(tid, _LOSE_VOTES, -1.0)
        engine.record_open(b)
        engine.complete(str(tid), {"pnl_r": -1.0, "won": False})
        tid += 1


@pytest.fixture
def cf_engine():
    d = tempfile.mkdtemp()
    eng = CounterfactualEngine(
        db_path=os.path.join(d, "cf.db"), enabled=True, min_trades_for_attribution=1,
    )
    yield eng
    eng.close()


def _make_evolver(cf_engine, **overrides):
    d = tempfile.mkdtemp()
    kwargs = dict(
        enabled=True, db_path=os.path.join(d, "pe.db"),
        current_values_provider=lambda: {"min_net_score": 1.0},
        candidates_per_param=8, min_replay_trades=5,
        shadow_validation_trades=10, significance_threshold=0.05,
        evolution_cooldown_hours=0.0,
    )
    kwargs.update(overrides)
    return ParameterEvolver(cf_engine, **kwargs)


# ── EvolvableParam / candidate generation ───────────────────────────────────

def test_clamp_respects_bounds_and_int():
    p = EvolvableParam("min_contributors", LOC_THRESHOLD, 1, 5, is_int=True)
    assert p.clamp(0) == 1
    assert p.clamp(9) == 5
    assert p.clamp(2.7) == 3  # rounded for int params
    f = EvolvableParam("min_net_score", LOC_THRESHOLD, 0.5, 4.0)
    assert f.clamp(10.0) == 4.0
    assert f.clamp(0.1) == 0.5


def test_generate_candidates_excludes_current_and_clamps():
    p = EvolvableParam("min_net_score", LOC_THRESHOLD, 0.5, 4.0)
    cands = generate_candidates(p, 1.0, n_candidates=6)
    assert cands, "should produce candidates"
    for c in cands:
        assert 0.5 <= c.proposed_value <= 4.0
        assert c.proposed_value != 1.0  # current value excluded
        assert c.param_name == "min_net_score"


# ── Replay evaluation semantics ──────────────────────────────────────────────

def test_candidate_retains_winner_drops_marginal():
    win = _attr(0, _WIN_VOTES, 2.0)
    lose = _attr(1, _LOSE_VOTES, -1.0)
    # Tighten min_net_score to 1.5: winner (net 1.8) retained, loser (1.0) dropped.
    cand = ParameterCandidate("min_net_score", LOC_THRESHOLD, 1.0, 1.5)
    assert candidate_retains_trade(win, cand) is True
    assert candidate_retains_trade(lose, cand) is False


def test_evaluate_candidate_improvement_and_guards(cf_engine):
    _seed(cf_engine, n_pairs=20)
    trades = cf_engine.get_closed_attributions(500)
    cand = ParameterCandidate("min_net_score", LOC_THRESHOLD, 1.0, 1.5)
    ev = evaluate_candidate(cand, trades)
    # Dropping the 20 marginal -1R losers raises total R by +20.
    assert ev.dropped_trades == 20
    assert ev.retained_trades == 20
    assert ev.improvement_r == pytest.approx(20.0)
    assert ev.walk_forward_ok is True
    assert ev.robust is True


def test_evaluate_candidate_no_trades_is_empty():
    cand = ParameterCandidate("min_net_score", LOC_THRESHOLD, 1.0, 1.5)
    ev = evaluate_candidate(cand, [])
    assert ev.trades_analyzed == 0
    assert ev.improvement_r == 0.0
    assert ev.walk_forward_ok is False


# ── Tournament → shadow → promotion lifecycle ────────────────────────────────

def test_run_tournament_seeds_shadows(cf_engine):
    _seed(cf_engine, n_pairs=20)
    ev = _make_evolver(cf_engine)
    evals = ev.run_tournament()
    assert evals, "tournament produced evaluations"
    qualifying = [e for e in evals if e.walk_forward_ok and e.robust]
    assert qualifying, "at least one tightening candidate should qualify"
    assert ev.get_active_shadows(), "qualifying candidates seeded to shadow"


def test_shadow_promotes_winning_candidate_via_callback(cf_engine):
    _seed(cf_engine, n_pairs=20)
    applied = []
    ev = _make_evolver(
        cf_engine, promote_callback=lambda n, loc, v: applied.append((n, loc, v)) or True,
        shadow_validation_trades=10,
    )
    ev.run_tournament()
    decisions = ev.evaluate_shadows()
    promoted = [d for d in decisions if d["decision"] == "promoted"]
    assert promoted, "a clearly-improving candidate should be promoted"
    assert applied, "promote_callback applied the value"
    # History persisted.
    hist = ev.get_promotion_history()
    assert any(h["decision"] == "promoted" for h in hist)


def test_shadow_rejects_losing_candidate(cf_engine):
    _seed(cf_engine, n_pairs=20)
    ev = _make_evolver(cf_engine, shadow_validation_trades=10)
    # Inject a deliberately bad shadow: a LOOSER value that drops winners. Force
    # it by directly evaluating a candidate that retains losers but drops a
    # winner is hard with this data, so instead assert the resolve path rejects
    # a candidate whose running improvement is negative.
    from adaptive.param_evolution import ShadowCandidate, SHADOW
    bad = ShadowCandidate(
        param_name="min_net_score", location=LOC_THRESHOLD,
        current_value=1.0, proposed_value=2.0, generation_method="grid",
        state=SHADOW, shadow_trades=10, candidate_r=-5.0, actual_r=0.0,
    )
    ev._insert_shadow(bad)
    # Re-read it (row_id assigned) and resolve directly.
    shadow = ev.get_active_shadows()[0]
    shadow.shadow_trades = 10
    shadow.candidate_r = -5.0
    shadow.actual_r = 0.0
    ev._save_shadow(shadow)
    decision = ev._resolve_shadow(shadow)
    assert decision is not None and decision["decision"] == "rejected"


def test_cooldown_blocks_second_promotion(cf_engine):
    _seed(cf_engine, n_pairs=20)
    applied = []
    ev = _make_evolver(
        cf_engine, promote_callback=lambda *a: applied.append(a) or True,
        shadow_validation_trades=10, evolution_cooldown_hours=48.0,
    )
    ev.run_tournament()
    ev.evaluate_shadows()
    # With a 48h cooldown, at most one promotion is applied in a single pass.
    assert len(applied) <= 1


def test_disabled_engine_is_noop(cf_engine):
    _seed(cf_engine, n_pairs=20)
    ev = _make_evolver(cf_engine, enabled=False)
    assert ev.run_tournament() == []
    assert ev.run_cycle() == {"enabled": False}


def test_get_state_shape(cf_engine):
    ev = _make_evolver(cf_engine)
    st = ev.get_state()
    assert st["enabled"] is True
    assert "min_net_score" in st["evolvable_params"]
    assert st["active_shadow_count"] == 0
    assert st["recent_promotions"] == []


# ── TunerAgent adapter ───────────────────────────────────────────────────────

def test_adapter_metadata(cf_engine):
    ev = _make_evolver(cf_engine)
    adapter = ParameterEvolverTunable(ev, min_trades=5)
    assert adapter.tunable_name == "parameter_evolver"
    assert adapter.frequency == TuneFrequency.PERIODIC
    assert adapter.dependencies == ["counterfactual"]


def test_adapter_skips_when_disabled(cf_engine):
    ev = _make_evolver(cf_engine, enabled=False)
    adapter = ParameterEvolverTunable(ev, min_trades=0)
    res = adapter.tune(TuneContext(total_trades=100, force=True))
    assert res.success and res.skipped


def test_adapter_runs_cycle_under_agent(cf_engine):
    _seed(cf_engine, n_pairs=20)
    applied = []
    ev = _make_evolver(
        cf_engine, promote_callback=lambda *a: applied.append(a) or True,
        shadow_validation_trades=10,
    )
    agent = TunerAgent(enabled=True, audit_db_path=os.path.join(tempfile.mkdtemp(), "a.db"))
    agent.register(ParameterEvolverTunable(ev, min_trades=5))
    results = agent.on_periodic_tick(TuneContext(total_trades=40, force=True))
    names = {r.tunable_name for r in results}
    assert "parameter_evolver" in names
    agent.close()
