"""
APEX TRADER — Live State Manager
Bridges the TradingLoop / PlatformManager to the dashboard API.
Falls back to simulated data when no platforms are connected.
"""

import random
import time as _time
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger


class LiveState:
    """
    Central state provider for the dashboard.

    When a TradingLoop is attached (live mode), all data comes from the
    real engine.  When nothing is attached, realistic simulated data is
    returned so the dashboard UI can still be exercised.
    """

    def __init__(self) -> None:
        self._trading_loop: Any = None
        self._platform_manager: Any = None
        self._start_time = _time.monotonic()
        self._running = False
        self._connection_status: dict[str, bool] = {}

        self._sim_trade_counter = 0
        self._sim_history: list[dict] = []
        self._sim_balance = 10000.0
        self._sim_pnl = 0.0

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

    # ── Status ────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        uptime = _time.monotonic() - self._start_time

        if self.is_live:
            return self._live_status(uptime)
        return self._sim_status(uptime)

    def _live_status(self, uptime: float) -> dict:
        loop = self._trading_loop
        pm = self._platform_manager

        balance = 10000.0
        try:
            balance = pm.get_total_balance() or 10000.0
        except Exception:
            pass

        dd = loop.drawdown.get_status(datetime.now(timezone.utc))
        journal = loop.journal

        total = journal.total_trades if hasattr(journal, "total_trades") else 0
        wins = journal.wins if hasattr(journal, "wins") else 0
        losses = journal.losses if hasattr(journal, "losses") else 0
        win_rate = (wins / total * 100) if total > 0 else 0.0

        return {
            "bot_status": "running" if loop.running else "stopped",
            "mode": "live",
            "uptime_seconds": round(uptime, 2),
            "risk_mode": dd.mode.value if hasattr(dd.mode, "value") else str(dd.mode),
            "win_rate": round(win_rate, 1),
            "total_trades": total,
            "win_count": wins,
            "loss_count": losses,
            "daily_pnl": round(getattr(dd, "daily_pnl_pct", 0.0) * balance / 100, 2),
            "account_balance": round(balance, 2),
            "daily_loss_pct": round(abs(getattr(dd, "daily_pnl_pct", 0.0)), 1),
            "max_daily_loss_pct": 5.0,
            "open_trade_count": len(loop.managed_positions),
            "consecutive_losses": getattr(dd, "consecutive_losses", 0),
            "consecutive_wins": getattr(dd, "consecutive_wins", 0),
            "mt5_connected": self._connection_status.get("mt5", False),
            "deriv_connected": self._connection_status.get("deriv", False),
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
            "account_balance": 10000.0,
            "daily_loss_pct": 0.0,
            "max_daily_loss_pct": 5.0,
            "open_trade_count": 0,
            "consecutive_losses": 0,
            "consecutive_wins": 0,
            "mt5_connected": False,
            "deriv_connected": False,
        }

    # ── Open trades ───────────────────────────────────────────────────────

    def get_open_trades(self) -> list[dict]:
        if self.is_live:
            return self._live_open_trades()
        return []

    def _live_open_trades(self) -> list[dict]:
        trades: list[dict] = []
        for oid, pos in self._trading_loop.managed_positions.items():
            trades.append({
                "order_id": oid,
                "symbol": pos.symbol,
                "direction": pos.direction,
                "lots": pos.lots,
                "entry_price": pos.entry_price,
                "sl": pos.sl,
                "tp1": pos.tp1,
                "tp2": pos.tp2,
                "score": pos.score,
                "regime": pos.regime,
                "session": pos.session,
                "tp1_hit": pos.tp1_hit,
                "at_breakeven": pos.at_breakeven,
                "trailing": pos.trailing,
                "open_time": pos.open_time.isoformat(),
                "platform": pos.platform,
            })
        return trades

    # ── Trade history ─────────────────────────────────────────────────────

    def get_trade_history(self) -> list[dict]:
        if self.is_live:
            return self._live_history()
        return []

    def _live_history(self) -> list[dict]:
        journal = self._trading_loop.journal
        if not hasattr(journal, "records"):
            return []
        records: list[dict] = []
        for r in journal.records:
            records.append({
                "symbol": getattr(r, "symbol", ""),
                "direction": getattr(r, "direction", ""),
                "entry_price": getattr(r, "entry_price", 0),
                "exit_price": getattr(r, "exit_price", 0),
                "pnl_pips": getattr(r, "pnl_pips", 0),
                "pnl_pct": getattr(r, "pnl_pct", 0),
                "outcome": getattr(r, "outcome", ""),
                "score": getattr(r, "score", 0),
                "regime": getattr(r, "regime", ""),
                "session": getattr(r, "session", ""),
                "open_time": str(getattr(r, "open_time", "")),
                "close_time": str(getattr(r, "close_time", "")),
            })
        return records

    # ── Scanner ───────────────────────────────────────────────────────────

    def get_scanner_results(self) -> list[dict]:
        if not self.is_live:
            return []
        scanner = self._trading_loop.scanner
        if not hasattr(scanner, "last_report") or scanner.last_report is None:
            return []
        results: list[dict] = []
        for r in scanner.last_report:
            results.append({
                "pair": getattr(r, "pair", ""),
                "score": getattr(r, "score", 0),
                "direction": getattr(r, "direction", ""),
                "status": getattr(r, "status", ""),
                "regime": getattr(r, "regime", ""),
            })
        return results

    # ── Risk ──────────────────────────────────────────────────────────────

    def get_risk_status(self) -> dict:
        if self.is_live:
            dd = self._trading_loop.drawdown.get_status(datetime.now(timezone.utc))
            return {
                "mode": dd.mode.value if hasattr(dd.mode, "value") else str(dd.mode),
                "current_risk_pct": getattr(dd, "current_risk_pct", 2.0),
                "daily_pnl_pct": getattr(dd, "daily_pnl_pct", 0.0),
                "weekly_pnl_pct": getattr(dd, "weekly_pnl_pct", 0.0),
                "score_threshold": getattr(dd, "score_threshold", 85),
                "consecutive_losses": getattr(dd, "consecutive_losses", 0),
                "open_trade_count": len(self._trading_loop.managed_positions),
                "max_open_trades": self._trading_loop.config.risk.max_open_trades,
            }
        return {
            "mode": "NORMAL",
            "current_risk_pct": 2.0,
            "daily_pnl_pct": 0.0,
            "weekly_pnl_pct": 0.0,
            "score_threshold": 85,
            "consecutive_losses": 0,
            "open_trade_count": 0,
            "max_open_trades": 6,
        }

    # ── Performance ───────────────────────────────────────────────────────

    def get_performance(self) -> dict:
        if self.is_live:
            journal = self._trading_loop.journal
            total = getattr(journal, "total_trades", 0)
            wins = getattr(journal, "wins", 0)
            losses = getattr(journal, "losses", 0)
            return {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / total * 100) if total > 0 else 0, 1),
                "best_trade_pips": getattr(journal, "best_trade_pips", 0),
                "worst_trade_pips": getattr(journal, "worst_trade_pips", 0),
                "avg_win_pips": getattr(journal, "avg_win_pips", 0),
                "avg_loss_pips": getattr(journal, "avg_loss_pips", 0),
                "profit_factor": getattr(journal, "profit_factor", 0),
                "daily_trades": self._trading_loop._daily_trades,
            }
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "best_trade_pips": 0,
            "worst_trade_pips": 0,
            "avg_win_pips": 0,
            "avg_loss_pips": 0,
            "profit_factor": 0,
            "daily_trades": 0,
        }

    # ── Controls ──────────────────────────────────────────────────────────

    def pause_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = False
            return {"status": "paused"}
        return {"status": "no_engine_attached"}

    def resume_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = True
            return {"status": "resumed"}
        return {"status": "no_engine_attached"}

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
        return {"status": "emergency_close_complete", "closed": closed}
