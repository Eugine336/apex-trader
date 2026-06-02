"""APEX TRADER — Dashboard Performance Mixin."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from dashboard.state_helpers import HelpersMixin, pct_to_fraction, safe_float
from loguru import logger


class PerformanceMixin(HelpersMixin):
    """get_performance()."""

    def get_performance(self) -> dict:
        today = datetime.now(timezone.utc).date()
        if not self.is_live:
            date_key = today.isoformat()
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_count": 0, "loss_count": 0, "win_rate": 0.0,
                "daily_pnl": 0.0, "weekly_pnl": 0.0, "monthly_pnl": 0.0,
                "total_pnl": 0.0, "best_trade_pips": 0.0, "worst_trade_pips": 0.0,
                "avg_win_pips": 0.0, "avg_loss_pips": 0.0, "profit_factor": 0.0,
                "daily_trades": 0,
                "equity_curve": [{"date": date_key, "equity": 10000.0, "time": date_key, "value": 10000.0}],
                "pnl_history": [],
            }

        loop = self._trading_loop
        balance = self._get_balance()
        dd = loop.drawdown.get_status(datetime.now(timezone.utc))
        rows = self._build_history_rows(balance)

        wins = sum(1 for r in rows if r["outcome"] == "WIN")
        losses = sum(1 for r in rows if r["outcome"] == "LOSS")
        total_trades = wins + losses

        pnl_pips = [safe_float(r["pnl_pips"], 0.0) for r in rows]
        win_pips = [p for p in pnl_pips if p > 0]
        loss_pips = [p for p in pnl_pips if p < 0]

        best_trade = max(pnl_pips) if pnl_pips else 0.0
        worst_trade = min(pnl_pips) if pnl_pips else 0.0
        avg_win = sum(win_pips) / len(win_pips) if win_pips else 0.0
        avg_loss = abs(sum(loss_pips) / len(loss_pips)) if loss_pips else 0.0
        gross_win = sum(win_pips)
        gross_loss = abs(sum(loss_pips))
        pf_calc = (gross_win / gross_loss) if gross_loss > 0 else 0.0

        date_pnl: dict[str, float] = defaultdict(float)
        for row in rows:
            key = str(row["opened_at"])[:10]
            date_pnl[key] += safe_float(row["pnl_dollars"], 0.0)

        daily_pnl = 0.0
        weekly_pnl = 0.0
        monthly_pnl = 0.0
        for date_key, pnl in date_pnl.items():
            try:
                d = datetime.fromisoformat(date_key).date()
            except Exception as exc:
                logger.debug("[dashboard] date parse failed for PnL key, skipping: {}", exc)
                continue
            if d == today:
                daily_pnl += pnl
            if d >= today - timedelta(days=6):
                weekly_pnl += pnl
            if d >= today - timedelta(days=29):
                monthly_pnl += pnl

        if not rows:
            daily_frac = pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0))
            weekly_frac = pct_to_fraction(getattr(dd, "weekly_pnl_pct", 0.0))
            daily_pnl = daily_frac * balance
            weekly_pnl = weekly_frac * balance

        total_pnl = sum(safe_float(r["pnl_dollars"], 0.0) for r in rows)

        rows_asc = sorted(rows, key=lambda r: r["opened_at"])
        start_balance = balance - total_pnl
        if start_balance <= 0:
            start_balance = 10000.0

        equity_curve: list[dict[str, Any]] = []
        running = start_balance
        for row in rows_asc:
            running += safe_float(row["pnl_dollars"], 0.0)
            date_key = str(row["opened_at"])[:10]
            point = {
                "date": date_key,
                "equity": round(running, 2),
                "time": date_key,
                "value": round(running, 2),
            }
            if equity_curve and equity_curve[-1]["date"] == date_key:
                equity_curve[-1] = point
            else:
                equity_curve.append(point)

        if not equity_curve:
            today_key = today.isoformat()
            equity_curve = [{
                "date": today_key,
                "equity": round(balance, 2),
                "time": today_key,
                "value": round(balance, 2),
            }]

        pnl_history = [
            {"date": d, "pnl": round(v, 2)}
            for d, v in sorted(date_pnl.items(), key=lambda x: x[0])
        ]

        return {
            "total_trades": total_trades,
            "wins": wins, "losses": losses,
            "win_count": wins, "loss_count": losses,
            "win_rate": round((wins / total_trades * 100) if total_trades > 0 else 0.0, 1),
            "daily_pnl": round(daily_pnl, 2),
            "weekly_pnl": round(weekly_pnl, 2),
            "monthly_pnl": round(monthly_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "best_trade_pips": round(best_trade, 1),
            "worst_trade_pips": round(worst_trade, 1),
            "avg_win_pips": round(avg_win, 1),
            "avg_loss_pips": round(avg_loss, 1),
            "profit_factor": round(pf_calc, 2),
            "daily_trades": int(getattr(loop, "_daily_trades", 0)),
            "equity_curve": equity_curve,
            "pnl_history": pnl_history,
        }
