"""Session 2 — Multi-Opportunity Entry Generation.

Verifies that the entry layer evaluates EACH ranked candidate independently
instead of collapsing the panel into a single net-summed direction:

* The consensus path forms a thesis from each candidate's OWN votes, so a LONG
  swing and a SHORT scalp can both survive as separate candidate entries.
* The within-cycle selector (``select_cycle_candidates``) ranks best-first and
  applies the Session-2 direction lock (highest score wins; Session 3 replaces
  it with capital-aware allocation).
* The structural zone path attaches the same end-to-end candidate provenance to
  its emitted decisions.
* Empty-candidate inputs fall back to legacy single-direction behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from brain.candidate_models import Candidate, CandidateEntryDecision
from brain.decision_core import decide_candidates
from brain.directional_consensus import Vote, decide, decide_opportunities
from brain.fvg_detector import FairValueGap, FVGStatus
from brain.opportunity_ranker import rank_opportunities
from brain.world_model import WorldModelStore, build_world_model
from config import ConsensusConfig
from entry.entry_orchestrator import EntryOrchestrator
from entry.models import EntryConfig
from event_driven_bootstrap import EventDrivenSystem, select_cycle_candidates


# ── Shared fixtures ────────────────────────────────────────────────────────


def _long_swing_votes() -> list[Vote]:
    """A coherent LONG swing cluster (slow modules, HTF)."""
    return [
        Vote("structure", "LONG", 0.8, 3.0, timeframe="H4"),
        Vote("wyckoff", "LONG", 0.7, 1.5, timeframe="H1"),
        Vote("order_block", "LONG", 0.7, 1.5, timeframe="H1"),
    ]


def _short_scalp_votes() -> list[Vote]:
    """A coherent SHORT scalp cluster (fast modules, LTF)."""
    return [
        Vote("momentum", "SHORT", 0.8, 1.0, timeframe="M5"),
        Vote("volume", "SHORT", 0.7, 1.0, timeframe="M5"),
        Vote("vwap", "SHORT", 0.7, 1.0, timeframe="M5"),
    ]


def _mixed_panel() -> list[Vote]:
    return _long_swing_votes() + _short_scalp_votes()


def _m5_df(n: int = 60, base: float = 1.1000) -> pd.DataFrame:
    step = 0.0001
    return pd.DataFrame({
        "open": [base + i * step for i in range(n)],
        "high": [base + i * step + 0.0006 for i in range(n)],
        "low": [base + i * step - 0.0004 for i in range(n)],
        "close": [base + (i + 0.5) * step for i in range(n)],
        "tick_volume": [100 + i for i in range(n)],
    })


# ── 1. No collapse at generation: opposing candidates both survive ─────────


def test_scalar_decide_collapses_but_candidates_preserve_both():
    """The scalar consensus collapses the mixed panel to ONE direction, while
    the ranker keeps the opposing LONG swing and SHORT scalp as separate ideas."""
    votes = _mixed_panel()

    scalar = decide(
        votes,
        min_net_score=1.5,
        min_agreement=0.55,
        high_authority_modules=[],
        high_authority_oppose_confidence=0.6,
        min_contributors=2,
    )
    # Net sum favours LONG (swing mass > scalp mass) — the SHORT idea is gone.
    assert scalar.direction == "LONG"

    candidates = decide_candidates(votes, min_vote_count=2)
    directions = {c.direction for c in candidates}
    assert directions == {"LONG", "SHORT"}, directions


def test_per_candidate_thesis_uses_only_its_own_votes():
    """form_thesis scoped to a candidate's votes yields THAT candidate's
    direction — proving conviction is not diluted by opposing clusters."""
    from brain.directional_consensus import form_thesis

    candidates = decide_candidates(_mixed_panel(), min_vote_count=2)
    by_dir = {c.direction: c for c in candidates}

    for direction, cand in by_dir.items():
        thesis = form_thesis(
            list(cand.contributing_votes),
            min_net_score=1.5,
            min_agreement=0.55,
            high_authority_modules=[],
            high_authority_oppose_confidence=0.6,
            min_contributors=2,
            conviction_threshold=0.62,
        )
        assert thesis.direction == direction
        assert thesis.trigger is True


# ── 2. Cycle selector: rank + within-cycle direction lock ──────────────────


def _item(direction: str, score: float, ev: float = 0.0):
    cand = Candidate(direction=direction, timeframe_class="SWING", score=score,
                     ev_estimate=ev)
    dec = {"symbol": "EURUSD", "direction": direction, "candidate_id": cand.candidate_id}
    env = CandidateEntryDecision(symbol="EURUSD", candidate=cand, source="consensus")
    return (env, dec)


def test_select_cycle_empty_is_safe():
    survivors, dropped, winning = select_cycle_candidates([])
    assert survivors == [] and dropped == [] and winning == ""


def test_select_cycle_highest_score_wins_opposing_dropped():
    items = [_item("LONG", 0.6), _item("SHORT", 0.9), _item("LONG", 0.7)]
    survivors, dropped, winning = select_cycle_candidates(items)
    assert winning == "SHORT"
    assert len(survivors) == 1 and survivors[0][0].candidate.direction == "SHORT"
    assert {it[0].candidate.direction for it in dropped} == {"LONG"}


def test_select_cycle_same_direction_all_survive_best_first():
    items = [_item("LONG", 0.6, ev=0.5), _item("LONG", 0.9, ev=1.0),
             _item("LONG", 0.7, ev=0.2)]
    survivors, dropped, winning = select_cycle_candidates(items)
    assert winning == "LONG"
    assert len(survivors) == 3 and dropped == []
    scores = [it[0].candidate.score for it in survivors]
    assert scores == sorted(scores, reverse=True)


# ── 3. Consensus path: multiple candidates → multiple decisions ────────────


def _make_consensus_system(wm_store):
    sys = EventDrivenSystem.__new__(EventDrivenSystem)
    sys._ctx = None
    sys._wm_store = wm_store
    sys._consensus_entry_cooldown = {}
    sys._fetch_candles = lambda symbol, tf, n: _m5_df()
    sys._safe_pip_size = lambda symbol: 0.0001
    sys._get_spread_pips = lambda symbol: 0.5

    class _PM:
        def get_all_open_positions(self):
            return []

    sys._pm = _PM()
    captured: list = []
    sys._on_entry_decision = lambda d: captured.append(d)

    class _Cfg:
        consensus = ConsensusConfig()

    sys._config = _Cfg()
    return sys, captured


def test_consensus_path_evaluates_each_candidate_independently():
    """A mixed panel produces candidate items for BOTH directions before the
    cycle direction lock selects the winner — no upstream net-sum collapse."""
    votes = _mixed_panel()
    candidates = rank_opportunities(votes)
    assert {c.direction for c in candidates} == {"LONG", "SHORT"}

    store = WorldModelStore()
    wm = build_world_model(
        symbol="EURUSD", version=store.next_version(),
        votes=votes, candidates=candidates,
    )
    store.publish(wm)

    sys, captured = _make_consensus_system(store)

    # Each candidate independently builds an item (its own thesis + geometry).
    items = []
    for opp in candidates:
        cand = Candidate.from_opportunity(opp)
        item = sys._build_consensus_candidate_item("EURUSD", cand, ConsensusConfig())
        if item is not None:
            items.append(item)
    built_dirs = {env.candidate.direction for env, _ in items}
    assert built_dirs == {"LONG", "SHORT"}, built_dirs
    assert all(env.source == "consensus" for env, _ in items)

    # Full path: cycle selector locks to one direction this cycle and dispatches.
    sys._evaluate_consensus_entry("EURUSD")
    assert len(captured) >= 1
    dispatched_dirs = {d["direction"] for d in captured}
    assert len(dispatched_dirs) == 1  # within-cycle direction lock
    d = captured[0]
    assert d["source"] == "consensus"
    assert d["candidate_id"]
    assert d["timeframe_class"] in ("SWING", "SCALP")
    assert d["contributing_modules"]


def test_consensus_decision_carries_candidate_provenance():
    cand = Candidate.from_opportunity(rank_opportunities(_long_swing_votes())[0])
    store = WorldModelStore()
    sys, _ = _make_consensus_system(store)
    item = sys._build_consensus_candidate_item("EURUSD", cand, ConsensusConfig())
    assert item is not None
    env, dec = item
    assert dec["source"] == "consensus"
    assert dec["candidate_id"] == cand.candidate_id
    assert dec["timeframe_class"] == cand.timeframe_class
    assert dec["direction"] == "LONG"
    assert dec["tp1"] > dec["entry_price"] > 0
    assert dec["stop_loss"] < dec["entry_price"]


def test_consensus_legacy_fallback_when_no_candidates():
    """No ranked candidates on the WorldModel → single net-summed thesis still
    fires through the legacy fallback path."""
    votes = _long_swing_votes()
    store = WorldModelStore()
    wm = build_world_model(
        symbol="EURUSD", version=store.next_version(), votes=votes,
    )  # candidates intentionally omitted
    store.publish(wm)

    sys, captured = _make_consensus_system(store)
    sys._evaluate_consensus_entry("EURUSD")

    assert len(captured) == 1
    assert captured[0]["source"] == "consensus"
    assert captured[0]["direction"] == "LONG"


# ── 4. Zone path: candidate provenance attached to emitted decision ────────


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_fvg(kind="BULLISH", top=1.0850, bottom=1.0840) -> FairValueGap:
    return FairValueGap(
        kind=kind, top=top, bottom=bottom, midpoint=(top + bottom) / 2,
        size_pips=10.0, strength="STRONG", status=FVGStatus.OPEN,
        candle_index=50, timestamp=_ts(), timeframe="M5",
    )


def _bullish_m1(n=30, base=1.0835) -> pd.DataFrame:
    step = 0.00005
    return pd.DataFrame({
        "open": [base + i * step for i in range(n)],
        "high": [base + i * step + 0.0003 for i in range(n)],
        "low": [base + i * step - 0.0001 for i in range(n)],
        "close": [base + (i + 0.5) * step for i in range(n)],
        "tick_volume": [100 + i * 10 for i in range(n)],
    })


@dataclass
class _FakeTick:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime


def _fire_zone(candidates):
    store = WorldModelStore()
    decisions: list = []
    orch = EntryOrchestrator(
        world_model_store=store,
        config=EntryConfig(min_entry_score=50),
        pip_size_lookup=lambda _: 0.0001,
        on_entry_decision=lambda d: decisions.append(d),
        get_m1_dataframe=lambda _: _bullish_m1(),
    )
    wm = build_world_model(
        symbol="EURUSD", version=store.next_version(),
        fvgs={"M5": [_make_fvg()]},
        votes=_long_swing_votes(),
        candidates=candidates,
    )
    store.publish(wm)
    orch.on_world_model_update("EURUSD")
    orch.on_tick(_FakeTick("EURUSD", 1.0844, 1.0845, datetime.now(timezone.utc)))
    orch.on_m1_close("EURUSD")
    return decisions


def test_zone_path_attaches_candidate_provenance():
    candidates = rank_opportunities(_long_swing_votes())  # a LONG candidate
    decisions = _fire_zone(candidates)
    assert len(decisions) == 1
    d = decisions[0]
    assert d["source"] == "zone"
    assert d["direction"] == "LONG"
    assert d["candidate_id"]  # matched the LONG candidate
    assert d["timeframe_class"] in ("SWING", "SCALP")
    assert d["contributing_modules"]


def test_zone_path_blank_provenance_when_no_candidate():
    decisions = _fire_zone([])  # no candidates on the WorldModel
    assert len(decisions) == 1
    d = decisions[0]
    assert d["source"] == "zone"
    assert d["candidate_id"] == ""
    assert d["timeframe_class"] == ""
    assert d["contributing_modules"] == []
