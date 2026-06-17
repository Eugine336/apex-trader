"""
Tests for the L5b Module Interaction Discovery engine
(:mod:`adaptive.interaction_discovery`) and its TunerAgent adapter.

The engine reuses the L4 ``replay_consensus`` math, so the scenarios below pin
the consensus thresholds (``min_net_score=1.5`` etc.) and build vote panels whose
leave-K-out behaviour is hand-verifiable:

  * A two-module panel ``A+B`` (each weight 1) nets ``2.0`` → a trade is taken;
    remove either and the survivor nets ``1.0`` (< 1.5) → the trade collapses.
  * A three-module panel ``A+B+C`` nets ``3.0``; removing any ONE leaves ``2.0``
    (trade survives), removing TWO leaves ``1.0`` (trade collapses) — the
    redundancy that produces a non-zero interaction term.
"""

from __future__ import annotations

import pytest

from adaptive.counterfactual import CounterfactualEngine, TradeAttribution
from adaptive.interaction_discovery import (
    INDEPENDENT,
    SYNERGY,
    TOXIC,
    InteractionAnalyzer,
    OptimalSubsetResult,
    _directional_modules,
    _prepare,
)
from adaptive.tunable import TuneContext, TuneFrequency
from adaptive.tunable_adapters import InteractionAnalyzerTunable
from adaptive.tuner_agent import TunerAgent
from config import InteractionConfig

_TH = {"min_net_score": 1.5, "min_agreement": 0.55, "min_contributors": 1}


def _v(module: str, direction: str, conf: float = 1.0, weight: float = 1.0) -> dict:
    return {"module": module, "direction": direction, "confidence": conf, "weight": weight}


def _trade(tid: str, direction: str, votes: list[dict], pnl: float, ts: float = 0.0) -> TradeAttribution:
    return TradeAttribution(
        trade_id=tid,
        pair="X",
        direction=direction,
        votes=votes,
        thresholds=dict(_TH),
        ranker_kwargs={},
        pnl_r=pnl,
        won=pnl > 0,
        outcome="WIN" if pnl > 0 else "LOSS",
        closed=True,
        timestamp_open=ts,
    )


class _FakeCF:
    """Minimal stand-in exposing only ``get_closed_attributions``."""

    def __init__(self, trades, *, raises: bool = False) -> None:
        self._trades = list(trades)
        self._raises = raises

    def get_closed_attributions(self, lookback=None):  # noqa: D401, ANN001
        if self._raises:
            raise RuntimeError("boom")
        return list(self._trades)


def _analyzer(trades, tmp_path, **kw) -> InteractionAnalyzer:
    defaults = dict(
        enabled=True,
        lookback=500,
        interval=1,
        min_trades=1,
        db_path=str(tmp_path / "itx.db"),
    )
    defaults.update(kw)
    return InteractionAnalyzer(_FakeCF(trades), **defaults)


# ── Config ───────────────────────────────────────────────────────────────


def test_config_defaults_on_and_values():
    cfg = InteractionConfig()
    assert cfg.interaction_discovery_enabled is True
    assert cfg.interaction_lookback == 500
    assert cfg.interaction_interval == 500
    assert cfg.toxic_threshold == -0.05
    assert cfg.synergy_threshold == 0.05
    assert cfg.exhaustive_search_max_modules == 12


@pytest.mark.parametrize(
    "kw",
    [
        {"interaction_lookback": 0},
        {"interaction_interval": 0},
        {"exhaustive_search_max_modules": 0},
        {"toxic_threshold": 0.5, "synergy_threshold": 0.1},  # toxic > synergy
    ],
)
def test_config_validation_rejects_bad(kw):
    with pytest.raises(ValueError):
        InteractionConfig(**kw)


# ── Preparation helpers ────────────────────────────────────────────────────


def test_prepare_filters_unclosed_and_directionless():
    good = _trade("1", "LONG", [_v("a", "LONG")], 1.0)
    no_pnl = TradeAttribution(trade_id="2", pair="X", direction="LONG",
                              votes=[_v("a", "LONG")], thresholds=_TH, pnl_r=None, closed=True)
    neutral_dir = _trade("3", "NEUTRAL", [_v("a", "LONG")], 1.0)
    no_votes = _trade("4", "LONG", [], 1.0)
    prepared = _prepare([good, no_pnl, neutral_dir, no_votes])
    assert len(prepared) == 1
    assert prepared[0].direction == "LONG"


def test_directional_modules_only_counts_directional_voters():
    trades = [_trade("1", "LONG", [_v("a", "LONG"), _v("b", "NEUTRAL"), _v("c", "SHORT")], 1.0)]
    prepared = _prepare(trades)
    assert _directional_modules(prepared) == ["a", "c"]


# ── Book replay ────────────────────────────────────────────────────────────


def test_book_drops_trades_that_no_longer_survive(tmp_path):
    # a+b LONG nets 2.0 -> taken; removing b leaves a at 1.0 -> collapses.
    trades = [_trade(str(i), "LONG", [_v("a", "LONG"), _v("b", "LONG")], 1.0, ts=i) for i in range(3)]
    an = _analyzer(trades, tmp_path)
    prepared = _prepare(trades)
    base, seq, n = an._book(prepared, frozenset())
    assert n == 3 and base == pytest.approx(3.0)
    # Remove b -> all three collapse to NEUTRAL -> none taken.
    drop, seq2, n2 = an._book(prepared, frozenset({"b"}))
    assert n2 == 0 and drop == pytest.approx(0.0)
    an.close()


# ── Pairwise interaction matrix ─────────────────────────────────────────────


def test_pairwise_toxic_pair(tmp_path):
    # a+b LONG (net 2) lose -1R each; remove either -> survivor 1.0 collapses.
    trades = [_trade(str(i), "LONG", [_v("a", "LONG"), _v("b", "LONG")], -1.0, ts=i) for i in range(3)]
    an = _analyzer(trades, tmp_path)
    prepared = _prepare(trades)
    matrix = an.compute_pairwise_interactions(prepared)
    res = matrix[("a", "b")]
    # remove a -> +3 (losers gone); remove b -> +3; remove both -> +3.
    assert res.removal_delta_a == pytest.approx(3.0)
    assert res.removal_delta_b == pytest.approx(3.0)
    assert res.removal_delta_ab == pytest.approx(3.0)
    assert res.interaction_effect == pytest.approx(3.0 - (3.0 + 3.0))  # -3
    assert res.relationship == TOXIC
    an.close()


def test_pairwise_synergy_pair(tmp_path):
    # a+b+c LONG (net 3) lose; removing ONE leaves 2.0 (survives), removing the
    # pair leaves c at 1.0 (collapses) -> superadditive removal delta -> effect>0.
    trades = [
        _trade(str(i), "LONG", [_v("a", "LONG"), _v("b", "LONG"), _v("c", "LONG")], -1.0, ts=i)
        for i in range(3)
    ]
    an = _analyzer(trades, tmp_path)
    prepared = _prepare(trades)
    matrix = an.compute_pairwise_interactions(prepared)
    res = matrix[("a", "b")]
    assert res.removal_delta_a == pytest.approx(0.0)   # c+b still net 2 -> kept
    assert res.removal_delta_b == pytest.approx(0.0)
    assert res.removal_delta_ab == pytest.approx(3.0)  # collapses -> losers gone
    assert res.interaction_effect == pytest.approx(3.0)
    assert res.relationship == SYNERGY
    an.close()


def test_pairwise_independent_pair(tmp_path):
    # a and b are each decisive on DISJOINT profitable trades (c backs both but
    # alone nets 1.0). Removing a only hits trade1, removing b only hits trade2,
    # so the joint removal is exactly additive -> interaction ~ 0.
    t1 = _trade("1", "LONG", [_v("a", "LONG"), _v("c", "LONG")], 1.0, ts=0)
    t2 = _trade("2", "LONG", [_v("b", "LONG"), _v("c", "LONG")], 1.0, ts=1)
    an = _analyzer([t1, t2], tmp_path)
    prepared = _prepare([t1, t2])
    matrix = an.compute_pairwise_interactions(prepared)
    res = matrix[("a", "b")]
    assert res.interaction_effect == pytest.approx(0.0)
    assert res.relationship == INDEPENDENT
    an.close()


def test_classify_respects_thresholds(tmp_path):
    an = _analyzer([], tmp_path, toxic_threshold=-0.10, synergy_threshold=0.10)
    assert an._classify(0.2) == SYNERGY
    assert an._classify(-0.2) == TOXIC
    assert an._classify(0.05) == INDEPENDENT
    assert an._classify(-0.05) == INDEPENDENT
    an.close()


def test_pairwise_needs_two_modules(tmp_path):
    trades = [_trade("1", "LONG", [_v("a", "LONG")], 1.0)]
    an = _analyzer(trades, tmp_path)
    assert an.compute_pairwise_interactions(_prepare(trades)) == {}
    an.close()


# ── Single-module delta == L4 r_difference ─────────────────────────────────


def test_single_module_delta_matches_l4(tmp_path):
    # 3 winners (structure+liquidity) + 3 losers (badmod+structure). badmod is
    # decisive on the losers; removing it deletes their -3R.
    trades = []
    for i in range(3):
        trades.append(_trade(f"w{i}", "LONG", [_v("structure", "LONG"), _v("liquidity", "LONG")], 1.0, ts=i))
    for i in range(3):
        trades.append(_trade(f"l{i}", "LONG", [_v("badmod", "LONG"), _v("structure", "LONG")], -1.0, ts=10 + i))

    cf = CounterfactualEngine(
        db_path=str(tmp_path / "cf.db"), enabled=True,
        attribution_lookback=500, attribution_interval=1, min_trades_for_attribution=1,
    )
    for t in trades:
        assert cf.record_open(t)
    l4 = cf.compute_module_attribution("badmod")

    an = InteractionAnalyzer(cf, enabled=True, interval=1, min_trades=1, db_path=str(tmp_path / "itx.db"))
    prepared = _prepare(cf.get_closed_attributions(500))
    base, _, _ = an._book(prepared, frozenset())
    without_badmod, _, _ = an._book(prepared, frozenset({"badmod"}))
    removal_delta_badmod = without_badmod - base
    # L4: r_difference = -marginal_r (the book delta from removing the module).
    assert removal_delta_badmod == pytest.approx(l4.r_difference)
    assert removal_delta_badmod == pytest.approx(3.0)
    an.close()
    cf.close()


# ── Optimal subset search ──────────────────────────────────────────────────


def _harmful_module_book(n_good: int = 3, n_bad: int = 3) -> list[TradeAttribution]:
    trades = []
    for i in range(n_good):
        trades.append(_trade(f"w{i}", "LONG", [_v("structure", "LONG"), _v("liquidity", "LONG")], 1.0, ts=i))
    for i in range(n_bad):
        trades.append(_trade(f"l{i}", "LONG", [_v("badmod", "LONG"), _v("structure", "LONG")], -1.0, ts=10 + i))
    return trades


def test_optimal_subset_shadows_harmful_module(tmp_path):
    trades = _harmful_module_book()
    an = _analyzer(trades, tmp_path)
    opt = an.find_optimal_subset(_prepare(trades))
    assert opt.search == "exhaustive"
    assert "badmod" in opt.shadow_modules
    assert "badmod" not in opt.active_modules
    assert opt.baseline_r == pytest.approx(0.0)   # +3 winners − 3 losers
    assert opt.total_r == pytest.approx(3.0)      # losers shadowed away
    assert opt.improvement == pytest.approx(3.0)
    an.close()


def test_optimal_subset_all_good_keeps_everything(tmp_path):
    trades = [_trade(str(i), "LONG", [_v("structure", "LONG"), _v("liquidity", "LONG")], 1.0, ts=i) for i in range(4)]
    an = _analyzer(trades, tmp_path)
    opt = an.find_optimal_subset(_prepare(trades))
    assert opt.improvement == pytest.approx(0.0)
    assert set(opt.active_modules) == {"structure", "liquidity"}
    assert opt.shadow_modules == []
    an.close()


def test_optimal_subset_empty_trades(tmp_path):
    an = _analyzer([], tmp_path)
    opt = an.find_optimal_subset(_prepare([]))
    assert isinstance(opt, OptimalSubsetResult)
    assert opt.active_modules == []
    assert opt.improvement == pytest.approx(0.0)
    an.close()


def test_greedy_search_path(tmp_path):
    trades = _harmful_module_book()
    # Force greedy: only allow exhaustive at <=1 module (3 vote here).
    an = _analyzer(trades, tmp_path, exhaustive_search_max_modules=1)
    opt = an.find_optimal_subset(_prepare(trades))
    assert opt.search == "greedy"
    assert "badmod" in opt.shadow_modules
    assert opt.improvement == pytest.approx(3.0)
    an.close()


# ── Compute + cache + accessors ────────────────────────────────────────────


def test_compute_and_cache_roundtrip(tmp_path):
    trades = _harmful_module_book()
    an = _analyzer(trades, tmp_path)
    payload = an.compute_and_cache()
    assert payload["trades_analyzed"] == 6
    assert payload["module_count"] == 3
    assert payload["optimal_subset"]["improvement"] == pytest.approx(3.0)
    cached = an.get_cached()
    assert cached["optimal_subset"] == payload["optimal_subset"]
    # toxic / synergy read-only recommendation surface.
    assert isinstance(an.get_toxic_pairs(), list)
    assert isinstance(an.get_synergy_pairs(), list)
    an.close()


def test_cache_empty_shape_before_compute(tmp_path):
    an = _analyzer([], tmp_path)
    cached = an.get_cached()
    assert cached["computed_at"] is None
    assert cached["modules"] == []
    assert cached["pairs"] == []
    assert "optimal_subset" in cached
    an.close()


def test_cache_persists_across_instances(tmp_path):
    trades = _harmful_module_book()
    db = str(tmp_path / "persist.db")
    an = InteractionAnalyzer(_FakeCF(trades), enabled=True, interval=1, min_trades=1, db_path=db)
    an.compute_and_cache()
    an.close()
    # Reopen: the cached payload survives.
    an2 = InteractionAnalyzer(_FakeCF(trades), enabled=True, db_path=db)
    cached = an2.get_cached()
    assert cached["trades_analyzed"] == 6
    assert cached["optimal_subset"]["improvement"] == pytest.approx(3.0)
    an2.close()


# ── Cadence gating ─────────────────────────────────────────────────────────


def test_maybe_recompute_gating(tmp_path):
    trades = _harmful_module_book()
    an = _analyzer(trades, tmp_path, interval=10, min_trades=5)
    assert an.maybe_recompute(4) is None           # below floor
    assert an.maybe_recompute(5) is not None        # first qualifying run
    assert an.maybe_recompute(6) is None            # within interval
    assert an.maybe_recompute(15) is not None       # interval elapsed
    an.close()


# ── Exception safety ───────────────────────────────────────────────────────


def test_compute_safe_when_cf_raises(tmp_path):
    an = InteractionAnalyzer(_FakeCF([], raises=True), enabled=True, interval=1, min_trades=1,
                             db_path=str(tmp_path / "itx.db"))
    payload = an.compute_and_cache()  # must not raise
    assert payload["trades_analyzed"] == 0
    assert payload["modules"] == []
    an.close()


def test_set_params_clamps(tmp_path):
    an = _analyzer([], tmp_path)
    an.set_params(lookback=-5, interval=0)
    assert an.lookback == 1
    assert an.interval == 1
    an.close()


# ── TunerAgent adapter ─────────────────────────────────────────────────────


def test_tunable_metadata(tmp_path):
    an = _analyzer([], tmp_path)
    t = InteractionAnalyzerTunable(an, min_trades=1)
    assert t.tunable_name == "interaction_analyzer"
    assert t.frequency == TuneFrequency.ON_TRADE_BATCH
    assert t.dependencies == ["counterfactual"]
    params = t.get_current_params()
    assert params["enabled"] is True
    assert "lookback" in params and "interval" in params
    assert t.rollback() is True   # no snapshot -> safe no-op
    an.close()


def test_tunable_skips_when_disabled(tmp_path):
    an = _analyzer([], tmp_path, enabled=False)
    t = InteractionAnalyzerTunable(an, min_trades=1)
    res = t.tune(TuneContext(total_trades=100, force=True))
    assert res.success and res.skipped
    an.close()


def test_tunable_runs_and_reports(tmp_path):
    trades = _harmful_module_book()
    an = _analyzer(trades, tmp_path, interval=1, min_trades=1)
    t = InteractionAnalyzerTunable(an, min_trades=1)
    res = t.tune(TuneContext(total_trades=6, force=True))
    assert res.success and res.changed and not res.skipped
    assert "trades" in res.reason
    # The recompute populated the cache.
    assert an.get_cached()["computed_at"] is not None
    an.close()


def test_tuner_agent_drives_interaction(tmp_path):
    trades = _harmful_module_book()
    an = _analyzer(trades, tmp_path, interval=1, min_trades=1)
    agent = TunerAgent(enabled=True, audit_db_path=str(tmp_path / "audit.db"))
    agent.register(InteractionAnalyzerTunable(an, min_trades=1))
    results = agent.on_trade_close(TuneContext(total_trades=6))
    names = {r.tunable_name for r in results}
    assert "interaction_analyzer" in names
    assert an.get_cached()["computed_at"] is not None
    agent.close()
    an.close()
