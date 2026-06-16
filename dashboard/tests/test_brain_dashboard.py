"""
Dashboard Brain panels — module votes + opportunity ranker exposure tests.

Verifies BrainMixin reads the live scan report, serialises the per-module
``Vote`` grid and the ranker's ``Opportunity`` clusters, and computes the
aggregates the panels render.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from brain.directional_consensus import Vote
from brain.opportunity_ranker import rank_opportunities
from dashboard.state_brain import BrainMixin


def _make_state(results, *, live=True, execute=False):
    """A minimal BrainMixin host wired to a fake trading loop + scan report."""
    config = SimpleNamespace(
        opportunity_ranker=SimpleNamespace(
            scalp_modules=None, swing_modules=None, execute=execute,
        )
    )
    report = SimpleNamespace(results=results)
    scanner = SimpleNamespace(last_report=report)
    loop = SimpleNamespace(scanner=scanner, config=config)

    class FakeState(BrainMixin):
        def __init__(self_inner):
            self_inner._trading_loop = loop

        @property
        def is_live(self_inner):
            return live

    return FakeState()


def _panel_result(pair, votes, *, direction="NEUTRAL", consensus="NEUTRAL"):
    candidates = rank_opportunities(votes)
    return SimpleNamespace(
        pair=pair,
        votes=votes,
        candidates=candidates,
        direction=direction,
        consensus_direction=consensus,
        consensus_net=sum(v.signed for v in votes),
        consensus_agreement=0.6,
        selected_horizon=candidates[0].timeframe_class if candidates else "",
    )


# Mixed panel: slow modules LONG, fast modules SHORT — the canonical case the
# scalar consensus would dissolve to NEUTRAL but the ranker preserves both.
def _mixed_votes():
    return [
        Vote("structure", "LONG", 0.85, 3.0),
        Vote("currency_strength", "LONG", 0.70, 2.0),
        Vote("momentum", "SHORT", 0.90, 1.0),
        Vote("vwap", "SHORT", 0.80, 1.0),
        Vote("liquidity", "SHORT", 0.85, 1.0),
        Vote("volume", "NEUTRAL", 0.0, 0.0),
    ]


class TestModuleVotes:
    def test_idle_returns_empty(self):
        state = _make_state([], live=False)
        out = state.get_module_votes()
        assert out["pairs"] == []
        assert out["modules"] == []
        assert out["source"] == "idle"

    def test_votes_serialized_per_pair(self):
        results = [_panel_result("EURUSD", _mixed_votes(), consensus="NEUTRAL")]
        state = _make_state(results)
        out = state.get_module_votes()
        assert out["pair_count"] == 1
        pair = out["pairs"][0]
        assert pair["pair"] == "EURUSD"
        # 6 votes built; NEUTRAL ones are still serialised in the grid.
        assert len(pair["votes"]) == 6
        modules_seen = {v["module"]: v for v in pair["votes"]}
        assert modules_seen["structure"]["direction"] == "LONG"
        assert modules_seen["momentum"]["direction"] == "SHORT"
        assert pair["long_count"] == 2
        assert pair["short_count"] == 3
        assert pair["neutral_count"] == 1

    def test_horizon_classification(self):
        results = [_panel_result("EURUSD", _mixed_votes())]
        state = _make_state(results)
        out = state.get_module_votes()
        vm = {v["module"]: v["horizon"] for v in out["pairs"][0]["votes"]}
        assert vm["momentum"] == "SCALP"
        assert vm["structure"] == "SWING"

    def test_module_participation_aggregate(self):
        results = [
            _panel_result("EURUSD", _mixed_votes()),
            _panel_result("GBPUSD", _mixed_votes()),
        ]
        state = _make_state(results)
        out = state.get_module_votes()
        mods = {m["module"]: m for m in out["modules"]}
        # structure voted LONG on both pairs.
        assert mods["structure"]["long"] == 2
        assert mods["structure"]["participation"] == 2
        assert mods["momentum"]["short"] == 2
        assert mods["volume"]["participation"] == 0


class TestRanker:
    def test_idle_returns_empty(self):
        state = _make_state([], live=False)
        out = state.get_ranker()
        assert out["pairs"] == []
        assert out["source"] == "idle"

    def test_preserves_both_clusters(self):
        results = [_panel_result("EURUSD", _mixed_votes(), consensus="NEUTRAL")]
        state = _make_state(results)
        out = state.get_ranker()
        assert out["pair_count"] == 1
        pair = out["pairs"][0]
        # Both a LONG swing and a SHORT scalp survive as independent ideas.
        dirs = {(o["direction"], o["timeframe_class"]) for o in pair["opportunities"]}
        assert ("LONG", "SWING") in dirs
        assert ("SHORT", "SCALP") in dirs
        # Best-first ordering by EV.
        evs = [o["expected_value"] for o in pair["opportunities"]]
        assert evs == sorted(evs, reverse=True)
        assert pair["best"]["expected_value"] == max(evs)

    def test_override_flag_when_live_and_diverges(self):
        results = [_panel_result("EURUSD", _mixed_votes(), consensus="LONG")]
        state = _make_state(results, execute=True)
        out = state.get_ranker()
        pair = out["pairs"][0]
        # Best idea direction differs from the LONG scalar consensus → override.
        if pair["best"]["direction"] != "LONG":
            assert pair["ranker_override"] is True
            assert out["override_count"] == 1

    def test_no_override_in_shadow_mode(self):
        results = [_panel_result("EURUSD", _mixed_votes(), consensus="LONG")]
        state = _make_state(results, execute=False)
        out = state.get_ranker()
        assert out["execute"] is False
        assert out["pairs"][0]["ranker_override"] is False

    def test_aggregates(self):
        results = [_panel_result("EURUSD", _mixed_votes())]
        state = _make_state(results)
        out = state.get_ranker()
        assert out["total_opportunities"] >= 2
        assert out["horizon_distribution"]["SCALP"] >= 1
        assert out["horizon_distribution"]["SWING"] >= 1
        assert isinstance(out["avg_expected_value"], float)
