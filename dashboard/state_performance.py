"""APEX TRADER — Dashboard Performance Mixin."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from dashboard.state_helpers import HelpersMixin, pct_to_fraction, safe_float
from loguru import logger


_REVERSAL_TAG = "REVERSAL_TRADE"


def _summarize_reversal_split(trades: list[dict]) -> dict:
    """Split realized journal trades into REVERSAL vs CONTINUATION and report
    count / win-rate / EV for each. Reversal trades are tagged at entry time
    with the ``REVERSAL_TRADE`` confluence (roadmap E). EV is the count-weighted
    mean R (pnl_dollars / risk_dollars) when risk is known, else the average
    dollar P&L is reported via avg_pnl. Lets an operator see whether the
    counter-trend reversal book is actually paying for itself."""
    def _blank() -> dict:
        return {
            "count": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "ev_r": 0.0, "ev_samples": 0, "avg_pnl": 0.0, "total_pnl": 0.0,
        }

    out = {"reversal": _blank(), "continuation": _blank()}
    acc = {
        "reversal": {"pnl": 0.0, "r_sum": 0.0, "r_n": 0},
        "continuation": {"pnl": 0.0, "r_sum": 0.0, "r_n": 0},
    }
    for t in trades or []:
        conf = t.get("confluences_raw") or t.get("confluences") or []
        is_rev = any(_REVERSAL_TAG in str(c).upper() for c in conf)
        key = "reversal" if is_rev else "continuation"
        g, s = out[key], acc[key]
        pnl_d = t.get("pnl_dollars")
        pnl_pips = t.get("pnl")
        if isinstance(pnl_d, (int, float)):
            won, lost = pnl_d > 0, pnl_d < 0
            s["pnl"] += float(pnl_d)
        elif isinstance(pnl_pips, (int, float)):
            won, lost = pnl_pips > 0, pnl_pips < 0
        else:
            won = lost = False
        g["count"] += 1
        if won:
            g["wins"] += 1
        elif lost:
            g["losses"] += 1
        risk_d = t.get("risk_dollars")
        if isinstance(risk_d, (int, float)) and risk_d > 0 and isinstance(pnl_d, (int, float)):
            s["r_sum"] += pnl_d / risk_d
            s["r_n"] += 1

    for key in out:
        g, s = out[key], acc[key]
        decisive = g["wins"] + g["losses"]
        g["win_rate"] = round(g["wins"] / decisive * 100, 1) if decisive else 0.0
        g["avg_pnl"] = round(s["pnl"] / g["count"], 2) if g["count"] else 0.0
        g["total_pnl"] = round(s["pnl"], 2)
        g["ev_samples"] = s["r_n"]
        g["ev_r"] = round(s["r_sum"] / s["r_n"], 3) if s["r_n"] else 0.0
    return out


class PerformanceMixin(HelpersMixin):
    """get_performance()."""

    def get_reversal_breakdown(self) -> dict:
        """Realized EV/win-rate of counter-trend REVERSAL trades vs everything
        else (roadmap E tracking). Reads the journal cache; empty when offline."""
        try:
            trades = self._refresh_journal_cache() if self.is_live else []
        except Exception as exc:
            logger.debug("[dashboard] reversal breakdown read failed: {}", exc)
            trades = []
        return _summarize_reversal_split(trades)

    def get_system_performance(self) -> dict:
        """System (not trading) performance for the dashboard Performance panel:
        candle-cache hit rate, recent scan-cycle durations, and configured
        parallelism. All reads are defensive — returns zeros when offline."""
        empty = {
            "candle_cache": {
                "hits": 0, "misses": 0, "expired": 0, "stores": 0,
                "total": 0, "hit_rate": 0.0, "entries": 0,
                "per_tf_hits": {}, "per_tf_misses": {},
            },
            "cycle": {
                "samples": 0, "last_ms": 0.0, "avg_ms": 0.0,
                "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0,
            },
            "parallel_scan": {"enabled": False, "max_workers": 0},
            "account_info_cache_enabled": False,
        }
        if not self.is_live:
            return empty

        loop = self._trading_loop
        ed = getattr(self, "_event_driven_system", None)
        out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in empty.items()}

        # ── ED system stats ──
        if ed is not None:
            try:
                ed_stats = ed.stats()
                tr = ed_stats.get("tick_router", {})
                ch = ed_stats.get("candle_handler", {})
                out["cycle"] = {
                    "samples": ed_stats.get("position_evals", 0),
                    "last_ms": 0.0,
                    "avg_ms": 0.0,
                    "p50_ms": 0.0,
                    "p95_ms": 0.0,
                    "max_ms": 0.0,
                }
                out["candle_cache"]["hits"] = ch.get("candles_fetched", 0)
                out["candle_cache"]["total"] = ch.get("models_built", 0)
                out["parallel_scan"]["enabled"] = True
                return out
            except Exception:
                pass

        if loop is None:
            return out

        # ── Candle cache stats ──
        try:
            cache = getattr(loop.platforms, "candle_cache", None)
            if cache is not None:
                out["candle_cache"] = cache.cache_stats()
        except Exception as exc:
            logger.debug("[dashboard] candle cache stats read failed: {}", exc)

        # ── Recent scan-cycle durations ──
        try:
            durations = list(getattr(loop, "_cycle_durations_ms", []) or [])
            if durations:
                ordered = sorted(durations)
                n = len(ordered)
                out["cycle"] = {
                    "samples": n,
                    "last_ms": round(durations[-1], 1),
                    "avg_ms": round(sum(ordered) / n, 1),
                    "p50_ms": round(ordered[int(n * 0.50)] if n else 0.0, 1),
                    "p95_ms": round(ordered[min(n - 1, int(n * 0.95))] if n else 0.0, 1),
                    "max_ms": round(ordered[-1], 1),
                }
        except Exception as exc:
            logger.debug("[dashboard] cycle timing read failed: {}", exc)

        # ── Configured parallelism / caching ──
        try:
            perf = getattr(loop.config, "performance", None)
            if perf is not None:
                out["parallel_scan"] = {
                    "enabled": bool(getattr(perf, "parallel_scan_enabled", False)),
                    "max_workers": int(getattr(perf, "parallel_scan_max_workers", 0)),
                }
                out["account_info_cache_enabled"] = bool(
                    getattr(perf, "account_info_cache_enabled", False)
                )
        except Exception as exc:
            logger.debug("[dashboard] perf config read failed: {}", exc)

        return out

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

        ed = getattr(self, "_event_driven_system", None)
        ctx = getattr(ed, "_ctx", None) if ed is not None else None

        dd = None
        if loop is not None:
            dd = loop.drawdown.get_status(datetime.now(timezone.utc))
        elif ctx is not None and ctx.drawdown_guard is not None:
            dd = ctx.drawdown_guard.get_status(datetime.now(timezone.utc))

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
            "daily_trades": int(getattr(loop, "_daily_trades", 0)) if loop is not None else 0,
            "equity_curve": equity_curve,
            "pnl_history": pnl_history,
        }
