"""
Tests for the Counterfactual Attribution engine (adaptive/counterfactual.py)
and its TunerAgent adapter (adaptive/tunable_adapters.CounterfactualTunable).

Covers the leave-one-out replay math (faithful to ``decide`` + the ranker
NEUTRAL rescue), the four trade classifications (DECISIVE / SUPPORTING /
OPPOSING / ABSENT), per-module aggregation (marginal R, expectancy, Sharpe,
drawdown, better-off-without), the SQLite record/complete round-trip + cache,
the periodic recompute cadence + min-trades gating, config validation, and the
agent integration (registration metadata, agent-driven tune, skip when disabled).

Dependency-light: builds vote panels directly so the consensus outcomes are
deterministic; uses temp SQLite DBs so nothing touches the real data/ dir.
"""

import os
import tempfile

import pytest

from config import CounterfactualConfig
from adaptive.counterfactual import (
    ABSENT,
    DECISIVE,
    OPPOSING,
    SUPPORTING,
    CounterfactualEngine,
    ModuleAttribution,
    TradeAttribution,
    _max_drawdown,
    _std,
    _votes_from_dicts,
    replay_consensus,
    replay_without,
)
from adaptive.tunable import TuneContext, TuneFrequency
from adaptive.tunable_adapters import CounterfactualTunable
from adaptive.tuner_agent import TunerAgent
from brain.directional_consensus import Vote, decide


# ── Fixtures / helpers ─────────────────────────────────────────────────────

_THRESHOLDS = {
    "min_net_score": 1.5,
    "min_agreement": 0.55,
    "high_authority_modules": [],
    "high_authority_oppose_confidence": 0.6,
    "min_contributors": 2,
}
_NO_RESCUE = {"execute": False, "rescue_neutral_consensus": False}


def _v(module, direction, confidence, weight):
    return {"module": module, "direction": direction, "confidence": confidence, "weight": weight}


def _attr(trade_id, direction, votes, *, pnl_r=None, ts=0.0, thresholds=None, ranker=None):
    return TradeAttribution(
        trade_id=trade_id,
        pair="EURUSD",
        direction=direction,
        timestamp_open=ts or 1.0,
        votes=votes,
        thresholds=thresholds or dict(_THRESHOLDS),
        ranker_kwargs=ranker if ranker is not None else dict(_NO_RESCUE),
        pnl_r=pnl_r,
        won=None if pnl_r is None else pnl_r > 0,
        closed=pnl_r is not None,
    )


@pytest.fixture
def engine():
    d = tempfile.mkdtemp()
    eng = CounterfactualEngine(
        db_path=os.path.join(d, "cf.db"),
        enabled=True,
        attribution_lookback=500,
        attribution_interval=10,
        min_trades_for_attribution=3,
    )
    yield eng
    eng.close()


# ── Replay faithfulness ─────────────────────────────────────────────────────

def test_replay_consensus_matches_decide_long():
    votes = [
        Vote("structure", "LONG", 1.0, 1.0),
        Vote("momentum", "LONG", 1.0, 1.0),
        Vote("volume", "SHORT", 0.5, 1.0),
    ]
    direction, net = replay_consensus(votes, _THRESHOLDS, _NO_RESCUE)
    expected = decide(
        votes,
        min_net_score=1.5, min_agreement=0.55,
        high_authority_modules=[], high_authority_oppose_confidence=0.6,
        min_contributors=2, log_suppressed_minorities=False,
    )
    assert direction == expected.direction == "LONG"
    assert net == pytest.approx(expected.net_score)


def test_replay_consensus_neutral_without_rescue():
    # Fast SHORT cluster vs a heavier slow LONG vote: |net| below min_net_score.
    votes = [
        Vote("momentum", "SHORT", 0.9, 1.0),
        Vote("vwap", "SHORT", 0.8, 1.0),
        Vote("structure", "LONG", 0.85, 3.0),
    ]
    direction, _ = replay_consensus(votes, _THRESHOLDS, _NO_RESCUE)
    assert direction == "NEUTRAL"


def test_replay_consensus_rescues_neutral_when_executor_live():
    votes = [
        Vote("momentum", "SHORT", 0.9, 1.0),
        Vote("vwap", "SHORT", 0.8, 1.0),
        Vote("structure", "LONG", 0.85, 3.0),
    ]
    ranker = {
        "execute": True,
        "rescue_neutral_consensus": True,
        "scalp_modules": ["momentum", "volume", "vwap", "liquidity"],
        "swing_modules": ["structure", "currency_strength", "wyckoff", "order_block", "fvg"],
        "scalp_reward_risk": 1.5,
        "swing_reward_risk": 2.5,
        "base_win_rate": 0.40,
        "confidence_win_rate_gain": 0.40,
        "min_expected_value": 0.0,
        "min_cluster_confidence": 0.0,
        "min_cluster_contributors": 1,
    }
    direction, _ = replay_consensus(votes, _THRESHOLDS, ranker)
    assert direction in ("LONG", "SHORT")  # rescued to a ranked opportunity


# ── Classification ──────────────────────────────────────────────────────────

def test_classify_decisive_and_opposing():
    # net = 1.0 + 1.0 - 0.5 = 1.5 (exactly clears) -> LONG.
    votes = [_v("structure", "LONG", 1.0, 1.0), _v("momentum", "LONG", 1.0, 1.0),
             _v("volume", "SHORT", 0.5, 1.0)]
    attr = _attr("t1", "LONG", votes)

    # Removing either LONG module drops net to 0.5 (< 1.5) and contributors to 1
    # (< 2) -> NEUTRAL: the module was decisive.
    assert replay_without(attr, "structure").classification == DECISIVE
    assert replay_without(attr, "momentum").classification == DECISIVE
    # volume voted SHORT against the LONG trade -> opposing.
    assert replay_without(attr, "volume").classification == OPPOSING


def test_classify_supporting_and_absent():
    votes = [_v("structure", "LONG", 1.0, 3.0), _v("currency_strength", "LONG", 1.0, 2.0),
             _v("momentum", "LONG", 1.0, 1.0), _v("volume", "NEUTRAL", 0.0, 1.0)]
    attr = _attr("t2", "LONG", votes)

    # Strong panel: dropping any single LONG module still leaves a LONG trade.
    assert replay_without(attr, "momentum").classification == SUPPORTING
    assert replay_without(attr, "structure").classification == SUPPORTING
    # NEUTRAL vote never participated.
    assert replay_without(attr, "volume").classification == ABSENT
    # A module that did not even vote on this trade is ABSENT.
    assert replay_without(attr, "not_present").classification == ABSENT


# ── Per-module aggregation ──────────────────────────────────────────────────

def test_module_attribution_metrics(engine):
    # Three trades where `structure` is decisive on each (paired with momentum,
    # both needed to clear the gate). R = [2.0, -1.0, 1.0].
    for i, r in enumerate([2.0, -1.0, 1.0]):
        votes = [_v("structure", "LONG", 1.0, 1.0), _v("momentum", "LONG", 1.0, 1.0),
                 _v("volume", "SHORT", 0.5, 1.0)]
        engine.record_open(_attr(f"d{i}", "LONG", votes, ts=float(i + 1)))
        engine.complete(f"d{i}", {"pnl_r": r, "won": r > 0})

    agg = engine.compute_module_attribution("structure")
    assert agg.decisive_trades == 3
    assert agg.trades_involved == 3
    assert agg.marginal_r == pytest.approx(2.0)            # 2 - 1 + 1
    assert agg.expectancy_when_decisive == pytest.approx(2.0 / 3.0)
    assert agg.sharpe_contribution != 0.0
    assert agg.drawdown_contribution >= 0.0
    assert agg.better_off_without is False                  # marginal R positive

    # volume opposed every winning trade.
    vol = engine.compute_module_attribution("volume")
    assert vol.opposing_trades == 3
    assert vol.decisive_trades == 0
    assert vol.marginal_r == pytest.approx(0.0)


def test_module_better_off_without_when_marginal_negative(engine):
    # A decisive module whose unique trades net negative -> system better off.
    for i, r in enumerate([-2.0, -1.0, 0.5]):
        votes = [_v("structure", "LONG", 1.0, 1.0), _v("momentum", "LONG", 1.0, 1.0)]
        engine.record_open(_attr(f"n{i}", "LONG", votes, ts=float(i + 1)))
        engine.complete(f"n{i}", {"pnl_r": r, "won": r > 0})

    agg = engine.compute_module_attribution("structure")
    assert agg.decisive_trades == 3
    assert agg.marginal_r == pytest.approx(-2.5)
    assert agg.better_off_without is True
    assert agg.r_difference == pytest.approx(2.5)           # removing it gains +2.5R


def test_compute_all_attributions_ranks_best_first(engine):
    for i, r in enumerate([1.5, 1.0]):
        votes = [_v("structure", "LONG", 1.0, 1.0), _v("momentum", "LONG", 1.0, 1.0)]
        engine.record_open(_attr(f"a{i}", "LONG", votes, ts=float(i + 1)))
        engine.complete(f"a{i}", {"pnl_r": r, "won": True})

    all_attr = engine.compute_all_attributions()
    assert set(all_attr) == {"structure", "momentum"}
    assert all(isinstance(v, ModuleAttribution) for v in all_attr.values())


# ── Persistence + cache ─────────────────────────────────────────────────────

def test_record_open_and_complete_roundtrip(engine):
    votes = [_v("structure", "LONG", 1.0, 1.0), _v("momentum", "LONG", 1.0, 1.0)]
    assert engine.record_open(_attr("rt1", "LONG", votes)) is True

    fetched = engine.get_attribution("rt1")
    assert fetched is not None
    assert fetched.closed is False
    assert fetched.pnl_r is None
    assert len(fetched.votes) == 2

    assert engine.complete("rt1", {"pnl_r": 1.8, "won": True, "outcome": "WIN"}) is True
    done = engine.get_attribution("rt1")
    assert done.closed is True
    assert done.pnl_r == pytest.approx(1.8)
    # Only closed+graded trades come back from get_closed_attributions.
    assert [a.trade_id for a in engine.get_closed_attributions()] == ["rt1"]


def test_compute_and_cache_then_read(engine):
    for i in range(3):
        votes = [_v("structure", "LONG", 1.0, 1.0), _v("momentum", "LONG", 1.0, 1.0)]
        engine.record_open(_attr(f"c{i}", "LONG", votes, ts=float(i + 1)))
        engine.complete(f"c{i}", {"pnl_r": 1.0, "won": True})

    payload = engine.compute_and_cache()
    assert payload["trades_analyzed"] == 3
    assert payload["module_count"] >= 1

    cached = engine.get_cached_attributions()
    assert cached["trades_analyzed"] == 3
    assert cached["computed_at"] is not None
    # Ranked best-first by marginal R (descending).
    rs = [m["marginal_r"] for m in cached["modules"]]
    assert rs == sorted(rs, reverse=True)


def test_get_cached_empty_before_any_compute(engine):
    cached = engine.get_cached_attributions()
    assert cached["computed_at"] is None
    assert cached["modules"] == []


# ── Recompute cadence ───────────────────────────────────────────────────────

def test_maybe_recompute_respects_min_trades_and_interval(engine):
    # min_trades_for_attribution=3, interval=10.
    for i in range(3):
        votes = [_v("structure", "LONG", 1.0, 1.0), _v("momentum", "LONG", 1.0, 1.0)]
        engine.record_open(_attr(f"m{i}", "LONG", votes, ts=float(i + 1)))
        engine.complete(f"m{i}", {"pnl_r": 1.0, "won": True})

    assert engine.maybe_recompute(total_trades=2) is None    # below min_trades
    first = engine.maybe_recompute(total_trades=3)
    assert first is not None                                  # first qualifying run
    assert engine.maybe_recompute(total_trades=5) is None     # < interval since last
    assert engine.maybe_recompute(total_trades=13) is not None  # interval elapsed


def test_disabled_engine_still_records_but_can_be_gated(engine):
    # A disabled engine flag is enforced by the wiring layer / adapter, not the
    # storage layer — record/compute still work so unit tests are deterministic.
    engine.enabled = False
    votes = [_v("structure", "LONG", 1.0, 1.0), _v("momentum", "LONG", 1.0, 1.0)]
    assert engine.record_open(_attr("x1", "LONG", votes)) is True


# ── Pure helpers ────────────────────────────────────────────────────────────

def test_max_drawdown():
    assert _max_drawdown([]) == 0.0
    assert _max_drawdown([1.0, 1.0, 1.0]) == 0.0           # only rising
    # peak 3 then -2-2 = trough -1 -> dd = 4
    assert _max_drawdown([3.0, -2.0, -2.0]) == pytest.approx(4.0)


def test_std_and_votes_rebuild():
    assert _std([1.0]) == 0.0
    assert _std([1.0, 1.0]) == 0.0
    assert _std([0.0, 2.0]) == pytest.approx(1.0)
    votes = _votes_from_dicts([_v("m", "LONG", 0.5, 2.0), {"bad": True}])
    assert votes[0].module == "m" and votes[0].direction == "LONG"


# ── Config ──────────────────────────────────────────────────────────────────

def test_config_defaults_on_and_validates():
    cfg = CounterfactualConfig()
    assert cfg.counterfactual_enabled is True
    assert cfg.attribution_lookback == 500
    assert cfg.attribution_interval == 100
    with pytest.raises(ValueError):
        CounterfactualConfig(attribution_lookback=0)
    with pytest.raises(ValueError):
        CounterfactualConfig(attribution_interval=0)
    with pytest.raises(ValueError):
        CounterfactualConfig(min_trades_for_attribution=0)


def test_appconfig_has_counterfactual_section():
    from config import AppConfig

    cfg = AppConfig()
    assert isinstance(cfg.counterfactual, CounterfactualConfig)


# ── TunerAgent adapter ──────────────────────────────────────────────────────

def test_tunable_metadata(engine):
    t = CounterfactualTunable(engine, min_trades=3)
    assert t.tunable_name == "counterfactual"
    assert t.frequency == TuneFrequency.ON_TRADE_BATCH
    assert t.dependencies == []
    ok, _ = t.validate_params(t.get_current_params())
    assert ok is True


def test_tunable_skips_when_engine_disabled(engine):
    engine.enabled = False
    t = CounterfactualTunable(engine, min_trades=1)
    res = t.tune(TuneContext(total_trades=100, force=True))
    assert res.success is True
    assert res.skipped is True


def test_agent_drives_counterfactual_tune(engine):
    # Seed enough closed trades to clear the floor.
    for i in range(3):
        votes = [_v("structure", "LONG", 1.0, 1.0), _v("momentum", "LONG", 1.0, 1.0)]
        engine.record_open(_attr(f"g{i}", "LONG", votes, ts=float(i + 1)))
        engine.complete(f"g{i}", {"pnl_r": 1.0, "won": True})

    d = tempfile.mkdtemp()
    agent = TunerAgent(enabled=True, audit_db_path=os.path.join(d, "audit.db"))
    agent.register(CounterfactualTunable(engine, min_trades=1))
    assert "counterfactual" in agent.registered_names

    results = agent.on_trade_close(TuneContext(total_trades=50))
    names = [r.tunable_name for r in results]
    assert "counterfactual" in names
    # The cache should now be populated by the agent-driven run.
    assert engine.get_cached_attributions()["trades_analyzed"] == 3
    agent.close()
