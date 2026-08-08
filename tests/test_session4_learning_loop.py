"""Session 4 — Learning Loop Closure.

Verifies the two feedback-loop gaps closed in Session 4 so trade outcomes
genuinely flow back into the adaptive layer:

1. **Counterfactual vote-panel capture** — ``_on_order_filled`` previously
   captured a snapshot with an EMPTY ``votes`` list, which made every module
   classify as ABSENT and the entire leave-one-out attribution inert (an
   orphaned data path feeding VoteCalibrator / ModuleGovernor /
   InteractionAnalyzer / ParamEvolver). ``EventDrivenSystem._build_open_attribution``
   now captures the published WorldModel vote panel plus the live consensus
   thresholds and ranker kwargs, so the replay is faithful and produces real
   attribution.

2. **Periodic-recompute gating** — the close path handed the TunerAgent a
   ``TuneContext(total_trades=0)``, which pinned every ``maybe_recompute``
   (counterfactual / signal / interaction / behavior discovery) permanently
   below its min-trades floor. A running ``_closed_trade_count`` (seeded from
   persisted history at startup via ``count_closed_trades``) is now forwarded
   so the recompute cadence actually fires.

Dependency-light: builds vote panels directly so the consensus outcomes are
deterministic; uses temp SQLite DBs so nothing touches the real data/ dir.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import pytest

from config import ConsensusConfig, OpportunityRankerConfig
from adaptive.counterfactual import (
    CounterfactualEngine,
    TradeAttribution,
)
from brain.directional_consensus import Vote
from event_driven_bootstrap import EventDrivenSystem


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
    return {"module": module, "direction": direction,
            "confidence": confidence, "weight": weight}


def _decisive_votes():
    """structure is decisive: removing it drops net 1.5 -> 0.5 (< gate)."""
    return [_v("structure", "LONG", 1.0, 1.0),
            _v("momentum", "LONG", 1.0, 1.0),
            _v("volume", "SHORT", 0.5, 1.0)]


def _attr(trade_id, direction, votes, *, pnl_r=None, ts=1.0,
          thresholds=None, ranker=None):
    return TradeAttribution(
        trade_id=trade_id,
        pair="EURUSD",
        direction=direction,
        timestamp_open=ts,
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


def _bare_system(**attrs):
    """An EventDrivenSystem with only the attributes a method under test reads
    (mirrors the existing multi-opportunity test pattern)."""
    sys = EventDrivenSystem.__new__(EventDrivenSystem)
    for k, v in attrs.items():
        setattr(sys, k, v)
    return sys


# ── count_closed_trades: cold start + counting + persistence ───────────────

def test_count_closed_trades_cold_start(engine):
    assert engine.count_closed_trades() == 0
    # Cold-start attribution computes without raising and yields nothing.
    assert engine.compute_all_attributions() == {}
    # The old hardcoded total_trades=0 could never clear the floor.
    assert engine.maybe_recompute(0) is None


def test_count_closed_trades_counts_only_completed(engine):
    # Three opened; only two completed (closed + pnl_r present).
    for i in range(3):
        engine.record_open(_attr(f"t{i}", "LONG", _decisive_votes(), ts=float(i + 1)))
    engine.complete("t0", {"pnl_r": 1.0, "won": True})
    engine.complete("t1", {"pnl_r": -1.0, "won": False})
    assert engine.count_closed_trades() == 2


def test_count_closed_trades_persists_across_reopen():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "cf.db")
    eng = CounterfactualEngine(db_path=path, enabled=True,
                               min_trades_for_attribution=3)
    try:
        for i in range(2):
            eng.record_open(_attr(f"p{i}", "LONG", _decisive_votes(), ts=float(i + 1)))
            eng.complete(f"p{i}", {"pnl_r": 1.0, "won": True})
        assert eng.count_closed_trades() == 2
    finally:
        eng.close()

    # Reopen the SAME db (simulates a process restart) — count survives.
    eng2 = CounterfactualEngine(db_path=path, enabled=True,
                                min_trades_for_attribution=3)
    try:
        assert eng2.count_closed_trades() == 2
    finally:
        eng2.close()


# ── Vote-panel capture makes attribution non-inert (the core fix) ──────────

def test_populated_votes_yield_real_attribution(engine):
    engine.record_open(_attr("a1", "LONG", _decisive_votes()))
    engine.complete("a1", {"pnl_r": 2.0, "won": True})

    agg = engine.compute_module_attribution("structure")
    # With a real vote panel the module is involved (not ABSENT) and decisive.
    assert agg.trades_involved == 1
    assert agg.decisive_trades == 1
    assert agg.absent_trades == 0


def test_empty_votes_are_inert_regression(engine):
    # The pre-fix snapshot captured no votes — proving why attribution was dead.
    engine.record_open(_attr("z1", "LONG", [], pnl_r=None))
    engine.complete("z1", {"pnl_r": 2.0, "won": True})

    agg = engine.compute_module_attribution("structure")
    assert agg.trades_involved == 0
    assert agg.decisive_trades == 0
    # No module discovered at all — compute_all is empty despite a closed trade.
    assert engine.compute_all_attributions() == {}


# ── total_trades gates the periodic recompute (Gap B premise) ──────────────

def test_maybe_recompute_gated_by_total_trades(engine):
    for i in range(3):
        engine.record_open(_attr(f"r{i}", "LONG", _decisive_votes(), ts=float(i + 1)))
        engine.complete(f"r{i}", {"pnl_r": 1.0, "won": True})

    # The hardcoded zero the close path used never cleared the floor.
    assert engine.maybe_recompute(0) is None
    # A real running count >= min_trades fires the recompute.
    payload = engine.maybe_recompute(3)
    assert payload is not None
    assert payload["trades_analyzed"] == 3
    # And the leave-one-out ranking is non-empty now that votes are captured.
    assert payload["module_count"] >= 1


# ── _build_open_attribution: captures votes + live config (Gap A wiring) ───

def _ed_votes():
    return [Vote(module="structure", direction="LONG", confidence=1.0, weight=1.0,
                 timeframe="H1"),
            Vote(module="momentum", direction="LONG", confidence=1.0, weight=1.0,
                 timeframe="M5"),
            Vote(module="volume", direction="SHORT", confidence=0.5, weight=1.0,
                 timeframe="M5")]


def test_build_open_attribution_captures_votes_and_config():
    cfg = SimpleNamespace(
        consensus=ConsensusConfig(),
        opportunity_ranker=OpportunityRankerConfig(),
    )
    sys = _bare_system(_config=cfg)
    wm = SimpleNamespace(votes_list=lambda: _ed_votes())

    ta = sys._build_open_attribution("EURUSD", "LONG", "T1", wm)

    assert ta.trade_id == "T1"
    assert ta.pair == "EURUSD"
    assert ta.direction == "LONG"
    assert ta.consensus_direction == "LONG"

    # Votes serialized to the dict shape the replay consumes.
    assert len(ta.votes) == 3
    structure = next(v for v in ta.votes if v["module"] == "structure")
    assert structure["direction"] == "LONG"
    assert structure["confidence"] == 1.0
    assert structure["weight"] == 1.0

    # Live consensus thresholds threaded through (authoritative, not defaults).
    assert ta.thresholds["min_net_score"] == cfg.consensus.min_net_score
    assert ta.thresholds["min_agreement"] == cfg.consensus.min_agreement
    assert ta.thresholds["min_contributors"] == cfg.consensus.min_contributors

    # Ranker kwargs threaded through (needed for the NEUTRAL-rescue replay).
    assert ta.ranker_kwargs["execute"] == bool(cfg.opportunity_ranker.execute)
    assert ta.ranker_kwargs["rescue_neutral_consensus"] == bool(
        cfg.opportunity_ranker.rescue_neutral_consensus
    )
    assert ta.ranker_kwargs["base_win_rate"] == cfg.opportunity_ranker.base_win_rate


def test_build_open_attribution_handles_missing_worldmodel():
    cfg = SimpleNamespace(
        consensus=ConsensusConfig(),
        opportunity_ranker=OpportunityRankerConfig(),
    )
    sys = _bare_system(_config=cfg)

    ta = sys._build_open_attribution("EURUSD", "SHORT", "T2", None)
    # No WorldModel → no votes, but the snapshot is still valid + config-bearing.
    assert ta.votes == []
    assert ta.direction == "SHORT"
    assert ta.thresholds  # consensus config still captured
    assert ta.ranker_kwargs


def test_build_open_attribution_feeds_live_engine_round_trip(engine):
    # The exact snapshot the live path produces, fed into the real engine,
    # yields a non-inert attribution (the orphaned data path is closed).
    cfg = SimpleNamespace(
        consensus=ConsensusConfig(),
        opportunity_ranker=OpportunityRankerConfig(),
    )
    sys = _bare_system(_config=cfg)
    wm = SimpleNamespace(votes_list=lambda: _ed_votes())

    ta = sys._build_open_attribution("EURUSD", "LONG", "RT1", wm)
    engine.record_open(ta)
    engine.complete("RT1", {"pnl_r": 1.5, "won": True})

    agg = engine.compute_module_attribution("structure")
    # structure cast a directional vote on the trade — no longer ABSENT.
    assert agg.trades_involved == 1
    assert agg.absent_trades == 0


# ── _seed_learning_counters: warm-start the recompute cadence ──────────────

def test_seed_learning_counters_from_history(engine):
    for i in range(2):
        engine.record_open(_attr(f"s{i}", "LONG", _decisive_votes(), ts=float(i + 1)))
        engine.complete(f"s{i}", {"pnl_r": 1.0, "won": True})

    sys = _bare_system(
        _closed_trade_count=0,
        _ctx=SimpleNamespace(counterfactual_engine=engine),
    )
    sys._seed_learning_counters()
    assert sys._closed_trade_count == 2


def test_seed_learning_counters_no_engine_is_safe():
    sys = _bare_system(
        _closed_trade_count=0,
        _ctx=SimpleNamespace(counterfactual_engine=None),
    )
    sys._seed_learning_counters()  # must not raise
    assert sys._closed_trade_count == 0

    # No ctx at all is also safe.
    sys2 = _bare_system(_closed_trade_count=0, _ctx=None)
    sys2._seed_learning_counters()
    assert sys2._closed_trade_count == 0
