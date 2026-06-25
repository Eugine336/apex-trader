"""Session 4 — Multi-Opportunity Position Management.

Verifies that an open position is managed against the SAME modules + timeframes
that voted it open (its Candidate), instead of the latest net-summed direction —
so a LONG swing and a SHORT scalp on one symbol live and die by their own theses.

Covered:
* ``_record_candidate_position`` captures full provenance keyed by ticket.
* ``_scope_votes_to_candidate`` keeps only the contributing panel's votes.
* ``_check_candidate_thesis`` returns HOLD (scoped votes) when the opening panel
  still supports the position, CLOSE/``thesis_invalidated`` when it flips, and
  CLOSE/``thesis_silent`` when it goes quiet; and falls through (None) when
  disabled or when no provenance was captured.
* Two opposing positions on one symbol are evaluated independently.
* The trade journal persists + reads back candidate provenance.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from brain.candidate_models import CandidatePosition
from brain.directional_consensus import Vote
from brain.trade_journal import TradeJournal, TradeRecord
from event_driven_bootstrap import EventDrivenSystem


# ── helpers ──────────────────────────────────────────────────────────────────


def _vote(module: str, direction: str, tf: str = "", conf: float = 0.8) -> Vote:
    return Vote(module=module, direction=direction, confidence=conf, weight=1.0,
                timeframe=tf)


def _wm(votes: list) -> SimpleNamespace:
    return SimpleNamespace(votes_list=lambda: list(votes))


def _system(scoped_enabled: bool = True, min_live: int = 1) -> EventDrivenSystem:
    sys = EventDrivenSystem.__new__(EventDrivenSystem)
    sys._candidate_positions = {}
    sys._config = SimpleNamespace(
        decision=SimpleNamespace(
            candidate_scoped_management_enabled=scoped_enabled,
            candidate_thesis_min_live_votes=min_live,
        ),
    )
    return sys


def _prov(direction: str, modules: list, tfs: list) -> CandidatePosition:
    return CandidatePosition(
        symbol="EURUSD", direction=direction, candidate_id="cand1",
        timeframe_class="SWING", contributing_modules=modules,
        contributing_timeframes=tfs,
    )


# ── 1. provenance capture ─────────────────────────────────────────────────────


def test_record_candidate_position_captures_provenance():
    sys = _system()
    decision = {
        "candidate_id": "abc123",
        "timeframe_class": "SWING",
        "contributing_modules": ["structure", "wyckoff"],
        "contributing_timeframes": ["H4", "D1"],
        "regime_at_entry": "trending",
    }
    sys._record_candidate_position("T1", "EURUSD", "BUY", decision)
    prov = sys._candidate_positions["T1"]
    assert isinstance(prov, CandidatePosition)
    assert prov.candidate_id == "abc123"
    assert prov.timeframe_class == "SWING"
    assert prov.contributing_modules == ["structure", "wyckoff"]
    assert prov.contributing_timeframes == ["H4", "D1"]
    assert prov.entry_regime == "trending"


# ── 2. vote scoping ───────────────────────────────────────────────────────────


def test_scope_votes_filters_to_contributing_panel():
    prov = _prov("LONG", ["structure", "wyckoff"], ["H4", "D1"])
    votes = [
        _vote("structure", "LONG", "H4"),   # kept
        _vote("wyckoff", "LONG", "D1"),     # kept
        _vote("momentum", "SHORT", "M5"),   # dropped — not a contributing module
        _vote("structure", "SHORT", "M5"),  # dropped — wrong timeframe
    ]
    scoped = EventDrivenSystem._scope_votes_to_candidate(votes, prov)
    mods = sorted((v.module, v.timeframe) for v in scoped)
    assert mods == [("structure", "H4"), ("wyckoff", "D1")]


def test_scope_votes_no_modules_returns_all():
    prov = _prov("LONG", [], [])
    votes = [_vote("structure", "LONG", "H4"), _vote("momentum", "SHORT", "M5")]
    assert EventDrivenSystem._scope_votes_to_candidate(votes, prov) == votes


# ── 3. thesis intact → HOLD with scoped votes ─────────────────────────────────


def test_thesis_intact_holds_and_scopes_votes():
    sys = _system()
    sys._candidate_positions["T1"] = _prov("LONG", ["structure", "wyckoff"], ["H4", "D1"])
    wm = _wm([
        _vote("structure", "LONG", "H4"),
        _vote("wyckoff", "LONG", "D1"),
        _vote("momentum", "SHORT", "M5"),  # not on the panel — must be ignored
    ])
    result = sys._check_candidate_thesis("T1", "BUY", wm)
    assert result is not None
    verdict, payload = result
    assert verdict == "HOLD"
    # Scoped votes exclude the off-panel momentum SHORT.
    assert all(v.module in ("structure", "wyckoff") for v in payload)


# ── 4. thesis flipped → CLOSE thesis_invalidated ──────────────────────────────


def test_thesis_flipped_closes_invalidated():
    sys = _system()
    sys._candidate_positions["T1"] = _prov("LONG", ["structure", "wyckoff"], ["H4", "D1"])
    wm = _wm([
        _vote("structure", "SHORT", "H4"),
        _vote("wyckoff", "SHORT", "D1"),
    ])
    verdict, reason = sys._check_candidate_thesis("T1", "BUY", wm)
    assert verdict == "CLOSE"
    assert reason == "thesis_invalidated"


# ── 5. panel silent → CLOSE thesis_silent ─────────────────────────────────────


def test_thesis_silent_closes_conservative():
    sys = _system()
    sys._candidate_positions["T1"] = _prov("LONG", ["structure", "wyckoff"], ["H4", "D1"])
    # Contributing modules now produce only NEUTRAL reads (no directional vote).
    wm = _wm([
        _vote("structure", "NEUTRAL", "H4"),
        _vote("momentum", "LONG", "M5"),  # off-panel, must not rescue the thesis
    ])
    verdict, reason = sys._check_candidate_thesis("T1", "BUY", wm)
    assert verdict == "CLOSE"
    assert reason == "thesis_silent"


# ── 6. disabled flag / no provenance → fall through (None) ────────────────────


def test_disabled_flag_returns_none():
    sys = _system(scoped_enabled=False)
    sys._candidate_positions["T1"] = _prov("LONG", ["structure"], ["H4"])
    wm = _wm([_vote("structure", "SHORT", "H4")])
    assert sys._check_candidate_thesis("T1", "BUY", wm) is None


def test_no_provenance_returns_none():
    sys = _system()
    wm = _wm([_vote("structure", "SHORT", "H4")])
    assert sys._check_candidate_thesis("UNKNOWN", "BUY", wm) is None


# ── 7. opposing positions on one symbol managed independently ─────────────────


def test_opposing_positions_managed_independently():
    sys = _system()
    # LONG swing managed by H4 structure + D1 wyckoff.
    sys._candidate_positions["LONG_SWING"] = _prov(
        "LONG", ["structure", "wyckoff"], ["H4", "D1"],
    )
    # SHORT scalp managed by M5 momentum + M5 order_block.
    sys._candidate_positions["SHORT_SCALP"] = _prov(
        "SHORT", ["momentum", "order_block"], ["M5"],
    )
    wm = _wm([
        # Swing panel still bullish → swing HOLDs.
        _vote("structure", "LONG", "H4"),
        _vote("wyckoff", "LONG", "D1"),
        # Scalp panel turned bullish → opposes the SHORT scalp → invalidated.
        _vote("momentum", "LONG", "M5"),
        _vote("order_block", "LONG", "M5"),
    ])
    swing = sys._check_candidate_thesis("LONG_SWING", "LONG", wm)
    scalp = sys._check_candidate_thesis("SHORT_SCALP", "SHORT", wm)
    assert swing[0] == "HOLD"            # swing untouched by scalp-panel reads
    assert scalp == ("CLOSE", "thesis_invalidated")


# ── 8. trade journal persists + reads candidate provenance ────────────────────


def test_trade_journal_round_trips_candidate_metadata():
    async def _run() -> dict:
        with tempfile.TemporaryDirectory() as d:
            journal = TradeJournal(db_path=str(Path(d) / "tj.db"))
            await journal.log_trade(TradeRecord(
                pair="EURUSD", direction="LONG", entry=1.10, exit=1.11,
                pnl=10.0, score=0, confluences=[], regime="trending",
                session="LONDON", spread=0.0, slippage=0.0,
                entry_type="event_driven", time_to_tp1=0.0, time_to_exit=0.0,
                outcome="WIN", pnl_dollars=100.0, source="zone",
                candidate_id="cand-xyz", timeframe_class="SWING",
                candidate_score=0.82,
                contributing_modules=["structure", "wyckoff"],
                competing_candidates=3, regime_at_entry="trending",
            ))
            trades = await journal.get_all_trades_as_dicts()
            return trades[0]

    row = asyncio.run(_run())
    assert row["candidate_id"] == "cand-xyz"
    assert row["timeframe_class"] == "SWING"
    assert abs(float(row["candidate_score"]) - 0.82) < 1e-9
    assert row["contributing_modules"] == ["structure", "wyckoff"]
    assert int(row["competing_candidates"]) == 3
    assert row["regime_at_entry"] == "trending"
    assert row["source"] == "zone"


# ── 9. min-live-votes threshold respected ─────────────────────────────────────


def test_min_live_votes_threshold():
    sys = _system(min_live=2)
    sys._candidate_positions["T1"] = _prov("LONG", ["structure", "wyckoff"], ["H4", "D1"])
    # Only ONE directional contributing vote — below the min_live=2 threshold.
    wm = _wm([
        _vote("structure", "LONG", "H4"),
        _vote("wyckoff", "NEUTRAL", "D1"),
    ])
    verdict, reason = sys._check_candidate_thesis("T1", "BUY", wm)
    assert verdict == "CLOSE"
    assert reason == "thesis_silent"
