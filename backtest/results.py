"""Backtest results capture, performance metrics and reporting.

Pure-Python / numpy metrics (no pandas, no plotting deps). Computes the standard
risk-adjusted statistics — Sharpe, Sortino, Calmar, max drawdown, profit factor,
expectancy — plus per-pair / per-regime / per-behaviour breakdowns and the full
trade log and equity curve.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass
class TradeRecord:
    """One completed round-trip trade."""

    pair: str
    direction: str
    lots: float
    open_price: float
    close_price: float
    open_time: datetime
    close_time: datetime
    pnl: float
    pnl_pips: float
    pnl_r: float
    exit_reason: str
    regime: str = "UNKNOWN"
    horizon: str = "SWING"
    profile: str = "default"

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.close_time - self.open_time).total_seconds())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["open_time"] = self.open_time.isoformat()
        d["close_time"] = self.close_time.isoformat()
        d["duration_sec"] = round(self.duration_sec, 1)
        return d


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


class BacktestResults:
    """Accumulates trades + equity curve and computes performance metrics."""

    def __init__(self, starting_balance: float) -> None:
        self.starting_balance = float(starting_balance)
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.risk_events: list[dict] = []
        self.blocked_signals: int = 0

    # ── Recording ────────────────────────────────────────────────────────

    def record_trade(self, trade: TradeRecord) -> None:
        self.trades.append(trade)

    def record_equity(self, when: datetime, equity: float) -> None:
        self.equity_curve.append((when, float(equity)))

    def record_risk_event(self, pair: str, rule: str, reason: str) -> None:
        self.risk_events.append({"pair": pair, "rule": rule, "reason": reason})
        self.blocked_signals += 1

    # ── Derived series ───────────────────────────────────────────────────

    @property
    def ending_balance(self) -> float:
        if self.equity_curve:
            return self.equity_curve[-1][1]
        return self.starting_balance + sum(t.pnl for t in self.trades)

    def _r_multiples(self) -> list[float]:
        return [t.pnl_r for t in self.trades]

    def _pnls(self) -> list[float]:
        return [t.pnl for t in self.trades]

    # ── Metrics ──────────────────────────────────────────────────────────

    def max_drawdown(self) -> tuple[float, int]:
        """Return (max drawdown fraction [0,1], longest drawdown length in points)."""
        if not self.equity_curve:
            return 0.0, 0
        eq = np.array([v for _, v in self.equity_curve], dtype=float)
        peaks = np.maximum.accumulate(eq)
        dd = (peaks - eq) / np.maximum(peaks, 1e-9)
        max_dd = float(np.max(dd)) if dd.size else 0.0
        # Longest run below a peak.
        longest = cur = 0
        for i in range(eq.size):
            if eq[i] < peaks[i]:
                cur += 1
                longest = max(longest, cur)
            else:
                cur = 0
        return max_dd, longest

    def sharpe(self) -> float:
        r = self._r_multiples()
        if len(r) < 2:
            return 0.0
        arr = np.array(r, dtype=float)
        std = float(np.std(arr, ddof=1))
        if std == 0:
            return 0.0
        return float(np.mean(arr) / std * math.sqrt(len(arr)))

    def sortino(self) -> float:
        r = self._r_multiples()
        if len(r) < 2:
            return 0.0
        arr = np.array(r, dtype=float)
        downside = arr[arr < 0]
        if downside.size == 0:
            return float("inf") if float(np.mean(arr)) > 0 else 0.0
        dstd = float(np.sqrt(np.mean(downside ** 2)))
        if dstd == 0:
            return 0.0
        return float(np.mean(arr) / dstd * math.sqrt(len(arr)))

    def calmar(self) -> float:
        max_dd, _ = self.max_drawdown()
        total_return = _safe_div(
            self.ending_balance - self.starting_balance, self.starting_balance
        )
        if max_dd <= 0:
            return float("inf") if total_return > 0 else 0.0
        return total_return / max_dd

    def profit_factor(self) -> float:
        pnls = self._pnls()
        gains = sum(p for p in pnls if p > 0)
        losses = sum(p for p in pnls if p < 0)
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / abs(losses)

    def expectancy_r(self) -> float:
        r = self._r_multiples()
        return float(np.mean(r)) if r else 0.0

    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades)

    def _span_days(self) -> float:
        if len(self.equity_curve) >= 2:
            start = self.equity_curve[0][0]
            end = self.equity_curve[-1][0]
        elif self.trades:
            start = min(t.open_time for t in self.trades)
            end = max(t.close_time for t in self.trades)
        else:
            return 0.0
        return max(0.0, (end - start).total_seconds() / 86400.0)

    def metrics(self) -> dict:
        max_dd, dd_len = self.max_drawdown()
        pnls = self._pnls()
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        span = self._span_days()
        durations = [t.duration_sec for t in self.trades]
        return {
            "starting_balance": round(self.starting_balance, 2),
            "ending_balance": round(self.ending_balance, 2),
            "total_pnl": round(sum(pnls), 2),
            "total_return_pct": round(
                100.0 * _safe_div(
                    self.ending_balance - self.starting_balance,
                    self.starting_balance,
                ), 3,
            ),
            "num_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(self.win_rate(), 4),
            "avg_win": round(_safe_div(sum(t.pnl for t in wins), len(wins)), 2),
            "avg_loss": round(_safe_div(sum(t.pnl for t in losses), len(losses)), 2),
            "expectancy_r": round(self.expectancy_r(), 4),
            "profit_factor": _round_inf(self.profit_factor()),
            "max_drawdown_pct": round(100.0 * max_dd, 3),
            "max_drawdown_len": dd_len,
            "sharpe": round(self.sharpe(), 4),
            "sortino": _round_inf(self.sortino()),
            "calmar": _round_inf(self.calmar()),
            "avg_trade_duration_sec": round(_safe_div(sum(durations), len(durations)), 1),
            "trades_per_day": round(_safe_div(len(self.trades), span), 3) if span else 0.0,
            "blocked_by_risk": self.blocked_signals,
        }

    # ── Breakdowns ───────────────────────────────────────────────────────

    def _breakdown(self, key) -> dict:
        groups: dict[str, list[TradeRecord]] = {}
        for t in self.trades:
            groups.setdefault(str(key(t)), []).append(t)
        out: dict[str, dict] = {}
        for name, items in sorted(groups.items()):
            r = [t.pnl_r for t in items]
            wins = sum(1 for t in items if t.pnl > 0)
            out[name] = {
                "trades": len(items),
                "win_rate": round(_safe_div(wins, len(items)), 4),
                "total_pnl": round(sum(t.pnl for t in items), 2),
                "expectancy_r": round(float(np.mean(r)) if r else 0.0, 4),
            }
        return out

    def per_pair(self) -> dict:
        return self._breakdown(lambda t: t.pair)

    def per_regime(self) -> dict:
        return self._breakdown(lambda t: t.regime)

    def per_behavior(self) -> dict:
        return self._breakdown(lambda t: t.profile)

    def per_exit_reason(self) -> dict:
        return self._breakdown(lambda t: t.exit_reason)

    # ── Serialisation ────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "metrics": self.metrics(),
            "per_pair": self.per_pair(),
            "per_regime": self.per_regime(),
            "per_behavior": self.per_behavior(),
            "per_exit_reason": self.per_exit_reason(),
            "risk_events": self.risk_events[-200:],
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": [
                {"time": ts.isoformat(), "equity": round(eq, 2)}
                for ts, eq in self.equity_curve
            ],
        }


def _round_inf(value: float) -> float:
    if value == float("inf"):
        return float("inf")
    if value == float("-inf"):
        return float("-inf")
    return round(value, 4)


class BacktestReporter:
    """Renders :class:`BacktestResults` to console / JSON / CSV."""

    def __init__(self, results: BacktestResults) -> None:
        self.results = results

    def console_summary(self) -> str:
        m = self.results.metrics()
        lines = [
            "═══════════════ BACKTEST SUMMARY ═══════════════",
            f"  Trades            : {m['num_trades']}  "
            f"(W {m['wins']} / L {m['losses']}, win {m['win_rate']*100:.1f}%)",
            f"  Net P&L           : {m['total_pnl']:+.2f} "
            f"({m['total_return_pct']:+.2f}%)",
            f"  Balance           : {m['starting_balance']:.2f} → {m['ending_balance']:.2f}",
            f"  Expectancy        : {m['expectancy_r']:+.3f} R / trade",
            f"  Profit factor     : {m['profit_factor']}",
            f"  Max drawdown      : {m['max_drawdown_pct']:.2f}% (len {m['max_drawdown_len']})",
            f"  Sharpe / Sortino  : {m['sharpe']} / {m['sortino']}",
            f"  Calmar            : {m['calmar']}",
            f"  Avg duration      : {m['avg_trade_duration_sec']:.0f}s  "
            f"| {m['trades_per_day']} trades/day",
            f"  Blocked by risk   : {m['blocked_by_risk']}",
        ]
        per_regime = self.results.per_regime()
        if per_regime:
            lines.append("  ── Per regime ──")
            for name, b in per_regime.items():
                lines.append(
                    f"    {name:<14} {b['trades']:>4} trades  "
                    f"win {b['win_rate']*100:4.0f}%  EV {b['expectancy_r']:+.2f}R"
                )
        lines.append("═════════════════════════════════════════════════")
        return "\n".join(lines)

    def to_json(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w") as f:
            json.dump(self.results.to_dict(), f, indent=2, default=_json_default)

    def to_trade_csv(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "pair", "direction", "lots", "open_price", "close_price",
            "open_time", "close_time", "duration_sec", "pnl", "pnl_pips",
            "pnl_r", "exit_reason", "regime", "horizon", "profile",
        ]
        with p.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for t in self.results.trades:
                row = t.to_dict()
                writer.writerow({k: row.get(k) for k in fields})


def _json_default(obj):
    if obj == float("inf"):
        return "Infinity"
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)
