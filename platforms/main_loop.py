"""
APEX TRADER — Main Trading Loop
The heartbeat of the system.
Scan → Analyse → Trigger → Manage → Repeat.
Always watching. Always ready. In and out like a sniper.
"""

import asyncio
import signal
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
from config import AppConfig, INSTRUMENT_REGISTRY, get_pip_size
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
from platforms.platform_manager import PlatformManager
from platform_context import PlatformContext, build_context_for_symbol
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
        self.density_tracker = OpportunityDensityTracker(window_minutes=60)
        self.vol_monitor = SystemVolatilityMonitor()
        self.trade_manager = TradeManager(
            partial_close_ratio=0.5,
            breakeven_buffer_pips=2.0,
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
        self._restore_positions()
        self._reconcile_positions()

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
            self._update_positions()
            return cycle

        should_scan = self.scheduler.should_scan_now(
            self._last_scan_time,
            session_status,
            news_status,
            has_active_positions=len(self.managed_positions) > 0,
        )

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
            closed_count = self._update_positions()
            self.watchdog.record_trade_check_success()
        except Exception as exc:
            logger.error("Position update error: {}", exc)
            closed_count = 0
            self.watchdog.record_trade_check_failure()

        cycle["positions_updated"] = len(self.managed_positions)
        cycle["positions_closed"] = closed_count

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
            del self.managed_positions[oid]
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
        from config import is_always_open

        return any(is_always_open(pair) for pair in self.config.enabled_pairs)

    def _scan_and_enter(self, session_status, news_status, now: datetime, cycle: dict) -> None:
        try:
            market_data = self.platforms.fetch_all_market_data(now_utc=now)
        except Exception as exc:
            logger.error("Market data fetch failed: {}", exc)
            return

        if not market_data:
            return

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
        order = self.platforms.execute_entry(
            pair,
            direction,
            adjusted_lots,
            signal.stop_loss,
            signal.tp1,
            comment=f"APEX|{signal.score}|{session}",
            stake_usd=stake_usd,
        )

        if not order.success:
            self._execution_breaker.record_failure()
            return False

        self._execution_breaker.record_success()

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

    # ── Position management ──────────────────────────────────────────────

    def _update_positions(self) -> int:
        closed_count = 0
        to_remove: list[str] = []

        self._reconcile_externally_closed(to_remove)
        for oid in to_remove:
            del self.managed_positions[oid]
            self.position_store.remove_position(oid)
        closed_count += len(to_remove)
        to_remove = []

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
                    # MT5: native partial close
                    partial_lots = round(pos.lots * 0.5, 2)
                    partial_lots = max(0.01, partial_lots)
                    result = self.platforms.close_trade(oid, pos.platform, partial_lots)
                    if result.success:
                        pos.tp1_hit = True
                        pos.lots = round(pos.lots - partial_lots, 2)
                        self.position_store.update_position(oid, tp1_hit=True, lots=pos.lots)
                        logger.info("✅ TP1 HIT (MT5 partial) — {} {} | 50% closed", pos.direction, pos.symbol)
                else:
                    # Deriv: partial close is not supported — close the full contract
                    # and immediately open a fresh smaller one to simulate TP1 management.
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        pos.tp1_hit = True
                        self._record_closed_trade(pos, result.close_price, "TP1_FULL_CLOSE_REOPEN", close_result=result)
                        to_remove.append(oid)
                        closed_count += 1
                        # Attempt re-open at half stake (50% of original risk)
                        try:
                            half_stake = round(pos.stake_usd * 0.5, 2) if pos.stake_usd > 0 else None
                            reopen_order = self.platforms.execute_entry(
                                pos.symbol,
                                pos.direction,
                                0.0,  # lots unused on Deriv
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

            if tm_trade.stop_loss != prev_sl:
                pos_ctx = build_context_for_symbol(pos.symbol)
                if pos_ctx.supports_modify:
                    self.platforms.modify_trade(oid, pos.platform, new_sl=tm_trade.stop_loss)
                    pos.sl = tm_trade.stop_loss
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
                else:
                    pos.sl = tm_trade.stop_loss
                    if tm_trade.breakeven_active and not pos.at_breakeven:
                        pos.at_breakeven = True
                        self.position_store.update_position(oid, sl=pos.sl, at_breakeven=True)
                    else:
                        self.position_store.update_position(oid, sl=pos.sl)
                    logger.debug(
                        "SL update for Deriv {} {} tracked locally only (modify not supported)",
                        pos.direction,
                        pos.symbol,
                    )

            pos.trailing = tm_trade.status == TradeStatus.TRAILING
            pos.re_entry_eligible = tm_trade.re_entry_eligible
            pos.last_update = datetime.now(timezone.utc)

        for oid in to_remove:
            del self.managed_positions[oid]
            self.position_store.remove_position(oid)

        return closed_count

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
