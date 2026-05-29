"""
APEX TRADER — Drawdown Guard
When the battlefield changes, risk must adapt instantly.
This guard enforces caution, recovery, and hard freeze states.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import numpy as np


class DrawdownMode(Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    RECOVERY = "RECOVERY"
    FROZEN = "FROZEN"


@dataclass
class DrawdownStatus:
    mode: str
    current_risk_pct: float
    current_score_threshold: int
    consecutive_losses: int
    consecutive_wins: int
    daily_pnl_pct: float
    weekly_pnl_pct: float
    equity_slope: float


class DrawdownGuard:
    """
    Adaptive protection logic:
    - NORMAL: 2% risk, score >= 85
    - CAUTION: 1.5% risk, score >= 88
    - RECOVERY: 1% risk, score >= 92
    - FROZEN: no trading until next day
    """

    def __init__(self):
        self.mode = DrawdownMode.NORMAL
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.daily_pnl_history: dict[str, float] = {}
        self.weekly_pnl_history: dict[str, float] = {}
        self.equity_points: list[tuple[str, float]] = []
        self.last_trade_day: str | None = None

        self.risk_map = {
            DrawdownMode.NORMAL: 0.02,
            DrawdownMode.CAUTION: 0.015,
            DrawdownMode.RECOVERY: 0.01,
            DrawdownMode.FROZEN: 0.0,
        }
        self.score_map = {
            DrawdownMode.NORMAL: 85,
            DrawdownMode.CAUTION: 88,
            DrawdownMode.RECOVERY: 92,
            DrawdownMode.FROZEN: 999,
        }

    def register_trade_result(
        self, pnl_pct: float, timestamp: datetime | None = None
    ) -> DrawdownStatus:
        timestamp = timestamp or datetime.now(timezone.utc)
        day_key = timestamp.strftime("%Y-%m-%d")
        week_key = f"{timestamp.isocalendar().year}-W{timestamp.isocalendar().week}"

        self._roll_day_if_needed(day_key)
        self.daily_pnl_history[day_key] = (
            self.daily_pnl_history.get(day_key, 0.0) + pnl_pct
        )
        self.weekly_pnl_history[week_key] = (
            self.weekly_pnl_history.get(week_key, 0.0) + pnl_pct
        )

        if pnl_pct < 0:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        elif pnl_pct > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0

        equity = (self.equity_points[-1][1] if self.equity_points else 0.0) + pnl_pct
        self.equity_points.append((day_key, equity))

        self._update_mode(day_key)
        return self.get_status(timestamp)

    def can_trade(self, timestamp: datetime | None = None) -> tuple[bool, str]:
        status = self.get_status(timestamp)
        if status.mode == DrawdownMode.FROZEN.value:
            return False, "Drawdown freeze active — wait for next trading day reset"
        return (
            True,
            f"Mode={status.mode}, risk={status.current_risk_pct:.2%}, min_score={status.current_score_threshold}",
        )

    def get_status(self, timestamp: datetime | None = None) -> DrawdownStatus:
        timestamp = timestamp or datetime.now(timezone.utc)
        day_key = timestamp.strftime("%Y-%m-%d")
        week_key = f"{timestamp.isocalendar().year}-W{timestamp.isocalendar().week}"
        daily_pnl = self.daily_pnl_history.get(day_key, 0.0)
        weekly_pnl = self.weekly_pnl_history.get(week_key, 0.0)
        slope = self._equity_slope()
        return DrawdownStatus(
            mode=self.mode.value,
            current_risk_pct=self.risk_map[self.mode],
            current_score_threshold=self.score_map[self.mode],
            consecutive_losses=self.consecutive_losses,
            consecutive_wins=self.consecutive_wins,
            daily_pnl_pct=round(daily_pnl, 4),
            weekly_pnl_pct=round(weekly_pnl, 4),
            equity_slope=round(slope, 6),
        )

    def _roll_day_if_needed(self, day_key: str) -> None:
        if self.last_trade_day is None:
            self.last_trade_day = day_key
            return
        if day_key != self.last_trade_day and self.mode == DrawdownMode.FROZEN:
            self.mode = DrawdownMode.RECOVERY
            self.consecutive_losses = max(self.consecutive_losses, 1)
        self.last_trade_day = day_key

    def _update_mode(self, day_key: str) -> None:
        daily_pnl = self.daily_pnl_history.get(day_key, 0.0)
        slope = self._equity_slope()

        if daily_pnl <= -0.05:
            self.mode = DrawdownMode.FROZEN
            return

        if (
            self.mode == DrawdownMode.RECOVERY
            and self.consecutive_wins >= 2
            and daily_pnl > -0.03
        ):
            self.mode = DrawdownMode.CAUTION
            self.consecutive_wins = 0
            return

        if (
            self.mode == DrawdownMode.CAUTION
            and self.consecutive_wins >= 2
            and slope >= 0
        ):
            self.mode = DrawdownMode.NORMAL
            self.consecutive_wins = 0
            return

        if self.consecutive_losses >= 3 or daily_pnl <= -0.03:
            self.mode = DrawdownMode.RECOVERY
            return

        if self.consecutive_losses >= 2 or slope < 0:
            self.mode = DrawdownMode.CAUTION
            return

        self.mode = DrawdownMode.NORMAL

    def _equity_slope(self) -> float:
        if len(self.equity_points) < 5:
            return 0.0
        y = np.array([point[1] for point in self.equity_points[-5:]], dtype=float)
        x = np.arange(len(y), dtype=float)
        slope = np.polyfit(x, y, 1)[0]
        return float(slope)
