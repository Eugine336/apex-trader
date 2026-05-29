"""
APEX TRADER — Main Trading Loop
The heartbeat of the system.
Scan → Analyse → Trigger → Manage → Repeat.
Always watching. Always ready. In and out like a sniper.
"""

import time as _time
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from brain import (
    CorrelationEngine,
    DrawdownGuard,
    ExecutionMonitor,
    MTFOrchestrator,
    OpenTrade,
    SessionEngine,
    NewsGuard,
    TradeJournal,
    TradeRecord,
    DecisionRecord,
)
from config import AppConfig, INSTRUMENT_REGISTRY, get_pip_size
from platforms.base_connector import OrderResult, PositionInfo
from platforms.platform_manager import PlatformManager
from scanner import PairScanner, PairRanker, ScanScheduler


class ManagedPosition:
    """Tracks a live position through its lifecycle."""

    __slots__ = (
        "order_id", "platform", "symbol", "direction", "lots",
        "entry_price", "sl", "tp1", "tp2", "score", "regime",
        "session", "entry_type", "open_time", "tp1_hit",
        "at_breakeven", "trailing", "last_update",
    )

    def __init__(self, order: OrderResult, tp1: float, tp2: float,
                 score: int = 0, regime: str = "", session: str = "",
                 entry_type: str = ""):
        self.order_id = order.order_id
        self.platform = order.platform
        self.symbol = order.symbol
        self.direction = order.direction
        self.lots = order.lots
        self.entry_price = order.fill_price
        self.sl = order.sl
        self.tp1 = tp1
        self.tp2 = tp2
        self.score = score
        self.regime = regime
        self.session = session
        self.entry_type = entry_type
        self.open_time = datetime.now(timezone.utc)
        self.tp1_hit = False
        self.at_breakeven = False
        self.trailing = False
        self.last_update = datetime.now(timezone.utc)


class TradingLoop:
    """
    Master trading loop — orchestrates the full pipeline.
    Scan → Entry → Manage → Risk → Repeat.
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()
        self.platforms = PlatformManager(self.config)
        self.scanner = PairScanner(self.config)
        self.ranker = PairRanker()
        self.scheduler = ScanScheduler()
        self.orchestrator = MTFOrchestrator(min_entry_score=self.config.scoring.min_entry_score)
        self.drawdown = DrawdownGuard()
        self.correlation = CorrelationEngine()
        self.execution_monitor = ExecutionMonitor()
        self.session_engine = SessionEngine()
        self.news_guard = NewsGuard()
        self.journal = TradeJournal()

        self.managed_positions: dict[str, ManagedPosition] = {}
        self.running = False
        self._last_scan_time: Optional[datetime] = None
        self._daily_trades = 0
        self._last_reset_day: Optional[str] = None

    # ── Main loop ────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info("  APEX TRADER — GOING LIVE")
        logger.info("=" * 60)

        connection_status = self.platforms.connect_all()
        if not self.platforms.any_connected:
            logger.error("No platforms connected — cannot trade")
            return

        logger.info("Platforms: MT5={} | Deriv={}", connection_status["mt5"], connection_status["deriv"])
        self.running = True

        try:
            while self.running:
                self._check_daily_reset()
                self.run_once()
                interval = self._get_sleep_interval()
                _time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Shutdown signal received")
        finally:
            self.stop()

    def run_once(self) -> dict:
        """Single iteration — scan, enter, manage. Returns cycle summary."""
        now = datetime.now(timezone.utc)
        cycle: dict = {
            "timestamp": now.isoformat(),
            "scanned": False,
            "entries_attempted": 0,
            "entries_filled": 0,
            "positions_updated": 0,
            "positions_closed": 0,
        }

        session_status = self.session_engine.get_status(now)
        news_status = self.news_guard.check(self.config.enabled_pairs, now)

        can_trade, reason = self.drawdown.can_trade(now)
        if not can_trade:
            logger.debug("Trading paused: {}", reason)
            self._update_positions()
            return cycle

        should_scan = self.scheduler.should_scan_now(
            self._last_scan_time, session_status, news_status
        )

        if should_scan and session_status.is_tradeable:
            cycle["scanned"] = True
            self._scan_and_enter(session_status, news_status, now, cycle)
            self._last_scan_time = now

        closed_count = self._update_positions()
        cycle["positions_updated"] = len(self.managed_positions)
        cycle["positions_closed"] = closed_count

        return cycle

    def stop(self) -> None:
        self.running = False
        logger.info(
            "APEX TRADER SHUTTING DOWN — {} positions open, {} trades today",
            len(self.managed_positions), self._daily_trades,
        )
        self.platforms.disconnect_all()

    # ── Scan → Entry pipeline ────────────────────────────────────────────

    def _scan_and_enter(
        self, session_status, news_status, now: datetime, cycle: dict
    ) -> None:
        try:
            market_data = self.platforms.fetch_all_market_data()
        except Exception as exc:
            logger.error("Market data fetch failed: {}", exc)
            return

        if not market_data:
            return

        report = self.scanner.scan_all(market_data, utc_now=now)
        ready = self.scanner.get_ready_setups(report)

        if not ready:
            return

        open_pairs = [p.symbol for p in self.managed_positions.values()]
        ranked = self.ranker.rank(ready, open_pairs)
        top = self.ranker.get_top_n(ranked, n=3)

        for setup in top:
            result = setup.result
            if result.pair in open_pairs:
                continue

            open_trades = [
                OpenTrade(pair=p.symbol, direction=p.direction, risk_pct=0.02)
                for p in self.managed_positions.values()
            ]
            can_open, corr_reason = self.correlation.can_open_trade(
                result.pair, result.direction, open_trades
            )
            if not can_open:
                self._log_rejection(result.pair, result.direction, result.score, corr_reason)
                continue

            if len(self.managed_positions) >= self.config.risk.max_open_trades:
                self._log_rejection(result.pair, result.direction, result.score, "Max trades reached")
                break

            cycle["entries_attempted"] += 1
            filled = self._execute_entry(result, session_status.current_session, now)
            if filled:
                cycle["entries_filled"] += 1
                open_pairs.append(result.pair)

    def _execute_entry(self, result, session: str, now: datetime) -> bool:
        pair = result.pair
        direction = result.direction

        data = self.platforms.fetch_market_data(pair, ["H4", "H1", "M15", "M5", "M1"])
        if len(data) < 4:
            self._log_rejection(pair, direction, result.score, "Insufficient TF data")
            return False

        setup = self.orchestrator.build_setup(pair, data, now)
        if setup is None:
            self._log_rejection(pair, direction, result.score, "MTF orchestrator rejected")
            return False

        spread = 0.0
        try:
            spread = self.platforms.get_spread(pair)
        except Exception:
            pass

        pip_size = get_pip_size(pair)
        typical = INSTRUMENT_REGISTRY_SPREAD.get(pair, 2.0)
        if spread > typical * self.config.risk.max_spread_multiplier:
            self._log_rejection(pair, direction, result.score, f"Spread too wide: {spread}")
            return False

        dd_status = self.drawdown.get_status(now)
        risk_pct = dd_status.current_risk_pct
        balance = self.platforms.get_total_balance() or 10000
        risk_pips = abs(setup.entry_price - setup.stop_loss) / pip_size
        if risk_pips <= 0:
            self._log_rejection(pair, direction, result.score, "Invalid SL distance")
            return False

        risk_amount = balance * risk_pct
        pip_value = INSTRUMENT_REGISTRY_PIP_VALUE.get(pair, 10.0)
        lots = round(risk_amount / (risk_pips * pip_value), 2)
        lots = max(0.01, min(lots, 10.0))

        order = self.platforms.execute_entry(
            pair, direction, lots, setup.stop_loss, setup.tp1,
            comment=f"APEX|{result.score}|{session}",
        )

        if not order.success:
            return False

        self.execution_monitor.record_execution(
            requested_price=order.requested_price,
            filled_price=order.fill_price,
            signal_timestamp=now,
            fill_timestamp=datetime.now(timezone.utc),
            spread=spread,
            requote=False,
        )

        managed = ManagedPosition(
            order=order, tp1=setup.tp1, tp2=setup.tp2,
            score=setup.score, regime=setup.regime, session=session,
            entry_type="MTF",
        )
        self.managed_positions[order.order_id] = managed
        self._daily_trades += 1

        logger.info(
            "🎯 TRADE OPENED — {} {} {:.2f}lots @ {:.5f} | SL {:.5f} | TP1 {:.5f} | TP2 {:.5f} | Score {}",
            direction, pair, lots, order.fill_price,
            setup.stop_loss, setup.tp1, setup.tp2, setup.score,
        )
        return True

    # ── Position management ──────────────────────────────────────────────

    def _update_positions(self) -> int:
        closed_count = 0
        to_remove: list[str] = []

        for oid, pos in self.managed_positions.items():
            try:
                tick = self.platforms.get_price(pos.symbol)
            except Exception:
                continue

            current = tick.bid if pos.direction == "BUY" else tick.ask
            pip_size = get_pip_size(pos.symbol)

            is_buy = pos.direction == "BUY"
            pnl_pips = (current - pos.entry_price) / pip_size if is_buy else (pos.entry_price - current) / pip_size

            if self._check_sl(pos, current, is_buy):
                result = self.platforms.close_trade(oid, pos.platform)
                if result.success:
                    self._record_closed_trade(pos, result.close_price, "SL_HIT")
                    to_remove.append(oid)
                    closed_count += 1
                continue

            if not pos.tp1_hit and self._check_tp1(pos, current, is_buy):
                partial_lots = round(pos.lots * 0.5, 2)
                partial_lots = max(0.01, partial_lots)
                result = self.platforms.close_trade(oid, pos.platform, partial_lots)
                if result.success:
                    pos.tp1_hit = True
                    pos.lots = round(pos.lots - partial_lots, 2)
                    be_price = pos.entry_price + (2 * pip_size if is_buy else -2 * pip_size)
                    self.platforms.modify_trade(oid, pos.platform, new_sl=be_price)
                    pos.sl = be_price
                    pos.at_breakeven = True
                    logger.info("✅ TP1 HIT — {} {} | 50% closed, SL→BE", pos.direction, pos.symbol)

            if pos.tp1_hit and self._check_tp2(pos, current, is_buy):
                result = self.platforms.close_trade(oid, pos.platform)
                if result.success:
                    self._record_closed_trade(pos, result.close_price, "TP2_HIT")
                    to_remove.append(oid)
                    closed_count += 1
                    logger.info("🏆 TP2 HIT — {} {} | FULL CLOSE", pos.direction, pos.symbol)
                continue

            if pos.at_breakeven and not pos.trailing:
                tp1_dist = abs(pos.tp1 - pos.entry_price)
                progress = abs(current - pos.entry_price)
                if progress > tp1_dist * 1.2:
                    pos.trailing = True

            if pos.trailing:
                trail_dist = abs(pos.tp1 - pos.entry_price) * 0.4
                new_sl = (current - trail_dist) if is_buy else (current + trail_dist)
                if (is_buy and new_sl > pos.sl) or (not is_buy and new_sl < pos.sl):
                    self.platforms.modify_trade(oid, pos.platform, new_sl=new_sl)
                    pos.sl = new_sl

            stall_minutes = (datetime.now(timezone.utc) - pos.open_time).total_seconds() / 60
            if stall_minutes > 75 and abs(pnl_pips) < 5 and not pos.tp1_hit:
                result = self.platforms.close_trade(oid, pos.platform)
                if result.success:
                    self._record_closed_trade(pos, result.close_price, "STALL_EXIT")
                    to_remove.append(oid)
                    closed_count += 1
                    logger.info("⏱ STALL EXIT — {} {} | {:.0f}min, {:.1f}pip", pos.direction, pos.symbol, stall_minutes, pnl_pips)

            pos.last_update = datetime.now(timezone.utc)

        for oid in to_remove:
            del self.managed_positions[oid]

        return closed_count

    def _check_sl(self, pos: ManagedPosition, price: float, is_buy: bool) -> bool:
        if is_buy:
            return price <= pos.sl
        return price >= pos.sl

    def _check_tp1(self, pos: ManagedPosition, price: float, is_buy: bool) -> bool:
        if is_buy:
            return price >= pos.tp1
        return price <= pos.tp1

    def _check_tp2(self, pos: ManagedPosition, price: float, is_buy: bool) -> bool:
        if is_buy:
            return price >= pos.tp2
        return price <= pos.tp2

    # ── Logging & journal ────────────────────────────────────────────────

    def _record_closed_trade(
        self, pos: ManagedPosition, close_price: float, outcome: str
    ) -> None:
        pip_size = get_pip_size(pos.symbol)
        is_buy = pos.direction == "BUY"
        pnl_pips = (close_price - pos.entry_price) / pip_size if is_buy else (pos.entry_price - close_price) / pip_size
        pnl_pct = pnl_pips * 0.0002

        self.drawdown.register_trade_result(pnl_pct)

        hold_seconds = (datetime.now(timezone.utc) - pos.open_time).total_seconds()
        logger.info(
            "📊 TRADE CLOSED — {} {} | {:.1f}pip | {} | {:.0f}s",
            pos.direction, pos.symbol, pnl_pips, outcome, hold_seconds,
        )

    def _log_rejection(self, pair: str, direction: str, score: int, reason: str) -> None:
        logger.debug("❌ REJECTED {} {} (score {}) — {}", direction, pair, score, reason)

    # ── Daily reset ──────────────────────────────────────────────────────

    def _check_daily_reset(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_reset_day != today:
            if self._last_reset_day is not None:
                logger.info("📅 Daily reset — {} trades yesterday", self._daily_trades)
            self._daily_trades = 0
            self._last_reset_day = today

    def _get_sleep_interval(self) -> float:
        now = datetime.now(timezone.utc)
        session_status = self.session_engine.get_status(now)
        news_status = self.news_guard.check(self.config.enabled_pairs, now)
        return self.scheduler.get_scan_interval(session_status, news_status)


def _build_instrument_lookups() -> tuple[dict[str, float], dict[str, float]]:
    spreads: dict[str, float] = {}
    pip_values: dict[str, float] = {}
    for sym, info in INSTRUMENT_REGISTRY.items():
        spreads[sym] = info.typical_spread_pips
        pip_values[sym] = info.pip_value_per_lot
    return spreads, pip_values


INSTRUMENT_REGISTRY_SPREAD, INSTRUMENT_REGISTRY_PIP_VALUE = _build_instrument_lookups()
