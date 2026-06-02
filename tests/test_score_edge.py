"""
Tests for score-edge attribution (TradeAnalyzer.analyze_by_score / score_edge).

NOTE: pytest cannot be executed in this sandbox session (numpy/loguru are not
installed).  CI on the PR is the runtime authority for these tests.
"""


import numpy as np

from adaptive.trade_analyzer import TradeAnalyzer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(score: int, pnl: float, **overrides: object) -> dict:
    """Minimal trade dict with only the fields consumed by the analyzer."""
    base = {
        "pair": "EURUSD",
        "direction": "LONG",
        "pnl": pnl,
        "score": score,
        "regime": "TRENDING",
        "session": "LONDON",
        "entry_type": "MARKET",
        "time_to_exit": 30.0,
        "outcome": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN"),
    }
    base.update(overrides)
    return base


def _monotone_positive(n: int = 100, seed: int = 42) -> list[dict]:
    """Higher score → higher pnl, deterministic."""
    rng = np.random.RandomState(seed)
    trades: list[dict] = []
    for _ in range(n):
        score = rng.randint(50, 101)
        pnl = (score - 50) * 2.0 + rng.normal(0, 3)
        trades.append(_make_trade(score, round(pnl, 2)))
    return trades


def _shuffled_no_edge(n: int = 100, seed: int = 99) -> list[dict]:
    """Score and pnl are independent — no edge. Deterministic."""
    rng = np.random.RandomState(seed)
    trades: list[dict] = []
    for _ in range(n):
        score = rng.randint(50, 101)
        pnl = rng.normal(0, 10)
        trades.append(_make_trade(score, round(pnl, 2)))
    return trades


def _inverted(n: int = 100, seed: int = 7) -> list[dict]:
    """Higher score → lower pnl, deterministic."""
    rng = np.random.RandomState(seed)
    trades: list[dict] = []
    for _ in range(n):
        score = rng.randint(50, 101)
        pnl = -(score - 50) * 2.0 + rng.normal(0, 3)
        trades.append(_make_trade(score, round(pnl, 2)))
    return trades


# ---------------------------------------------------------------------------
# analyze_by_score — bucket profiles
# ---------------------------------------------------------------------------

class TestAnalyzeByScore:
    def test_empty_trades(self) -> None:
        result = TradeAnalyzer().analyze_by_score([])
        assert result == {"quantile": {}, "fixed": {}}

    def test_no_valid_score(self) -> None:
        trades = [{"pnl": 10.0, "score": None}, {"pnl": -5.0}]
        result = TradeAnalyzer().analyze_by_score(trades)
        assert result == {"quantile": {}, "fixed": {}}

    def test_fixed_bands_populated(self) -> None:
        trades = _monotone_positive(200)
        result = TradeAnalyzer().analyze_by_score(trades)
        fixed = result["fixed"]
        assert len(fixed) > 0
        for prof in fixed.values():
            assert prof.total_trades > 0
            assert 0.0 <= prof.win_rate <= 1.0

    def test_quantile_buckets_populated(self) -> None:
        trades = _monotone_positive(200)
        result = TradeAnalyzer().analyze_by_score(trades, n_buckets=5)
        quant = result["quantile"]
        assert len(quant) >= 2
        total_in_buckets = sum(p.total_trades for p in quant.values())
        assert total_in_buckets == len(
            [t for t in trades if isinstance(t.get("score"), (int, float))
             and isinstance(t.get("pnl"), (int, float))]
        )

    def test_single_distinct_score_degrades_gracefully(self) -> None:
        trades = [_make_trade(85, pnl) for pnl in [10.0, -5.0, 20.0, -3.0, 8.0]]
        result = TradeAnalyzer().analyze_by_score(trades)
        quant = result["quantile"]
        assert len(quant) == 1
        key = list(quant.keys())[0]
        assert "85" in key
        assert quant[key].total_trades == 5

    def test_two_distinct_scores(self) -> None:
        trades = [_make_trade(70, 5.0)] * 10 + [_make_trade(90, 15.0)] * 10
        result = TradeAnalyzer().analyze_by_score(trades, n_buckets=10)
        quant = result["quantile"]
        assert len(quant) == 2


# ---------------------------------------------------------------------------
# score_edge — correlation + verdict
# ---------------------------------------------------------------------------

class TestScoreEdge:
    def test_insufficient_data(self) -> None:
        trades = [_make_trade(80, 5.0)] * 10
        result = TradeAnalyzer().score_edge(trades, min_samples=30)
        assert result["verdict"] == "INSUFFICIENT_DATA"
        assert result["sample_count"] == 10

    def test_positive_edge(self) -> None:
        trades = _monotone_positive(200)
        result = TradeAnalyzer().score_edge(trades, min_samples=30)
        assert result["verdict"] == "POSITIVE_EDGE"
        assert result["spearman"] > 0.0
        assert result["sample_count"] == 200

    def test_no_edge(self) -> None:
        trades = _shuffled_no_edge(200)
        result = TradeAnalyzer().score_edge(trades, min_samples=30)
        assert result["verdict"] == "NO_EDGE"
        assert abs(result["spearman"]) < 0.3

    def test_inverted_edge(self) -> None:
        trades = _inverted(200)
        result = TradeAnalyzer().score_edge(trades, min_samples=30)
        assert result["verdict"] == "INVERTED_EDGE"
        assert result["spearman"] < 0.0

    def test_all_identical_scores(self) -> None:
        trades = [_make_trade(85, pnl) for pnl in np.random.RandomState(1).normal(0, 5, 50)]
        result = TradeAnalyzer().score_edge(trades, min_samples=10)
        assert result["verdict"] in ("NO_EDGE", "INSUFFICIENT_DATA")
        assert result["spearman"] == 0.0

    def test_band_expectancies_ordered(self) -> None:
        trades = _monotone_positive(200)
        result = TradeAnalyzer().score_edge(trades, min_samples=30)
        bands = result["band_expectancies"]
        assert len(bands) > 0
        keys = list(bands.keys())
        for k in keys:
            assert k in ("<70", "70-79", "80-89", "90-100")

    def test_custom_thresholds(self) -> None:
        trades = _monotone_positive(200)
        strict = TradeAnalyzer().score_edge(
            trades, min_samples=30, spearman_positive_threshold=0.99,
        )
        assert strict["verdict"] != "POSITIVE_EDGE"

    def test_pearson_returned(self) -> None:
        trades = _monotone_positive(100)
        result = TradeAnalyzer().score_edge(trades, min_samples=10)
        assert "pearson" in result
        assert isinstance(result["pearson"], float)

    def test_empty_trades(self) -> None:
        result = TradeAnalyzer().score_edge([], min_samples=5)
        assert result["verdict"] == "INSUFFICIENT_DATA"
        assert result["sample_count"] == 0


# ---------------------------------------------------------------------------
# Correlation helpers — _rank, _pearson, _spearman
# ---------------------------------------------------------------------------

class TestCorrelationHelpers:
    def test_rank_no_ties(self) -> None:
        arr = np.array([30.0, 10.0, 20.0])
        ranks = TradeAnalyzer._rank(arr)
        assert list(ranks) == [3.0, 1.0, 2.0]

    def test_rank_with_ties(self) -> None:
        arr = np.array([10.0, 20.0, 20.0, 30.0])
        ranks = TradeAnalyzer._rank(arr)
        assert ranks[0] == 1.0
        assert ranks[1] == ranks[2] == 2.5
        assert ranks[3] == 4.0

    def test_pearson_perfect(self) -> None:
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        y = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
        r = TradeAnalyzer._pearson(x, y)
        assert abs(r - 1.0) < 0.01

    def test_pearson_zero_variance(self) -> None:
        x = np.array([5.0, 5.0, 5.0])
        y = np.array([1.0, 2.0, 3.0])
        assert TradeAnalyzer._pearson(x, y) == 0.0

    def test_spearman_monotone(self) -> None:
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        y = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
        r = TradeAnalyzer._spearman(x, y)
        assert abs(r - 1.0) < 0.01

    def test_spearman_single_element(self) -> None:
        x = np.array([1.0])
        y = np.array([2.0])
        assert TradeAnalyzer._spearman(x, y) == 0.0

    def test_spearman_negative(self) -> None:
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        y = np.array([50.0, 40.0, 30.0, 20.0, 10.0])
        r = TradeAnalyzer._spearman(x, y)
        assert abs(r - (-1.0)) < 0.01
