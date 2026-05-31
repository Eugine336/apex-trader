"""
APEX TRADER — Trade Analyzer
Dissects every trade to find the edge.
Which pairs win most? Which sessions? Which entry types?
The answers are in the data — I just have to look.
"""

from dataclasses import dataclass, field
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
                    wr = wins / len(group)
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

        win_rate = len(winners) / total
        loss_rate = 1.0 - win_rate
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
