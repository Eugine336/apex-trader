"""APEX TRADER — Dashboard Risk Status Mixin."""

from datetime import datetime, timezone
from typing import Any

from dashboard.state_helpers import HelpersMixin, safe_float, pct_to_fraction
from loguru import logger


class RiskMixin(HelpersMixin):
    """get_risk_status()."""

    def get_risk_status(self) -> dict:
        if self.is_live:
            ed = getattr(self, "_event_driven_system", None)
            if ed is not None:
                return self._ed_risk_status()
            loop = None
            ed = getattr(self, "_event_driven_system", None)
            ctx = getattr(ed, "_ctx", None) if ed is not None else None

            dd = None
            if loop is not None:
                dd = loop.drawdown.get_status(datetime.now(timezone.utc))
            elif ctx is not None and ctx.drawdown_guard is not None:
                dd = ctx.drawdown_guard.get_status(datetime.now(timezone.utc))

            config_risk = getattr(getattr(loop, "config", None), "risk", None) if loop is not None else None
            max_open = int(getattr(config_risk, "max_open_trades", 6)) if config_risk is not None else 6

            if loop is not None:
                open_count = loop.get_positions_count() if hasattr(loop, "get_positions_count") else len(getattr(loop, "managed_positions", {}))
            else:
                try:
                    open_count = len(self._platform_manager.get_all_open_positions()) if self._platform_manager else 0
                except Exception:
                    open_count = 0

            balance = self._get_balance()

            risk_raw = safe_float(getattr(dd, "current_risk_pct", 2.0), 2.0) if dd is not None else 2.0
            risk_pct = risk_raw * 100 if risk_raw <= 1 else risk_raw
            daily_frac = pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0)) if dd is not None else 0.0
            weekly_frac = pct_to_fraction(getattr(dd, "weekly_pnl_pct", 0.0)) if dd is not None else 0.0
            exposure_pct = ((open_count / max_open) * risk_pct) if max_open > 0 else 0.0
            max_daily_loss = float(getattr(config_risk, "max_daily_drawdown_pct", 5.0)) if config_risk is not None else 5.0

            mode = str(getattr(dd, "mode", "NORMAL")) if dd is not None else "NORMAL"
            result: dict[str, Any] = {
                "mode": mode,
                "risk_mode": mode,
                "current_risk_pct": round(risk_pct, 2),
                "daily_pnl_pct": round(daily_frac * 100, 2),
                "weekly_pnl_pct": round(weekly_frac * 100, 2),
                "daily_loss_pct": round(abs(min(daily_frac, 0.0)) * 100, 2),
                "max_daily_loss_pct": round(max_daily_loss, 2),
                "score_threshold": int(getattr(dd, "current_score_threshold", 85)) if dd is not None else 85,
                "consecutive_losses": int(getattr(dd, "consecutive_losses", 0)) if dd is not None else 0,
                "consecutive_wins": int(getattr(dd, "consecutive_wins", 0)) if dd is not None else 0,
                "open_trade_count": open_count,
                "max_open_trades": max_open,
                "exposure_pct": round(exposure_pct, 2),
                "account_balance": round(balance, 2),
            }

            exec_mon = getattr(loop, "execution_monitor", None) if loop is not None else (ctx.execution_monitor if ctx is not None else None)
            if exec_mon is not None:
                try:
                    stats = exec_mon.get_stats()
                    result["execution_quality"] = stats.execution_quality
                    result["avg_slippage_pips"] = stats.avg_slippage_pips
                    result["avg_latency_ms"] = stats.avg_latency_ms
                    result["spread_is_wide"] = stats.spread_is_wide
                    result["requote_count"] = stats.requote_count
                except Exception as exc:
                    logger.debug("[dashboard] execution stats read failed: {}", exc)

            reporter = getattr(loop, "risk_reporter", None) if loop is not None else (ctx.risk_reporter if ctx is not None else None)
            risk_engine = getattr(loop, "risk_engine", None) if loop is not None else (ctx.risk_engine if ctx is not None else None)
            if reporter is not None and risk_engine is not None:
                try:
                    _dash_risk = risk_engine.drawdown_guard.risk_map.get(
                        risk_engine.drawdown_guard.mode, 0.005
                    ) if hasattr(risk_engine, "drawdown_guard") and risk_engine.drawdown_guard is not None else 0.005
                    if loop is not None:
                        positions_snap = loop.get_positions_snapshot() if hasattr(loop, "get_positions_snapshot") else {}
                    else:
                        positions_snap = {}
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
                except Exception as exc:
                    logger.debug("[dashboard] risk report read failed: {}", exc)

            density_tracker = getattr(loop, "density_tracker", None) if loop is not None else (ctx.opportunity_density_tracker if ctx is not None else None)
            if density_tracker is not None:
                snap = density_tracker.get_snapshot()
                if snap is not None:
                    result["opportunity_density_tier"] = snap.tier
                    result["opportunity_density_1h"] = snap.ready_count_1h
                    result["opportunity_size_mult"] = snap.size_multiplier

            vol_monitor = getattr(loop, "vol_monitor", None) if loop is not None else (ctx.system_volatility_monitor if ctx is not None else None)
            if vol_monitor is not None:
                vs = vol_monitor.get_state()
                if vs is not None:
                    result["system_vol_state"] = vs.state
                    result["system_vol_mult"] = vs.size_multiplier
                    result["system_vol_note"] = vs.note

            acct_risk = getattr(loop, "_account_risk", None) if loop is not None else (ctx.account_risk if ctx is not None else None)
            if acct_risk is not None and hasattr(acct_risk, "snapshot"):
                try:
                    result["account_silos"] = acct_risk.snapshot()
                except Exception as exc:
                    logger.debug("[dashboard] account silos read failed: {}", exc)

            try:
                rl_bridge = getattr(getattr(loop, "scanner", None), "_rl", None) if loop is not None else None
                if rl_bridge is not None and hasattr(rl_bridge, "status"):
                    result["rl"] = rl_bridge.status()
            except Exception as exc:
                logger.debug("[dashboard] RL status read failed: {}", exc)

            gate_tuner = getattr(loop, "_gate_tuner", None) if loop is not None else (ctx.gate_tuner if ctx is not None else None)
            if gate_tuner is not None and hasattr(gate_tuner, "all_offsets"):
                try:
                    result["gate_offsets"] = gate_tuner.all_offsets()
                except Exception as exc:
                    logger.debug("[dashboard] gate offsets read failed: {}", exc)

            try:
                cf = getattr(loop, "_last_gate_counterfactuals", None) if loop is not None else None
                if cf:
                    result["gate_counterfactuals"] = cf
            except Exception as exc:
                logger.debug("[dashboard] gate counterfactuals read failed: {}", exc)

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

    def _ed_risk_status(self) -> dict:
        """Risk status when running in event-driven mode."""
        ctx = getattr(self, "_system_context", None)
        balance = self._get_balance()
        open_count = 0
        try:
            positions = self._platform_manager.get_all_open_positions()
            open_count = len(positions) if positions else 0
        except Exception:
            pass

        max_open = 6
        max_daily = 5.0
        try:
            cfg = getattr(self._event_driven_system, "_config", None)
            if cfg is not None:
                max_open = int(getattr(cfg.risk, "max_open_trades", 6))
                max_daily = float(getattr(cfg.risk, "max_daily_drawdown_pct", 5.0))
        except Exception:
            pass

        dd_mode = "NORMAL"
        daily_pnl_pct = 0.0
        weekly_pnl_pct = 0.0
        risk_pct = 2.0
        score_threshold = 85
        consecutive_losses = 0
        consecutive_wins = 0

        if ctx is not None and ctx.drawdown_guard is not None:
            try:
                dd = ctx.drawdown_guard.get_status(datetime.now(timezone.utc))
                dd_mode = str(getattr(dd, "mode", "NORMAL"))
                risk_raw = safe_float(getattr(dd, "current_risk_pct", 2.0), 2.0)
                risk_pct = risk_raw * 100 if risk_raw <= 1 else risk_raw
                daily_pnl_pct = pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0)) * 100
                weekly_pnl_pct = pct_to_fraction(getattr(dd, "weekly_pnl_pct", 0.0)) * 100
                score_threshold = int(getattr(dd, "current_score_threshold", 85))
                consecutive_losses = int(getattr(dd, "consecutive_losses", 0))
                consecutive_wins = int(getattr(dd, "consecutive_wins", 0))
            except Exception:
                pass

        exposure_pct = ((open_count / max_open) * risk_pct) if max_open > 0 else 0.0
        result: dict[str, Any] = {
            "mode": dd_mode,
            "risk_mode": dd_mode,
            "current_risk_pct": round(risk_pct, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "weekly_pnl_pct": round(weekly_pnl_pct, 2),
            "daily_loss_pct": round(abs(min(daily_pnl_pct / 100, 0.0)) * 100, 2),
            "max_daily_loss_pct": round(max_daily, 2),
            "score_threshold": score_threshold,
            "consecutive_losses": consecutive_losses,
            "consecutive_wins": consecutive_wins,
            "open_trade_count": open_count,
            "max_open_trades": max_open,
            "exposure_pct": round(exposure_pct, 2),
            "account_balance": round(balance, 2),
        }

        if ctx is not None and ctx.execution_monitor is not None:
            try:
                stats = ctx.execution_monitor.get_stats()
                result["execution_quality"] = stats.execution_quality
                result["avg_slippage_pips"] = stats.avg_slippage_pips
                result["avg_latency_ms"] = stats.avg_latency_ms
                result["spread_is_wide"] = stats.spread_is_wide
                result["requote_count"] = stats.requote_count
            except Exception:
                pass

        if ctx is not None and ctx.risk_reporter is not None and ctx.risk_engine is not None:
            try:
                _dash_risk = ctx.risk_engine.drawdown_guard.risk_map.get(
                    ctx.risk_engine.drawdown_guard.mode, 0.005
                )
                open_trades = [
                    {"pair": getattr(p, "symbol", ""), "direction": getattr(p, "direction", ""), "risk_pct": _dash_risk}
                    for p in (positions or [])
                ]
                report = ctx.risk_reporter.generate_report(
                    risk_engine=ctx.risk_engine,
                    pnl_tracker=ctx.risk_engine.pnl_tracker,
                    spread_monitor=ctx.risk_engine.spread_monitor,
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

        if ctx is not None and ctx.account_risk is not None:
            try:
                result["account_silos"] = ctx.account_risk.snapshot()
            except Exception:
                pass

        if ctx is not None and ctx.gate_tuner is not None:
            try:
                result["gate_offsets"] = ctx.gate_tuner.all_offsets()
            except Exception:
                pass

        return result
