"""
APEX TRADER — Main Trading Loop
The heartbeat of the system.
Scan → Analyse → Trigger → Manage → Repeat.
Always watching. Always ready. In and out like a sniper.
"""

import asyncio
import signal
import threading
import time as _time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pandas as pd
from loguru import logger

from persistence.position_store import PositionStore
from platforms.circuit_breaker import CircuitBreaker
from platforms.health_watchdog import HealthWatchdog
from platforms.maintenance import DailyMaintenance
from platforms.startup_check import StartupCheck

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
from brain.opportunity_density import OpportunityDensityTracker
from brain.regime_detector import SystemVolatilityMonitor
from config import AppConfig, INSTRUMENT_REGISTRY, get_pip_size, is_always_open
from management.re_entry import ReEntryManager
from management.trade_manager import (
    TradeManager,
    TradeStatus,
    TERMINAL_STATUSES,
    EntrySignal as TMEntrySignal,
)
from adaptive.optimizer import AdaptiveOptimizer as MLAdapter, TradeAdjustments
from platforms.base_connector import OrderResult, CloseResult, PositionInfo
from platforms.deriv.deriv_connector import DerivConnector
from platforms.order_idempotency import generate_idempotency_key
from platforms.platform_manager import PlatformManager
from platform_context import PlatformContext, build_context_for_symbol
from risk.portfolio_risk_state import (
    PortfolioRiskStateMachine,
    PortfolioRiskState,
    PortfolioRiskSnapshot,
    PositionRisk,
    EmergencyTriggerSnapshot,
    evaluate_emergency_triggers,
    compute_position_risk_dollars,
    compute_live_heat_pct,
    is_eligible_for_defensive_breakeven,
    is_position_data_insufficient,
    rank_positions_weakest_first,
)
from risk.risk_engine import RiskEngine
from risk.risk_reporter import RiskReporter
from scanner import PairScanner, PairRanker, ScanScheduler
from trigger.entry_engine import EntryEngine, EntrySignal, EntryRejection
from trigger.entry_validator import EntryValidator, ValidationResult


class ManagedPosition:
    """Tracks a live position through its lifecycle."""

    __slots__ = (
        "order_id",
        "platform",
        "symbol",
        "direction",
        "lots",
        "entry_price",
        "sl",
        "tp1",
        "tp2",
        "score",
        "regime",
        "session",
        "entry_type",
        "open_time",
        "tp1_hit",
        "at_breakeven",
        "trailing",
        "last_update",
        "re_entry_eligible",
        "tm_trade_id",
        "stake_usd",
        "multiplier",
        "broker_pnl",
        "broker_lots",
        "scale_in_count",
        "idempotency_key",
    )

    def __init__(
        self,
        order: OrderResult,
        tp1: float,
        tp2: float,
        score: int = 0,
        regime: str = "",
        session: str = "",
        entry_type: str = "",
        stake_usd: float = 0.0,
        multiplier: int = 100,
        idempotency_key: str = "",
    ):
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
        self.stake_usd = stake_usd  # Deriv only; 0.0 for MT5
        self.multiplier = multiplier  # Deriv contract multiplier; 100 default
        self.broker_pnl = 0.0  # Live P&L from broker (source of truth)
        self.broker_lots = 0.0  # Live lots from broker (detects partial fills)
        self.scale_in_count = 0
        self.idempotency_key = idempotency_key


class _LockedPositions:
    """Thread-safe dict wrapper for managed positions.

    Prevents 'dictionary changed size during iteration' when the
    dashboard/API thread reads while the trading-loop thread mutates.
    Iteration helpers return snapshot lists; every operation acquires
    an RLock so compound operations from a single thread cannot deadlock.
    """

    def __init__(self) -> None:
        self._data: dict[str, ManagedPosition] = {}
        self.lock = threading.RLock()

    def __getitem__(self, key: str) -> ManagedPosition:
        with self.lock:
            return self._data[key]

    def __setitem__(self, key: str, value: ManagedPosition) -> None:
        with self.lock:
            self._data[key] = value

    def __delitem__(self, key: str) -> None:
        with self.lock:
            del self._data[key]

    def __contains__(self, key: object) -> bool:
        with self.lock:
            return key in self._data

    def __len__(self) -> int:
        with self.lock:
            return len(self._data)

    def __bool__(self) -> bool:
        with self.lock:
            return bool(self._data)

    def __iter__(self):
        with self.lock:
            return iter(list(self._data))

    def get(self, key: str, default=None):
        with self.lock:
            return self._data.get(key, default)

    def pop(self, key: str, *args):
        with self.lock:
            return self._data.pop(key, *args)

    def keys(self):
        with self.lock:
            return list(self._data.keys())

    def values(self):
        with self.lock:
            return list(self._data.values())

    def items(self):
        with self.lock:
            return list(self._data.items())

    def clear(self) -> None:
        with self.lock:
            self._data.clear()

    def snapshot(self) -> dict:
        """Return a shallow copy for safe cross-thread iteration."""
        with self.lock:
            return dict(self._data)

    def __eq__(self, other: object) -> bool:
        with self.lock:
            if isinstance(other, _LockedPositions):
                return self._data == other._data
            if isinstance(other, dict):
                return self._data == other
            return NotImplemented


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
        self.correlation = CorrelationEngine(
            max_correlated_trades=self.config.risk.max_correlated_trades,
            max_cluster_same_direction=self.config.risk.max_cluster_same_direction,
        )
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
        self.density_tracker = OpportunityDensityTracker(window_minutes=60)
        self.vol_monitor = SystemVolatilityMonitor()
        self.trade_manager = TradeManager(
            partial_close_ratio=0.5,
            breakeven_buffer_pips=2.0,
            tp_adjust_enabled=self.config.risk.tp_adjust_enabled,
            tp3_ladder_enabled=self.config.risk.tp3_ladder_enabled,
            tp3_r_multiple=self.config.risk.tp3_r_multiple,
            tp3_close_ratio=self.config.risk.tp3_close_ratio,
        )
        self._journal_loop = asyncio.new_event_loop()
        self.position_store = PositionStore()

        self.watchdog = HealthWatchdog()
        self.maintenance = DailyMaintenance()
        self._scan_breaker = CircuitBreaker("scan", failure_threshold=5, cooldown_seconds=300)
        self._execution_breaker = CircuitBreaker("execution", failure_threshold=3, cooldown_seconds=600)
        self._health_check_interval = 10

        # In-memory activity feed — surfaced in dashboard /api/activity
        # Capped at 200 entries; newest first.
        self.system_warnings: list[dict] = []
        self._MAX_WARNINGS = 200

        self.managed_positions: _LockedPositions = _LockedPositions()
        self._pending_orders: dict[str, dict] = {}
        self._weekend_protected_oids: set[str] = set()
        self.running = False
        self._last_scan_time: Optional[datetime] = None
        self._daily_trades = 0
        self._last_reset_day: Optional[str] = None
        # Live reconciliation heartbeat
        self._last_reconcile_time: Optional[datetime] = None
        self._reconcile_interval_seconds: int = 30
        self._recovery_completed: bool = False
        # In-trade analysis state — keyed by order_id
        # Tracks score history for conviction monitoring and HTF reassessment
        self._position_scores: dict[str, list[int]] = {}       # recent N scores per position
        self._position_last_h1_close: dict[str, datetime] = {} # last H1 candle time seen
        self._news_exit_protected: set[str] = set()            # oids already tightened for news
        self._last_market_data: dict = {}                       # most recent market data for in-trade analysis

        # ── Portfolio risk state machine (M8 Phase 4a + 4b + 4c) ────────────
        cfg_r = self.config.risk
        if cfg_r.portfolio_risk_engine_enabled:
            self._portfolio_risk_sm = PortfolioRiskStateMachine(
                heat_defensive_pct=cfg_r.heat_defensive_pct,
                heat_recovery_pct=cfg_r.heat_recovery_pct,
                recovery_dwell_seconds=cfg_r.recovery_dwell_seconds,
                heat_reduction_pct=cfg_r.heat_reduction_pct,
                reduction_persist_seconds=cfg_r.reduction_persist_seconds,
                heat_emergency_pct=cfg_r.heat_emergency_pct,
            )
        else:
            self._portfolio_risk_sm: Optional[PortfolioRiskStateMachine] = None
        self._defensive_action_timestamps: dict[str, float] = {}  # oid → monotonic time of last action
        self._reduction_action_timestamps: dict[str, float] = {}  # oid → monotonic time of last trim
        self._reduction_actions_this_hour: list[float] = []  # monotonic timestamps of trims
        self._emergency_action_timestamps: dict[str, float] = {}  # oid → monotonic time of last emergency close
        self._emergency_closes_this_hour: list[float] = []  # monotonic timestamps of emergency closes

    # ── Thread-safe position accessors ──────────────────────────────────

    def get_positions_snapshot(self) -> dict[str, ManagedPosition]:
        """Return a shallow copy of managed positions for safe cross-thread reads."""
        return self.managed_positions.snapshot()

    def get_positions_count(self) -> int:
        """Thread-safe count of managed positions."""
        return len(self.managed_positions)

    def emergency_close_all_positions(self, platform_manager) -> int:
        """Thread-safe emergency close — used by the dashboard API thread."""
        with self.managed_positions.lock:
            closed = 0
            for oid, pos in list(self.managed_positions._data.items()):
                try:
                    result = platform_manager.close_trade(oid, pos.platform)
                    if result.success:
                        closed += 1
                except Exception as exc:
                    logger.error("Emergency close failed for {}: {}", oid, exc)
            self.managed_positions._data.clear()
            return closed

    # ── Main loop ────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info("  APEX TRADER — GOING LIVE")
        logger.info("=" * 60)

        passed, results = StartupCheck().run_all()
        for r in results:
            lvl = "INFO" if r.passed else "ERROR"
            logger.log(lvl, "  [{}] {} — {} ({:.0f}ms)", "✅" if r.passed else "❌", r.name, r.message, r.duration_ms)
        if not passed:
            logger.error("Startup self-test FAILED — aborting to protect capital")
            return

        connection_status = self.platforms.connect_all()
        if not self.platforms.any_connected:
            logger.error("No platforms connected — cannot trade")
            return

        logger.info("Platforms: MT5={} | Deriv={}", connection_status["mt5"], connection_status["deriv"])

        self._install_signal_handlers()
        self._perform_startup_recovery()

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

        self.watchdog.record_cycle()
        self._check_and_reconnect()

        if self.watchdog._cycles % self._health_check_interval == 0:
            report = self.watchdog.check_health()
            if not report.is_healthy:
                for w in report.warnings:
                    logger.warning("⚠️ HEALTH: {}", w)

        session_status = self.session_engine.get_status(now)
        news_status = self.news_guard.check(self.config.enabled_pairs, now)

        can_trade, reason = self.drawdown.can_trade(now)
        if not can_trade:
            logger.debug("Trading paused: {}", reason)
            self._check_pending_orders()
            self._check_weekend_protection()
            self._update_positions()
        else:
            should_scan = self.scheduler.should_scan_now(
                self._last_scan_time,
                session_status,
                news_status,
                has_active_positions=len(self.managed_positions) > 0,
            )

            market_data: dict = {}  # unused in run_once scope — populated in _scan_and_enter via _last_market_data

            if should_scan and (session_status.is_tradeable or self._has_always_open_instruments()):
                if self._scan_breaker.can_execute():
                    cycle["scanned"] = True
                    try:
                        self._scan_and_enter(session_status, news_status, now, cycle)
                        self._scan_breaker.record_success()
                        self.watchdog.record_scan_success()
                    except Exception as exc:
                        logger.error("Scan cycle error: {}", exc)
                        self._scan_breaker.record_failure()
                        self.watchdog.record_scan_failure()
                    self._last_scan_time = now
                else:
                    status = self._scan_breaker.get_status()
                    logger.debug(
                        "Scan circuit OPEN — cooldown {:.0f}s remaining",
                        status.cooldown_remaining_seconds,
                    )

            try:
                self._check_pending_orders()
                self._check_weekend_protection()
                closed_count = self._update_positions()
                self._check_scale_in()
                # ── In-trade active management (new capabilities) ──────────
                if self.managed_positions:
                    self._check_news_exit(now)
                    self._check_session_close(now)
                    self._check_portfolio_heat()
                    self._check_spread_deterioration()
                    if self._last_market_data:
                        self._analyse_open_trades(self._last_market_data, now)
                self.watchdog.record_trade_check_success()
            except Exception as exc:
                logger.error("Position update error: {}", exc)
                closed_count = 0
                self.watchdog.record_trade_check_failure()

            cycle["positions_updated"] = len(self.managed_positions)
            cycle["positions_closed"] = closed_count

        # ── Periodic reconciliation heartbeat (H6) ───────────────────────
        if self._recovery_completed and self._last_reconcile_time is not None:
            elapsed = (now - self._last_reconcile_time).total_seconds()
            if elapsed >= self._reconcile_interval_seconds:
                try:
                    self._reconcile_positions()
                    self._last_reconcile_time = datetime.now(timezone.utc)
                except Exception as exc:
                    logger.warning("Periodic reconciliation error: {}", exc)

        return cycle

    def stop(self) -> None:
        self.running = False
        n_positions = len(self.managed_positions)
        for pos in self.managed_positions.values():
            self.position_store.save_position(pos)
        logger.info(
            "APEX TRADER SHUTTING DOWN — {} positions persisted for restart recovery, {} trades today",
            n_positions,
            self._daily_trades,
        )
        self.position_store.close()
        self.platforms.disconnect_all()
        try:
            self._journal_loop.close()
        except Exception:
            pass

    # ── Startup & recovery ───────────────────────────────────────────────

    def _perform_startup_recovery(self) -> None:
        """Restore persisted positions and reconcile with the broker.

        Idempotent — safe to call from both ``run()`` and the dashboard
        startup path.  The flag ensures restore+reconcile execute at most
        once per process lifetime.
        """
        if self._recovery_completed:
            return
        self._restore_positions()
        self._reconcile_positions()
        self._last_reconcile_time = datetime.now(timezone.utc)
        self._recovery_completed = True
        logger.info("Startup recovery complete — periodic reconcile armed ({}s)", self._reconcile_interval_seconds)

    def _install_signal_handlers(self) -> None:
        """Register SIGTERM/SIGINT so the trading loop shuts down cleanly."""

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.info("Received {} — initiating graceful shutdown", sig_name)
            self.running = False

        try:
            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)
        except (OSError, ValueError):
            logger.debug("Signal handlers not installed (not main thread)")

    def _restore_positions(self) -> None:
        """Reload positions persisted before the last shutdown/crash."""
        rows = self.position_store.load_all_positions()
        if not rows:
            logger.info("Position store — no persisted positions to restore")
            return
        for row in rows:
            try:
                dummy_order = OrderResult(
                    success=True,
                    order_id=row["order_id"],
                    fill_price=row["entry_price"],
                    requested_price=row["entry_price"],
                    slippage_pips=0.0,
                    lots=row["lots"],
                    symbol=row["symbol"],
                    direction=row["direction"],
                    sl=row["sl"],
                    tp=row["tp1"],
                    platform=row["platform"],
                )
                pos = ManagedPosition(
                    order=dummy_order,
                    tp1=row["tp1"],
                    tp2=row["tp2"],
                    score=row["score"],
                    regime=row["regime"],
                    session=row["session"],
                    entry_type=row["entry_type"],
                    stake_usd=row["stake_usd"],
                    multiplier=row.get("multiplier", 100),
                )
                pos.open_time = datetime.fromisoformat(row["open_time"])
                pos.tp1_hit = bool(row["tp1_hit"])
                pos.at_breakeven = bool(row["at_breakeven"])
                pos.trailing = bool(row["trailing"])
                pos.tm_trade_id = row["tm_trade_id"]
                pos.last_update = datetime.fromisoformat(row["last_update"])

                tm_signal = TMEntrySignal(
                    pair=pos.symbol,
                    direction=pos.direction,
                    entry_price=pos.entry_price,
                    stop_loss=pos.sl,
                    tp1=pos.tp1,
                    tp2=pos.tp2,
                    risk_reward_1=1.0,
                    risk_reward_2=2.0,
                    position_size_lots=pos.lots,
                    score=pos.score,
                )
                tm_trade = self.trade_manager.open_trade(tm_signal)
                if pos.tp1_hit:
                    tm_trade.partial_closed = True
                    tm_trade.breakeven_active = True
                pos.tm_trade_id = tm_trade.trade_id
                self.managed_positions[pos.order_id] = pos
                logger.info(
                    "🔄 RESTORED — {} {} | lots={} | SL={:.5f} | tp1_hit={}",
                    pos.direction,
                    pos.symbol,
                    pos.lots,
                    pos.sl,
                    pos.tp1_hit,
                )
            except Exception as exc:
                logger.error("Failed to restore position {}: {}", row.get("order_id"), exc)
        logger.info("Position store — {} positions restored from disk", len(self.managed_positions))

    def _reconcile_positions(self) -> None:
        """Compare persisted positions with broker's live positions on startup."""
        broker_positions: list[PositionInfo] = []
        try:
            broker_positions = self.platforms.get_all_open_positions()
        except Exception as exc:
            logger.warning("Broker position query failed during reconciliation: {}", exc)
            return

        broker_by_id: dict[str, PositionInfo] = {p.order_id: p for p in broker_positions}
        persisted_ids = set(self.managed_positions.keys())
        broker_ids = set(broker_by_id.keys())

        for oid in persisted_ids - broker_ids:
            pos = self.managed_positions[oid]
            logger.info(
                "📋 RECONCILE — {} {} was closed externally while offline — removing",
                pos.direction,
                pos.symbol,
            )
            self.managed_positions.pop(oid, None)
            self.position_store.remove_position(oid)

        for oid in broker_ids - persisted_ids:
            bp = broker_by_id[oid]
            logger.warning(
                "⚠️ RECONCILE — Orphaned position found: {} {} {:.2f} lots — adopting",
                bp.direction,
                bp.symbol,
                bp.lots,
            )
            dummy_order = OrderResult(
                success=True,
                order_id=bp.order_id,
                fill_price=bp.open_price,
                requested_price=bp.open_price,
                slippage_pips=0.0,
                lots=bp.lots,
                symbol=bp.symbol,
                direction=bp.direction,
                sl=bp.sl,
                tp=bp.tp,
                platform=bp.platform,
            )
            managed = ManagedPosition(
                order=dummy_order,
                tp1=bp.tp,
                tp2=0.0,
                score=0,
                regime="UNKNOWN",
                session="UNKNOWN",
                entry_type="ORPHAN_ADOPTED",
            )
            tm_signal = TMEntrySignal(
                pair=bp.symbol,
                direction=bp.direction,
                entry_price=bp.open_price,
                stop_loss=bp.sl,
                tp1=bp.tp,
                tp2=0.0,
                risk_reward_1=1.0,
                risk_reward_2=1.0,
                position_size_lots=bp.lots,
                score=0,
            )
            tm_trade = self.trade_manager.open_trade(tm_signal)
            managed.tm_trade_id = tm_trade.trade_id
            self.managed_positions[oid] = managed
            self.position_store.save_position(managed)

        for oid in persisted_ids & broker_ids:
            bp = broker_by_id[oid]
            pos = self.managed_positions[oid]
            if abs(bp.sl - pos.sl) > 1e-8:
                logger.debug("RECONCILE — {} SL updated from broker: {:.5f} → {:.5f}", pos.symbol, pos.sl, bp.sl)
                pos.sl = bp.sl
                self.position_store.update_position(oid, sl=bp.sl)

        logger.info(
            "Reconciliation complete — {} managed, {} on broker, {} adopted, {} removed",
            len(self.managed_positions),
            len(broker_positions),
            len(broker_ids - persisted_ids),
            len(persisted_ids - broker_ids),
        )

    # ── Auto-reconnect ─────────────────────────────────────────────────

    def _reconcile_externally_closed(self, to_remove: list[str]) -> None:
        """Drop managed positions that no longer exist at the broker."""
        if not self.managed_positions:
            return
        try:
            broker_positions = self.platforms.get_all_open_positions()
        except Exception:
            return
        broker_ids = {p.order_id for p in broker_positions}
        broker_pnl = {p.order_id: p.pnl for p in broker_positions}

        for oid, pos in list(self.managed_positions.items()):
            if oid not in broker_ids:
                real_pnl = broker_pnl.get(oid, 0.0)
                fake_close = CloseResult(
                    success=True,
                    order_id=oid,
                    close_price=pos.entry_price,
                    lots_closed=pos.lots,
                    pnl=real_pnl,
                    platform=pos.platform,
                )
                try:
                    tick = self.platforms.get_price(pos.symbol)
                    is_buy = pos.direction == "BUY"
                    fake_close.close_price = tick.bid if is_buy else tick.ask
                except Exception:
                    pass
                self._record_closed_trade(
                    pos,
                    fake_close.close_price,
                    "CLOSED_EXTERNALLY",
                    close_result=fake_close if real_pnl != 0.0 else None,
                )
                to_remove.append(oid)
                self._add_warning(
                    "warning",
                    f"{pos.direction} {pos.symbol} closed externally by broker",
                    symbol=pos.symbol,
                )
                logger.info(
                    "📋 LIVE RECONCILE — {} {} closed externally — removed",
                    pos.direction,
                    pos.symbol,
                )

    def _check_and_reconnect(self) -> None:
        """Non-blocking reconnect check — attempts only when backoff timer allows."""
        for platform in ("mt5", "deriv"):
            if self.platforms.should_attempt_reconnect(platform):
                success = self.platforms.reconnect_platform(platform)
                if success:
                    logger.info("🔄 {} recovered — reconciling positions", platform.upper())
                    try:
                        self._reconcile_positions()
                    except Exception as exc:
                        logger.warning("Post-reconnect reconciliation error: {}", exc)

    # ── Scan → Entry pipeline ────────────────────────────────────────────

    def _has_always_open_instruments(self) -> bool:
        """True if any enabled symbol trades outside FX session hours (24/5 non-FX or 24/7).
        Keeps the scan loop alive during FX dead zones. Reads from instrument registry."""
        return any(is_always_open(pair) for pair in self.config.enabled_pairs)

    def _scan_and_enter(self, session_status, news_status, now: datetime, cycle: dict) -> None:
        try:
            market_data = self.platforms.fetch_all_market_data(now_utc=now)
        except Exception as exc:
            logger.error("Market data fetch failed: {}", exc)
            return

        if not market_data:
            return

        # Store for in-trade analysis this cycle
        self._last_market_data = market_data

        # Build currency_data for the strength meter — H1 data keyed by symbol
        currency_data = {pair: frames["H1"] for pair, frames in market_data.items() if "H1" in frames}

        report = self.scanner.scan_all(market_data, currency_data=currency_data, utc_now=now)
        ready = self.scanner.get_ready_setups(report)

        # Track opportunity density — feeds into position sizing
        density = self.density_tracker.record_scan(len(ready), utc_now=now)

        # Update system-wide volatility state — reduce all sizes during market vol spikes
        try:
            from brain.regime_detector import RegimeDetector

            _regime_det = RegimeDetector()
            _vol_analyses = []
            for pair, frames in market_data.items():
                h4 = frames.get("H4")
                if h4 is not None and len(h4) >= 50:
                    try:
                        _vol_analyses.append(_regime_det.analyze(h4))
                    except Exception:
                        pass
            if _vol_analyses:
                _vol_state = self.vol_monitor.update(_vol_analyses)
                if _vol_state.state != "NORMAL":
                    logger.warning(
                        "⚡ SYSTEM VOL {} — {} — all sizes ×{:.2f}",
                        _vol_state.state,
                        _vol_state.note,
                        _vol_state.size_multiplier,
                    )
        except Exception as _exc:
            logger.debug("Vol monitor update error: {}", _exc)

        if not ready:
            return

        # Portfolio risk state gate — freeze entries in DEFENSIVE or REDUCING
        if (
            getattr(self, '_portfolio_risk_sm', None) is not None
            and self._portfolio_risk_sm.state in (PortfolioRiskState.DEFENSIVE, PortfolioRiskState.REDUCING)
        ):
            logger.info(
                "[PortfolioRisk] {} — new entries frozen (heat={:.2f}%)",
                self._portfolio_risk_sm.state.name,
                getattr(self, '_current_portfolio_heat', 0.0),
            )
            return

        # Portfolio heat gate — don't open new trades if total exposure too high
        current_heat = getattr(self, '_current_portfolio_heat', 0.0)
        if self.config.risk.portfolio_heat_enabled and current_heat >= self.config.risk.portfolio_heat_block_pct:
            logger.info(
                "🌡️ PORTFOLIO HEAT GATE — {:.2f}% heat blocks new entries (limit {:.1f}%)",
                current_heat, self.config.risk.portfolio_heat_block_pct,
            )
            return

        open_pairs = [p.symbol for p in self.managed_positions.values()]
        # Build pair multiplier map from ML learner for opportunity ranking
        pair_mult_map = {}
        try:
            for r in ready:
                pair_mult_map[r.pair] = self.ml.pair_learner.get_pair_multiplier(r.pair)
        except Exception:
            pass

        ranked = self.ranker.rank(ready, open_pairs)
        # Re-rank by opportunity score using pair learner and EV estimate
        ready_results = [s.result for s in ranked]
        reranked = self.ranker.rank_opportunities(ready_results, pair_mult_map)
        # Rebuild ranked list preserving RankedSetup structure
        rank_map = {s.result.pair: s for s in ranked}
        top_results = reranked[:3]
        top = [rank_map[r.pair] for r in top_results if r.pair in rank_map]

        for setup in top:
            result = setup.result
            if result.pair in open_pairs:
                continue

            _current_risk = self.risk_engine.drawdown_guard.risk_map.get(self.risk_engine.drawdown_guard.mode, 0.005)
            open_trades = [
                OpenTrade(pair=p.symbol, direction=p.direction, risk_pct=_current_risk)
                for p in self.managed_positions.values()
            ]
            can_open, corr_reason = self.correlation.can_open_trade(result.pair, result.direction, open_trades)
            if not can_open:
                self._log_rejection(result.pair, result.direction, result.score, corr_reason)
                continue

            if self.config.risk.margin_guardian_enabled:
                margin_ok, margin_reason = self._check_margin_for_entry(result.pair)
                if not margin_ok:
                    self._log_rejection(result.pair, result.direction, result.score, margin_reason)
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

        # Resolve actual risk % from current drawdown mode — never hardcode 0.02
        _exec_risk = self.risk_engine.drawdown_guard.risk_map.get(self.risk_engine.drawdown_guard.mode, 0.005)

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
                {"pair": p.symbol, "direction": p.direction, "risk_pct": _exec_risk}
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
                {"pair": p.symbol, "direction": p.direction, "risk_pct": _exec_risk}
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

        # ── EV gate — skip when historical EV is negative at medium+ confidence ──
        # ev_estimate=0.0 means insufficient data (new pair) — always allow.
        # Only block when the system has enough history to be confident it's a loser.
        ev_val = getattr(result, "ev_estimate", 0.0)
        if ev_val < 0.0:
            # Determine confidence from EVEstimator sample size indirectly via score
            # We gate on negative EV only when pair_mult is also below 1.0 (i.e. learner
            # has marked this pair as REDUCE_SIZE or worse) — belt + braces gate.
            pair_mult = pair_mult_map = 1.0
            try:
                pair_mult = self.ml.pair_learner.get_pair_multiplier(pair)
            except Exception:
                pass
            if pair_mult < 1.0:
                self._log_rejection(
                    pair, direction, result.score, f"EV gate: negative EV ({ev_val:.4f}) + pair_mult={pair_mult:.2f}"
                )
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
            density_mult = self.density_tracker.get_size_multiplier()
            vol_mult = self.vol_monitor.get_size_multiplier()
            adjusted_lots = round(
                signal.position_size_lots * adjustments.position_size_multiplier * density_mult * vol_mult,
                2,
            )
            risk_ceiling = signal.position_size_lots
            if adjusted_lots > risk_ceiling:
                logger.info(
                    "[RiskAuthority] {} — adaptive sizing capped {:.2f} → {:.2f} lots (risk ceiling)",
                    pair, adjusted_lots, risk_ceiling,
                )
                adjusted_lots = risk_ceiling
            adjusted_lots = max(0.01, adjusted_lots)
        except Exception as exc:
            logger.debug("ML adjustments error: {}", exc)
            adjusted_lots = signal.position_size_lots

        # Use the context to decide sizing path — no more string comparison
        stake_usd: float | None = None
        if ctx.uses_stake:
            stake_usd = assessment.stake_usd or assessment.max_loss_dollars

        if not self._execution_breaker.can_execute():
            status = self._execution_breaker.get_status()
            logger.debug(
                "Execution circuit OPEN — cooldown {:.0f}s remaining",
                status.cooldown_remaining_seconds,
            )
            return False

        pre_exec_ts = datetime.now(timezone.utc)
        idem_key = generate_idempotency_key(pair, direction, adjusted_lots, pre_exec_ts)

        if self.position_store:
            self.position_store.record_in_flight(idem_key, pair, direction, adjusted_lots)

        use_pending = False
        if self.config.risk.pending_orders_enabled and not ctx.uses_stake:
            try:
                tick = self.platforms.get_price(pair)
                current = tick.ask if direction == "LONG" else tick.bid
                pip_size = get_pip_size(pair)
                distance_pips = abs(current - signal.entry_price) / pip_size
                if distance_pips > 3.0:
                    is_buy = direction == "LONG"
                    if is_buy and current > signal.entry_price:
                        order_kind = "BUY_LIMIT"
                        use_pending = True
                    elif not is_buy and current < signal.entry_price:
                        order_kind = "SELL_LIMIT"
                        use_pending = True
                    elif is_buy and current < signal.entry_price:
                        order_kind = "BUY_STOP"
                        use_pending = True
                    elif not is_buy and current > signal.entry_price:
                        order_kind = "SELL_STOP"
                        use_pending = True
            except Exception:
                pass

        if use_pending:
            order = self.platforms.place_pending_entry(
                pair,
                order_kind,
                signal.entry_price,
                adjusted_lots,
                signal.stop_loss,
                signal.tp1,
                comment=f"APEX_PEND|{signal.score}|{session}|{idem_key}",
                idempotency_key=idem_key,
            )
            if order.success:
                pending_id = order.order_id
                max_wait = self.config.risk.pending_max_wait_minutes
                self._pending_orders[pending_id] = {
                    "symbol": pair,
                    "direction": direction,
                    "placed_at": datetime.now(timezone.utc),
                    "max_wait_minutes": max_wait,
                    "signal": signal,
                    "session": session,
                }
                if self.position_store:
                    self.position_store.resolve_in_flight(idem_key, pending_id)
                logger.info(
                    "📋 PENDING ORDER PLACED — {} {} @ {:.5f} | expires in {}min",
                    order_kind, pair, signal.entry_price, max_wait,
                )
                return True
            self._execution_breaker.record_failure()
            if self.position_store:
                self.position_store.cancel_in_flight(idem_key)
            return False

        order = self.platforms.execute_entry(
            pair,
            direction,
            adjusted_lots,
            signal.stop_loss,
            signal.tp1,
            comment=f"APEX|{signal.score}|{session}|{idem_key}",
            stake_usd=stake_usd,
            idempotency_key=idem_key,
        )

        if not order.success:
            self._execution_breaker.record_failure()
            if self.position_store:
                self.position_store.cancel_in_flight(idem_key)
            return False

        self._execution_breaker.record_success()
        if self.position_store:
            self.position_store.resolve_in_flight(idem_key, order.order_id)

        if not ctx.uses_stake:
            self.execution_monitor.record_execution(
                requested_price=signal.entry_price,
                filled_price=order.fill_price,
                signal_timestamp=pre_exec_ts,
                fill_timestamp=datetime.now(timezone.utc),
                spread=spread,
                requote=False,
                pip_size=get_pip_size(pair),
            )

        if self.execution_monitor.should_alert():
            stats = self.execution_monitor.get_stats()
            logger.warning(
                "⚠️ EXECUTION QUALITY {} — avg slip {:.2f}pip, latency {:.0f}ms, spread {}",
                stats.execution_quality,
                stats.avg_slippage_pips,
                stats.avg_latency_ms,
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
            stake_usd=stake_usd or 0.0,
            multiplier=self._get_deriv_multiplier(pair) if ctx.uses_stake else 100,
            idempotency_key=idem_key,
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
            entry_timeframe=signal.entry_timeframe,
        )
        tm_trade = self.trade_manager.open_trade(tm_signal)
        managed.tm_trade_id = tm_trade.trade_id

        self.managed_positions[order.order_id] = managed
        self.position_store.save_position(managed)
        self._daily_trades += 1

        logger.info(
            "🎯 TRADE OPENED — {} {} {:.2f}lots @ {:.5f} | SL {:.5f} | TP1 {:.5f} | TP2 {:.5f} | Score {}",
            direction,
            pair,
            signal.position_size_lots,
            order.fill_price,
            signal.stop_loss,
            signal.tp1,
            signal.tp2,
            signal.score,
        )
        return True

    # ── Pending order management ────────────────────────────────────────

    def _check_pending_orders(self) -> None:
        if not self._pending_orders:
            return
        expired: list[str] = []
        now = datetime.now(timezone.utc)
        broker_positions = {}
        try:
            for p in self.platforms.get_all_open_positions():
                broker_positions[p.order_id] = p
        except Exception:
            return

        for oid, info in list(self._pending_orders.items()):
            age_min = (now - info["placed_at"]).total_seconds() / 60.0
            if oid in broker_positions:
                bp = broker_positions[oid]
                logger.info(
                    "📋 PENDING FILLED — {} {} @ {:.5f}",
                    info["direction"], info["symbol"], bp.open_price,
                )
                order = OrderResult(
                    success=True,
                    order_id=oid,
                    fill_price=bp.open_price,
                    requested_price=bp.open_price,
                    slippage_pips=0.0,
                    lots=bp.lots,
                    symbol=info["symbol"],
                    direction=info["direction"],
                    sl=bp.sl,
                    tp=bp.tp,
                    platform=bp.platform,
                )
                sig = info["signal"]
                managed = ManagedPosition(
                    order=order,
                    tp1=sig.tp1,
                    tp2=sig.tp2,
                    score=sig.score,
                    session=info["session"],
                    entry_type=sig.entry_type,
                )
                tm_signal = TMEntrySignal(
                    pair=info["symbol"],
                    direction=info["direction"],
                    entry_price=bp.open_price,
                    stop_loss=sig.stop_loss,
                    tp1=sig.tp1,
                    tp2=sig.tp2,
                    risk_reward_1=sig.risk_reward_1,
                    risk_reward_2=sig.risk_reward_2,
                    position_size_lots=bp.lots,
                    score=sig.score,
                    entry_timeframe=sig.entry_timeframe,
                )
                tm_trade = self.trade_manager.open_trade(tm_signal)
                managed.tm_trade_id = tm_trade.trade_id
                self.managed_positions[oid] = managed
                self.position_store.save_position(managed)
                self._daily_trades += 1
                expired.append(oid)
            elif age_min > info["max_wait_minutes"]:
                logger.info(
                    "📋 PENDING EXPIRED — {} {} after {:.0f}min",
                    info["direction"], info["symbol"], age_min,
                )
                try:
                    from platforms.mt5.mt5_connector import MT5Connector
                    connector = self.platforms.get_connector(info["symbol"])
                    if isinstance(connector, MT5Connector):
                        import MetaTrader5 as mt5
                        mt5.order_send({
                            "action": mt5.TRADE_ACTION_REMOVE,
                            "order": int(oid),
                        })
                except Exception:
                    pass
                expired.append(oid)
        for oid in expired:
            del self._pending_orders[oid]

    # ── Position management ──────────────────────────────────────────────

    def _update_positions(self) -> int:
        closed_count = 0
        to_remove: list[str] = []

        # ── STEP 1: Fetch broker state ONCE ─────────────────────────────
        # The broker is the source of truth for what positions exist and
        # their real P&L.  We use this to detect broker-side closes (SL/TP
        # fills the broker executed) and to sync floating P&L for still-open
        # positions, instead of relying on a local price-feed simulation.
        broker_map: dict[str, PositionInfo] = {}
        try:
            broker_positions = self.platforms.get_all_open_positions()
            broker_map = {p.order_id: p for p in broker_positions}
        except Exception:
            pass  # broker unreachable — skip reconciliation this cycle

        # ── STEP 1.5: Margin-level guardian ──────────────────────────────
        if self.config.risk.margin_guardian_enabled and self.managed_positions:
            self._margin_guardian_check()

        # ── STEP 2: Detect broker-side closes ────────────────────────────
        # If a managed position is no longer at the broker, the broker
        # closed it (SL hit, TP hit, margin call, manual close).  Record
        # using the best-available broker P&L and remove from management
        # within THIS cycle — no phantom open trades.
        if broker_map is not None:
            for oid, pos in list(self.managed_positions.items()):
                if oid not in broker_map:
                    realized = self.platforms.get_realized_pnl(oid, pos.platform)
                    broker_pnl = realized if realized is not None else pos.broker_pnl
                    close_price = pos.entry_price
                    try:
                        tick = self.platforms.get_price(pos.symbol)
                        is_buy = pos.direction == "BUY"
                        close_price = tick.bid if is_buy else tick.ask
                    except Exception:
                        pass
                    fake_close = CloseResult(
                        success=True,
                        order_id=oid,
                        close_price=close_price,
                        lots_closed=pos.lots,
                        pnl=broker_pnl,
                        platform=pos.platform,
                    )
                    self._record_closed_trade(
                        pos,
                        close_price,
                        "BROKER_CLOSED",
                        close_result=fake_close if broker_pnl != 0.0 else None,
                    )
                    to_remove.append(oid)
                    closed_count += 1
                    self._add_warning(
                        "info",
                        f"{pos.direction} {pos.symbol} closed by broker (SL/TP/external)",
                        symbol=pos.symbol,
                    )
                    logger.info(
                        "📋 BROKER CLOSED — {} {} | pnl={:.2f} — removed from management",
                        pos.direction,
                        pos.symbol,
                        broker_pnl,
                    )
            for oid in to_remove:
                self.managed_positions.pop(oid, None)
                self.position_store.remove_position(oid)
            to_remove = []

        # ── STEP 3: Manage still-open positions ─────────────────────────
        # For positions that remain open at the broker, sync their real
        # P&L and lots, then run ONLY value-add management: TP1 partial
        # close, breakeven SL modify, structure trailing, stall exit.
        # We do NOT use the simulation to detect hard SL/TP2 hits —
        # the broker already enforces those server-side.
        for oid, pos in self.managed_positions.items():
            # Sync broker state onto the managed position
            bp = broker_map.get(oid)
            if bp is not None:
                pos.broker_pnl = bp.pnl
                pos.broker_lots = bp.lots
                if abs(bp.sl - pos.sl) > 1e-8:
                    pos.sl = bp.sl

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
            prev_tp2 = tm_trade.tp2
            was_partial = tm_trade.partial_closed
            was_tp3_hit = getattr(tm_trade, "tp3_hit", False)

            # Feed the price to the trade manager for value-add management
            # (TP1 detection, breakeven, trailing, stall/structure exit).
            # The manager may set TERMINAL status for stall/structure exits
            # that the broker cannot enforce — those we still close ourselves.
            tm_trade = self.trade_manager.update(tm_trade, current, m5_df)

            # Only act on TERMINAL status from stall or structure exit —
            # NOT from simulated SL/TP2, which the broker handles.
            if tm_trade.status in TERMINAL_STATUSES:
                is_stall_or_structure = tm_trade.close_reason and (
                    "Stall" in tm_trade.close_reason
                    or "Structure" in tm_trade.close_reason
                    or "stall" in tm_trade.close_reason.lower()
                    or "structure" in tm_trade.close_reason.lower()
                )
                is_simulated_sl_tp = tm_trade.close_reason and (
                    "Stop loss" in tm_trade.close_reason
                    or "TP2" in tm_trade.close_reason
                    or "Stopped at breakeven" in tm_trade.close_reason
                )
                if is_stall_or_structure:
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        self._record_closed_trade(
                            pos, result.close_price, tm_trade.close_reason or "CLOSED", close_result=result
                        )
                        to_remove.append(oid)
                        closed_count += 1
                        if tm_trade.re_entry_eligible:
                            self._check_re_entry(pos)
                    continue
                elif is_simulated_sl_tp:
                    # The simulation thinks SL/TP2 was hit, but the broker
                    # manages hard SL/TP server-side.  If the broker already
                    # closed it, step 2 caught it.  If the position is still
                    # open at the broker, the simulation fired early/late due
                    # to price-feed divergence — reset to let the broker handle it.
                    tm_trade.status = TradeStatus.TRAILING if tm_trade.breakeven_active else TradeStatus.OPEN
                    tm_trade.close_reason = None
                    tm_trade.close_time = None
                else:
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        self._record_closed_trade(
                            pos, result.close_price, tm_trade.close_reason or "CLOSED", close_result=result
                        )
                        to_remove.append(oid)
                        closed_count += 1
                        if tm_trade.re_entry_eligible:
                            self._check_re_entry(pos)
                    continue

            if tm_trade.partial_closed and not was_partial:
                pos_ctx = build_context_for_symbol(pos.symbol)
                if pos_ctx.supports_partial_close:
                    partial_lots = round(pos.lots * 0.5, 2)
                    partial_lots = max(0.01, partial_lots)
                    result = self.platforms.close_trade(oid, pos.platform, partial_lots)
                    if result.success:
                        pos.tp1_hit = True
                        pos.lots = round(pos.lots - partial_lots, 2)
                        self.position_store.update_position(oid, tp1_hit=True, lots=pos.lots)
                        logger.info("✅ TP1 HIT (MT5 partial) — {} {} | 50% closed", pos.direction, pos.symbol)
                else:
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        pos.tp1_hit = True
                        self._record_closed_trade(pos, result.close_price, "TP1_FULL_CLOSE_REOPEN", close_result=result)
                        to_remove.append(oid)
                        closed_count += 1
                        try:
                            half_stake = round(pos.stake_usd * 0.5, 2) if pos.stake_usd > 0 else None
                            reopen_order = self.platforms.execute_entry(
                                pos.symbol,
                                pos.direction,
                                0.0,
                                tm_trade.stop_loss,
                                tm_trade.tp2,
                                comment=f"APEX|TP1_REOPEN|{pos.score}",
                                stake_usd=half_stake if half_stake > 0 else None,
                            )
                            if reopen_order.success:
                                logger.info(
                                    "✅ TP1 HIT (Deriv reopen) — {} {} | full close + reopen at half stake",
                                    pos.direction,
                                    pos.symbol,
                                )
                        except Exception as reopen_err:
                            logger.warning("Deriv TP1 reopen failed: {}", reopen_err)
                    continue

            if getattr(tm_trade, "tp3_hit", False) and not was_tp3_hit:
                pos_ctx = build_context_for_symbol(pos.symbol)
                tp3_lots = round(pos.lots * self.config.risk.tp3_close_ratio, 2)
                tp3_lots = max(0.01, tp3_lots)
                if pos_ctx.supports_partial_close:
                    result = self.platforms.close_trade(oid, pos.platform, tp3_lots)
                    if result.success:
                        pos.lots = round(pos.lots - tp3_lots, 2)
                        self.position_store.update_position(oid, lots=pos.lots)
                        logger.info(
                            "✅ TP3 HIT (partial) — {} {} | {:.0%} of runner closed",
                            pos.direction, pos.symbol, self.config.risk.tp3_close_ratio,
                        )

            sl_changed = tm_trade.stop_loss != prev_sl
            tp_changed = tm_trade.tp2 != prev_tp2
            if sl_changed or tp_changed:
                pos_ctx = build_context_for_symbol(pos.symbol)
                new_sl = tm_trade.stop_loss if sl_changed else None
                new_tp = tm_trade.tp2 if tp_changed else None
                if pos_ctx.supports_modify:
                    self.platforms.modify_trade(oid, pos.platform, new_sl=new_sl, new_tp=new_tp)
                    if sl_changed:
                        pos.sl = tm_trade.stop_loss
                    if tp_changed:
                        pos.tp2 = tm_trade.tp2
                    if tm_trade.breakeven_active and not pos.at_breakeven:
                        pos.at_breakeven = True
                        self.position_store.update_position(oid, sl=pos.sl, at_breakeven=True)
                        logger.info(
                            "✅ BREAKEVEN (MT5 modify) — {} {} | SL→{:.5f}",
                            pos.direction,
                            pos.symbol,
                            tm_trade.stop_loss,
                        )
                    else:
                        self.position_store.update_position(oid, sl=pos.sl)
                    if tp_changed:
                        logger.info(
                            "✅ TP MODIFIED — {} {} | TP2→{:.5f}",
                            pos.direction,
                            pos.symbol,
                            tm_trade.tp2,
                        )
                else:
                    if sl_changed:
                        pos.sl = tm_trade.stop_loss
                    if tp_changed:
                        pos.tp2 = tm_trade.tp2
                    if tm_trade.breakeven_active and not pos.at_breakeven:
                        pos.at_breakeven = True
                        self.position_store.update_position(oid, sl=pos.sl, at_breakeven=True)
                    else:
                        self.position_store.update_position(oid, sl=pos.sl)
                    logger.debug(
                        "SL/TP update for Deriv {} {} tracked locally only (modify not supported)",
                        pos.direction,
                        pos.symbol,
                    )

            pos.trailing = tm_trade.status == TradeStatus.TRAILING
            pos.re_entry_eligible = tm_trade.re_entry_eligible
            pos.last_update = datetime.now(timezone.utc)

        for oid in to_remove:
            self.managed_positions.pop(oid, None)
            self.position_store.remove_position(oid)

        return closed_count

    # ── Weekend-gap protection ───────────────────────────────────────────

    def _check_weekend_protection(self, utc_now=None):
        if not self.config.risk.weekend_protection_enabled:
            return
        utc_now = utc_now or datetime.now(timezone.utc)
        mins = self.session_engine.minutes_to_fx_close(utc_now)
        if mins > self.config.risk.weekend_close_buffer_minutes:
            self._weekend_protected_oids.clear()
            return
        mode = self.config.risk.weekend_protection_mode
        for oid, pos in list(self.managed_positions.items()):
            if oid in self._weekend_protected_oids:
                continue
            if is_always_open(pos.symbol):
                continue
            self._weekend_protected_oids.add(oid)
            try:
                if mode == "flatten":
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        self._record_closed_trade(
                            pos, result.close_price, "WEEKEND_FLATTEN", close_result=result,
                        )
                        self.managed_positions.pop(oid, None)
                        self.position_store.remove_position(oid)
                        logger.info("🌙 WEEKEND FLATTEN — {}", pos.symbol)
                elif mode == "derisk":
                    be_price = pos.entry_price
                    is_buy = pos.direction == "BUY"
                    already_at_be = (
                        (is_buy and pos.sl >= be_price)
                        or (not is_buy and pos.sl <= be_price)
                    )
                    if not already_at_be:
                        self.platforms.modify_trade(oid, pos.platform, new_sl=be_price, new_tp=None)
                        pos.sl = be_price
                        pos.at_breakeven = True
                        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                        if tm_trade:
                            tm_trade.stop_loss = be_price
                            tm_trade.breakeven_active = True
                        self.position_store.update_position(oid, sl=be_price, at_breakeven=True)
                        logger.info("🌙 WEEKEND DE-RISK (SL→BE) — {}", pos.symbol)
            except Exception as exc:
                logger.warning("Weekend protection failed for {}: {}", pos.symbol, exc)

    # ── Scale-in / pyramiding ────────────────────────────────────────────

    # ══════════════════════════════════════════════════════════════════════
    # IN-TRADE ACTIVE MANAGEMENT — Everything a trader does while in a trade
    # ══════════════════════════════════════════════════════════════════════

    def _analyse_open_trades(self, market_data: dict, now: datetime) -> None:
        """
        Continuously re-analyse every open instrument every scan cycle.

        A real trader never stops watching their chart after entry.
        This method:
          1. Re-scores the instrument with fresh data
          2. Checks for early invalidation (opposing setup forms)
          3. Monitors conviction decay (score dropping cycle after cycle)
          4. Checks HTF candle closes for bias change
          5. Applies dynamic SL tightening as trade moves in profit
          6. Wires scale-in to live scanner data

        The scanner runs on ALL instruments including open ones.
        Results for open instruments are used for management, not new entries.
        """
        if not self.managed_positions:
            return

        cfg = self.config.risk
        currency_data = {
            pair: frames["H1"]
            for pair, frames in market_data.items()
            if "H1" in frames
        }

        for oid, pos in list(self.managed_positions.items()):
            pair = pos.symbol
            frames = market_data.get(pair)
            if not frames:
                continue

            h4 = frames.get("H4")
            h1 = frames.get("H1")
            m15 = frames.get("M15")
            m5 = frames.get("M5")
            if h4 is None or h1 is None or m15 is None or m5 is None:
                continue

            try:
                # ── Re-score this instrument with fresh data ───────────────
                scan_result = self.scanner.scan_pair(
                    pair, h4, h1, m15, m5, currency_data, now,
                )

                # Track score history for conviction monitoring
                if oid not in self._position_scores:
                    self._position_scores[oid] = []
                self._position_scores[oid].append(scan_result.score)
                # Keep only last N cycles
                max_cycles = cfg.conviction_decline_cycles + 2
                self._position_scores[oid] = self._position_scores[oid][-max_cycles:]

                hold_minutes = (now - pos.open_time).total_seconds() / 60

                # ── 1. Early invalidation exit ─────────────────────────────
                if cfg.continuous_analysis_enabled and hold_minutes >= cfg.invalidation_min_hold_minutes:
                    self._check_invalidation(oid, pos, scan_result, now)
                    if oid not in self.managed_positions:
                        continue  # was closed

                # ── 2. Conviction monitoring ───────────────────────────────
                if cfg.conviction_monitoring_enabled and hold_minutes >= cfg.invalidation_min_hold_minutes:
                    self._check_conviction_collapse(oid, pos, now)
                    if oid not in self.managed_positions:
                        continue

                # ── 3. HTF candle close reassessment ──────────────────────
                if cfg.htf_reassessment_enabled and cfg.htf_reassess_on_h1_close:
                    self._check_htf_candle_close(oid, pos, h1, now)
                    if oid not in self.managed_positions:
                        continue

                # ── 4. Dynamic SL tightening ──────────────────────────────
                if cfg.dynamic_sl_tightening_enabled:
                    self._apply_dynamic_sl_tightening(oid, pos)

                # ── 5. Scale-in on strength (wired to live scan) ───────────
                if cfg.scale_in_enabled:
                    self._check_scale_in_on_scan(oid, pos, scan_result)

            except Exception as exc:
                logger.debug("In-trade analysis error for {}: {}", pair, exc)

    def _check_invalidation(
        self,
        oid: str,
        pos: ManagedPosition,
        scan_result,
        now: datetime,
    ) -> None:
        """
        Exit early if the market is now showing a strong setup AGAINST our position.
        A real trader sees a bearish engulfing form against their long and cuts it —
        they don't wait for SL to get hit.
        """
        cfg = self.config.risk
        is_long = pos.direction == "BUY"
        result_direction = scan_result.direction  # "LONG", "SHORT", or "NEUTRAL"

        # Score too low overall — market has lost conviction on any direction
        if scan_result.score < cfg.invalidation_score_threshold:
            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            pnl_pips = tm_trade.pnl_pips if tm_trade else 0.0
            # Only exit on low score if trade is not already in profit
            if pnl_pips <= 0:
                result = self.platforms.close_trade(oid, pos.platform)
                if result.success:
                    logger.info(
                        "🔴 INVALIDATION EXIT (low score) — {} {} | score={} | pnl={:.1f}pip",
                        pos.direction, pos.symbol, scan_result.score, pnl_pips,
                    )
                    self._record_closed_trade(
                        pos, result.close_price,
                        f"INVALIDATION_LOW_SCORE({scan_result.score})",
                        close_result=result,
                    )
                    self.managed_positions.pop(oid, None)
                    self.position_store.remove_position(oid)
                    self._position_scores.pop(oid, None)
                return

        # Strong opposing signal — market has flipped
        opposing = (
            (is_long and result_direction == "SHORT")
            or (not is_long and result_direction == "LONG")
        )
        if opposing and scan_result.score >= cfg.opposing_signal_threshold:
            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            pnl_pips = tm_trade.pnl_pips if tm_trade else 0.0
            result = self.platforms.close_trade(oid, pos.platform)
            if result.success:
                logger.info(
                    "🔴 INVALIDATION EXIT (opposing signal) — {} {} | opposing={} score={} | pnl={:.1f}pip",
                    pos.direction, pos.symbol, result_direction, scan_result.score, pnl_pips,
                )
                self._record_closed_trade(
                    pos, result.close_price,
                    f"INVALIDATION_OPPOSING({result_direction}@{scan_result.score})",
                    close_result=result,
                )
                self.managed_positions.pop(oid, None)
                self.position_store.remove_position(oid)
                self._position_scores.pop(oid, None)

    def _check_conviction_collapse(
        self, oid: str, pos: ManagedPosition, now: datetime,
    ) -> None:
        """
        Exit if score has been declining consistently for N consecutive cycles.
        A falling score means the market conditions that justified the trade
        are dissolving — a real trader feels this and starts getting out.
        """
        cfg = self.config.risk
        scores = self._position_scores.get(oid, [])
        n = cfg.conviction_decline_cycles
        if len(scores) < n:
            return  # not enough history yet

        recent = scores[-n:]
        # Check if every consecutive pair is declining by at least min_drop
        is_declining = all(
            recent[i] - recent[i + 1] >= cfg.conviction_decline_min_drop
            for i in range(len(recent) - 1)
        )
        if not is_declining:
            return

        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        pnl_pips = tm_trade.pnl_pips if tm_trade else 0.0

        # Only exit on conviction collapse if trade is not in significant profit
        # — if we're well in profit, let the trailing stop handle it
        if pnl_pips > 20:
            return

        result = self.platforms.close_trade(oid, pos.platform)
        if result.success:
            logger.info(
                "📉 CONVICTION COLLAPSE EXIT — {} {} | scores={} | pnl={:.1f}pip",
                pos.direction, pos.symbol, recent, pnl_pips,
            )
            self._record_closed_trade(
                pos, result.close_price,
                f"CONVICTION_COLLAPSE(scores:{recent[0]}→{recent[-1]})",
                close_result=result,
            )
            self.managed_positions.pop(oid, None)
            self.position_store.remove_position(oid)
            self._position_scores.pop(oid, None)

    def _check_htf_candle_close(
        self,
        oid: str,
        pos: ManagedPosition,
        h1_df,
        now: datetime,
    ) -> None:
        """
        On every new H1 candle close, check if the candle closed against our
        trade direction. A bearish H1 close on a long trade means the higher
        timeframe is rejecting the move — a real trader reassesses immediately.
        """
        if h1_df is None or len(h1_df) < 3:
            return

        # Get the most recently CLOSED H1 candle (index -2, since -1 is forming)
        last_closed = h1_df.iloc[-2]
        candle_time = last_closed.name if hasattr(last_closed, 'name') else None

        if candle_time is None:
            return

        # Only act once per H1 close
        last_seen = self._position_last_h1_close.get(oid)
        if last_seen is not None and candle_time <= last_seen:
            return
        self._position_last_h1_close[oid] = candle_time

        # Check candle direction
        candle_open = float(last_closed.get("open", 0))
        candle_close = float(last_closed.get("close", 0))
        if candle_open == 0 or candle_close == 0:
            return

        is_long = pos.direction == "BUY"
        candle_bearish = candle_close < candle_open
        candle_bullish = candle_close > candle_open

        # Candle body size as a sanity filter — ignore tiny doji candles
        candle_body = abs(candle_close - candle_open)
        candle_range = float(last_closed.get("high", candle_close)) - float(last_closed.get("low", candle_open))
        if candle_range > 0 and (candle_body / candle_range) < 0.3:
            return  # doji — no directional conviction

        opposing_close = (is_long and candle_bearish) or (not is_long and candle_bullish)
        if not opposing_close:
            return

        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        if tm_trade is None:
            return

        pnl_pips = tm_trade.pnl_pips
        # Don't exit a trade that's well in profit just because of one H1 candle
        if pnl_pips > 30:
            return

        # If trade is in loss or marginal — exit on HTF rejection
        result = self.platforms.close_trade(oid, pos.platform)
        if result.success:
            direction_str = "BEARISH" if candle_bearish else "BULLISH"
            logger.info(
                "📊 HTF EXIT — {} {} | H1 {} candle close against trade | pnl={:.1f}pip",
                pos.direction, pos.symbol, direction_str, pnl_pips,
            )
            self._record_closed_trade(
                pos, result.close_price,
                f"HTF_H1_{direction_str}_CLOSE",
                close_result=result,
            )
            self.managed_positions.pop(oid, None)
            self.position_store.remove_position(oid)
            self._position_scores.pop(oid, None)
            self._position_last_h1_close.pop(oid, None)

    def _apply_dynamic_sl_tightening(self, oid: str, pos: ManagedPosition) -> None:
        """
        Beyond breakeven, progressively lock in profit as the trade develops.
        A trader manually moves their SL higher/lower as price moves in their
        favour — this does it automatically and executes the real modify call.

        Tightening logic:
          - Only activates after breakeven is set
          - Triggers when profit exceeds dynamic_sl_tighten_at_r (default 2R)
          - New SL = current_price - (original_risk * tighten_ratio)
          - Only ever moves SL in profit direction — never backwards
        """
        cfg = self.config.risk
        if not cfg.dynamic_sl_tightening_enabled:
            return

        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        if tm_trade is None or not tm_trade.breakeven_active:
            return

        is_long = pos.direction == "BUY"
        try:
            tick = self.platforms.get_price(pos.symbol)
            current = tick.bid if is_long else tick.ask
        except Exception:
            return

        original_risk = abs(pos.entry_price - pos.sl_original) if hasattr(pos, 'sl_original') else abs(pos.entry_price - tm_trade.stop_loss)
        if original_risk < 1e-8:
            return

        if is_long:
            profit_r = (current - pos.entry_price) / original_risk
        else:
            profit_r = (pos.entry_price - current) / original_risk

        if profit_r < cfg.dynamic_sl_tighten_at_r:
            return

        # Calculate tightened SL
        tighten_distance = original_risk * cfg.dynamic_sl_tighten_ratio
        if is_long:
            new_sl = current - tighten_distance
            if new_sl <= pos.sl:
                return  # no improvement
        else:
            new_sl = current + tighten_distance
            if new_sl >= pos.sl:
                return  # no improvement

        new_sl = round(new_sl, 5)
        success = self.platforms.modify_trade(oid, pos.platform, new_sl=new_sl)
        if success:
            old_sl = pos.sl
            pos.sl = new_sl
            tm_trade.stop_loss = new_sl
            self.position_store.update_position(oid, sl=new_sl)
            logger.info(
                "📈 DYNAMIC SL TIGHTEN — {} {} | {:.5f} → {:.5f} | {:.1f}R profit locked",
                pos.direction, pos.symbol, old_sl, new_sl, profit_r,
            )

    def _check_scale_in_on_scan(self, oid: str, pos: ManagedPosition, scan_result) -> None:
        """
        Scale-in wired to live scanner data.
        Only adds to a position when:
          - TP1 already hit (running on half position)
          - Breakeven active (house money)
          - Scanner STILL agrees with our direction AND score >= entry threshold
          - Trade is at N× profit (configurable)
          - Correlation and margin still allow it
        """
        if (
            getattr(self, '_portfolio_risk_sm', None) is not None
            and self._portfolio_risk_sm.state in (PortfolioRiskState.DEFENSIVE, PortfolioRiskState.REDUCING)
        ):
            return
        cfg = self.config.risk
        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        if tm_trade is None:
            return
        if not tm_trade.partial_closed or not tm_trade.breakeven_active:
            return
        if pos.scale_in_count >= cfg.scale_in_max_adds:
            return

        # Scanner must agree with our direction
        is_long = pos.direction == "BUY"
        direction_match = (
            (is_long and scan_result.direction == "LONG")
            or (not is_long and scan_result.direction == "SHORT")
        )
        if not direction_match:
            return

        # Score must be strong — don't add to a fading winner
        if scan_result.score < self.config.scoring.min_entry_score:
            return

        # Must be at minimum profit R
        is_long_trade = self.trade_manager._is_long(tm_trade.direction)
        risk_distance = abs(tm_trade.entry_price - tm_trade.stop_loss)
        if risk_distance < 1e-8:
            return

        try:
            tick = self.platforms.get_price(pos.symbol)
            current = tick.bid if is_long_trade else tick.ask
        except Exception:
            return

        profit_r = (
            (current - tm_trade.entry_price) / risk_distance if is_long_trade
            else (tm_trade.entry_price - current) / risk_distance
        )
        if profit_r < cfg.scale_in_min_profit_r:
            return

        # Correlation check
        _current_risk = self.risk_engine.drawdown_guard.risk_map.get(
            self.risk_engine.drawdown_guard.mode, 0.005,
        )
        open_trades_list = [
            OpenTrade(pair=p.symbol, direction=p.direction, risk_pct=_current_risk)
            for p in self.managed_positions.values()
        ]
        can_open, reason = self.correlation.can_open_trade(pos.symbol, pos.direction, open_trades_list)
        if not can_open:
            return

        if len(self.managed_positions) >= cfg.max_open_trades:
            return

        if cfg.margin_guardian_enabled:
            ml = self._get_margin_level()
            if ml is not None and ml < cfg.margin_block_entry_pct:
                return

        add_lots = round(pos.lots * cfg.scale_in_add_ratio, 2)
        add_lots = max(0.01, add_lots)

        from platform_context import build_context_for_symbol
        ctx = build_context_for_symbol(pos.symbol)
        if ctx.uses_stake:
            return  # Deriv stake-based — scale-in not supported

        order = self.platforms.execute_entry(
            pos.symbol,
            pos.direction,
            add_lots,
            tm_trade.stop_loss,
            tm_trade.tp2,
            comment=f"APEX|SCALEIN_SCAN|{pos.score}|{scan_result.score}",
        )
        if order.success:
            pos.scale_in_count += 1
            logger.info(
                "📈 SCALE-IN (scan-wired) — {} {} | +{} lots (add #{}) | fresh score={}",
                pos.direction, pos.symbol, add_lots, pos.scale_in_count, scan_result.score,
            )

    def _check_news_exit(self, now: datetime) -> None:
        """
        Close or tighten open trades before high-impact news events.
        A real trader checks their economic calendar before every news event
        and manages their exposure accordingly.
        """
        cfg = self.config.risk
        if not cfg.news_exit_enabled:
            return
        if not self.managed_positions:
            return

        try:
            open_pairs = [pos.symbol for pos in self.managed_positions.values()]
            news_status = self.news_guard.check(open_pairs, now)
        except Exception:
            return

        if not hasattr(news_status, 'upcoming_events'):
            return

        for event in getattr(news_status, 'upcoming_events', []):
            minutes_until = getattr(event, 'minutes_until', None)
            affected_pairs = getattr(event, 'affected_pairs', [])
            impact = getattr(event, 'impact', 'LOW')

            if impact not in ('HIGH', 'MEDIUM'):
                continue
            if minutes_until is None or minutes_until > cfg.news_exit_minutes_before:
                continue

            for oid, pos in list(self.managed_positions.items()):
                if pos.symbol not in affected_pairs:
                    continue
                if oid in self._news_exit_protected:
                    continue

                if cfg.news_exit_mode == "close":
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        logger.info(
                            "📰 NEWS EXIT — {} {} | {} in {:.0f}min | closed @ {:.5f}",
                            pos.direction, pos.symbol, getattr(event, 'name', 'event'),
                            minutes_until, result.close_price,
                        )
                        self._record_closed_trade(
                            pos, result.close_price,
                            f"NEWS_EXIT({getattr(event, 'name', 'event')})",
                            close_result=result,
                        )
                        self.managed_positions.pop(oid, None)
                        self.position_store.remove_position(oid)
                        self._news_exit_protected.discard(oid)

                elif cfg.news_exit_mode == "tighten":
                    # Move SL to breakeven to protect position
                    tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                    if tm_trade is None:
                        continue
                    be_level = pos.entry_price
                    success = self.platforms.modify_trade(oid, pos.platform, new_sl=be_level)
                    if success:
                        pos.sl = be_level
                        tm_trade.stop_loss = be_level
                        self._news_exit_protected.add(oid)
                        self.position_store.update_position(oid, sl=be_level)
                        logger.info(
                            "📰 NEWS TIGHTEN — {} {} | SL→entry {:.5f} | {} in {:.0f}min",
                            pos.direction, pos.symbol, be_level,
                            getattr(event, 'name', 'event'), minutes_until,
                        )

    def _check_session_close(self, now: datetime) -> None:
        """
        Manage open trades as sessions end.

        Real traders:
          - Close index trades before the exchange closes for the day
          - Reduce or exit trades entering the dead zone (00:00-02:00 UTC)
          - Don't hold GER40 into the Xetra close at 20:00 UTC
        """
        cfg = self.config.risk
        if not cfg.session_close_enabled:
            return

        utc_hour = now.hour
        utc_minute = now.minute

        # Index session close windows (UTC)
        index_close_windows = {
            "HK50":  (8, 0),    # HKEX closes 08:00 UTC
            "JP225": (6, 30),   # Osaka closes 06:30 UTC
            "AUS200":(6, 0),    # ASX closes 06:00 UTC
            "GER40": (20, 0),   # Xetra closes 20:00 UTC
            "FRA40": (20, 0),   # Euronext closes 20:00 UTC
            "UK100": (16, 30),  # LSE closes 16:30 UTC
            "US30":  (21, 0),   # CME equity closes 21:00 UTC
            "US100": (21, 0),
            "US500": (21, 0),
        }

        for oid, pos in list(self.managed_positions.items()):
            symbol = pos.symbol

            # Check index session close
            close_time = index_close_windows.get(symbol)
            if close_time:
                close_h, close_m = close_time
                # Minutes until close
                close_total = close_h * 60 + close_m
                now_total = utc_hour * 60 + utc_minute
                diff = close_total - now_total
                # Within buffer window before close
                if 0 < diff <= cfg.index_close_buffer_minutes:
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                        pnl = tm_trade.pnl_pips if tm_trade else 0.0
                        logger.info(
                            "🕐 SESSION CLOSE EXIT — {} {} | exchange closes in {}min | pnl={:.1f}pip",
                            pos.direction, symbol, diff, pnl,
                        )
                        self._record_closed_trade(
                            pos, result.close_price,
                            f"SESSION_CLOSE({symbol})",
                            close_result=result,
                        )
                        self.managed_positions.pop(oid, None)
                        self.position_store.remove_position(oid)
                        self._position_scores.pop(oid, None)
                    continue

            # Dead zone management for forex (00:00-02:00 UTC)
            if cfg.dead_zone_management and pos.symbol.find("USD") >= 0 or pos.symbol.find("JPY") >= 0:
                in_dead_zone = (utc_hour == 0 or (utc_hour == 1 and utc_minute <= 59))
                if in_dead_zone:
                    tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                    if tm_trade and not tm_trade.breakeven_active:
                        # Move to breakeven during dead zone — don't hold unprotected
                        from management.partial_close import PartialCloseCalculator
                        pip_size = tm_trade.pip_size if hasattr(tm_trade, 'pip_size') else 0.0001
                        be_level = PartialCloseCalculator.calculate_breakeven_level(
                            pos.entry_price, pos.direction, 2.0, pip_size,
                        )
                        success = self.platforms.modify_trade(oid, pos.platform, new_sl=be_level)
                        if success:
                            pos.sl = be_level
                            tm_trade.stop_loss = be_level
                            tm_trade.breakeven_active = True
                            self.position_store.update_position(oid, sl=be_level, at_breakeven=True)
                            logger.info(
                                "🌙 DEAD ZONE PROTECTION — {} {} | SL→BE {:.5f}",
                                pos.direction, symbol, be_level,
                            )

    def _check_portfolio_heat(self) -> None:
        """
        Compute live capital-at-risk heat and feed the portfolio risk
        state machine.  When in DEFENSIVE, advance eligible positions
        to breakeven.  When in REDUCING and reduction enabled, trim the
        weakest position (Phase 4b).
        """
        cfg = self.config.risk
        if not cfg.portfolio_heat_enabled:
            return
        if not self.managed_positions:
            self._current_portfolio_heat = 0.0
            return

        equity = getattr(self.risk_engine, 'balance', 0.0) or 0.0
        position_risks: list[PositionRisk] = []

        for oid, pos in list(self.managed_positions.items()):
            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            at_be = bool(tm_trade and tm_trade.breakeven_active)

            info = INSTRUMENT_REGISTRY.get(pos.symbol)
            pip_size = info.pip_size if info else 0.0001
            pip_val = info.pip_value_per_lot if info else 10.0

            ctx = build_context_for_symbol(pos.symbol)
            is_deriv = ctx.uses_stake

            risk_dollars, is_fallback = compute_position_risk_dollars(
                direction=pos.direction,
                entry_price=pos.entry_price,
                sl=pos.sl,
                lots=pos.lots,
                pip_size=pip_size,
                pip_value_per_lot=pip_val,
                at_breakeven=at_be,
                stake_usd=pos.stake_usd,
                multiplier=pos.multiplier,
                is_deriv_stake=is_deriv,
            )
            if is_fallback:
                risk_dollars = equity * cfg.risk_per_trade_pct / 100.0
                logger.debug(
                    "[PortfolioRisk] {} fallback to static proxy ${:.2f}",
                    pos.symbol, risk_dollars,
                )
            position_risks.append(PositionRisk(
                order_id=oid,
                symbol=pos.symbol,
                direction=pos.direction,
                risk_dollars=risk_dollars,
                is_at_breakeven=at_be,
                is_fallback=is_fallback,
            ))

        live_heat = compute_live_heat_pct(position_risks, equity)
        self._current_portfolio_heat = live_heat

        if live_heat >= cfg.max_portfolio_heat_pct:
            logger.warning(
                "🌡️ PORTFOLIO HEAT {:.2f}% — max {:.1f}% | {} positions | equity ${:.2f}",
                live_heat, cfg.max_portfolio_heat_pct,
                len(self.managed_positions), equity,
            )
            self._add_warning(
                "warning",
                f"Portfolio heat {live_heat:.2f}% — approaching limit",
            )

        if self._portfolio_risk_sm is None:
            return

        _current_risk = self.risk_engine.drawdown_guard.risk_map.get(
            self.risk_engine.drawdown_guard.mode, 0.005,
        )
        open_trades = [
            OpenTrade(pair=p.symbol, direction=p.direction, risk_pct=_current_risk)
            for p in self.managed_positions.values()
        ]
        exposure = self.correlation.calculate_exposure(open_trades)

        snapshot = PortfolioRiskSnapshot(
            live_heat_pct=live_heat,
            position_risks=position_risks,
            correlation_safe=exposure.is_safe,
            max_currency_exposure=exposure.max_single_currency_exposure,
            timestamp=_time.monotonic(),
        )
        transition = self._portfolio_risk_sm.evaluate(snapshot)

        # ── Emergency trigger evaluation (Phase 4c) ──────────────────────
        cfg_e = self.config.risk
        if cfg_e.portfolio_emergency_enabled and self._portfolio_risk_sm is not None:
            reconcile_age = (
                (datetime.now(timezone.utc) - self._last_reconcile_time).total_seconds()
                if self._last_reconcile_time is not None
                else 999999.0
            )
            broker_positions: list = []
            try:
                broker_positions = self.platforms.get_all_open_positions()
            except Exception:
                pass
            emergency_snap = EmergencyTriggerSnapshot(
                live_heat_pct=live_heat,
                drawdown_mode=self.drawdown.mode.value,
                reconcile_age_seconds=reconcile_age,
                managed_count=len(self.managed_positions),
                broker_count=len(broker_positions),
            )
            trigger_result = evaluate_emergency_triggers(
                emergency_snap,
                heat_emergency_pct=cfg_e.heat_emergency_pct,
                emergency_reconcile_failure_seconds=cfg_e.emergency_reconcile_failure_seconds,
                emergency_broker_exposure_tolerance=cfg_e.emergency_broker_exposure_tolerance,
            )
            if trigger_result.any_fired:
                transition = self._portfolio_risk_sm.escalate_to_emergency(
                    trigger_result, live_heat, exposure.is_safe,
                )

        if transition.changed:
            severity = "info"
            if transition.state in (
                PortfolioRiskState.DEFENSIVE,
                PortfolioRiskState.REDUCING,
                PortfolioRiskState.EMERGENCY,
            ):
                severity = "critical"
            self._add_warning(
                severity,
                f"[PortfolioRisk] {transition.reason}",
            )

        if transition.state == PortfolioRiskState.DEFENSIVE:
            self._apply_defensive_actions()
        elif transition.state == PortfolioRiskState.REDUCING:
            self._apply_defensive_actions()
            self._apply_reduction_actions(position_risks, exposure)
        elif transition.state == PortfolioRiskState.EMERGENCY:
            self._apply_defensive_actions()
            self._apply_reduction_actions(position_risks, exposure)
            self._apply_emergency_actions(position_risks, exposure, transition)

    def _apply_defensive_actions(self) -> None:
        """
        Non-destructive defensive actions: advance eligible positions to
        breakeven.  Respects per-position cooldowns and idempotency.
        No positions are closed or reduced (Phase 4b/4c).
        """
        cfg = self.config.risk
        now = _time.monotonic()

        for oid, pos in list(self.managed_positions.items()):
            if pos.at_breakeven:
                continue

            last_action = self._defensive_action_timestamps.get(oid, 0.0)
            if (now - last_action) < cfg.defensive_action_cooldown_seconds:
                continue

            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            if tm_trade is None:
                continue
            if tm_trade.breakeven_active:
                continue

            try:
                tick = self.platforms.get_price(pos.symbol)
                current_price = tick.bid if pos.direction == "BUY" else tick.ask
            except Exception:
                continue

            eligible = is_eligible_for_defensive_breakeven(
                direction=pos.direction,
                entry_price=pos.entry_price,
                sl=pos.sl,
                current_price=current_price,
                tp1_hit=pos.tp1_hit,
                partial_closed=getattr(tm_trade, 'partial_closed', False),
                at_breakeven=pos.at_breakeven,
                be_eligible_r_multiple=cfg.be_eligible_r_multiple,
            )
            if not eligible:
                continue

            from management.partial_close import PartialCloseCalculator
            pip_size = tm_trade.pip_size if hasattr(tm_trade, 'pip_size') else 0.0001
            be_level = PartialCloseCalculator.calculate_breakeven_level(
                pos.entry_price, pos.direction, 2.0, pip_size,
            )
            is_improvement = (
                (pos.direction == "BUY" and be_level > pos.sl)
                or (pos.direction == "SELL" and be_level < pos.sl)
            )
            if not is_improvement:
                continue

            success = self.platforms.modify_trade(oid, pos.platform, new_sl=be_level)
            if success:
                pos.sl = be_level
                tm_trade.stop_loss = be_level
                tm_trade.breakeven_active = True
                pos.at_breakeven = True
                self.position_store.update_position(oid, sl=be_level, at_breakeven=True)
                self._defensive_action_timestamps[oid] = now
                logger.warning(
                    "[PortfolioRisk] DEFENSIVE BE — {} {} | SL→{:.5f} | eligible via {}",
                    pos.direction, pos.symbol, be_level,
                    "tp1_hit" if pos.tp1_hit else f">={cfg.be_eligible_r_multiple}R excursion",
                )

    def _apply_reduction_actions(
        self,
        position_risks: list[PositionRisk],
        exposure,
    ) -> None:
        """
        Graduated exposure reduction: trim the single weakest position
        by a configurable fraction.  Bypasses entry circuit-breaker /
        cooldowns / opportunity throttles — risk exits are not entries.

        Safety rails:
          • Gated behind portfolio_reduction_enabled (default OFF).
          • Per-position cooldown prevents repeated trims of the same position.
          • Hourly cap prevents a reduction storm.
          • Never fully closes a position (floor at broker minimum 0.01).
          • Orphan/unknown positions are neutral-ranked, never auto-targeted.
        """
        cfg = self.config.risk
        if not cfg.portfolio_reduction_enabled:
            return

        now = _time.monotonic()

        cutoff = now - 3600.0
        self._reduction_actions_this_hour = [
            t for t in self._reduction_actions_this_hour if t > cutoff
        ]
        if len(self._reduction_actions_this_hour) >= cfg.max_reductions_per_hour:
            logger.debug(
                "[PortfolioRisk] REDUCING — hourly cap reached ({}/{})",
                len(self._reduction_actions_this_hour), cfg.max_reductions_per_hour,
            )
            return

        total_risk = sum(pr.risk_dollars for pr in position_risks)
        pos_dicts: list[dict] = []

        for oid, pos in list(self.managed_positions.items()):
            try:
                tick = self.platforms.get_price(pos.symbol)
                current_price = tick.bid if pos.direction == "BUY" else tick.ask
            except Exception:
                continue

            pr_match = next((pr for pr in position_risks if pr.order_id == oid), None)
            risk_dollars = pr_match.risk_dollars if pr_match else 0.0

            pos_dicts.append({
                "order_id": oid,
                "symbol": pos.symbol,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "sl": pos.sl,
                "current_price": current_price,
                "score": pos.score,
                "regime": pos.regime,
                "entry_type": pos.entry_type,
                "open_time_utc": pos.open_time,
                "risk_dollars": risk_dollars,
                "lots": pos.lots,
            })

        if not pos_dicts:
            return

        ranked = rank_positions_weakest_first(pos_dicts, total_risk)
        if not ranked:
            return

        for candidate in ranked:
            if candidate.is_insufficient_data:
                continue

            oid = candidate.order_id
            pos = self.managed_positions.get(oid)
            if pos is None:
                continue

            last_trim = self._reduction_action_timestamps.get(oid, 0.0)
            if (now - last_trim) < cfg.reduction_action_cooldown_seconds:
                continue

            if pos.lots <= 0.01:
                continue

            trim_lots = round(pos.lots * cfg.reduction_partial_ratio, 2)
            trim_lots = max(0.01, trim_lots)
            remaining_after = round(pos.lots - trim_lots, 2)
            if remaining_after < 0.01:
                trim_lots = round(pos.lots - 0.01, 2)
                if trim_lots < 0.01:
                    continue

            pos_ctx = build_context_for_symbol(pos.symbol)
            if not pos_ctx.supports_partial_close:
                logger.debug(
                    "[PortfolioRisk] REDUCING — {} does not support partial close, skipping",
                    pos.symbol,
                )
                continue

            logger.warning(
                "[PortfolioRisk] REDUCING TRIM — {} {} | {:.2f} → {:.2f} lots "
                "| weakness={:.1f} ({}) | R={:.2f} | score={} | risk_share={:.1f}%",
                pos.direction, pos.symbol,
                pos.lots, remaining_after,
                candidate.weakness_score, candidate.reason,
                candidate.r_multiple, candidate.entry_score,
                candidate.risk_share_pct,
            )

            result = self.platforms.close_trade(oid, pos.platform, trim_lots)
            if result.success:
                pos.lots = remaining_after
                self.position_store.update_position(oid, lots=remaining_after)
                self._reduction_action_timestamps[oid] = now
                self._reduction_actions_this_hour.append(now)

                tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                if tm_trade:
                    tm_trade.remaining_size_lots = remaining_after

                self._add_warning(
                    "critical",
                    f"[PortfolioRisk] REDUCING — trimmed {pos.direction} {pos.symbol} "
                    f"by {trim_lots} lots (weakness={candidate.weakness_score:.1f})",
                )
                logger.warning(
                    "[PortfolioRisk] REDUCING TRIM SUCCESS — {} {} | trimmed {:.2f} lots, "
                    "remaining {:.2f} | heat was {:.2f}%",
                    pos.direction, pos.symbol, trim_lots, remaining_after,
                    self._current_portfolio_heat,
                )
            else:
                logger.warning(
                    "[PortfolioRisk] REDUCING TRIM FAILED — {} {} | broker rejected",
                    pos.direction, pos.symbol,
                )
            return

    def _apply_emergency_actions(
        self,
        position_risks: list[PositionRisk],
        exposure,
        transition,
    ) -> None:
        """
        Progressive full-close of weakest positions until heat returns
        below emergency threshold.  Bypasses entry circuit-breaker /
        cooldowns / opportunity throttles — risk exits are not entries.

        Precedence: margin_guardian owns margin events; this method owns
        heat/correlation-survival, drawdown-frozen, and reconcile-failure.
        Before every close, re-check managed_positions membership to
        prevent double-closing a position margin_guardian already closed.

        Safety rails:
          • Gated behind portfolio_emergency_enabled (default OFF).
          • Per-position cooldown prevents repeated closes.
          • Per-hour cap prevents an unbounded close storm.
          • emergency_max_closes_per_cycle limits closes per loop.
          • Orphan/unknown positions are neutral-ranked.
        """
        cfg = self.config.risk
        if not cfg.portfolio_emergency_enabled:
            return

        now = _time.monotonic()

        cutoff = now - 3600.0
        self._emergency_closes_this_hour = [
            t for t in self._emergency_closes_this_hour if t > cutoff
        ]
        if len(self._emergency_closes_this_hour) >= cfg.emergency_max_closes_per_hour:
            logger.warning(
                "[PortfolioRisk] EMERGENCY — hourly close cap reached ({}/{})",
                len(self._emergency_closes_this_hour), cfg.emergency_max_closes_per_hour,
            )
            return

        total_risk = sum(pr.risk_dollars for pr in position_risks)
        pos_dicts: list[dict] = []

        for oid, pos in list(self.managed_positions.items()):
            try:
                tick = self.platforms.get_price(pos.symbol)
                current_price = tick.bid if pos.direction == "BUY" else tick.ask
            except Exception:
                continue

            pr_match = next((pr for pr in position_risks if pr.order_id == oid), None)
            risk_dollars = pr_match.risk_dollars if pr_match else 0.0

            pos_dicts.append({
                "order_id": oid,
                "symbol": pos.symbol,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "sl": pos.sl,
                "current_price": current_price,
                "score": pos.score,
                "regime": pos.regime,
                "entry_type": pos.entry_type,
                "open_time_utc": pos.open_time,
                "risk_dollars": risk_dollars,
                "lots": pos.lots,
            })

        if not pos_dicts:
            return

        ranked = rank_positions_weakest_first(pos_dicts, total_risk)
        if not ranked:
            return

        closes_this_cycle = 0
        for candidate in ranked:
            if closes_this_cycle >= cfg.emergency_max_closes_per_cycle:
                break
            if len(self._emergency_closes_this_hour) >= cfg.emergency_max_closes_per_hour:
                break

            if candidate.is_insufficient_data:
                continue

            oid = candidate.order_id
            pos = self.managed_positions.get(oid)
            if pos is None:
                continue

            last_action = self._emergency_action_timestamps.get(oid, 0.0)
            if (now - last_action) < cfg.emergency_action_cooldown_seconds:
                continue

            logger.critical(
                "[PortfolioRisk] EMERGENCY CLOSE — {} {} | weakness={:.1f} ({}) "
                "| R={:.2f} | score={} | risk_share={:.1f}% | trigger={}",
                pos.direction, pos.symbol,
                candidate.weakness_score, candidate.reason,
                candidate.r_multiple, candidate.entry_score,
                candidate.risk_share_pct,
                transition.emergency_trigger or "sustained",
            )

            result = self.platforms.close_trade(oid, pos.platform)
            if result.success:
                self._record_closed_trade(
                    pos, result.close_price,
                    f"EMERGENCY_CLOSE({transition.emergency_trigger or 'sustained'})",
                    close_result=result,
                )
                self.managed_positions.pop(oid, None)
                self.position_store.remove_position(oid)
                self._emergency_action_timestamps[oid] = now
                self._emergency_closes_this_hour.append(now)
                closes_this_cycle += 1

                self._add_warning(
                    "critical",
                    f"[PortfolioRisk] EMERGENCY — closed {pos.direction} {pos.symbol} "
                    f"(weakness={candidate.weakness_score:.1f}, "
                    f"trigger={transition.emergency_trigger or 'sustained'})",
                )
                logger.critical(
                    "[PortfolioRisk] EMERGENCY CLOSE SUCCESS — {} {} | "
                    "heat was {:.2f}% | closes this hour: {}",
                    pos.direction, pos.symbol,
                    self._current_portfolio_heat,
                    len(self._emergency_closes_this_hour),
                )
            else:
                logger.warning(
                    "[PortfolioRisk] EMERGENCY CLOSE FAILED — {} {} | broker rejected",
                    pos.direction, pos.symbol,
                )

    def _check_spread_deterioration(self) -> None:
        """
        Monitor spread quality on open positions.
        If spread widens beyond N× normal (e.g. ahead of news, broker issues,
        thin liquidity), tighten SL to protect against a spike close-out.
        """
        cfg = self.config.risk
        if not cfg.spread_monitor_enabled:
            return

        for oid, pos in list(self.managed_positions.items()):
            try:
                tick = self.platforms.get_price(pos.symbol)
                if not hasattr(tick, 'spread') or tick.spread is None:
                    continue

                # Get typical spread for this instrument from execution monitor
                typical_spread = self.execution_monitor.get_typical_spread(pos.symbol)
                if typical_spread is None or typical_spread < 1e-8:
                    continue

                current_spread = tick.spread
                spread_ratio = current_spread / typical_spread

                if spread_ratio >= cfg.spread_deterioration_multiplier:
                    tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                    if tm_trade is None or tm_trade.breakeven_active:
                        continue

                    # Tighten SL to breakeven as protection
                    from management.partial_close import PartialCloseCalculator
                    pip_size = tm_trade.pip_size if hasattr(tm_trade, 'pip_size') else 0.0001
                    be_level = PartialCloseCalculator.calculate_breakeven_level(
                        pos.entry_price, pos.direction, 2.0, pip_size,
                    )
                    is_improvement = (
                        (pos.direction == "BUY" and be_level > pos.sl)
                        or (pos.direction == "SELL" and be_level < pos.sl)
                    )
                    if not is_improvement:
                        continue

                    success = self.platforms.modify_trade(oid, pos.platform, new_sl=be_level)
                    if success:
                        pos.sl = be_level
                        tm_trade.stop_loss = be_level
                        tm_trade.breakeven_active = True
                        self.position_store.update_position(oid, sl=be_level, at_breakeven=True)
                        logger.warning(
                            "📊 SPREAD DETERIORATION — {} {} | spread {:.1f}× normal | SL→BE",
                            pos.direction, pos.symbol, spread_ratio,
                        )
            except Exception as exc:
                logger.debug("Spread monitor error for {}: {}", pos.symbol, exc)

    def _check_scale_in(self):
        if not self.config.risk.scale_in_enabled:
            return
        if (
            getattr(self, '_portfolio_risk_sm', None) is not None
            and self._portfolio_risk_sm.state in (PortfolioRiskState.DEFENSIVE, PortfolioRiskState.REDUCING)
        ):
            return
        for oid, pos in list(self.managed_positions.items()):
            try:
                ctx = build_context_for_symbol(pos.symbol)
                if ctx.uses_stake:
                    continue
                tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                if tm_trade is None:
                    continue
                if not tm_trade.partial_closed or not tm_trade.breakeven_active:
                    continue
                if pos.scale_in_count >= self.config.risk.scale_in_max_adds:
                    continue
                risk_distance = abs(tm_trade.entry_price - tm_trade.original_stop_loss)
                if risk_distance < 1e-8:
                    continue
                is_long = self.trade_manager._is_long(tm_trade.direction)
                if is_long:
                    profit_r = (tm_trade.current_price - tm_trade.entry_price) / risk_distance
                else:
                    profit_r = (tm_trade.entry_price - tm_trade.current_price) / risk_distance
                if profit_r < self.config.risk.scale_in_min_profit_r:
                    continue
                open_trades_list = [
                    {"pair": p.symbol, "direction": p.direction}
                    for p in self.managed_positions.values()
                ]
                if not self.correlation.can_open_trade(pos.symbol, pos.direction, open_trades_list):
                    continue
                if len(self.managed_positions) >= self.config.risk.max_open_trades:
                    continue
                if self.config.risk.margin_guardian_enabled:
                    ml = self._get_margin_level()
                    if ml is not None and ml < self.config.risk.margin_block_entry_pct:
                        continue
                add_lots = round(pos.lots * self.config.risk.scale_in_add_ratio, 2)
                add_lots = max(0.01, add_lots)
                order = self.platforms.execute_entry(
                    pos.symbol,
                    pos.direction,
                    add_lots,
                    tm_trade.stop_loss,
                    tm_trade.tp2,
                    comment=f"APEX|SCALEIN|{pos.score}",
                )
                if order.success:
                    pos.scale_in_count += 1
                    logger.info(
                        "📈 SCALE-IN — {} {} | +{} lots (add #{})",
                        pos.direction, pos.symbol, add_lots, pos.scale_in_count,
                    )
            except Exception as exc:
                logger.warning("Scale-in check failed for {}: {}", pos.symbol, exc)

    # ── Logging & journal ────────────────────────────────────────────────

    def _record_closed_trade(
        self,
        pos: ManagedPosition,
        close_price: float,
        outcome: str,
        close_result: Optional[CloseResult] = None,
    ) -> None:
        pip_size = get_pip_size(pos.symbol)
        is_buy = pos.direction == "BUY"
        pnl_pips = (close_price - pos.entry_price) / pip_size if is_buy else (pos.entry_price - close_price) / pip_size

        if close_result is not None and close_result.pnl != 0.0:
            pnl_dollars = round(close_result.pnl, 2)
        else:
            pos_ctx = build_context_for_symbol(pos.symbol)
            if pos_ctx.uses_stake:
                price_move_pct = abs(close_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0
                signed_move = price_move_pct if is_buy == (close_price >= pos.entry_price) else -price_move_pct
                pnl_dollars = round(pos.stake_usd * signed_move * pos.multiplier, 2)
            else:
                info = INSTRUMENT_REGISTRY.get(pos.symbol.upper())
                pip_value = info.pip_value_per_lot if info else 10.0
                pnl_dollars = round(pnl_pips * pip_value * pos.lots, 2)

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
            pos.direction,
            pos.symbol,
            pnl_pips,
            outcome,
            hold_seconds,
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
            pnl_dollars=pnl_dollars,
        )
        self._run_journal_async(self.journal.log_trade(trade_record))
        self.ml.register_new_trade()

    def _add_warning(self, level: str, message: str, symbol: str = "") -> None:
        """Append a system event to the in-memory activity feed for the dashboard."""
        from datetime import datetime, timezone

        entry = {
            "level": level,  # "warning" | "rejection" | "info"
            "symbol": symbol,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.system_warnings.insert(0, entry)
        if len(self.system_warnings) > self._MAX_WARNINGS:
            self.system_warnings = self.system_warnings[: self._MAX_WARNINGS]

    def _log_rejection(self, pair: str, direction: str, score: int, reason: str) -> None:
        logger.debug("❌ REJECTED {} {} (score {}) — {}", direction, pair, score, reason)
        self._add_warning(
            level="rejection",
            symbol=pair,
            message=f"REJECTED {direction} (score {score}) — {reason}",
        )
        decision = DecisionRecord(
            pair=pair,
            direction=direction,
            score=score,
            reason_rejected=reason,
        )
        self._run_journal_async(self.journal.log_decision(decision))

    # ── Margin guardian ──────────────────────────────────────────────────

    def _get_margin_level(self, symbol: str | None = None) -> float:
        """Return the margin_level for the platform serving *symbol*.
        Returns 0.0 when unavailable (Deriv, disconnected) — callers must
        treat 0.0 as *unknown*, never as a flatten trigger."""
        try:
            if symbol:
                connector = self.platforms.get_connector(symbol)
            else:
                for i, c in enumerate(self.platforms.mt5_connectors):
                    if self.platforms._mt5_connected_flags[i]:
                        connector = c
                        break
                else:
                    return 0.0
            info = connector.get_account_info()
            return info.margin_level
        except Exception:
            return 0.0

    def _check_margin_for_entry(self, symbol: str) -> tuple[bool, str]:
        ml = self._get_margin_level(symbol)
        if ml <= 0.0:
            return True, "margin_level unknown — skipping check"
        threshold = self.config.risk.margin_block_entry_pct
        if ml < threshold:
            return False, f"Margin level {ml:.0f}% < entry floor {threshold:.0f}%"
        return True, "Margin OK"

    def _margin_guardian_check(self) -> None:
        ml = self._get_margin_level()
        if ml <= 0.0:
            return
        risk_cfg = self.config.risk
        if ml < risk_cfg.margin_flatten_pct:
            logger.critical(
                "🚨 MARGIN GUARDIAN — margin {:.0f}% < flatten floor {:.0f}% — flattening all positions",
                ml, risk_cfg.margin_flatten_pct,
            )
            self._emergency_flatten_all()
        elif ml < risk_cfg.margin_warn_pct:
            logger.warning(
                "⚠️ MARGIN WARNING — margin {:.0f}% < warn level {:.0f}%",
                ml, risk_cfg.margin_warn_pct,
            )

    def _emergency_flatten_all(self) -> None:
        closed = 0
        for oid, pos in list(self.managed_positions.items()):
            try:
                result = self.platforms.close_trade(oid, pos.platform)
                if result.success:
                    self._record_closed_trade(
                        pos, result.close_price, "MARGIN_FLATTEN", close_result=result
                    )
                    closed += 1
            except Exception as exc:
                logger.error("Margin flatten failed for {}: {}", oid, exc)
        self.managed_positions.clear()
        self.position_store.clear_all()
        from brain.drawdown_guard import DrawdownMode
        self.drawdown._mode = DrawdownMode.FROZEN
        logger.critical("🚨 MARGIN FLATTEN COMPLETE — {} positions closed, risk FROZEN", closed)

    # ── Deriv multiplier lookup ──────────────────────────────────────────

    def _get_deriv_multiplier(self, symbol: str) -> int:
        """Retrieve the Deriv contract multiplier for a symbol via the connector."""
        try:
            if isinstance(self.platforms.deriv, DerivConnector):
                mapped = self.platforms.deriv.symbol_map(symbol)
                return self.platforms.deriv._get_multiplier(mapped)
        except Exception as exc:
            logger.debug("Deriv multiplier lookup failed for {} — using default 100: {}", symbol, exc)
        return 100

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
                            stats.execution_quality,
                            stats.avg_slippage_pips,
                            stats.avg_latency_ms,
                            stats.requote_count,
                        )
                except Exception:
                    pass
            self._daily_trades = 0
            self._last_reset_day = today

            if self.maintenance.should_run():
                try:
                    maint_result = self.maintenance.run()
                    logger.info("🧹 Daily maintenance — {}", maint_result)
                except Exception as exc:
                    logger.warning("Maintenance error: {}", exc)

        if self.ml.should_retrain():
            self._run_ml_optimization()

    def _get_sleep_interval(self) -> float:
        now = datetime.now(timezone.utc)
        session_status = self.session_engine.get_status(now)
        news_status = self.news_guard.check(self.config.enabled_pairs, now)
        return self.scheduler.get_scan_interval(
            session_status,
            news_status,
            has_active_positions=len(self.managed_positions) > 0,
        )

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
            raw_trades = self._journal_loop.run_until_complete(self.journal.get_all_trades_as_dicts())
            if not raw_trades:
                return
            for t in raw_trades:
                raw = t.pop("confluences_raw", [])
                if not isinstance(raw, list):
                    raw = []
                t["confluences_tags"] = _parse_confluence_tags(raw)
            report = self.ml.run_optimization(raw_trades)
            logger.info(
                "🧠 ML optimization — {} recommendations",
                len(report.recommendations),
            )
            for rec in report.recommendations[:3]:
                logger.info("  ML: {}", rec)

            # Feed trade history into scanner so EVEstimator has live data
            try:
                self.scanner._trade_history = raw_trades
            except Exception:
                pass
        except Exception:
            logger.exception("ML retraining error")

    # ── Re-entry evaluation ───────────────────────────────────────────

    def _check_re_entry(self, pos: ManagedPosition) -> None:
        """After a breakeven stop, check if the setup is still valid."""
        try:
            m5_data = self.platforms.fetch_market_data(pos.symbol, ["M5"], count=50)
            m5_df = m5_data.get("M5")
            if m5_df is None:
                return
            minutes_since = (datetime.now(timezone.utc) - pos.open_time).total_seconds() / 60
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
                    pos.direction,
                    pos.symbol,
                    opp.new_entry_zone,
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
