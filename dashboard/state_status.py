"""APEX TRADER — Dashboard Status Mixin."""

from datetime import datetime, timezone

from dashboard.state_helpers import HelpersMixin, pct_to_fraction, safe_float
from loguru import logger


class StatusMixin(HelpersMixin):
    """get_status(), _live_status(), _sim_status()."""

    _start_time: float
    _connection_status: dict[str, bool]

    def get_status(self) -> dict:
        import time as _time

        uptime = _time.monotonic() - self._start_time
        if self.is_live:
            ed = getattr(self, "_event_driven_system", None)
            if ed is not None:
                return self._ed_status(uptime)
            return self._live_status(uptime)
        return self._sim_status(uptime)

    def _live_status(self, uptime: float) -> dict:
        loop = self._trading_loop
        balance = self._get_balance()
        mt5_balance, deriv_balance = self._get_platform_balances()

        ed = getattr(self, "_event_driven_system", None)
        ctx = getattr(ed, "_ctx", None) if ed is not None else None

        dd = None
        if loop is not None:
            dd = loop.drawdown.get_status(datetime.now(timezone.utc))
        elif ctx is not None and ctx.drawdown_guard is not None:
            dd = ctx.drawdown_guard.get_status(datetime.now(timezone.utc))

        records = self._build_history_rows(balance)

        wins = sum(1 for r in records if r["outcome"] == "WIN")
        losses = sum(1 for r in records if r["outcome"] == "LOSS")
        total = wins + losses

        win_rate = (wins / total * 100) if total > 0 else 0.0
        total_pnl = round(sum(r["pnl_dollars"] for r in records), 2) if records else 0.0

        today = datetime.now(timezone.utc).date()
        daily_pnl = 0.0
        for r in records:
            try:
                d_str = str(r.get("opened_at", ""))[:10]
                d = datetime.fromisoformat(d_str).date()
                if d == today:
                    daily_pnl += safe_float(r["pnl_dollars"], 0.0)
            except Exception as exc:
                logger.debug("[dashboard] daily PnL date parse failed, skipping record: {}", exc)
                pass
        if not records and dd is not None:
            daily_frac = pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0))
            daily_pnl = daily_frac * balance

        is_running = (loop is not None and bool(loop.running)) or (ed is not None and getattr(ed, "is_running", False))
        open_count = 0
        if loop is not None:
            open_count = loop.get_positions_count() if hasattr(loop, "get_positions_count") else len(getattr(loop, "managed_positions", {}))

        return {
            "bot_status": "running" if is_running else "stopped",
            "mode": "live",
            "uptime_seconds": round(uptime, 2),
            "risk_mode": str(getattr(dd, "mode", "NORMAL")) if dd is not None else "NORMAL",
            "win_rate": round(win_rate, 1),
            "total_trades": total,
            "win_count": wins,
            "loss_count": losses,
            "daily_pnl": round(daily_pnl, 2),
            "total_pnl": total_pnl,
            "account_balance": round(balance, 2),
            "mt5_balance": mt5_balance,
            "deriv_balance": deriv_balance,
            "daily_loss_pct": round(abs(min(daily_pnl / balance, 0.0)) * 100, 2) if balance > 0 else 0.0,
            "max_daily_loss_pct": float(
                getattr(getattr(loop, "config", None), "risk", None).max_daily_drawdown_pct
                if loop is not None and hasattr(getattr(loop, "config", None), "risk")
                else 5.0
            ),
            "open_trade_count": open_count,
            "consecutive_losses": int(getattr(dd, "consecutive_losses", 0)) if dd is not None else 0,
            "consecutive_wins": int(getattr(dd, "consecutive_wins", 0)) if dd is not None else 0,
            "mt5_connected": self._connection_status.get("mt5", False),
            "deriv_connected": self._connection_status.get("deriv", False),
            "trade_manager_trades": len(getattr(getattr(loop, "trade_manager", None), "_trades", {})) if loop is not None else 0,
        }

    def _ed_status(self, uptime: float) -> dict:
        """Status when running in event-driven mode."""
        balance = self._get_balance()
        mt5_balance, deriv_balance = self._get_platform_balances()
        ctx = getattr(self, "_system_context", None)

        dd_mode = "NORMAL"
        consecutive_losses = 0
        consecutive_wins = 0
        daily_pnl = 0.0
        risk_pct = 0.75
        max_daily = 5.0

        if ctx is not None:
            if ctx.drawdown_guard is not None:
                try:
                    dd = ctx.drawdown_guard.get_status(datetime.now(timezone.utc))
                    dd_mode = str(getattr(dd, "mode", "NORMAL"))
                    consecutive_losses = int(getattr(dd, "consecutive_losses", 0))
                    consecutive_wins = int(getattr(dd, "consecutive_wins", 0))
                    daily_frac = pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0))
                    daily_pnl = round(daily_frac * balance, 2)
                except Exception:
                    pass
            if ctx.risk_engine is not None:
                try:
                    risk_pct = getattr(ctx.risk_engine, "_risk_per_trade_pct", 0.75)
                except Exception:
                    pass

        try:
            cfg_risk = getattr(self._event_driven_system, "_config", None)
            if cfg_risk is not None:
                max_daily = float(getattr(cfg_risk.risk, "max_daily_drawdown_pct", 5.0))
        except Exception:
            pass

        records = self._build_history_rows(balance)
        wins = sum(1 for r in records if r["outcome"] == "WIN")
        losses = sum(1 for r in records if r["outcome"] == "LOSS")
        total = wins + losses
        win_rate = (wins / total * 100) if total > 0 else 0.0
        total_pnl = round(sum(r["pnl_dollars"] for r in records), 2) if records else 0.0

        open_count = 0
        try:
            positions = self._platform_manager.get_all_open_positions()
            open_count = len(positions) if positions else 0
        except Exception:
            pass

        return {
            "bot_status": "running" if self.is_live else "stopped",
            "mode": "event_driven",
            "uptime_seconds": round(uptime, 2),
            "risk_mode": dd_mode,
            "win_rate": round(win_rate, 1),
            "total_trades": total,
            "win_count": wins,
            "loss_count": losses,
            "daily_pnl": round(daily_pnl, 2),
            "total_pnl": total_pnl,
            "account_balance": round(balance, 2),
            "mt5_balance": mt5_balance,
            "deriv_balance": deriv_balance,
            "daily_loss_pct": round(abs(min(daily_pnl / balance, 0.0)) * 100, 2) if balance > 0 else 0.0,
            "max_daily_loss_pct": max_daily,
            "open_trade_count": open_count,
            "consecutive_losses": consecutive_losses,
            "consecutive_wins": consecutive_wins,
            "mt5_connected": self._connection_status.get("mt5", False),
            "deriv_connected": self._connection_status.get("deriv", False),
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
