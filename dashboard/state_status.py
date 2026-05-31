"""APEX TRADER — Dashboard Status Mixin."""

from datetime import datetime, timezone
from typing import Any

from dashboard.state_helpers import HelpersMixin, pct_to_fraction


class StatusMixin(HelpersMixin):
    """get_status(), _live_status(), _sim_status()."""

    _start_time: float
    _connection_status: dict[str, bool]

    def get_status(self) -> dict:
        import time as _time
        uptime = _time.monotonic() - self._start_time
        if self.is_live:
            return self._live_status(uptime)
        return self._sim_status(uptime)

    def _live_status(self, uptime: float) -> dict:
        loop = self._trading_loop
        balance = self._get_balance()
        mt5_balance, deriv_balance = self._get_platform_balances()

        dd = loop.drawdown.get_status(datetime.now(timezone.utc))
        records = self._build_history_rows(balance)

        wins = sum(1 for r in records if r["outcome"] == "WIN")
        losses = sum(1 for r in records if r["outcome"] == "LOSS")
        total = wins + losses

        win_rate = (wins / total * 100) if total > 0 else 0.0
        daily_frac = pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0))
        total_pnl = round(sum(r["pnl_dollars"] for r in records), 2) if records else 0.0

        return {
            "bot_status": "running" if bool(loop.running) else "stopped",
            "mode": "live",
            "uptime_seconds": round(uptime, 2),
            "risk_mode": str(getattr(dd, "mode", "NORMAL")),
            "win_rate": round(win_rate, 1),
            "total_trades": total,
            "win_count": wins,
            "loss_count": losses,
            "daily_pnl": round(daily_frac * balance, 2),
            "total_pnl": total_pnl,
            "account_balance": round(balance, 2),
            "mt5_balance": mt5_balance,
            "deriv_balance": deriv_balance,
            "daily_loss_pct": round(abs(min(daily_frac, 0.0)) * 100, 2),
            "max_daily_loss_pct": float(
                getattr(getattr(loop, "config", None), "risk", None).max_daily_drawdown_pct
                if hasattr(getattr(loop, "config", None), "risk")
                else 5.0
            ),
            "open_trade_count": len(getattr(loop, "managed_positions", {})),
            "consecutive_losses": int(getattr(dd, "consecutive_losses", 0)),
            "consecutive_wins": int(getattr(dd, "consecutive_wins", 0)),
            "mt5_connected": self._connection_status.get("mt5", False),
            "deriv_connected": self._connection_status.get("deriv", False),
            "trade_manager_trades": len(
                getattr(getattr(loop, "trade_manager", None), "_trades", {})
            ),
        }

    @staticmethod
    def _sim_status(uptime: float) -> dict:
        return {
            "bot_status": "running",
            "mode": "simulated",
            "uptime_seconds": round(uptime, 2),
            "risk_mode": "NORMAL",
            "win_rate": 0.0,
            "total_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "daily_pnl": 0.0,
            "total_pnl": 0.0,
            "account_balance": 10000.0,
            "mt5_balance": 0.0,
            "deriv_balance": 0.0,
            "daily_loss_pct": 0.0,
            "max_daily_loss_pct": 5.0,
            "open_trade_count": 0,
            "consecutive_losses": 0,
            "consecutive_wins": 0,
            "mt5_connected": False,
            "deriv_connected": False,
        }
