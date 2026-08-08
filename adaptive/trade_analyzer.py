"""
APEX TRADER — Trade Analyzer
Dissects every trade to find the edge.
Which pairs win most? Which sessions? Which entry types?
The answers are in the data — I just have to look.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class PerformanceProfile:
    total_trades: int = 0
    win_rate: float = 0.0
    avg_pnl_pips: float = 0.0
    avg_winner_pips: float = 0.0
    avg_loser_pips: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    sharpe_ratio: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_hold_time_minutes: float = 0.0
    best_pair: Optional[str] = None
    worst_pair: Optional[str] = None
    best_session: Optional[str] = None
    best_regime: Optional[str] = None
    best_entry_type: Optional[str] = None


class TradeAnalyzer:
    """
    APEX TRADER — Trade Analyzer
    Dissects every trade to find the edge.
    Which pairs win most? Which sessions? Which entry types?
    The answers are in the data — I just have to look.
    """

    def analyze_all(self, trades: list[dict]) -> PerformanceProfile:
        if not trades:
            return PerformanceProfile()
        return self._build_profile(trades)

    def analyze_by_pair(self, trades: list[dict]) -> dict[str, PerformanceProfile]:
        return self._group_and_analyze(trades, "pair")

    def analyze_by_session(self, trades: list[dict]) -> dict[str, PerformanceProfile]:
        return self._group_and_analyze(trades, "session")

    def analyze_by_regime(self, trades: list[dict]) -> dict[str, PerformanceProfile]:
        return self._group_and_analyze(trades, "regime")

    def analyze_by_entry_type(self, trades: list[dict]) -> dict[str, PerformanceProfile]:
        return self._group_and_analyze(trades, "entry_type")

    def analyze_by_source(self, trades: list[dict]) -> dict[str, PerformanceProfile]:
        """Per entry-source-path performance (e.g. "zone" vs "consensus").

        Lets the learning loop compare how the structural zone path and the
        zoneless consensus trigger actually perform once their trades close.
        """
        return self._group_and_analyze(trades, "source")

    def analyze_by_day_of_week(self, trades: list[dict]) -> dict[str, PerformanceProfile]:
        return self._group_and_analyze(trades, "day_of_week")

    def get_losing_patterns(self, trades: list[dict]) -> list[dict]:
        """Identify combinations that consistently lose."""
        patterns: list[dict] = []
        dimensions = ["pair", "session", "regime", "entry_type"]

        for dim_a in dimensions:
            for dim_b in dimensions:
                if dim_a >= dim_b:
                    continue
                grouped: dict[tuple[str, str], list[dict]] = {}
                for t in trades:
                    key = (str(t.get(dim_a, "unknown")), str(t.get(dim_b, "unknown")))
                    grouped.setdefault(key, []).append(t)

                for (val_a, val_b), group in grouped.items():
                    if len(group) < 10:
                        continue
                    wins = sum(1 for t in group if t.get("pnl", 0) > 0)
                    losses = sum(1 for t in group if t.get("pnl", 0) < 0)
                    decided = wins + losses  # exclude scratch (pnl == 0)
                    wr = wins / decided if decided else 0.0
                    if wr < 0.45:
                        patterns.append({
                            "dimensions": {dim_a: val_a, dim_b: val_b},
                            "win_rate": round(wr, 4),
                            "sample_size": len(group),
                            "avg_pnl": round(
                                float(np.mean([t.get("pnl", 0) for t in group])), 4
                            ),
                        })

        patterns.sort(key=lambda p: p["win_rate"])
        return patterns

    # ------------------------------------------------------------------
    # Score-edge attribution
    # ------------------------------------------------------------------

    _FIXED_BANDS: list[tuple[str, int, int]] = [
        ("<70", 0, 69),
        ("70-79", 70, 79),
        ("80-89", 80, 89),
        ("90-100", 90, 100),
    ]

    def analyze_by_score(
        self,
        trades: list[dict],
        n_buckets: int = 10,
    ) -> dict[str, dict[str, PerformanceProfile]]:
        """Score-bucketed performance profiles.

        Returns ``{"quantile": {label: profile}, "fixed": {label: profile}}``.

        *quantile* — trades split into ``n_buckets`` equal-frequency bins by
        score (numpy ``percentile``).  Degrades gracefully when distinct
        scores < ``n_buckets``.

        *fixed* — trades grouped into the four canonical score bands
        ``<70 / 70-79 / 80-89 / 90-100``.
        """
        valid = [
            t for t in trades
            if isinstance(t.get("score"), (int, float))
            and isinstance(t.get("pnl"), (int, float))
        ]
        if not valid:
            return {"quantile": {}, "fixed": {}}

        scores = np.array([float(t["score"]) for t in valid])

        # --- quantile buckets ---
        distinct = np.unique(scores)
        effective_n = min(n_buckets, len(distinct))
        quantile_profiles: dict[str, PerformanceProfile] = {}
        if effective_n >= 2:
            edges = np.percentile(
                scores,
                np.linspace(0, 100, effective_n + 1),
            )
            edges[0] -= 1e-9
            for i in range(len(edges) - 1):
                lo, hi = edges[i], edges[i + 1]
                bucket = [t for t, s in zip(valid, scores) if lo < s <= hi]
                if bucket:
                    label = f"Q{i + 1} ({lo + 1e-9:.0f}–{hi:.0f})"
                    quantile_profiles[label] = self._build_profile(bucket)
        elif effective_n == 1:
            label = f"ALL ({distinct[0]:.0f})"
            quantile_profiles[label] = self._build_profile(valid)

        # --- fixed bands ---
        fixed_profiles: dict[str, PerformanceProfile] = {}
        for label, lo, hi in self._FIXED_BANDS:
            bucket = [t for t in valid if lo <= float(t["score"]) <= hi]
            if bucket:
                fixed_profiles[label] = self._build_profile(bucket)

        return {"quantile": quantile_profiles, "fixed": fixed_profiles}

    def score_edge(
        self,
        trades: list[dict],
        min_samples: int = 30,
        spearman_positive_threshold: float = 0.10,
        spearman_inverted_threshold: float = -0.10,
    ) -> dict:
        """Measure whether live entry *score* predicts realized *pnl*.

        Verdict thresholds (all configurable via parameters):
        * ``INSUFFICIENT_DATA`` — fewer than *min_samples* qualifying trades.
        * ``POSITIVE_EDGE``    — Spearman ≥ *spearman_positive_threshold*
          **and** the fixed-band expectancy sequence is broadly
          monotone-increasing (at most one inversion among adjacent bands).
        * ``INVERTED_EDGE``    — Spearman ≤ *spearman_inverted_threshold*.
        * ``NO_EDGE``          — everything else.

        Returns a dict with ``sample_count``, ``spearman``, ``pearson``,
        ``band_expectancies`` (ordered dict label→float),
        ``monotonicity_inversions`` (int), and ``verdict`` (str).
        """
        valid = [
            t for t in trades
            if isinstance(t.get("score"), (int, float))
            and isinstance(t.get("pnl"), (int, float))
        ]
        n = len(valid)
        if n < min_samples:
            return {
                "sample_count": n,
                "spearman": 0.0,
                "pearson": 0.0,
                "band_expectancies": {},
                "monotonicity_inversions": 0,
                "verdict": "INSUFFICIENT_DATA",
            }

        scores = np.array([float(t["score"]) for t in valid])
        pnls = np.array([float(t["pnl"]) for t in valid])

        spearman = self._spearman(scores, pnls)
        pearson = self._pearson(scores, pnls)

        # Per-band expectancy (ordered low→high).
        band_exp: dict[str, float] = {}
        for label, lo, hi in self._FIXED_BANDS:
            bucket_pnls = [float(t["pnl"]) for t in valid if lo <= float(t["score"]) <= hi]
            if bucket_pnls:
                band_exp[label] = round(float(np.mean(bucket_pnls)), 4)

        # Monotonicity: count adjacent-band inversions.
        exp_values = list(band_exp.values())
        inversions = sum(
            1 for i in range(len(exp_values) - 1) if exp_values[i + 1] < exp_values[i]
        )
        broadly_monotone = inversions <= 1

        if spearman >= spearman_positive_threshold and broadly_monotone:
            verdict = "POSITIVE_EDGE"
        elif spearman <= spearman_inverted_threshold:
            verdict = "INVERTED_EDGE"
        else:
            verdict = "NO_EDGE"

        return {
            "sample_count": n,
            "spearman": round(spearman, 4),
            "pearson": round(pearson, 4),
            "band_expectancies": band_exp,
            "monotonicity_inversions": inversions,
            "verdict": verdict,
        }

    # ------------------------------------------------------------------
    # Correlation helpers (Spearman via rank-then-Pearson; Pearson direct)
    # ------------------------------------------------------------------

    @staticmethod
    def _rank(arr: np.ndarray) -> np.ndarray:
        """Average-rank with tie-handling (identical to scipy.stats.rankdata)."""
        order = arr.argsort()
        ranks = np.empty_like(order, dtype=float)
        ranks[order] = np.arange(1, len(arr) + 1, dtype=float)
        # Average tied ranks.
        sorted_arr = arr[order]
        i = 0
        while i < len(sorted_arr):
            j = i
            while j < len(sorted_arr) and sorted_arr[j] == sorted_arr[i]:
                j += 1
            if j > i + 1:
                avg_rank = np.mean(ranks[order[i:j]])
                ranks[order[i:j]] = avg_rank
            i = j
        return ranks

    @staticmethod
    def _pearson(x: np.ndarray, y: np.ndarray) -> float:
        if len(x) < 2:
            return 0.0
        std_x = float(np.std(x, ddof=1))
        std_y = float(np.std(y, ddof=1))
        if std_x == 0.0 or std_y == 0.0:
            return 0.0
        mean_x = float(np.mean(x))
        mean_y = float(np.mean(y))
        cov = float(np.mean((x - mean_x) * (y - mean_y)))
        return cov / (std_x * std_y) * (len(x) / (len(x) - 1))

    @classmethod
    def _spearman(cls, x: np.ndarray, y: np.ndarray) -> float:
        if len(x) < 2:
            return 0.0
        rx = cls._rank(x)
        ry = cls._rank(y)
        return cls._pearson(rx, ry)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _group_and_analyze(
        self, trades: list[dict], key: str
    ) -> dict[str, PerformanceProfile]:
        grouped: dict[str, list[dict]] = {}
        for t in trades:
            val = str(t.get(key, "unknown"))
            grouped.setdefault(val, []).append(t)
        return {k: self._build_profile(v) for k, v in grouped.items()}

    def _build_profile(self, trades: list[dict]) -> PerformanceProfile:
        pnls = [float(t.get("pnl", 0)) for t in trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]
        total = len(pnls)
        if total == 0:
            return PerformanceProfile()

        # Scratch trades (pnl == 0) are excluded from the win/loss split so
        # breakeven outcomes are not miscounted as losses.
        decided = len(winners) + len(losers)
        win_rate = len(winners) / decided if decided else 0.0
        loss_rate = 1.0 - win_rate if decided else 0.0
        avg_win = float(np.mean(winners)) if winners else 0.0
        avg_loss = float(np.mean(losers)) if losers else 0.0
        gross_wins = sum(winners)
        gross_losses = abs(sum(losers))
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")
        expectancy = avg_win * win_rate + avg_loss * loss_rate

        hold_times = [
            float(t["time_to_exit"])
            for t in trades
            if t.get("time_to_exit") is not None
        ]
        avg_hold = float(np.mean(hold_times)) if hold_times else 0.0

        max_cw, max_cl = self._streaks(pnls)

        pair_pnl = self._best_worst_dim(trades, "pair")
        session_pnl = self._best_dim(trades, "session")
        regime_pnl = self._best_dim(trades, "regime")
        entry_pnl = self._best_dim(trades, "entry_type")

        return PerformanceProfile(
            total_trades=total,
            win_rate=round(win_rate, 4),
            avg_pnl_pips=round(float(np.mean(pnls)), 4),
            avg_winner_pips=round(avg_win, 4),
            avg_loser_pips=round(abs(avg_loss), 4),
            profit_factor=round(profit_factor, 4) if np.isfinite(profit_factor) else float("inf"),
            expectancy=round(expectancy, 4),
            sharpe_ratio=round(self._sharpe(pnls), 4),
            max_consecutive_wins=max_cw,
            max_consecutive_losses=max_cl,
            avg_hold_time_minutes=round(avg_hold, 2),
            best_pair=pair_pnl[0],
            worst_pair=pair_pnl[1],
            best_session=session_pnl,
            best_regime=regime_pnl,
            best_entry_type=entry_pnl,
        )

    @staticmethod
    def _sharpe(returns: list[float]) -> float:
        if len(returns) < 2:
            return 0.0
        std = float(np.std(returns, ddof=1))
        if std == 0:
            return 0.0
        return float(np.mean(returns) / std * np.sqrt(len(returns)))

    @staticmethod
    def _streaks(pnls: list[float]) -> tuple[int, int]:
        max_w = max_l = cur_w = cur_l = 0
        for p in pnls:
            if p > 0:
                cur_w += 1
                cur_l = 0
            elif p < 0:
                cur_l += 1
                cur_w = 0
            else:
                cur_w = cur_l = 0
            max_w = max(max_w, cur_w)
            max_l = max(max_l, cur_l)
        return max_w, max_l

    @staticmethod
    def _best_dim(trades: list[dict], key: str) -> Optional[str]:
        grouped: dict[str, list[float]] = {}
        for t in trades:
            grouped.setdefault(str(t.get(key, "unknown")), []).append(
                float(t.get("pnl", 0))
            )
        if not grouped:
            return None
        ranked = sorted(grouped.items(), key=lambda x: np.mean(x[1]), reverse=True)
        return ranked[0][0] if ranked else None

    @staticmethod
    def _best_worst_dim(
        trades: list[dict], key: str
    ) -> tuple[Optional[str], Optional[str]]:
        grouped: dict[str, list[float]] = {}
        for t in trades:
            grouped.setdefault(str(t.get(key, "unknown")), []).append(
                float(t.get("pnl", 0))
            )
        if not grouped:
            return None, None
        ranked = sorted(grouped.items(), key=lambda x: np.mean(x[1]), reverse=True)
        best = ranked[0][0] if ranked else None
        worst = ranked[-1][0] if ranked else None
        return best, worst
