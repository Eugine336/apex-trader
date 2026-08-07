"""
APEX TRADER — Out-of-Sample Validation Gate Tests
Verifies that candidate scoring weights are adopted only when they
do not degrade win/loss discrimination on held-out (later-in-time) trades.
"""

import pytest

from adaptive.score_optimizer import ScoreOptimizer, ScoringWeights


def _make_trade(
    pnl: float,
    confluences_tags: list[str],
    timestamp: str | None = None,
) -> dict:
    return {
        "pair": "EURUSD",
        "pnl": pnl,
        "session": "LONDON",
        "regime": "TRENDING_STRONG",
        "entry_type": "FVG",
        "score": 90,
        "spread": 1.2,
        "time_to_exit": 25.0,
        "confluences_tags": confluences_tags,
        "timestamp": timestamp,
    }


def _build_predictive_dataset(n_train: int = 70, n_val: int = 30) -> list[dict]:
    """
    fvg is genuinely predictive in BOTH train and validation:
    wins always have fvg; losses never do.
    """
    trades: list[dict] = []
    for i in range(n_train):
        is_win = i % 2 == 0
        pnl = 15.0 if is_win else -10.0
        tags = ["structure", "fvg", "session"] if is_win else ["structure", "session"]
        trades.append(_make_trade(pnl, tags, timestamp=f"2025-01-{i + 1:02d}T00:00:00Z"))
    for i in range(n_val):
        is_win = i % 2 == 0
        pnl = 12.0 if is_win else -8.0
        tags = ["structure", "fvg", "session"] if is_win else ["structure", "session"]
        trades.append(_make_trade(pnl, tags, timestamp=f"2025-03-{i + 1:02d}T00:00:00Z"))
    return trades


def _build_overfit_dataset(n_train: int = 70, n_val: int = 30) -> list[dict]:
    """
    fvg appears predictive in train but REVERSES in validation:
    train wins have fvg, validation LOSSES have fvg.
    """
    trades: list[dict] = []
    for i in range(n_train):
        is_win = i % 2 == 0
        pnl = 15.0 if is_win else -10.0
        tags = ["structure", "fvg", "session"] if is_win else ["structure", "session"]
        trades.append(_make_trade(pnl, tags, timestamp=f"2025-01-{i + 1:02d}T00:00:00Z"))
    for i in range(n_val):
        is_win = i % 2 == 0
        pnl = 12.0 if is_win else -8.0
        tags = ["structure", "session"] if is_win else ["structure", "fvg", "session"]
        trades.append(_make_trade(pnl, tags, timestamp=f"2025-03-{i + 1:02d}T00:00:00Z"))
    return trades


class TestOOSValidationGate:
    def test_genuinely_predictive_factor_adopted(self, tmp_path):
        """Factor predictive in both halves -> candidate ADOPTED."""
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "weights.json")
        trades = _build_predictive_dataset()
        result = opt.optimize(trades, min_trades=50)
        assert result.as_dict() != ScoringWeights().as_dict()
        assert result.total == 123
        assert (tmp_path / "weights.json").exists()

    def test_overfit_trap_rejected(self, tmp_path):
        """Factor reverses in validation -> candidate REJECTED, incumbent retained."""
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "weights.json")
        original = ScoringWeights()
        trades = _build_overfit_dataset()
        result = opt.optimize(trades, min_trades=50)
        assert result.as_dict() == original.as_dict()

    def test_no_write_on_rejection(self, tmp_path):
        """Rejected candidate must not be persisted to disk."""
        fp = tmp_path / "weights.json"
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(fp)
        trades = _build_overfit_dataset()
        opt.optimize(trades, min_trades=50)
        assert not fp.exists()

    def test_small_validation_fallback(self, tmp_path):
        """Below MIN_VALIDATION -> falls back to fit-on-all without OOS gate."""
        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "weights.json")
        trades = _build_predictive_dataset(n_train=25, n_val=10)
        result = opt.optimize(trades, min_trades=30)
        assert result.total == 123
        assert (tmp_path / "weights.json").exists()

    def test_deterministic_decision(self):
        """Same input twice -> identical result."""
        trades = _build_predictive_dataset()
        opt1 = ScoreOptimizer()
        opt2 = ScoreOptimizer()
        r1 = opt1.optimize(trades, min_trades=50)
        r2 = opt2.optimize(trades, min_trades=50)
        assert r1.as_dict() == r2.as_dict()

    def test_validation_metric_perfect_discrimination(self):
        """Weights that perfectly separate wins from losses -> high positive metric."""
        weights = ScoringWeights(
            fvg_weight=40,
            structure_weight=15,
            ob_h1_weight=10,
            ob_m5_weight=10,
            mtf_confluence_weight=10,
            session_weight=8,
            news_weight=8,
            currency_strength_weight=7,
            liquidity_sweep_weight=5,
            volume_weight=4,
            inducement_weight=3,
            wyckoff_weight=3,
        )
        trades = [
            _make_trade(10.0, ["fvg"]),
            _make_trade(10.0, ["fvg"]),
            _make_trade(-5.0, ["session"]),
            _make_trade(-5.0, ["session"]),
        ]
        metric = ScoreOptimizer._compute_validation_metric(weights, trades)
        assert metric == pytest.approx(45.0)

    def test_validation_metric_no_discrimination(self):
        """All wins or all losses -> returns 0.0 (no separation measurable)."""
        weights = ScoringWeights()
        all_wins = [_make_trade(10.0, ["fvg"]) for _ in range(5)]
        assert ScoreOptimizer._compute_validation_metric(weights, all_wins) == 0.0
        all_losses = [_make_trade(-5.0, ["fvg"]) for _ in range(5)]
        assert ScoreOptimizer._compute_validation_metric(weights, all_losses) == 0.0

    def test_sort_by_time_with_timestamps(self):
        """Trades with timestamps are sorted ascending."""
        trades = [
            _make_trade(10.0, ["fvg"], "2025-03-01T00:00:00Z"),
            _make_trade(-5.0, ["fvg"], "2025-01-01T00:00:00Z"),
            _make_trade(8.0, ["fvg"], "2025-02-01T00:00:00Z"),
        ]
        result = ScoreOptimizer._sort_by_time(trades)
        assert result[0]["timestamp"] == "2025-01-01T00:00:00Z"
        assert result[1]["timestamp"] == "2025-02-01T00:00:00Z"
        assert result[2]["timestamp"] == "2025-03-01T00:00:00Z"

    def test_sort_by_time_without_timestamps(self):
        """Without timestamps, input order preserved (shallow copy)."""
        trades = [
            _make_trade(10.0, ["fvg"]),
            _make_trade(-5.0, ["session"]),
        ]
        result = ScoreOptimizer._sort_by_time(trades)
        assert result[0] is trades[0]
        assert result[1] is trades[1]
        assert result is not trades

    def test_adopted_weights_within_envelope(self, tmp_path):
        """Both adoption and fallback produce per-factor weights within ±25% of baseline."""
        baseline = ScoringWeights()
        bd = baseline.as_dict()

        opt = ScoreOptimizer()
        opt.DEFAULT_PATH = str(tmp_path / "w1.json")
        r1 = opt.optimize(_build_predictive_dataset(), min_trades=50)
        r1d = r1.as_dict()
        for key in r1d:
            lo = round(bd[key] * 0.75)
            hi = round(bd[key] * 1.25)
            assert lo <= r1d[key] <= hi, f"OOS path: {key}={r1d[key]} outside [{lo},{hi}]"

        opt2 = ScoreOptimizer()
        opt2.DEFAULT_PATH = str(tmp_path / "w2.json")
        small = _build_predictive_dataset(n_train=25, n_val=10)
        r2 = opt2.optimize(small, min_trades=30)
        r2d = r2.as_dict()
        for key in r2d:
            lo = round(bd[key] * 0.75)
            hi = round(bd[key] * 1.25)
            assert lo <= r2d[key] <= hi, f"fallback path: {key}={r2d[key]} outside [{lo},{hi}]"
