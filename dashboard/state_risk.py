"""APEX TRADER — Dashboard Risk Status Mixin."""

from datetime import datetime, timezone
from typing import Any

from dashboard.state_helpers import HelpersMixin, safe_float, pct_to_fraction


class RiskMixin(HelpersMixin):
    """get_risk_status()."""

    def get_risk_status(self) -> dict:
        if self.is_live:
            loop = self._trading_loop
            dd = loop.drawdown.get_status(datetime.now(timezone.utc))
            max_open = int(getattr(loop.config.risk, "max_open_trades", 6))
            open_count = loop.get_positions_count() if hasattr(loop, "get_positions_count") else len(getattr(loop, "managed_positions", {}))
            balance = self._get_balance()

            risk_raw = safe_float(getattr(dd, "current_risk_pct", 2.0), 2.0)
            risk_pct = risk_raw * 100 if risk_raw <= 1 else risk_raw
            daily_frac = pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0))
            weekly_frac = pct_to_fraction(getattr(dd, "weekly_pnl_pct", 0.0))
            exposure_pct = ((open_count / max_open) * risk_pct) if max_open > 0 else 0.0
            max_daily_loss = float(getattr(loop.config.risk, "max_daily_drawdown_pct", 5.0))

            mode = str(getattr(dd, "mode", "NORMAL"))
            result: dict[str, Any] = {
                "mode": mode,
                "risk_mode": mode,
                "current_risk_pct": round(risk_pct, 2),
                "daily_pnl_pct": round(daily_frac * 100, 2),
                "weekly_pnl_pct": round(weekly_frac * 100, 2),
                "daily_loss_pct": round(abs(min(daily_frac, 0.0)) * 100, 2),
                "max_daily_loss_pct": round(max_daily_loss, 2),
                "score_threshold": int(getattr(dd, "current_score_threshold", 85)),
                "consecutive_losses": int(getattr(dd, "consecutive_losses", 0)),
                "consecutive_wins": int(getattr(dd, "consecutive_wins", 0)),
                "open_trade_count": open_count,
                "max_open_trades": max_open,
                "exposure_pct": round(exposure_pct, 2),
                "account_balance": round(balance, 2),
            }

            exec_mon = getattr(loop, "execution_monitor", None)
            if exec_mon is not None:
                try:
                    stats = exec_mon.get_stats()
                    result["execution_quality"] = stats.execution_quality
                    result["avg_slippage_pips"] = stats.avg_slippage_pips
                    result["avg_latency_ms"] = stats.avg_latency_ms
                    result["spread_is_wide"] = stats.spread_is_wide
                    result["requote_count"] = stats.requote_count
                except Exception:
                    pass

            reporter = getattr(loop, "risk_reporter", None)
            if reporter is not None:
                try:
                    risk_engine = getattr(loop, "risk_engine", None)
                    if risk_engine is not None:
                        _dash_risk = risk_engine.drawdown_guard.risk_map.get(
                            risk_engine.drawdown_guard.mode, 0.005
                        )
                        positions_snap = loop.get_positions_snapshot() if hasattr(loop, "get_positions_snapshot") else {}
                        open_trades = [
                            {"pair": p.symbol, "direction": p.direction, "risk_pct": _dash_risk}
                            for p in positions_snap.values()
                        ]
                        report = reporter.generate_report(
                            risk_engine=risk_engine,
                            pnl_tracker=risk_engine.pnl_tracker,
                            spread_monitor=risk_engine.spread_monitor,
                            open_trades=open_trades,
                            account_balance=balance,
                        )
                        result["health"] = report.health
                        result["warnings"] = report.warnings
                        result["spread_alerts"] = report.spread_alerts
                        result["currency_exposures"] = report.currency_exposures
                        result["total_exposure_pct"] = report.total_exposure_pct
                        result["win_rate_today"] = report.win_rate_today
                        result["profit_factor"] = report.profit_factor
                        result["max_drawdown_today"] = report.max_drawdown_today
                except Exception:
                    pass

            # Opportunity density
            density_tracker = getattr(loop, "density_tracker", None)
            if density_tracker is not None:
                snap = density_tracker.get_snapshot()
                if snap is not None:
                    result["opportunity_density_tier"] = snap.tier
                    result["opportunity_density_1h"] = snap.ready_count_1h
                    result["opportunity_size_mult"] = snap.size_multiplier

            # System-wide volatility state
            vol_monitor = getattr(loop, "vol_monitor", None)
            if vol_monitor is not None:
                vs = vol_monitor.get_state()
                if vs is not None:
                    result["system_vol_state"] = vs.state
                    result["system_vol_mult"] = vs.size_multiplier
                    result["system_vol_note"] = vs.note

            return result

        return {
            "mode": "NORMAL",
            "risk_mode": "NORMAL",
            "current_risk_pct": 2.0,
            "daily_pnl_pct": 0.0,
            "weekly_pnl_pct": 0.0,
            "daily_loss_pct": 0.0,
            "max_daily_loss_pct": 5.0,
            "score_threshold": 85,
            "consecutive_losses": 0,
            "consecutive_wins": 0,
            "open_trade_count": 0,
            "max_open_trades": 6,
            "exposure_pct": 0.0,
            "account_balance": 10000.0,
        }
