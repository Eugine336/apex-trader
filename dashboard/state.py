"""
APEX TRADER — Live State Manager
Bridges TradingLoop / PlatformManager to dashboard API responses.
"""

import asyncio
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger

from config import INSTRUMENT_REGISTRY, get_instrument, get_pip_size


class LiveState:
    """
    Central state provider for the dashboard.

    When a TradingLoop is attached (live mode), all data comes from the
    real engine. When nothing is attached, stable fallback shapes are
    returned so the frontend never breaks.
    """

    def __init__(self) -> None:
        self._trading_loop: Any = None
        self._platform_manager: Any = None
        self._start_time = _time.monotonic()
        self._running = False
        self._connection_status: dict[str, bool] = {}
        # ── journal cache: populated by _refresh_journal_cache() ──────
        self._journal_cache: list[dict] = []
        self._journal_cache_ts: float = 0.0
        self._journal_cache_ttl: float = 5.0   # seconds

    # ── Attach live engine ────────────────────────────────────────────────

    def attach(
        self,
        trading_loop: Any,
        platform_manager: Any,
        connection_status: dict[str, bool],
    ) -> None:
        self._trading_loop = trading_loop
        self._platform_manager = platform_manager
        self._connection_status = connection_status
        self._running = True
        logger.info("LiveState attached — dashboard serving real data")

    @property
    def is_live(self) -> bool:
        return self._trading_loop is not None and self._running

    # ── Journal cache (bridges async SQLite → sync REST handlers) ─────────

    def _refresh_journal_cache(self) -> list[dict]:
        """
        Read closed trades from the SQLite-backed journal.
        Results are cached for _journal_cache_ttl seconds to avoid hammering
        SQLite on every API call.

        Uses the trading loop's dedicated _journal_loop (a separate thread's
        event loop) so we never conflict with FastAPI's async event loop.
        """
        now = _time.monotonic()
        if now - self._journal_cache_ts < self._journal_cache_ttl:
            return self._journal_cache

        journal = getattr(self._trading_loop, "journal", None) if self.is_live else None
        if journal is None:
            return self._journal_cache

        try:
            # Use the trading loop's dedicated journal event loop (runs in its
            # own thread — safe to call run_until_complete from any context).
            loop: asyncio.AbstractEventLoop = getattr(
                self._trading_loop, "_journal_loop", None
            )
            # Always run on a dedicated thread to avoid conflicting with
            # FastAPI/uvicorn's event loop which owns the current thread.
            import concurrent.futures

            def _run_in_thread():
                thread_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(thread_loop)
                try:
                    return thread_loop.run_until_complete(
                        journal.get_all_trades_as_dicts()
                    )
                finally:
                    thread_loop.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                rows: list[dict] = ex.submit(_run_in_thread).result(timeout=5.0)

            self._journal_cache = rows
            self._journal_cache_ts = now
        except Exception as exc:
            logger.warning("Journal cache refresh failed: {}", exc)

        return self._journal_cache

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _value(obj: Any, *keys: str, default: Any = None) -> Any:
        for key in keys:
            if isinstance(obj, dict) and key in obj:
                return obj[key]
            if hasattr(obj, key):
                return getattr(obj, key)
        return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _pct_to_fraction(value: Any) -> float:
        raw = LiveState._safe_float(value, 0.0)
        return raw / 100.0 if abs(raw) > 1 else raw

    @staticmethod
    def _normalize_direction(direction: Any) -> str:
        raw = str(direction or "").upper()
        if raw in {"BUY", "LONG"}:
            return "LONG"
        if raw in {"SELL", "SHORT"}:
            return "SHORT"
        return "NEUTRAL"

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text)
        except Exception:
            return None

    def _get_balance(self) -> float:
        if not self.is_live:
            return 10000.0
        try:
            bal = self._platform_manager.get_total_balance()
            return round(float(bal), 2) if bal else 10000.0
        except Exception:
            return 10000.0

    def _get_platform_balances(self) -> tuple[float, float]:
        if not self.is_live:
            return 0.0, 0.0
        try:
            summary = self._platform_manager.get_account_summary()
            mt5_info = summary.get("mt5")
            deriv_info = summary.get("deriv")
            mt5_balance = self._safe_float(getattr(mt5_info, "balance", 0.0), 0.0)
            deriv_balance = self._safe_float(getattr(deriv_info, "balance", 0.0), 0.0)
            return round(mt5_balance, 2), round(deriv_balance, 2)
        except Exception:
            return 0.0, 0.0

    def _estimate_pip_value(self, symbol: str) -> float:
        try:
            return float(get_instrument(symbol).pip_value_per_lot)
        except Exception:
            return 10.0

    def _build_history_rows(self, balance: float) -> list[dict[str, Any]]:
        """
        Build normalised history rows from the journal SQLite DB.
        TradeRecord fields: pair, direction, entry, exit, pnl, score,
        confluences, regime, session, spread, slippage, entry_type,
        time_to_tp1, time_to_exit, outcome, timestamp
        """
        raw_rows = self._refresh_journal_cache()
        rows: list[dict[str, Any]] = []

        for i, record in enumerate(raw_rows):
            # record is a plain dict from get_all_trades_as_dicts()
            symbol = str(record.get("pair", "")).upper()
            direction = self._normalize_direction(record.get("direction", ""))
            entry_price = self._safe_float(record.get("entry", record.get("entry_price", 0.0)))
            exit_price = self._safe_float(record.get("exit", record.get("exit_price", entry_price)))

            pnl_raw = self._safe_float(record.get("pnl", 0.0))
            # journal stores pnl as raw P&L in account currency (dollars)
            pnl_dollars = pnl_raw

            pip_size = self._safe_float(get_pip_size(symbol), 0.0001) or 0.0001
            sl_distance = abs(exit_price - entry_price)
            pnl_pips = sl_distance / pip_size if sl_distance > 0 else 0.0
            if pnl_raw < 0:
                pnl_pips = -pnl_pips

            time_to_exit = self._safe_float(record.get("time_to_exit"), 0.0)
            duration_minutes = time_to_exit / 60.0 if time_to_exit > 0 else 0.0

            outcome = str(record.get("outcome", "")).upper()
            if outcome not in {"WIN", "LOSS"}:
                outcome = "WIN" if pnl_raw >= 0 else "LOSS"

            ts = record.get("timestamp", datetime.now(timezone.utc).isoformat())
            opened_at = str(ts) if ts else datetime.now(timezone.utc).isoformat()

            rows.append({
                "id": str(record.get("id", f"hist_{i}")),
                "instrument": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pips": round(pnl_pips, 1),
                "pnl_dollars": round(pnl_dollars, 2),
                "duration_minutes": round(duration_minutes, 1),
                "score": int(round(self._safe_float(record.get("score", 0)))),
                "outcome": outcome,
                "opened_at": opened_at,
            })

        return rows

    @staticmethod
    def _build_stage(pos: Any) -> str:
        if bool(getattr(pos, "trailing", False)):
            return "TRAILING"
        if bool(getattr(pos, "at_breakeven", False)):
            return "BREAKEVEN"
        if bool(getattr(pos, "tp1_hit", False)):
            return "TP1_HIT"
        return "OPEN"

    @staticmethod
    def _base_factor_set() -> dict[str, int]:
        return {
            "structure": 0,
            "fvg": 0,
            "ob": 0,
            "liquidity": 0,
            "sweep": 0,
            "session": 0,
            "strength": 0,
        }

    def _scanner_factors(self, result: Any) -> dict[str, int]:
        bias_strength = str(
            self._value(result, "bias_strength", default="")
        ).upper()
        structure_pts = 15 if bias_strength == "STRONG" else 10 if bias_strength == "MODERATE" else 5

        return {
            "structure": structure_pts,
            "fvg": 10 if bool(self._value(result, "has_fvg", default=False)) else 0,
            "ob": 10 if bool(self._value(result, "has_order_block", default=False)) else 0,
            "liquidity": 10 if bool(self._value(result, "has_liquidity_target", default=False)) else 0,
            "sweep": 10 if bool(self._value(result, "sweep_detected", default=False)) else 0,
            "session": 10 if bool(self._value(result, "session_active", default=False)) else 0,
            "strength": 10 if bool(self._value(result, "currency_strength_aligned", default=False)) else 0,
        }

    # ── Status ────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
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
        daily_frac = self._pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0))
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

    def _sim_status(self, uptime: float) -> dict:
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

    # ── Open trades ───────────────────────────────────────────────────────

    def get_open_trades(self) -> dict:
        if self.is_live:
            return self._live_open_trades()
        return {"trades": [], "count": 0}

    def _live_open_trades(self) -> dict:
        trades: list[dict[str, Any]] = []
        positions = getattr(self._trading_loop, "managed_positions", {})

        for oid, pos in positions.items():
            symbol = str(getattr(pos, "symbol", ""))
            direction = self._normalize_direction(getattr(pos, "direction", ""))
            entry_price = self._safe_float(getattr(pos, "entry_price", 0.0), 0.0)
            current_price = entry_price

            try:
                tick = self._platform_manager.get_price(symbol)
                if direction == "LONG":
                    current_price = self._safe_float(getattr(tick, "bid", entry_price), entry_price)
                elif direction == "SHORT":
                    current_price = self._safe_float(getattr(tick, "ask", entry_price), entry_price)
            except Exception:
                pass

            pip_size = self._safe_float(get_pip_size(symbol), 0.0001)
            if pip_size <= 0:
                pip_size = 0.0001

            if direction == "LONG":
                pnl_pips = (current_price - entry_price) / pip_size
            elif direction == "SHORT":
                pnl_pips = (entry_price - current_price) / pip_size
            else:
                pnl_pips = 0.0

            lot_size = self._safe_float(getattr(pos, "lots", 0.0), 0.0)
            pip_value = self._estimate_pip_value(symbol)
            pnl_dollars = pnl_pips * pip_value * lot_size

            trades.append({
                "id": str(oid),
                "instrument": symbol,
                "direction": direction,
                "entry_price": round(entry_price, 5),
                "current_price": round(current_price, 5),
                "stop_loss": round(self._safe_float(getattr(pos, "sl", 0.0), 0.0), 5),
                "tp1": round(self._safe_float(getattr(pos, "tp1", 0.0), 0.0), 5),
                "tp2": round(self._safe_float(getattr(pos, "tp2", 0.0), 0.0), 5),
                "pnl_pips": round(pnl_pips, 1),
                "pnl_dollars": round(pnl_dollars, 2),
                "lot_size": round(lot_size, 2),
                "score": int(round(self._safe_float(getattr(pos, "score", 0), 0.0))),
                "stage": self._build_stage(pos),
            })

        return {"trades": trades, "count": len(trades)}

    # ── Trade history ─────────────────────────────────────────────────────

    def get_trade_history(self) -> dict:
        if not self.is_live:
            return {"trades": []}

        balance = self._get_balance()
        rows = self._build_history_rows(balance)
        rows.sort(key=lambda r: r["opened_at"], reverse=True)
        return {"trades": rows}

    # ── Scanner ───────────────────────────────────────────────────────────

    def get_scanner_results(self) -> dict:
        if not self.is_live:
            return {
                "instruments": [],
                "ready_count": 0,
                "watchlist_count": 0,
                "total_count": 0,
            }

        # Read directly from scanner.last_report — it's always up to date
        scanner = getattr(self._trading_loop, "scanner", None)
        report = getattr(scanner, "last_report", None) if scanner else None

        results_raw: list[Any] = []
        ready_count = 0
        watchlist_count = 0
        total_count = 0

        if report is not None:
            if hasattr(report, "results"):
                results_raw = list(getattr(report, "results", []))
                ready_count = int(getattr(report, "ready_count", 0))
                watchlist_count = int(getattr(report, "watchlist_count", 0))
                total_count = int(
                    getattr(report, "total_pairs_scanned", len(results_raw))
                )
            elif isinstance(report, list):
                results_raw = list(report)

        instruments: list[dict[str, Any]] = []
        for result in results_raw:
            symbol = str(self._value(result, "pair", "symbol", default="")).upper()
            if not symbol:
                continue

            try:
                info = get_instrument(symbol)
                name = info.name
                category = info.category.value
            except Exception:
                name = symbol
                category = str(
                    self._value(result, "instrument_category", "category", default="forex")
                ).lower()

            status = str(self._value(result, "status", default="WAITING")).upper()
            direction = self._normalize_direction(
                self._value(result, "direction", default="NEUTRAL")
            )

            instruments.append({
                "symbol": symbol,
                "name": name,
                "category": category,
                "direction": direction,
                "score": int(round(self._safe_float(self._value(result, "score", default=0), 0.0))),
                "status": status,
                "factors": self._scanner_factors(result),
            })

        if ready_count == 0 and watchlist_count == 0 and instruments:
            ready_count = sum(1 for i in instruments if i["status"] == "READY")
            watchlist_count = sum(1 for i in instruments if i["status"] == "WATCHLIST")

        instruments.sort(key=lambda i: i["score"], reverse=True)

        # Fallback before first scan completes
        if not instruments:
            enabled_symbols: list[str] = []
            try:
                enabled_symbols = list(getattr(self._trading_loop.config, "enabled_pairs", []))
            except Exception:
                enabled_symbols = list(INSTRUMENT_REGISTRY.keys())

            for symbol in enabled_symbols:
                try:
                    info = get_instrument(symbol)
                    name = info.name
                    category = info.category.value
                except Exception:
                    name = symbol
                    category = "forex"
                instruments.append({
                    "symbol": symbol,
                    "name": name,
                    "category": category,
                    "direction": "NEUTRAL",
                    "score": 0,
                    "status": "WAITING",
                    "factors": self._base_factor_set(),
                })

        if total_count == 0:
            total_count = len(instruments)

        return {
            "instruments": instruments,
            "ready_count": ready_count,
            "watchlist_count": watchlist_count,
            "total_count": total_count,
        }

    # ── Risk ──────────────────────────────────────────────────────────────

    def get_risk_status(self) -> dict:
        if self.is_live:
            loop = self._trading_loop
            dd = loop.drawdown.get_status(datetime.now(timezone.utc))
            max_open = int(getattr(loop.config.risk, "max_open_trades", 6))
            open_count = len(getattr(loop, "managed_positions", {}))
            balance = self._get_balance()

            risk_raw = self._safe_float(getattr(dd, "current_risk_pct", 2.0), 2.0)
            risk_pct = risk_raw * 100 if risk_raw <= 1 else risk_raw
            daily_frac = self._pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0))
            weekly_frac = self._pct_to_fraction(getattr(dd, "weekly_pnl_pct", 0.0))
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
                        open_trades = [
                            {"pair": p.symbol, "direction": p.direction, "risk_pct": 0.02}
                            for p in loop.managed_positions.values()
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

    # ── Performance ───────────────────────────────────────────────────────

    def get_performance(self) -> dict:
        today = datetime.now(timezone.utc).date()
        if not self.is_live:
            date_key = today.isoformat()
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "daily_pnl": 0.0,
                "weekly_pnl": 0.0,
                "monthly_pnl": 0.0,
                "total_pnl": 0.0,
                "best_trade_pips": 0.0,
                "worst_trade_pips": 0.0,
                "avg_win_pips": 0.0,
                "avg_loss_pips": 0.0,
                "profit_factor": 0.0,
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

        pnl_pips = [self._safe_float(r["pnl_pips"], 0.0) for r in rows]
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
            date_pnl[key] += self._safe_float(row["pnl_dollars"], 0.0)

        daily_pnl = 0.0
        weekly_pnl = 0.0
        monthly_pnl = 0.0
        for date_key, pnl in date_pnl.items():
            try:
                d = datetime.fromisoformat(date_key).date()
            except Exception:
                continue
            if d == today:
                daily_pnl += pnl
            if d >= today - timedelta(days=6):
                weekly_pnl += pnl
            if d >= today - timedelta(days=29):
                monthly_pnl += pnl

        if not rows:
            daily_frac = self._pct_to_fraction(getattr(dd, "daily_pnl_pct", 0.0))
            weekly_frac = self._pct_to_fraction(getattr(dd, "weekly_pnl_pct", 0.0))
            daily_pnl = daily_frac * balance
            weekly_pnl = weekly_frac * balance

        total_pnl = sum(self._safe_float(r["pnl_dollars"], 0.0) for r in rows)

        rows_asc = sorted(rows, key=lambda r: r["opened_at"])
        start_balance = balance - total_pnl
        if start_balance <= 0:
            start_balance = 10000.0

        equity_curve: list[dict[str, Any]] = []
        running = start_balance
        for row in rows_asc:
            running += self._safe_float(row["pnl_dollars"], 0.0)
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
            "wins": wins,
            "losses": losses,
            "win_count": wins,
            "loss_count": losses,
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

    # ── ML Insights ───────────────────────────────────────────────────────

    def get_ml_insights(self) -> dict:
        if not self.is_live:
            return {
                "score_adjustments": {},
                "regime_stats": {},
                "session_stats": {},
                "pair_stats": {},
            }
        ml = getattr(self._trading_loop, "ml", None)
        if ml is None:
            return {
                "score_adjustments": {},
                "regime_stats": {},
                "session_stats": {},
                "pair_stats": {},
            }
        try:
            optimizer = getattr(ml, "score_optimizer", None)
            regime_learner = getattr(ml, "regime_learner", None)
            session_learner = getattr(ml, "session_learner", None)
            pair_learner = getattr(ml, "pair_learner", None)

            score_adjustments: dict = {}
            if optimizer is not None:
                defaults = getattr(optimizer, "_DEFAULT_WEIGHTS", {})
                current = getattr(optimizer, "_weights", defaults)
                for k, v in current.items():
                    score_adjustments[k] = round(v - defaults.get(k, v), 1)

            regime_stats: dict = {}
            if regime_learner is not None and hasattr(regime_learner, "_strategies"):
                for regime, strategy in regime_learner._strategies.items():
                    regime_stats[regime] = {
                        "win_rate": round(getattr(strategy, "win_rate", 0) * 100, 1),
                        "trades": getattr(strategy, "trade_count", 0),
                        "recommendation": "trade" if getattr(strategy, "should_trade", True) else "skip",
                    }

            session_stats: dict = {}
            if session_learner is not None and hasattr(session_learner, "_profiles"):
                for session, profile in session_learner._profiles.items():
                    session_stats[session] = {
                        "win_rate": round(getattr(profile, "win_rate", 0) * 100, 1),
                        "trades": getattr(profile, "trade_count", 0),
                        "aggression": getattr(profile, "aggression", "normal"),
                    }

            pair_stats: dict = {}
            if pair_learner is not None and hasattr(pair_learner, "_profiles"):
                for pair, profile in pair_learner._profiles.items():
                    pair_stats[pair] = {
                        "win_rate": round(getattr(profile, "win_rate", 0) * 100, 1),
                        "trades": getattr(profile, "trade_count", 0),
                        "size_mult": round(getattr(profile, "size_multiplier", 1.0), 2),
                    }

            return {
                "score_adjustments": score_adjustments,
                "regime_stats": regime_stats,
                "session_stats": session_stats,
                "pair_stats": pair_stats,
            }
        except Exception as exc:
            logger.warning("ML insights error: {}", exc)
            return {"score_adjustments": {}, "regime_stats": {}, "session_stats": {}, "pair_stats": {}}

    # ── Controls ──────────────────────────────────────────────────────────

    def start_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = True
            return {"status": "started", "message": "Trading started"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def stop_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = False
            return {"status": "stopped", "message": "Trading stopped"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def pause_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = False
            return {"status": "paused", "message": "Trading paused"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def resume_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = True
            return {"status": "resumed", "message": "Trading resumed"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def set_risk_mode(self, mode: Optional[str]) -> dict:
        if not self.is_live or mode is None:
            return {"status": "no_engine_attached", "message": "No engine attached"}
        try:
            dd = self._trading_loop.drawdown
            from risk.risk_engine import DrawdownMode
            dd_mode = DrawdownMode(mode.upper())
            dd._mode = dd_mode
            return {"status": "ok", "message": f"Risk mode set to {mode}", "mode": mode}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def emergency_close_all(self) -> dict:
        if not self.is_live:
            return {"status": "no_engine_attached", "closed": 0}
        closed = 0
        for oid, pos in list(self._trading_loop.managed_positions.items()):
            try:
                result = self._platform_manager.close_trade(oid, pos.platform)
                if result.success:
                    closed += 1
            except Exception as exc:
                logger.error("Emergency close failed for {}: {}", oid, exc)
        self._trading_loop.managed_positions.clear()
        return {"status": "emergency_close_complete", "closed": closed, "message": f"Closed {closed} positions"}
