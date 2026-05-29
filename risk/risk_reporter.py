"""
APEX TRADER — Risk Reporter
Full transparency. Every metric visible.
The dashboard needs this data to show the account's health in real time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from risk.daily_tracker import PnLTracker
    from risk.risk_engine import RiskEngine
    from risk.spread_monitor import SpreadMonitor


@dataclass
class RiskReport:
    timestamp: datetime
    account_balance: float
    equity: float
    risk_mode: str
    daily_pnl_pct: float
    weekly_pnl_pct: float
    monthly_pnl_pct: float
    open_trades: int
    total_exposure_pct: float
    currency_exposures: dict[str, float]
    win_rate_today: float
    win_rate_week: float
    consecutive_losses: int
    consecutive_wins: int
    avg_risk_reward: float
    profit_factor: float
    max_drawdown_today: float
    spread_alerts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    health: str = "GOOD"


class RiskReporter:
    """
    Aggregates data from every risk component into a single report
    suitable for the live dashboard and logging.
    """

    def generate_report(
        self,
        risk_engine: RiskEngine,
        pnl_tracker: PnLTracker,
        spread_monitor: SpreadMonitor,
        open_trades: list | None = None,
        account_balance: float | None = None,
    ) -> RiskReport:
        from brain.correlation_engine import OpenTrade

        now = datetime.now(timezone.utc)
        balance = account_balance or risk_engine.balance
        trades = open_trades or []

        snapshot = pnl_tracker.get_snapshot(account_balance=balance, timestamp=now)
        dd_status = risk_engine.drawdown_guard.get_status(now)

        corr_trades = [
            OpenTrade(pair=t.get("pair", ""), direction=t.get("direction", "LONG"), risk_pct=t.get("risk_pct", 0.02))
            if isinstance(t, dict) else t
            for t in trades
        ]
        exposure_map = risk_engine.correlation_engine.calculate_exposure(corr_trades)

        spread_alerts: list[str] = []
        for t in trades:
            pair = t.get("pair") if isinstance(t, dict) else getattr(t, "pair", "")
            if pair:
                avg_spread = spread_monitor.get_average_spread(pair)
                status = spread_monitor.get_spread_status(pair, avg_spread)
                if status in {"WIDE", "DANGEROUS"}:
                    spread_alerts.append(f"{pair}: spread is {status}")

        wins = snapshot.wins_today
        total_today = snapshot.trades_today
        win_rate_today = round(wins / total_today * 100, 1) if total_today else 0.0

        daily_summary = pnl_tracker.get_performance_summary("daily", now)
        gross_profit = max(snapshot.best_trade_today, 0.0)
        gross_loss = abs(min(snapshot.worst_trade_today, 0.0))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0.0

        warnings: list[str] = []
        if dd_status.mode in {"RECOVERY", "FROZEN"}:
            warnings.append(f"DrawdownGuard mode: {dd_status.mode}")
        if dd_status.consecutive_losses >= 2:
            warnings.append(f"{dd_status.consecutive_losses} consecutive losses")
        if spread_alerts:
            warnings.append(f"{len(spread_alerts)} pair(s) with wide spreads")
        if len(trades) >= 5:
            warnings.append(f"Approaching max open trades ({len(trades)}/6)")

        health = self._assess_health(
            dd_status.mode, snapshot.daily_total_pct, dd_status.consecutive_losses,
            len(spread_alerts), len(trades),
        )

        return RiskReport(
            timestamp=now,
            account_balance=round(balance, 2),
            equity=round(balance + snapshot.daily_total, 2),
            risk_mode=dd_status.mode,
            daily_pnl_pct=snapshot.daily_total_pct,
            weekly_pnl_pct=snapshot.weekly_total_pct,
            monthly_pnl_pct=snapshot.monthly_total_pct,
            open_trades=len(trades),
            total_exposure_pct=round(exposure_map.max_single_currency_exposure * 100, 2),
            currency_exposures={k: round(v * 100, 2) for k, v in exposure_map.currency_exposures.items()},
            win_rate_today=win_rate_today,
            win_rate_week=0.0,
            consecutive_losses=dd_status.consecutive_losses,
            consecutive_wins=dd_status.consecutive_wins,
            avg_risk_reward=0.0,
            profit_factor=profit_factor,
            max_drawdown_today=abs(min(snapshot.daily_total_pct, 0.0)),
            spread_alerts=spread_alerts,
            warnings=warnings,
            health=health,
        )

    @staticmethod
    def _assess_health(
        mode: str,
        daily_pnl_pct: float,
        consecutive_losses: int,
        spread_alert_count: int,
        open_trade_count: int,
    ) -> str:
        if mode == "FROZEN":
            return "FROZEN"
        if mode == "RECOVERY" or daily_pnl_pct <= -3.0:
            return "CRITICAL"
        if (
            mode == "CAUTION"
            or consecutive_losses >= 2
            or spread_alert_count > 0
            or daily_pnl_pct <= -1.5
        ):
            return "CAUTION"
        if daily_pnl_pct >= 1.0 and open_trade_count <= 3:
            return "EXCELLENT"
        return "GOOD"

    @staticmethod
    def get_health_color(report: RiskReport) -> str:
        return {
            "EXCELLENT": "green",
            "GOOD": "green",
            "CAUTION": "yellow",
            "CRITICAL": "red",
            "FROZEN": "red",
        }.get(report.health, "grey")
