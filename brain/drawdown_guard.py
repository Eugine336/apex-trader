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
    high_water_mark: float = 0.0
    drawdown_from_peak_pct: float = 0.0


class DrawdownGuard:
    """
    Adaptive protection logic:
    - NORMAL: 2% risk, score >= 85
    - CAUTION: 1.5% risk, score >= 88
    - RECOVERY: 1% risk, score >= 92
    - FROZEN: no trading until next day
    """

    def __init__(self, base_risk_pct: float = 0.005):
        """
        base_risk_pct: matches config.risk_per_trade_pct (default 0.5%)
        Risk map scales DOWN from base in adverse conditions — never up.
        NORMAL   = base_risk_pct          (e.g. 0.5%)
        CAUTION  = base_risk_pct * 0.75   (e.g. 0.375%)
        RECOVERY = base_risk_pct * 0.5    (e.g. 0.25%)
        FROZEN   = 0
        """
        self.mode = DrawdownMode.NORMAL
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.daily_pnl_history: dict[str, float] = {}
        self.weekly_pnl_history: dict[str, float] = {}
        self.equity_points: list[tuple[str, float]] = []
        self.last_trade_day: str | None = None
        self.high_water_mark: float = 0.0
        self.hwm_timestamp: str | None = None

        self.risk_map = {
            DrawdownMode.NORMAL:   round(base_risk_pct, 4),
            DrawdownMode.CAUTION:  round(base_risk_pct * 0.75, 4),
            DrawdownMode.RECOVERY: round(base_risk_pct * 0.50, 4),
            DrawdownMode.FROZEN:   0.0,
        }
        self.score_map = {
            DrawdownMode.NORMAL: 65,
            DrawdownMode.CAUTION: 70,
            DrawdownMode.RECOVERY: 75,
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
        self.update_hwm(equity, timestamp)

        self._update_mode(day_key)
        return self.get_status(timestamp)

    def update_hwm(self, equity: float, timestamp: datetime | None = None) -> dict:
        timestamp = timestamp or datetime.now(timezone.utc)
        ts_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")

        if equity > self.high_water_mark:
            self.high_water_mark = equity
            self.hwm_timestamp = ts_str

        if self.high_water_mark > 0:
            drawdown_from_peak = (self.high_water_mark - equity) / self.high_water_mark
        else:
            drawdown_from_peak = 0.0

        is_at_peak = equity >= self.high_water_mark and self.high_water_mark > 0
        prev_equity = self.equity_points[-2][1] if len(self.equity_points) >= 2 else 0.0
        is_recovering = equity < self.high_water_mark and equity > prev_equity

        return {
            "hwm": self.high_water_mark,
            "current_equity": equity,
            "drawdown_from_peak_pct": round(drawdown_from_peak, 6),
            "is_at_peak": is_at_peak,
            "is_recovering": is_recovering,
        }

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

        current_equity = self.equity_points[-1][1] if self.equity_points else 0.0
        if self.high_water_mark > 0:
            dd_from_peak = (self.high_water_mark - current_equity) / self.high_water_mark
        else:
            dd_from_peak = 0.0

        return DrawdownStatus(
            mode=self.mode.value,
            current_risk_pct=self.risk_map[self.mode],
            current_score_threshold=self.score_map[self.mode],
            consecutive_losses=self.consecutive_losses,
            consecutive_wins=self.consecutive_wins,
            daily_pnl_pct=round(daily_pnl, 4),
            weekly_pnl_pct=round(weekly_pnl, 4),
            equity_slope=round(slope, 6),
            high_water_mark=round(self.high_water_mark, 6),
            drawdown_from_peak_pct=round(dd_from_peak, 6),
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

    def to_state(self) -> dict:
        return {
            "mode": self.mode.value,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "daily_pnl_history": dict(self.daily_pnl_history),
            "weekly_pnl_history": dict(self.weekly_pnl_history),
            "equity_points": list(self.equity_points),
            "last_trade_day": self.last_trade_day,
            "high_water_mark": self.high_water_mark,
            "hwm_timestamp": self.hwm_timestamp,
        }

    def restore_state(self, state: dict) -> None:
        mode_str = state.get("mode", "NORMAL")
        try:
            self.mode = DrawdownMode(mode_str)
        except (ValueError, KeyError):
            self.mode = DrawdownMode.NORMAL
        self.consecutive_losses = int(state.get("consecutive_losses", 0))
        self.consecutive_wins = int(state.get("consecutive_wins", 0))
        self.daily_pnl_history = {
            str(k): float(v) for k, v in state.get("daily_pnl_history", {}).items()
        }
        self.weekly_pnl_history = {
            str(k): float(v) for k, v in state.get("weekly_pnl_history", {}).items()
        }
        raw_points = state.get("equity_points", [])
        self.equity_points = [(str(p[0]), float(p[1])) for p in raw_points if len(p) >= 2]
        self.last_trade_day = state.get("last_trade_day")
        self.high_water_mark = float(state.get("high_water_mark", 0.0))
        self.hwm_timestamp = state.get("hwm_timestamp")
