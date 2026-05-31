"""
APEX TRADER — Main Trading Loop
The heartbeat of the system.
Scan → Analyse → Trigger → Manage → Repeat.
Always watching. Always ready. In and out like a sniper.
"""

import asyncio
import time as _time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pandas as pd
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
from management.re_entry import ReEntryManager
from management.trade_manager import (
    TradeManager,
    TradeStatus,
    TERMINAL_STATUSES,
    EntrySignal as TMEntrySignal,
)
from ml.ml_adapter import MLAdapter, TradeAdjustments
from platforms.base_connector import OrderResult, PositionInfo
from platforms.platform_manager import PlatformManager
from platforms.platform_context import PlatformContext, build_context_for_symbol
from risk.risk_engine import RiskEngine
from risk.risk_reporter import RiskReporter
from scanner import PairScanner, PairRanker, ScanScheduler
from trigger.entry_engine import EntryEngine, EntrySignal, EntryRejection
from trigger.entry_validator import EntryValidator, ValidationResult


class ManagedPosition:
    """Tracks a live position through its lifecycle."""

    __slots__ = (
        "order_id", "platform", "symbol", "direction", "lots",
        "entry_price", "sl", "tp1", "tp2", "score", "regime",
        "session", "entry_type", "open_time", "tp1_hit",
        "at_breakeven", "trailing", "last_update", "re_entry_eligible",
        "tm_trade_id",
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
        self.re_entry_eligible = False
        self.tm_trade_id = ""


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
        self.entry_engine = EntryEngine(config=self.config)
        self.drawdown = DrawdownGuard()
        self.correlation = CorrelationEngine()
        self.risk_engine = RiskEngine(config=self.config)
        self.execution_monitor = ExecutionMonitor()
        self.session_engine = SessionEngine()
        self.news_guard = NewsGuard()
        self.journal = TradeJournal()
        self.validator = EntryValidator(
            config=self.config,
            drawdown=self.drawdown,
            correlation=self.correlation,
        )
        self.risk_reporter = RiskReporter()
        self.ml = MLAdapter()
        self.re_entry = ReEntryManager()
        self.trade_manager = TradeManager(
            partial_close_ratio=0.5,
            breakeven_buffer_pips=2.0,
        )
        self._journal_loop = asyncio.new_event_loop()

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

        if should_scan and (session_status.is_tradeable or self._has_always_open_instruments()):
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
        try:
            self._journal_loop.close()
        except Exception:
            pass

    # ── Scan → Entry pipeline ────────────────────────────────────────────

    def _has_always_open_instruments(self) -> bool:
        """True if any enabled symbol trades outside FX session hours (24/5 non-FX or 24/7).
        Keeps the scan loop alive during FX dead zones. Reads from instrument registry."""
        from config import is_always_open
        return any(is_always_open(pair) for pair in self.config.enabled_pairs)

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

        # Build currency_data for the strength meter — H1 data keyed by symbol
        currency_data = {
            pair: frames["H1"]
            for pair, frames in market_data.items()
            if "H1" in frames
        }

        report = self.scanner.scan_all(market_data, currency_data=currency_data, utc_now=now)
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

        # Fetch more M1 bars than other timeframes — CHoCH detection needs
        # sufficient swing structure. 200 M1 bars = 3.3hrs, too few for Gold.
        # Fetch H4/H1/M15/M5 at 200, M1 at 400 (6.5hrs of micro structure).
        base_data = self.platforms.fetch_market_data(pair, ["H4", "H1", "M15", "M5"])
        m1_data = self.platforms.fetch_market_data(pair, ["M1"], count=400)
        data = {**base_data, **m1_data}
        if len(data) < 4:
            self._log_rejection(pair, direction, result.score, "Insufficient TF data")
            return False

        m5_df = data.get("M5")
        m1_df = data.get("M1")
        h1_df = data.get("H1")

        if m5_df is None or m1_df is None or h1_df is None:
            self._log_rejection(pair, direction, result.score, "Missing M5/M1/H1 data")
            return False

        balance = self.platforms.get_platform_balance(pair) or 10_000.0
        self.risk_engine.balance = balance

        # Build the platform context for this symbol — used by every downstream module
        broker = self.platforms.get_broker_name(pair)
        ctx: PlatformContext = build_context_for_symbol(
            pair,
            broker=broker,
            typical_spreads=self.platforms.get_typical_spreads(pair),
        )

        signal = self.entry_engine.calculate_entry(
            pair=pair,
            direction=direction,
            m5_df=m5_df,
            m1_df=m1_df,
            h1_df=h1_df,
            scan_result=result,
            account_balance=balance,
        )

        if isinstance(signal, EntryRejection):
            self._log_rejection(pair, direction, result.score, signal.reason)
            return False

        spread = 0.0
        try:
            spread = self.platforms.get_spread(pair)
        except Exception:
            pass

        validation = self.validator.validate(
            signal=signal,
            current_spread_pips=spread,
            open_trades=[
                {"pair": p.symbol, "direction": p.direction, "risk_pct": 0.02}
                for p in self.managed_positions.values()
            ],
            utc_now=now,
        )
        if not validation.valid:
            reasons = "; ".join(validation.checks_failed)
            self._log_rejection(pair, direction, signal.score, f"Validator: {reasons}")
            return False

        pip_size = get_pip_size(pair)
        # Use the broker-specific spread baseline from the context instead of a
        # global registry lookup — prevents Exness trades being rejected on
        # ICMarkets thresholds (and vice-versa).
        typical = ctx.typical_spread(pair, fallback=2.0)
        if spread > typical * self.config.risk.max_spread_multiplier:
            self._log_rejection(pair, direction, result.score, f"Spread too wide: {spread} (typical={typical})")
            return False

        assessment = self.risk_engine.assess(
            pair=pair,
            direction=direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            open_trades=[
                {"pair": p.symbol, "direction": p.direction, "risk_pct": 0.02}
                for p in self.managed_positions.values()
            ],
            account_balance=balance,
            context=ctx,
            current_spread_pips=spread if spread > 0 else None,
        )
        if not assessment.approved:
            reasons = "; ".join(assessment.rejections)
            self._log_rejection(pair, direction, signal.score, f"RiskEngine: {reasons}")
            return False

        try:
            adjustments = self.ml.get_trade_adjustments(
                pair=pair,
                regime=getattr(result, "regime", ""),
                session=session,
            )
            if not adjustments.should_trade:
                self._log_rejection(pair, direction, result.score, f"ML: {adjustments.reason}")
                return False
            adjusted_lots = round(signal.position_size_lots * adjustments.position_size_multiplier, 2)
            adjusted_lots = max(0.01, adjusted_lots)
        except Exception as exc:
            logger.debug("ML adjustments error: {}", exc)
            adjusted_lots = signal.position_size_lots

        # Use the context to decide sizing path — no more string comparison
        stake_usd: float | None = None
        if ctx.uses_stake:
            stake_usd = assessment.stake_usd or assessment.max_loss_dollars

        order = self.platforms.execute_entry(
            pair, direction,
            adjusted_lots,
            signal.stop_loss,
            signal.tp1,
            comment=f"APEX|{signal.score}|{session}",
            stake_usd=stake_usd,
        )

        if not order.success:
            return False

        self.execution_monitor.record_execution(
            requested_price=signal.entry_price,
            filled_price=order.fill_price,
            signal_timestamp=now,
            fill_timestamp=datetime.now(timezone.utc),
            spread=spread,
            requote=False,
        )

        if self.execution_monitor.should_alert():
            stats = self.execution_monitor.get_stats()
            logger.warning(
                "⚠️ EXECUTION QUALITY {} — avg slip {:.2f}pip, latency {:.0f}ms, spread {}",
                stats.execution_quality, stats.avg_slippage_pips, stats.avg_latency_ms,
                "WIDE" if stats.spread_is_wide else "OK",
            )

        managed = ManagedPosition(
            order=order,
            tp1=signal.tp1,
            tp2=signal.tp2,
            score=signal.score,
            regime=getattr(result, "regime", ""),
            session=session,
            entry_type=signal.entry_type,
        )

        tm_signal = TMEntrySignal(
            pair=pair,
            direction=direction,
            entry_price=order.fill_price,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            risk_reward_1=signal.risk_reward_1,
            risk_reward_2=signal.risk_reward_2,
            position_size_lots=adjusted_lots,
            score=signal.score,
            confluences=list(signal.confluences),
            entry_zone=signal.entry_zone,
        )
        tm_trade = self.trade_manager.open_trade(tm_signal)
        managed.tm_trade_id = tm_trade.trade_id

        self.managed_positions[order.order_id] = managed
        self._daily_trades += 1

        logger.info(
            "🎯 TRADE OPENED — {} {} {:.2f}lots @ {:.5f} | SL {:.5f} | TP1 {:.5f} | TP2 {:.5f} | Score {}",
            direction, pair, signal.position_size_lots, order.fill_price,
            signal.stop_loss, signal.tp1, signal.tp2, signal.score,
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

            is_buy = pos.direction == "BUY"
            current = tick.bid if is_buy else tick.ask

            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            if tm_trade is None:
                logger.warning("No TradeManager entry for {} — skipping", pos.symbol)
                continue

            m5_df = None
            stall_minutes = (datetime.now(timezone.utc) - pos.open_time).total_seconds() / 60
            if pos.at_breakeven or (not pos.tp1_hit and stall_minutes > 30):
                try:
                    m5_data = self.platforms.fetch_market_data(pos.symbol, ["M5"])
                    m5_df = m5_data.get("M5")
                except Exception:
                    pass

            prev_sl = tm_trade.stop_loss
            was_partial = tm_trade.partial_closed

            tm_trade = self.trade_manager.update(tm_trade, current, m5_df)

            if tm_trade.status in TERMINAL_STATUSES:
                result = self.platforms.close_trade(oid, pos.platform)
                if result.success:
                    self._record_closed_trade(pos, result.close_price, tm_trade.close_reason or "CLOSED")
                    to_remove.append(oid)
                    closed_count += 1
                    if tm_trade.re_entry_eligible:
                        self._check_re_entry(pos)
                continue

            if tm_trade.partial_closed and not was_partial:
                pos_ctx = build_context_for_symbol(pos.symbol)
                if pos_ctx.supports_partial_close:
                    # MT5: native partial close
                    partial_lots = round(pos.lots * 0.5, 2)
                    partial_lots = max(0.01, partial_lots)
                    result = self.platforms.close_trade(oid, pos.platform, partial_lots)
                    if result.success:
                        pos.tp1_hit = True
                        pos.lots = round(pos.lots - partial_lots, 2)
                        logger.info("✅ TP1 HIT (MT5 partial) — {} {} | 50% closed", pos.direction, pos.symbol)
                else:
                    # Deriv: partial close is not supported — close the full contract
                    # and immediately open a fresh smaller one to simulate TP1 management.
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        pos.tp1_hit = True
                        self._record_closed_trade(pos, result.close_price, "TP1_FULL_CLOSE_REOPEN")
                        to_remove.append(oid)
                        closed_count += 1
                        # Attempt re-open at half stake (50% of original risk)
                        try:
                            half_stake = (tm_trade.pip_value_per_lot or 0.0) * 0.5  # fallback
                            reopen_order = self.platforms.execute_entry(
                                pos.symbol, pos.direction,
                                0.0,            # lots unused on Deriv
                                tm_trade.stop_loss,
                                tm_trade.tp2,
                                comment=f"APEX|TP1_REOPEN|{pos.score}",
                                stake_usd=half_stake if half_stake > 0 else None,
                            )
                            if reopen_order.success:
                                logger.info(
                                    "✅ TP1 HIT (Deriv reopen) — {} {} | full close + reopen at half stake",
                                    pos.direction, pos.symbol,
                                )
                        except Exception as reopen_err:
                            logger.warning("Deriv TP1 reopen failed: {}", reopen_err)
                    continue

            if tm_trade.stop_loss != prev_sl:
                pos_ctx = build_context_for_symbol(pos.symbol)
                if pos_ctx.supports_modify:
                    # MT5: modify the existing order with a new price-level SL
                    self.platforms.modify_trade(oid, pos.platform, new_sl=tm_trade.stop_loss)
                    pos.sl = tm_trade.stop_loss
                    if tm_trade.breakeven_active and not pos.at_breakeven:
                        pos.at_breakeven = True
                        logger.info("✅ BREAKEVEN (MT5 modify) — {} {} | SL→{:.5f}", pos.direction, pos.symbol, tm_trade.stop_loss)
                else:
                    # Deriv: SL modify is not supported on multiplier contracts.
                    # The connector accepted a dollar-amount SL at entry; any
                    # change here would require closing and reopening the contract,
                    # which only makes sense at TP1 (handled above).
                    # For trailing purposes we track it locally only.
                    pos.sl = tm_trade.stop_loss
                    logger.debug(
                        "SL update for Deriv {} {} tracked locally only (modify not supported)",
                        pos.direction, pos.symbol,
                    )

            pos.trailing = tm_trade.status == TradeStatus.TRAILING
            pos.re_entry_eligible = tm_trade.re_entry_eligible
            pos.last_update = datetime.now(timezone.utc)

        for oid in to_remove:
            del self.managed_positions[oid]

        return closed_count

    # ── Logging & journal ────────────────────────────────────────────────

    def _record_closed_trade(
        self, pos: ManagedPosition, close_price: float, outcome: str
    ) -> None:
        pip_size = get_pip_size(pos.symbol)
        is_buy = pos.direction == "BUY"
        pnl_pips = (close_price - pos.entry_price) / pip_size if is_buy else (pos.entry_price - close_price) / pip_size

        info = INSTRUMENT_REGISTRY.get(pos.symbol.upper())
        pip_value = info.pip_value_per_lot if info else 10.0
        pnl_dollars = pnl_pips * pip_value * pos.lots

        balance = self.platforms.get_platform_balance(pos.symbol) or 10_000.0
        pnl_pct = pnl_dollars / balance if balance > 0 else 0.0

        self.drawdown.register_trade_result(pnl_pct)
        self.risk_engine.record_trade_result(
            pnl_dollars=pnl_dollars,
            pnl_pips=pnl_pips,
            pair=pos.symbol,
            direction=pos.direction,
        )

        hold_seconds = (datetime.now(timezone.utc) - pos.open_time).total_seconds()
        logger.info(
            "📊 TRADE CLOSED — {} {} | {:.1f}pip | {} | {:.0f}s",
            pos.direction, pos.symbol, pnl_pips, outcome, hold_seconds,
        )

        trade_record = TradeRecord(
            pair=pos.symbol,
            direction=pos.direction,
            entry=pos.entry_price,
            exit=close_price,
            pnl=pnl_pips,
            score=pos.score,
            confluences=[],
            regime=pos.regime,
            session=pos.session,
            spread=0.0,
            slippage=0.0,
            entry_type=pos.entry_type,
            time_to_tp1=None,
            time_to_exit=hold_seconds / 60.0,
            outcome=outcome,
        )
        self._run_journal_async(self.journal.log_trade(trade_record))
        self.ml.register_new_trade()

    def _log_rejection(self, pair: str, direction: str, score: int, reason: str) -> None:
        logger.debug("❌ REJECTED {} {} (score {}) — {}", direction, pair, score, reason)
        decision = DecisionRecord(
            pair=pair,
            direction=direction,
            score=score,
            reason_rejected=reason,
        )
        self._run_journal_async(self.journal.log_decision(decision))

    # ── Daily reset ──────────────────────────────────────────────────────

    def _check_daily_reset(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_reset_day != today:
            if self._last_reset_day is not None:
                logger.info("📅 Daily reset — {} trades yesterday", self._daily_trades)
                try:
                    stats = self.execution_monitor.get_stats()
                    if stats.avg_slippage_pips > 0:
                        logger.info(
                            "📊 Execution stats — quality: {}, avg slip: {:.2f}pip, avg latency: {:.0f}ms, requotes: {}",
                            stats.execution_quality, stats.avg_slippage_pips,
                            stats.avg_latency_ms, stats.requote_count,
                        )
                except Exception:
                    pass
            self._daily_trades = 0
            self._last_reset_day = today

        if self.ml.should_retrain():
            self._run_ml_optimization()

    def _get_sleep_interval(self) -> float:
        now = datetime.now(timezone.utc)
        session_status = self.session_engine.get_status(now)
        news_status = self.news_guard.check(self.config.enabled_pairs, now)
        return self.scheduler.get_scan_interval(session_status, news_status)

    # ── Async journal bridge ──────────────────────────────────────────

    def _run_journal_async(self, coro) -> None:
        """Run an async journal coroutine from the sync trading loop."""
        try:
            self._journal_loop.run_until_complete(coro)
        except Exception as exc:
            logger.warning("Journal async error: {}", exc)

    # ── ML optimisation ───────────────────────────────────────────────

    def _run_ml_optimization(self) -> None:
        """Fetch all trades from the journal and run ML optimisation."""
        try:
            raw_trades = self._journal_loop.run_until_complete(
                self.journal.get_all_trades_as_dicts()
            )
            if not raw_trades:
                return
            for t in raw_trades:
                t["confluences_tags"] = _parse_confluence_tags(
                    t.pop("confluences_raw", [])
                )
            report = self.ml.run_optimization(raw_trades)
            logger.info(
                "🧠 ML optimization — {} recommendations",
                len(report.recommendations),
            )
            for rec in report.recommendations[:3]:
                logger.info("  ML: {}", rec)
        except Exception as exc:
            logger.warning("ML retraining error: {}", exc)

    # ── Re-entry evaluation ───────────────────────────────────────────

    def _check_re_entry(self, pos: ManagedPosition) -> None:
        """After a breakeven stop, check if the setup is still valid."""
        try:
            m5_data = self.platforms.fetch_market_data(pos.symbol, ["M5"], count=50)
            m5_df = m5_data.get("M5")
            if m5_df is None:
                return
            minutes_since = (
                (datetime.now(timezone.utc) - pos.open_time).total_seconds() / 60
            )
            candles_since = int(minutes_since / 5)
            trade_obj = SimpleNamespace(
                pair=pos.symbol,
                direction=pos.direction,
                re_entry_eligible=True,
                candles_since_entry=candles_since,
                trade_id=pos.order_id,
            )
            opp = self.re_entry.check_re_entry(trade_obj, m5_df)
            if opp.eligible:
                logger.info(
                    "🔄 RE-ENTRY eligible — {} {} — {}",
                    pos.direction, pos.symbol, opp.new_entry_zone,
                )
        except Exception as exc:
            logger.debug("Re-entry check error for {}: {}", pos.symbol, exc)


_CONFLUENCE_TO_TAG = {
    "Structure": "structure",
    "Order block": "order_block",
    "FVG": "fvg",
    "Multi-TF": "mtf_confluence",
    "Session": "session",
    "News": "news",
    "Currency strength": "currency_strength",
}


def _parse_confluence_tags(confluences: list) -> list[str]:
    """Map display confluence strings to ML factor keys."""
    tags: list[str] = []
    for c in confluences:
        for prefix, tag in _CONFLUENCE_TO_TAG.items():
            if str(c).startswith(prefix):
                tags.append(tag)
                break
    return tags


def _build_instrument_lookups() -> tuple[dict[str, float], dict[str, float]]:
    spreads: dict[str, float] = {}
    pip_values: dict[str, float] = {}
    for sym, info in INSTRUMENT_REGISTRY.items():
        spreads[sym] = info.typical_spread_pips
        pip_values[sym] = info.pip_value_per_lot
    return spreads, pip_values


INSTRUMENT_REGISTRY_SPREAD, INSTRUMENT_REGISTRY_PIP_VALUE = _build_instrument_lookups()
