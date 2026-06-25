"""
Session 3 — Smarter Truncation (collapse points #12, #13, #15, #21, #27).

These tests pin the richer, non-truncating behaviour while proving the legacy
paths are unchanged when the new evidence is absent / the flags are off.

Dependency-light: pure vote math, dataclasses and pandas only (no torch).
"""

import pandas as pd
import pytest

from brain.directional_consensus import Vote
from brain.opportunity_ranker import (
    SCALP,
    SWING,
    classify_timeframe,
    cluster_votes,
    rank_opportunities,
)
from config import OpportunityRankerConfig, ScoringConfig
from management.opportunity_executor import OpportunityExecutor, ExecutionOutcome
from trigger.entry_patterns import EntryPatternDetector


# ── #12 — timeframe-aware horizon classification ──────────────────────────

class TestTimeframeClassification:
    def test_module_name_fallback_unchanged(self):
        # No explicit timeframe → module-name map (legacy behaviour).
        assert classify_timeframe("momentum", ["momentum"], ["structure"]) == SCALP
        assert classify_timeframe("structure", ["momentum"], ["structure"]) == SWING
        assert classify_timeframe("mystery", ["momentum"], ["structure"]) == SWING

    def test_real_timeframe_overrides_module_name(self):
        # H1 momentum is a SWING idea despite "momentum" being a scalp module.
        assert classify_timeframe("momentum", ["momentum"], ["structure"], "H1") == SWING
        # M5 structure is a SCALP idea despite "structure" being a swing module.
        assert classify_timeframe("structure", ["momentum"], ["structure"], "M5") == SCALP

    def test_timeframe_case_insensitive(self):
        assert classify_timeframe("x", [], [], "m15") == SCALP
        assert classify_timeframe("x", [], [], " h4 ") == SWING

    def test_vote_clusters_by_direction(self):
        # Clustering is by DIRECTION only now (informational horizon is derived
        # separately); a single LONG vote forms one LONG cluster.
        votes = [Vote("momentum", "LONG", 0.8, 1.0, timeframe="H1")]
        clusters = cluster_votes(votes)
        assert list(clusters.keys()) == ["LONG"]

    def test_opportunity_exposes_real_timeframes(self):
        votes = [
            Vote("momentum", "LONG", 0.8, 1.0, timeframe="M5"),
            Vote("vwap", "LONG", 0.7, 1.0, timeframe="M15"),
        ]
        opp = rank_opportunities(votes)[0]
        assert opp.timeframe_class == SCALP
        assert set(opp.timeframes) == {"M5", "M15"}


# ── #27 — richer win-prob (provenance + calibrated provider hook) ─────────

class TestWinProbProvenance:
    def test_components_recorded(self):
        opp = rank_opportunities([Vote("momentum", "LONG", 0.8, 1.0)])[0]
        comps = opp.win_prob_components
        assert comps["calibrated"] is False
        assert comps["base_win_rate"] == pytest.approx(0.40)
        # modelled_win_prob == base + gain term, and matches the scalar.
        assert comps["modelled_win_prob"] == pytest.approx(opp.win_prob)

    def test_provider_overrides_formula(self):
        def provider(direction, tf):
            return 0.9 if direction == "LONG" else None

        opp = rank_opportunities(
            [Vote("momentum", "LONG", 0.5, 1.0)], win_rate_provider=provider,
        )[0]
        assert opp.win_prob == pytest.approx(0.9)
        assert opp.win_prob_components["calibrated"] is True

    def test_provider_none_falls_back(self):
        def provider(direction, tf):
            return None  # no calibrated value → modelled formula stands

        base = rank_opportunities([Vote("momentum", "LONG", 0.5, 1.0)])[0]
        with_hook = rank_opportunities(
            [Vote("momentum", "LONG", 0.5, 1.0)], win_rate_provider=provider,
        )[0]
        assert with_hook.win_prob == pytest.approx(base.win_prob)
        assert with_hook.win_prob_components["calibrated"] is False

    def test_bad_provider_never_breaks_ranking(self):
        def provider(direction, tf):
            raise RuntimeError("boom")

        opp = rank_opportunities(
            [Vote("momentum", "LONG", 0.5, 1.0)], win_rate_provider=provider,
        )[0]
        # Falls back to the modelled formula instead of crashing.
        assert 0.0 <= opp.win_prob <= 1.0
        assert opp.win_prob_components["calibrated"] is False


# ── #13 — ranker never truncates the tail ─────────────────────────────────

class TestNoTailTruncation:
    def test_all_clusters_survive_sorted(self):
        votes = [
            Vote("structure", "LONG", 0.9, 1.0),
            Vote("currency_strength", "LONG", 0.6, 1.0),
            Vote("momentum", "SHORT", 0.8, 1.0),
            Vote("volume", "SHORT", 0.7, 1.0),
            Vote("vwap", "LONG", 0.5, 1.0),
        ]
        opps = rank_opportunities(votes)
        # Direction-only clustering: one LONG idea + one SHORT idea — both
        # survive, sorted best-first (no horizon split, no tail truncation).
        assert len(opps) == 2
        assert {o.direction for o in opps} == {"LONG", "SHORT"}
        evs = [o.expected_value for o in opps]
        assert evs == sorted(evs, reverse=True)


# ── #13 — executor preserves discarded alternatives, capacity override ────

class TestExecutorAlternatives:
    def _opps(self):
        return rank_opportunities([
            Vote("structure", "LONG", 0.9, 1.0),
            Vote("currency_strength", "LONG", 0.6, 1.0),
            Vote("momentum", "SHORT", 0.8, 1.0),
            Vote("volume", "SHORT", 0.7, 1.0),
        ])

    def test_select_top_respects_max_concurrent(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(max_concurrent=1))
        chosen = ex.select_top(self._opps())
        assert len(chosen) == 1

    def test_select_top_override(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(max_concurrent=1))
        chosen = ex.select_top(self._opps(), max_concurrent=2)
        assert len(chosen) == 2

    def test_execute_records_alternatives(self):
        ex = OpportunityExecutor(OpportunityRankerConfig(execute=True, max_concurrent=1))
        opps = self._opps()
        outcome = ex.execute(opps, dispatch=lambda o: True)
        assert isinstance(outcome, ExecutionOutcome)
        assert outcome.executed == 1
        # The un-selected ranked tail is preserved, not silently dropped.
        assert len(outcome.selected) == 1
        assert len(outcome.alternatives) == len(opps) - 1


# ── #15 — capacity-aware dispatch config ──────────────────────────────────

class TestDispatchConfig:
    def test_defaults_are_live(self):
        cfg = OpportunityRankerConfig()
        assert cfg.dispatch_top_n == 3
        assert cfg.slot_aware_dispatch is True

    def test_invalid_dispatch_rejected(self):
        with pytest.raises(ValueError):
            OpportunityRankerConfig(dispatch_top_n=0)
        with pytest.raises(ValueError):
            OpportunityRankerConfig(dispatch_max_n=0)


# ── #21 — entry pattern confluence preserved ──────────────────────────────

def _bullish_engulfing_pinbar_df():
    # Construct M1 candles where the last bar is a bullish engulfing that is
    # ALSO a bullish pin bar (long lower wick, small body near the top).
    rows = [
        {"open": 1.1000, "high": 1.1005, "low": 1.0995, "close": 1.0998, "volume": 100},
        {"open": 1.0998, "high": 1.1002, "low": 1.0990, "close": 1.0992, "volume": 100},  # bearish prev
        {"open": 1.0991, "high": 1.1012, "low": 1.0980, "close": 1.1010, "volume": 100},  # bullish engulf + lower wick
    ]
    return pd.DataFrame(rows)


class TestPatternConfluence:
    def test_get_best_pattern_unchanged(self):
        det = EntryPatternDetector()
        df = _bullish_engulfing_pinbar_df()
        name, desc = det.get_best_pattern(df, "LONG", 1.1010, 1.0990, 0.0001)
        # Strongest single pattern still wins (legacy contract).
        assert name == "engulfing"

    def test_get_all_patterns_keeps_co_occurring(self):
        det = EntryPatternDetector()
        df = _bullish_engulfing_pinbar_df()
        matches = det.get_all_patterns(df, "LONG", 1.1010, 1.0990, 0.0001)
        names = {m.name for m in matches}
        # More than one confirmation co-occurs — none discarded.
        assert "engulfing" in names
        assert len(matches) >= 2
        # Sorted strongest-first.
        strengths = [m.strength for m in matches]
        assert strengths == sorted(strengths, reverse=True)

    def test_confluence_matches_best_pattern(self):
        det = EntryPatternDetector()
        df = _bullish_engulfing_pinbar_df()
        best_name, best_desc, matches = det.get_pattern_confluence(
            df, "LONG", 1.1010, 1.0990, 0.0001,
        )
        legacy_name, legacy_desc = det.get_best_pattern(df, "LONG", 1.1010, 1.0990, 0.0001)
        assert (best_name, best_desc) == (legacy_name, legacy_desc)
        assert matches[0].name == best_name

    def test_no_pattern_returns_empty(self):
        det = EntryPatternDetector()
        flat = pd.DataFrame([
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
        ])
        # Zone far from price so no rejection-wick is triggered either.
        assert det.get_pattern_confluence(flat, "LONG", 2.0, 1.9, 0.0001) == ("", "", [])


# ── #21 — scoring confluence flag is opt-in ───────────────────────────────

class TestScoringConfluenceFlag:
    def test_default_on(self):
        cfg = ScoringConfig()
        assert cfg.pattern_confluence_bonus is True
        assert cfg.pattern_confluence_max_bonus == 2
