"""
Tests for the Outcome Feedback loop.

Dependency-light — JSONL persistence + join math, no torch/pandas/numpy.
Verifies entry→outcome linkage by trade key, per-module and per-horizon
accuracy aggregation, confidence calibration, and that unmatched halves are
ignored rather than crashing.
"""

import pytest

from brain.outcome_feedback import OutcomeFeedback


class _Cfg:
    enabled = True
    accuracy_lookback = 100

    def __init__(self, path):
        self.journal_path = path


@pytest.fixture
def fb(tmp_path):
    return OutcomeFeedback(_Cfg(str(tmp_path / "fb.jsonl")))


class TestRecordingAndJoin:
    def test_entry_then_outcome_joins(self, fb):
        fb.record_entry("ord1", {"horizon": "SCALP", "votes": {"momentum": ["LONG", 0.9]}})
        fb.record_outcome("ord1", {"pnl_r": 1.5, "won": True})
        completed = fb.get_completed()
        assert len(completed) == 1
        assert completed[0]["trade_key"] == "ord1"
        assert completed[0]["outcome"]["pnl_r"] == 1.5

    def test_entry_without_outcome_not_completed(self, fb):
        fb.record_entry("ord1", {"horizon": "SCALP"})
        assert fb.get_completed() == []

    def test_outcome_without_entry_not_completed(self, fb):
        fb.record_outcome("ord1", {"pnl_r": 1.0})
        assert fb.get_completed() == []

    def test_empty_trade_key_ignored(self, fb):
        fb.record_entry("", {"horizon": "SCALP"})
        fb.record_outcome("", {"pnl_r": 1.0})
        assert fb.get_completed() == []


class TestAccuracyAggregation:
    def _seed(self, fb):
        # momentum wins, structure loses; vwap on the winning trade too.
        fb.record_entry("a", {"horizon": "SCALP",
                              "votes": {"momentum": ["LONG", 0.9], "vwap": ["LONG", 0.8]}})
        fb.record_outcome("a", {"pnl_r": 2.0, "won": True})
        fb.record_entry("b", {"horizon": "SWING",
                              "votes": {"structure": ["SHORT", 0.7]}})
        fb.record_outcome("b", {"pnl_r": -1.0, "won": False})

    def test_overall_stats(self, fb):
        self._seed(fb)
        acc = fb.module_accuracy()
        assert acc["total_trades"] == 2
        assert acc["overall_win_rate"] == pytest.approx(0.5)
        assert acc["overall_avg_r"] == pytest.approx(0.5)

    def test_per_module_winrate(self, fb):
        self._seed(fb)
        acc = fb.module_accuracy()
        mods = {m["module"]: m for m in acc["modules"]}
        assert mods["momentum"]["win_rate"] == pytest.approx(1.0)
        assert mods["structure"]["win_rate"] == pytest.approx(0.0)

    def test_calibration_gap(self, fb):
        self._seed(fb)
        acc = fb.module_accuracy()
        mods = {m["module"]: m for m in acc["modules"]}
        # structure: predicted 0.7 conf, realised 0.0 win → overconfident (+0.7)
        assert mods["structure"]["calibration_gap"] == pytest.approx(0.7)
        # momentum: predicted 0.9, realised 1.0 → slightly underconfident (-0.1)
        assert mods["momentum"]["calibration_gap"] == pytest.approx(-0.1)

    def test_per_horizon_breakdown(self, fb):
        self._seed(fb)
        acc = fb.module_accuracy()
        hz = {h["horizon"]: h for h in acc["horizons"]}
        assert hz["SCALP"]["win_rate"] == pytest.approx(1.0)
        assert hz["SWING"]["win_rate"] == pytest.approx(0.0)

    def test_won_inferred_from_pnl_r_when_absent(self, fb):
        fb.record_entry("a", {"horizon": "SCALP", "votes": {"momentum": ["LONG", 0.5]}})
        fb.record_outcome("a", {"pnl_r": 1.2})  # no explicit "won"
        acc = fb.module_accuracy()
        assert acc["overall_win_rate"] == pytest.approx(1.0)

    def test_contributors_fallback_when_no_votes(self, fb):
        fb.record_entry("a", {"horizon": "SCALP", "contributors": ["liquidity", "fvg"]})
        fb.record_outcome("a", {"pnl_r": 1.0, "won": True})
        acc = fb.module_accuracy()
        names = {m["module"] for m in acc["modules"]}
        assert names == {"liquidity", "fvg"}
        # No confidence available → calibration gap is None, never a crash.
        for m in acc["modules"]:
            assert m["avg_confidence"] is None
            assert m["calibration_gap"] is None


class TestDisabled:
    def test_disabled_records_nothing(self, tmp_path):
        cfg = _Cfg(str(tmp_path / "fb.jsonl"))
        cfg.enabled = False
        fb = OutcomeFeedback(cfg)
        fb.record_entry("a", {"horizon": "SCALP"})
        fb.record_outcome("a", {"pnl_r": 1.0})
        assert fb.get_completed() == []
