"""
APEX TRADER — Drawdown Guard
When the battlefield changes, risk must adapt instantly.
This guard enforces caution, recovery, and hard freeze states.
"""

import functools
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum

import numpy as np


def _synchronized(method):
    """Run ``method`` while holding the instance's re-entrant ``_lock``."""
    @functools.wraps(method)
    def _wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return _wrapper


def _lock_public_methods(cls):
    """Wrap every public method so it runs under the instance's ``_lock``.

    Underscore-prefixed methods (including ``__init__``) are skipped: private
    helpers already run under the lock held by their public caller, and skipping
    ``__init__`` guarantees ``_lock`` exists before any wrapped call.
    """
    for name, attr in list(vars(cls).items()):
        if callable(attr) and not name.startswith("_"):
            setattr(cls, name, _synchronized(attr))
    return cls

# Trailing window (in calendar days) over which drawdown-from-peak is measured
# for the consumers that gate sizing (planner size-reduction, RiskEngine
# haircut). A lifetime measure never resets: an account that bled deeply once
# stays shrunk forever even after recent performance recovers. A rolling window
# lets the running peak age out so sizing recovers as recent equity does.
DEFAULT_DRAWDOWN_ROLLING_WINDOW_DAYS = 30


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
    lifetime_drawdown_from_peak_pct: float = 0.0


@_lock_public_methods
class DrawdownGuard:
    """
    Adaptive protection logic:
    - NORMAL: 2% risk, score >= 85
    - CAUTION: 1.5% risk, score >= 88
    - RECOVERY: 1% risk, score >= 92
    - FROZEN: no trading until next day
    """

    def __init__(
        self,
        base_risk_pct: float = 0.005,
        rolling_window_days: int = DEFAULT_DRAWDOWN_ROLLING_WINDOW_DAYS,
    ):
        """
        base_risk_pct: matches config.risk_per_trade_pct (default 0.5%)
        Risk map scales DOWN from base in adverse conditions — never up.
        NORMAL   = base_risk_pct          (e.g. 0.5%)
        CAUTION  = base_risk_pct * 0.75   (e.g. 0.375%)
        RECOVERY = base_risk_pct * 0.5    (e.g. 0.25%)
        FROZEN   = 0

        rolling_window_days: trailing window (calendar days) used by the
        consumer-facing drawdown-from-peak. Lifetime drawdown stays available
        via ``lifetime_drawdown_from_peak_pct`` for logging/display. A value
        <= 0 disables the rolling window and falls back to the lifetime peak.
        """
        self._lock = threading.RLock()
        self.mode = DrawdownMode.NORMAL
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.daily_pnl_history: dict[str, float] = {}
        self.weekly_pnl_history: dict[str, float] = {}
        self.equity_points: list[tuple[str, float]] = []
        self.last_trade_day: str | None = None
        self.high_water_mark: float = 0.0
        self.hwm_timestamp: str | None = None
        self.rolling_window_days: int = int(rolling_window_days)

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

        day_key = timestamp.strftime("%Y-%m-%d")
        drawdown_from_peak = self._rolling_drawdown_fraction(equity, day_key)

        is_at_peak = equity >= self.high_water_mark and self.high_water_mark > 0
        prev_equity = self.equity_points[-2][1] if len(self.equity_points) >= 2 else 0.0
        is_recovering = equity < self.high_water_mark and equity > prev_equity

        return {
            "hwm": self.high_water_mark,
            "current_equity": equity,
            "drawdown_from_peak_pct": round(drawdown_from_peak, 6),
            "lifetime_drawdown_from_peak_pct": round(self._drawdown_fraction(equity), 6),
            "is_at_peak": is_at_peak,
            "is_recovering": is_recovering,
        }

    def _parse_day(self, day_key: str | None) -> date | None:
        try:
            return datetime.strptime(str(day_key), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    def _rolling_drawdown_fraction(
        self, equity: float, reference_day_key: str | None = None
    ) -> float:
        """Peak-to-trough drawdown measured over a trailing window of the most
        recent ``rolling_window_days`` calendar days.

        Same 1.0-anchored principal curve and ``[0, 1]`` clamp as the lifetime
        measure (see ``_drawdown_fraction``) — only the running peak differs:
        here it is the highest equity *within the window* instead of the
        all-time high-water mark. An old trough the account has since climbed
        out of (or simply aged past the window) therefore stops suppressing
        size, so sizing recovers as recent performance does. A non-positive
        ``rolling_window_days`` disables the window and defers to the lifetime
        measure."""
        if self.rolling_window_days <= 0 or not self.equity_points:
            return self._drawdown_fraction(equity)

        ref = self._parse_day(reference_day_key)
        if ref is None:
            ref = self._parse_day(self.equity_points[-1][0])
        if ref is None:
            return self._drawdown_fraction(equity)

        peak_equity = equity
        for day_key, eq in self.equity_points:
            d = self._parse_day(day_key)
            if d is None:
                continue
            age = (ref - d).days
            if 0 <= age <= self.rolling_window_days and eq > peak_equity:
                peak_equity = eq

        peak_factor = 1.0 + peak_equity
        if peak_factor <= 0.0:
            return 1.0
        cur_factor = 1.0 + equity
        return max(0.0, min(1.0, (peak_factor - cur_factor) / peak_factor))

    def _drawdown_fraction(self, equity: float) -> float:
        """Lifetime peak-to-trough drawdown as a 0–1 fraction of peak equity.

        ``equity_points`` / ``high_water_mark`` track *cumulative return* off a
        zero base, so the running peak can sit at (or near) zero. The old
        ``(high_water_mark - equity) / high_water_mark`` divided the give-back by
        that near-zero peak and blew up — a net -40% account whose only peak was
        a tiny early +0.2% reported a ~20,000% "drawdown", which then tripped the
        planner's size-reduction gate (and the RiskEngine's). Anchor the curve at
        a 1.0 principal (equity factor = 1 + cumulative return) and measure the
        standard peak-to-trough decline, clamped to [0, 1]."""
        peak_factor = 1.0 + self.high_water_mark
        if peak_factor <= 0.0:
            # Peak itself was at/below a total wipe-out (≤ -100% cumulative) —
            # treat as fully drawn down rather than dividing by ≤ 0.
            return 1.0
        cur_factor = 1.0 + equity
        return max(0.0, min(1.0, (peak_factor - cur_factor) / peak_factor))

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
        dd_from_peak = self._rolling_drawdown_fraction(current_equity, day_key)
        lifetime_dd = self._drawdown_fraction(current_equity)

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
            lifetime_drawdown_from_peak_pct=round(lifetime_dd, 6),
        )

    def reset_daily(self, timestamp: datetime | None = None) -> None:
        """Observe the trading-day boundary independently of trade results.

        A FROZEN guard is only ever lifted inside ``_roll_day_if_needed``,
        which runs solely when a trade closes (``register_trade_result``). If
        the guard freezes (a -5% day, or a margin / daily-loss flatten) and is
        then left with no open positions to close, the day boundary is never
        observed and FROZEN persists across days — blocking every new entry
        indefinitely. The trading loop calls this once per UTC day so a stale
        freeze lifts to RECOVERY on the next trading day, honouring the
        documented "no trading until next day" contract. Same-day calls are a
        no-op (the freeze must outlast the day it was triggered on).
        """
        timestamp = timestamp or datetime.now(timezone.utc)
        self._roll_day_if_needed(timestamp.strftime("%Y-%m-%d"))

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
