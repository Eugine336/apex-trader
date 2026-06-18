"""
APEX TRADER — P&L Tracker
Tracks every dollar, every day.
The moment daily loss crosses the line, everything stops.
"""

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class PnLSnapshot:
    daily_realized: float = 0.0
    daily_unrealized: float = 0.0
    daily_total: float = 0.0
    daily_total_pct: float = 0.0
    weekly_realized: float = 0.0
    weekly_total_pct: float = 0.0
    monthly_realized: float = 0.0
    monthly_total_pct: float = 0.0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    best_trade_today: float = 0.0
    worst_trade_today: float = 0.0
    current_streak: int = 0


class PnLTracker:
    """
    Rolling tracker for daily, weekly, and monthly profit & loss.
    Provides the data the RiskEngine needs to decide if we can keep trading.
    """

    def __init__(self, starting_balance: float = 10_000.0):
        self.starting_balance = starting_balance

        self._daily: dict[str, float] = {}
        self._weekly: dict[str, float] = {}
        self._monthly: dict[str, float] = {}

        self._trades_today: dict[str, int] = {}
        self._wins_today: dict[str, int] = {}
        self._losses_today: dict[str, int] = {}
        self._best_today: dict[str, float] = {}
        self._worst_today: dict[str, float] = {}

        self._streak: int = 0

    def record(
        self,
        pnl: float,
        is_win: bool,
        timestamp: datetime | None = None,
    ) -> None:
        timestamp = timestamp or datetime.now(timezone.utc)
        day_key = timestamp.strftime("%Y-%m-%d")
        week_key = f"{timestamp.isocalendar().year}-W{timestamp.isocalendar().week:02d}"
        month_key = timestamp.strftime("%Y-%m")

        self._daily[day_key] = self._daily.get(day_key, 0.0) + pnl
        self._weekly[week_key] = self._weekly.get(week_key, 0.0) + pnl
        self._monthly[month_key] = self._monthly.get(month_key, 0.0) + pnl

        self._trades_today[day_key] = self._trades_today.get(day_key, 0) + 1

        if is_win:
            self._wins_today[day_key] = self._wins_today.get(day_key, 0) + 1
            self._streak = max(self._streak, 0) + 1
        else:
            self._losses_today[day_key] = self._losses_today.get(day_key, 0) + 1
            self._streak = min(self._streak, 0) - 1

        cur_best = self._best_today.get(day_key, 0.0)
        cur_worst = self._worst_today.get(day_key, 0.0)
        self._best_today[day_key] = max(cur_best, pnl)
        self._worst_today[day_key] = min(cur_worst, pnl)

    def get_snapshot(
        self,
        unrealized_pnl: float = 0.0,
        account_balance: float | None = None,
        timestamp: datetime | None = None,
    ) -> PnLSnapshot:
        timestamp = timestamp or datetime.now(timezone.utc)
        balance = account_balance or self.starting_balance
        day_key = timestamp.strftime("%Y-%m-%d")
        week_key = f"{timestamp.isocalendar().year}-W{timestamp.isocalendar().week:02d}"
        month_key = timestamp.strftime("%Y-%m")

        daily_realized = self._daily.get(day_key, 0.0)
        daily_total = daily_realized + unrealized_pnl
        weekly_realized = self._weekly.get(week_key, 0.0)
        monthly_realized = self._monthly.get(month_key, 0.0)

        return PnLSnapshot(
            daily_realized=round(daily_realized, 2),
            daily_unrealized=round(unrealized_pnl, 2),
            daily_total=round(daily_total, 2),
            daily_total_pct=round(daily_total / balance * 100, 2) if balance else 0.0,
            weekly_realized=round(weekly_realized, 2),
            weekly_total_pct=round(weekly_realized / balance * 100, 2) if balance else 0.0,
            monthly_realized=round(monthly_realized, 2),
            monthly_total_pct=round(monthly_realized / balance * 100, 2) if balance else 0.0,
            trades_today=self._trades_today.get(day_key, 0),
            wins_today=self._wins_today.get(day_key, 0),
            losses_today=self._losses_today.get(day_key, 0),
            best_trade_today=round(self._best_today.get(day_key, 0.0), 2),
            worst_trade_today=round(self._worst_today.get(day_key, 0.0), 2),
            current_streak=self._streak,
        )

    def is_daily_limit_hit(
        self,
        max_daily_drawdown_pct: float,
        account_balance: float,
        unrealized_pnl: float = 0.0,
        timestamp: datetime | None = None,
    ) -> bool:
        timestamp = timestamp or datetime.now(timezone.utc)
        day_key = timestamp.strftime("%Y-%m-%d")
        daily_total = self._daily.get(day_key, 0.0) + unrealized_pnl
        return daily_total <= -(max_daily_drawdown_pct / 100.0 * account_balance)

    def is_weekly_limit_hit(
        self,
        max_weekly_drawdown_pct: float = 8.0,
        account_balance: float | None = None,
        timestamp: datetime | None = None,
    ) -> bool:
        timestamp = timestamp or datetime.now(timezone.utc)
        balance = account_balance or self.starting_balance
        week_key = f"{timestamp.isocalendar().year}-W{timestamp.isocalendar().week:02d}"
        weekly_total = self._weekly.get(week_key, 0.0)
        return weekly_total <= -(max_weekly_drawdown_pct / 100.0 * balance)

    def reset_daily(self, timestamp: datetime | None = None) -> None:
        timestamp = timestamp or datetime.now(timezone.utc)
        day_key = timestamp.strftime("%Y-%m-%d")
        self._daily[day_key] = 0.0
        self._trades_today[day_key] = 0
        self._wins_today[day_key] = 0
        self._losses_today[day_key] = 0
        self._best_today[day_key] = 0.0
        self._worst_today[day_key] = 0.0

    def reset_weekly(self, timestamp: datetime | None = None) -> None:
        timestamp = timestamp or datetime.now(timezone.utc)
        week_key = f"{timestamp.isocalendar().year}-W{timestamp.isocalendar().week:02d}"
        self._weekly[week_key] = 0.0

    def get_performance_summary(
        self, period: str = "daily", timestamp: datetime | None = None,
    ) -> dict:
        timestamp = timestamp or datetime.now(timezone.utc)
        if period == "daily":
            day_key = timestamp.strftime("%Y-%m-%d")
            wins = self._wins_today.get(day_key, 0)
            losses = self._losses_today.get(day_key, 0)
            total = wins + losses
            return {
                "period": day_key,
                "pnl": round(self._daily.get(day_key, 0.0), 2),
                "trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / total * 100, 1) if total else 0.0,
                "best_trade": round(self._best_today.get(day_key, 0.0), 2),
                "worst_trade": round(self._worst_today.get(day_key, 0.0), 2),
                "streak": self._streak,
            }
        if period == "weekly":
            week_key = f"{timestamp.isocalendar().year}-W{timestamp.isocalendar().week:02d}"
            return {
                "period": week_key,
                "pnl": round(self._weekly.get(week_key, 0.0), 2),
            }
        month_key = timestamp.strftime("%Y-%m")
        return {
            "period": month_key,
            "pnl": round(self._monthly.get(month_key, 0.0), 2),
        }

    # ── Persistence ──────────────────────────────────────────────────────

    def to_state(self) -> dict:
        """Serialise daily/weekly/monthly tallies so a mid-day restart does NOT
        reset daily-loss tracking to zero (which would bypass the daily cap)."""
        return {
            "daily": dict(self._daily),
            "weekly": dict(self._weekly),
            "monthly": dict(self._monthly),
            "trades_today": dict(self._trades_today),
            "wins_today": dict(self._wins_today),
            "losses_today": dict(self._losses_today),
            "best_today": dict(self._best_today),
            "worst_today": dict(self._worst_today),
            "streak": self._streak,
        }

    def restore_state(self, state: dict) -> None:
        """Restore tallies from a persisted payload. Keys are date-bucketed, so
        a genuinely new day naturally reads 0 while a same-day restart resumes
        the running total."""
        if not state:
            return
        try:
            self._daily = {str(k): float(v) for k, v in (state.get("daily") or {}).items()}
            self._weekly = {str(k): float(v) for k, v in (state.get("weekly") or {}).items()}
            self._monthly = {str(k): float(v) for k, v in (state.get("monthly") or {}).items()}
            self._trades_today = {str(k): int(v) for k, v in (state.get("trades_today") or {}).items()}
            self._wins_today = {str(k): int(v) for k, v in (state.get("wins_today") or {}).items()}
            self._losses_today = {str(k): int(v) for k, v in (state.get("losses_today") or {}).items()}
            self._best_today = {str(k): float(v) for k, v in (state.get("best_today") or {}).items()}
            self._worst_today = {str(k): float(v) for k, v in (state.get("worst_today") or {}).items()}
            self._streak = int(state.get("streak", 0) or 0)
        except (TypeError, ValueError):
            # Corrupt payload — keep the fresh in-memory tallies rather than crash.
            return
