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
from adaptive.optimizer import AdaptiveOptimizer as MLAdapter
from brain.swap_model import load_swap_rates, estimate_swap
from platforms.base_connector import OrderResult, CloseResult, PositionInfo
from platforms.deriv.deriv_connector import DerivConnector
from platforms.order_idempotency import build_order_comment, generate_idempotency_key
from platforms.platform_manager import PlatformManager
from platform_context import PlatformContext, build_context_for_symbol
from risk.portfolio_risk_state import (
    PortfolioRiskStateMachine,
    PortfolioRiskState,
    compute_position_risk_dollars,
)
from risk.risk_engine import RiskEngine
from risk.risk_reporter import RiskReporter
from scanner import PairScanner, PairRanker, ScanScheduler
from trigger.entry_engine import EntryEngine, EntryRejection
from trigger.entry_validator import EntryValidator


from platforms.trading_loop.positions import ManagedPosition, _LockedPositions
from platforms.trading_loop.recovery_mixin import RecoveryReconciliationMixin
from platforms.trading_loop.risk_heat_mixin import RiskHeatMarginMixin
from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin

from persistence.event_store import get_event_store, new_cycle_id, new_setup_id
from persistence.domain_events import (
    DECISION_REJECT, ORDER_SENT, ORDER_FILLED, TRADE_OPEN, TRADE_CLOSE,
    SETUP_SKIPPED, SHADOW_CONTRACT_CREATED, BALANCE_UNAVAILABLE,
    PERSISTENCE_DEGRADED,
)
from persistence.shadow_store import ShadowStore, ShadowContract, new_contract_id


def _validate_stop_target_sidedness(
    direction: str,
    entry_price: float,
    stop_loss: float,
    tp1: float,
    tp2: float | None = None,
) -> tuple[bool, str]:
    is_long = direction.upper() in ("LONG", "BUY")
    if is_long:
        if not (stop_loss < entry_price):
            return False, f"LONG but SL({stop_loss}) >= entry({entry_price})"
        if not (entry_price < tp1):
            return False, f"LONG but entry({entry_price}) >= TP1({tp1})"
        if tp2 is not None and tp2 > 0 and not (tp1 <= tp2):
            return False, f"LONG but TP1({tp1}) > TP2({tp2})"
    else:
        if not (stop_loss > entry_price):
            return False, f"SHORT but SL({stop_loss}) <= entry({entry_price})"
        if not (entry_price > tp1):
            return False, f"SHORT but entry({entry_price}) <= TP1({tp1})"
        if tp2 is not None and tp2 > 0 and not (tp2 <= tp1):
            return False, f"SHORT but TP2({tp2}) > TP1({tp1})"
    return True, ""


class TradingLoop(RecoveryReconciliationMixin, RiskHeatMarginMixin, ExitChecksMixin):
    """
    Master trading loop — orchestrates the full pipeline.
    Scan → Entry → Manage → Risk → Repeat.
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()
        self.platforms = PlatformManager(self.config)
        _adaptive_weights = None
        _scanner_weights_dict = None
        if self.config.scoring.use_adaptive_scoring_weights:
            from adaptive.score_optimizer import (
                load_saved_weights as _load_weights,
                ScoringWeights as _SW,
                ADAPTIVE_WEIGHT_ENVELOPE_PCT as _ENV_PCT,
            )
            _adaptive_weights = _load_weights()
            _clamped = _adaptive_weights.clamped_to_envelope(_SW(), _ENV_PCT)
            _scanner_weights_dict = _clamped.as_dict()
        self.scanner = PairScanner(self.config, scoring_weights=_scanner_weights_dict)
        self.ranker = PairRanker()
        self.scheduler = ScanScheduler()
        self.orchestrator = MTFOrchestrator(
            min_entry_score=self.config.scoring.min_entry_score,
            use_adaptive_weights=self.config.scoring.use_adaptive_scoring_weights,
            scoring_weights=_adaptive_weights,
            volatility_stop_mode=self.config.risk.volatility_stop_mode,
            atr_stop_period=self.config.risk.atr_stop_period,
            atr_stop_mult=self.config.risk.atr_stop_mult,
            atr_stop_ratio_min=self.config.risk.atr_stop_ratio_min,
            atr_stop_ratio_max=self.config.risk.atr_stop_ratio_max,
            atr_stop_max_risk_mult=self.config.risk.atr_stop_max_risk_mult,
        )
        self.entry_engine = EntryEngine(config=self.config)
        self.drawdown = DrawdownGuard()
        self.correlation = CorrelationEngine(
            max_correlated_trades=self.config.risk.max_correlated_trades,
            max_cluster_same_direction=self.config.risk.max_cluster_same_direction,
            allow_intentional_hedge=self.config.risk.allow_intentional_hedge,
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
        self._shadow_store = ShadowStore()

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
        self._last_slot_blocked_candidate: dict | None = None    # best foregone candidate when slots full (F4)
        self._last_skipped_state: dict[str, tuple[str, int]] = {}  # symbol → (status, score) for emit-on-change

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
        cycle_id = new_cycle_id()
        with logger.contextualize(correlation_id=cycle_id, cycle_id=cycle_id):
            return self._run_once_inner(cycle_id)

    def _run_once_inner(self, cycle_id: str) -> dict:
        self._current_cycle_id = cycle_id
        self._current_setup_id = None
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
            try:
                self._save_position_checked(pos)
            except Exception as exc:
                logger.error(
                    "🔴 SHUTDOWN SAVE FAILED for {} ({}) — {}", pos.order_id, pos.symbol, exc,
                )
        if self.position_store.is_healthy():
            logger.info(
                "APEX TRADER SHUTTING DOWN — {} positions persisted for restart recovery, {} trades today",
                n_positions,
                self._daily_trades,
            )
        else:
            logger.error(
                "🔴 APEX TRADER SHUTTING DOWN — persistence degraded, {} positions may NOT be on disk. "
                "Restart recovery will rely on broker reconciliation. {} trades today",
                n_positions,
                self._daily_trades,
            )
        self.position_store.close()
        self.platforms.disconnect_all()
        try:
            self._journal_loop.close()
        except Exception as exc:
            logger.debug("[shutdown] journal loop close failed: {}", exc)
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
        self._emit_setup_skipped(report)

        # Track opportunity density — feeds into position sizing
        self.density_tracker.record_scan(len(ready), utc_now=now)

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
                    except Exception as exc:
                        logger.debug("[scan] volatility regime analysis failed for pair: {}", exc)
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
        except Exception as exc:
            logger.warning("[scan] pair multiplier map build failed: {}", exc)
            pass

        ranked = self.ranker.rank(ready, open_pairs)
        # Re-rank by opportunity score using pair learner and EV estimate
        ready_results = [s.result for s in ranked]
        reranked = self.ranker.rank_opportunities(ready_results, pair_mult_map)
        # Rebuild ranked list preserving RankedSetup structure
        rank_map = {s.result.pair: s for s in ranked}
        top_results = reranked[:3]
        top = [rank_map[r.pair] for r in top_results if r.pair in rank_map]

        self._last_slot_blocked_candidate = None
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
                self._last_slot_blocked_candidate = {
                    "pair": result.pair,
                    "direction": result.direction,
                    "score": result.score,
                }
                self._log_rejection(result.pair, result.direction, result.score, "Max trades reached")
                break

            cycle["entries_attempted"] += 1
            filled = self._execute_entry(result, session_status.current_session, now)
            if filled:
                cycle["entries_filled"] += 1
                open_pairs.append(result.pair)

    def _emit_setup_skipped(self, report) -> None:
        """Emit SETUP_SKIPPED for non-READY, non-MARKET_CLOSED results on change."""
        store = get_event_store()
        if store is None:
            return
        current: dict[str, tuple[str, int]] = {}
        for r in report.results:
            if r.status == "READY" or r.status == "MARKET_CLOSED":
                continue
            current[r.pair] = (r.status, r.score)
            prev = self._last_skipped_state.get(r.pair)
            if prev == (r.status, r.score):
                continue
            try:
                store.emit(
                    event_type=SETUP_SKIPPED,
                    severity="DEBUG",
                    symbol=r.pair,
                    correlation_id=getattr(self, "_current_cycle_id", None),
                    source_module="platforms.main_loop",
                    payload={
                        "status": r.status,
                        "score": r.score,
                        "direction": r.direction,
                        "trend_h4": r.trend_h4,
                        "trend_h1": r.trend_h1,
                        "bias_strength": r.bias_strength,
                        "regime": r.regime,
                        "session_active": r.session_active,
                        "has_fvg": r.has_fvg,
                        "has_order_block": r.has_order_block,
                        "confluences": r.confluences,
                        "instrument_category": r.instrument_category,
                        "ev_estimate": r.ev_estimate,
                    },
                )
            except Exception as exc:
                logger.debug("SETUP_SKIPPED emit failed: {}", exc)
        self._last_skipped_state = current

    def _save_position_checked(self, managed) -> None:
        """Persist position and surface any store degradation loudly."""
        self.position_store.save_position(managed)
        if not self.position_store.is_healthy():
            reason = self.position_store.degraded_reason()
            logger.error(
                "🔴 PERSISTENCE DEGRADED — position {} ({}) saved to memory but NOT persisted to disk: {}",
                managed.order_id, managed.symbol, reason,
            )
            try:
                store = get_event_store()
                if store:
                    store.emit(
                        event_type=PERSISTENCE_DEGRADED,
                        severity="ERROR",
                        symbol=managed.symbol,
                        correlation_id=getattr(self, "_current_cycle_id", None),
                        parent_id=getattr(self, "_current_setup_id", None),
                        source_module="platforms.main_loop",
                        payload={
                            "order_id": managed.order_id,
                            "symbol": managed.symbol,
                            "reason": reason,
                            "action": "position_retained_in_memory",
                        },
                    )
            except Exception as exc:
                logger.debug("PERSISTENCE_DEGRADED emit failed: {}", exc)

    def _execute_entry(self, result, session: str, now: datetime) -> bool:
        setup_id = new_setup_id()
        with logger.contextualize(setup_id=setup_id):
            return self._execute_entry_inner(result, session, now, setup_id)

    def _execute_entry_inner(self, result, session: str, now: datetime, setup_id: str) -> bool:
        self._current_setup_id = setup_id
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

        balance = self.platforms.get_platform_balance(pair)
        if not balance:
            logger.warning("⚠ Balance unavailable for {} — skipping entry (fail-closed)", pair)
            try:
                store = get_event_store()
                if store:
                    store.emit(
                        event_type=BALANCE_UNAVAILABLE,
                        severity="WARNING",
                        symbol=pair,
                        correlation_id=getattr(self, "_current_cycle_id", None),
                        parent_id=getattr(self, "_current_setup_id", None),
                        source_module="platforms.main_loop",
                        payload={"reason": "balance_fetch_returned_falsy", "action": "entry_skipped"},
                    )
            except Exception as exc:
                logger.debug("BALANCE_UNAVAILABLE emit failed: {}", exc)
            self._log_rejection(pair, direction, result.score, "Balance unavailable — fail-closed")
            return False
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
            h4_df=data.get("H4"),
            m15_df=data.get("M15"),
        )

        if isinstance(signal, EntryRejection):
            ctx = {}
            if signal.entry_price is not None:
                ctx["entry_price"] = signal.entry_price
            if signal.stop_loss is not None:
                ctx["stop_loss"] = signal.stop_loss
            self._log_rejection(pair, direction, result.score, signal.reason,
                                entry_context=ctx or None)
            if signal.entry_price is not None and signal.stop_loss is not None and h1_df is not None:
                try:
                    _tp1, _tp2 = self.entry_engine.calculate_targets(
                        pair, direction, signal.entry_price, signal.stop_loss,
                        h1_df, get_pip_size(pair),
                    )
                    self._persist_shadow_contract(
                        signal, rejecting_gate=f"entry_engine:{signal.reason}",
                        entry_price=signal.entry_price, stop_loss=signal.stop_loss,
                        tp1=_tp1, tp2=_tp2,
                    )
                except Exception:
                    logger.debug("[ShadowContract] TP computation failed for entry rejection on {}", pair)
            return False

        spread = 0.0
        try:
            spread = self.platforms.get_spread(pair)
        except Exception as exc:
            logger.warning("[entry] spread fetch failed, proceeding with zero spread: {}", exc)
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
            self._persist_shadow_contract(signal, rejecting_gate=f"validator:{reasons}")
            return False

        pip_size = get_pip_size(pair)
        # Use the broker-specific spread baseline from the context instead of a
        # global registry lookup — prevents Exness trades being rejected on
        # ICMarkets thresholds (and vice-versa).
        typical = ctx.typical_spread(pair, fallback=2.0)
        if spread > typical * self.config.risk.max_spread_multiplier:
            self._log_rejection(pair, direction, result.score, f"Spread too wide: {spread} (typical={typical})")
            self._persist_shadow_contract(signal, rejecting_gate=f"spread:{spread:.1f}")
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
            score=result.score,
            regime=getattr(result, "regime", ""),
            session=session,
        )
        if not assessment.approved:
            reasons = "; ".join(assessment.rejections)
            self._log_rejection(pair, direction, signal.score, f"RiskEngine: {reasons}")
            self._persist_shadow_contract(signal, rejecting_gate=f"risk_engine:{reasons}")
            return False

        # ── EV gate — skip when historical EV is negative at medium+ confidence ──
        # ev_estimate=0.0 means insufficient data (new pair) — always allow.
        # Only block when the system has enough history to be confident it's a loser.
        ev_val = getattr(result, "ev_estimate", 0.0)
        if ev_val < 0.0:
            # Determine confidence from EVEstimator sample size indirectly via score
            # We gate on negative EV only when pair_mult is also below 1.0 (i.e. learner
            # has marked this pair as REDUCE_SIZE or worse) — belt + braces gate.
            pair_mult = 1.0
            try:
                pair_mult = self.ml.pair_learner.get_pair_multiplier(pair)
            except Exception as exc:
                logger.warning("[entry] pair multiplier fetch for EV gate failed, defaulting to 1.0: {}", exc)
                pass
            if pair_mult < 1.0:
                self._log_rejection(
                    pair, direction, result.score, f"EV gate: negative EV ({ev_val:.4f}) + pair_mult={pair_mult:.2f}"
                )
                self._persist_shadow_contract(signal, rejecting_gate=f"ev_gate:ev={ev_val:.4f}")
                return False

        try:
            adjustments = self.ml.get_trade_adjustments(
                pair=pair,
                regime=getattr(result, "regime", ""),
                session=session,
            )
            if not adjustments.should_trade:
                self._log_rejection(pair, direction, result.score, f"ML: {adjustments.reason}")
                self._persist_shadow_contract(signal, rejecting_gate=f"ml:{adjustments.reason}")
                return False
            density_mult = self.density_tracker.get_size_multiplier()
            vol_mult = self.vol_monitor.get_size_multiplier()
            adjusted_lots = round(
                signal.position_size_lots * adjustments.position_size_multiplier * density_mult * vol_mult,
                2,
            )
            risk_ceiling = signal.position_size_lots
            if assessment.position_size_lots > 0:
                risk_ceiling = min(risk_ceiling, assessment.position_size_lots)
            if adjusted_lots > risk_ceiling:
                logger.info(
                    "[RiskAuthority] {} — adaptive sizing capped {:.2f} → {:.2f} lots (risk ceiling)",
                    pair, adjusted_lots, risk_ceiling,
                )
                adjusted_lots = risk_ceiling
            if adjusted_lots < 0.01 and assessment.position_size_lots > 0 and assessment.position_size_lots < signal.position_size_lots:
                logger.warning(
                    "[RiskAuthority] {} — daily-budget ceiling {:.4f} lots rounds below broker min 0.01 — REJECTING",
                    pair, assessment.position_size_lots,
                )
                self._persist_shadow_contract(signal, rejecting_gate="daily_budget_below_min_lot")
                return False
            adjusted_lots = max(0.01, adjusted_lots)
        except Exception as exc:
            logger.debug("ML adjustments error: {}", exc)
            adjusted_lots = signal.position_size_lots
            if assessment.position_size_lots > 0:
                adjusted_lots = min(adjusted_lots, assessment.position_size_lots)
                if adjusted_lots < 0.01:
                    logger.warning(
                        "[RiskAuthority] {} — daily-budget ceiling {:.4f} lots rounds below broker min 0.01 — REJECTING",
                        pair, assessment.position_size_lots,
                    )
                    self._persist_shadow_contract(signal, rejecting_gate="daily_budget_below_min_lot")
                    return False
            adjusted_lots = max(0.01, adjusted_lots)

        # Use the context to decide sizing path — no more string comparison
        stake_usd: float | None = None
        if ctx.uses_stake:
            stake_usd = assessment.stake_usd or assessment.max_loss_dollars

        sided_ok, sided_reason = _validate_stop_target_sidedness(
            direction, signal.entry_price, signal.stop_loss, signal.tp1,
            getattr(signal, "tp2", None),
        )
        if not sided_ok:
            logger.error(
                "🚫 MIS-SIDED SL/TP — {} {} entry={} sl={} tp1={} tp2={} — {}",
                direction, pair, signal.entry_price, signal.stop_loss,
                signal.tp1, getattr(signal, "tp2", None), sided_reason,
            )
            self._log_rejection(pair, direction, signal.score, f"Mis-sided SL/TP: {sided_reason}",
                                entry_context={"entry_price": signal.entry_price,
                                               "stop_loss": signal.stop_loss,
                                               "tp1": signal.tp1,
                                               "tp2": getattr(signal, "tp2", None)})
            self._persist_shadow_contract(signal, rejecting_gate=f"sidedness:{sided_reason}")
            return False

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
            except Exception as exc:
                logger.debug("[entry] pending order distance check failed, using market order: {}", exc)
                pass

        if use_pending:
            order = self.platforms.place_pending_entry(
                pair,
                order_kind,
                signal.entry_price,
                adjusted_lots,
                signal.stop_loss,
                signal.tp1,
                comment=build_order_comment("APND", idem_key, signal.score, session),
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

        try:
            store = get_event_store()
            store.emit(
                event_type=ORDER_SENT,
                severity="INFO",
                symbol=pair,
                correlation_id=getattr(self, "_current_cycle_id", None),
                parent_id=getattr(self, "_current_setup_id", None),
                source_module="platforms.main_loop",
                payload={
                    "direction": direction,
                    "lots": adjusted_lots,
                    "entry_price": signal.entry_price,
                    "stop_loss": signal.stop_loss,
                    "tp1": signal.tp1,
                    "score": signal.score,
                    "idempotency_key": idem_key,
                },
            )
        except Exception as exc:
            logger.debug("ORDER_SENT emit failed: {}", exc)

        order = self.platforms.execute_entry(
            pair,
            direction,
            adjusted_lots,
            signal.stop_loss,
            signal.tp1,
            comment=build_order_comment("APEX", idem_key, signal.score, session),
            stake_usd=stake_usd,
            idempotency_key=idem_key,
        )

        if not order.success:
            self._execution_breaker.record_failure()
            if self.position_store:
                self.position_store.cancel_in_flight(idem_key)
            return False

        try:
            store = get_event_store()
            store.emit(
                event_type=ORDER_FILLED,
                severity="INFO",
                symbol=pair,
                correlation_id=getattr(self, "_current_cycle_id", None),
                parent_id=getattr(self, "_current_setup_id", None),
                source_module="platforms.main_loop",
                payload={
                    "direction": direction,
                    "order_id": order.order_id,
                    "fill_price": order.fill_price,
                    "requested_price": signal.entry_price,
                    "slippage_pips": order.slippage_pips,
                    "lots": order.lots,
                    "stop_loss": signal.stop_loss,
                    "tp1": signal.tp1,
                    "tp2": signal.tp2,
                    "score": signal.score,
                },
            )
        except Exception as exc:
            logger.debug("ORDER_FILLED emit failed: {}", exc)

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
            confluences=list(signal.confluences),
        )

        info_risk = INSTRUMENT_REGISTRY.get(pair.upper())
        pip_sz = info_risk.pip_size if info_risk else 0.0001
        pip_val = info_risk.pip_value_per_lot if info_risk else 10.0
        risk_d, is_fb = compute_position_risk_dollars(
            direction=direction,
            entry_price=order.fill_price,
            sl=signal.stop_loss,
            lots=order.lots,
            pip_size=pip_sz,
            pip_value_per_lot=pip_val,
            at_breakeven=False,
            stake_usd=managed.stake_usd,
            multiplier=managed.multiplier,
            is_deriv_stake=ctx.uses_stake,
        )
        managed.initial_risk_dollars = risk_d if not is_fb else None

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
        self._save_position_checked(managed)
        self._daily_trades += 1

        logger.info(
            "🎯 TRADE OPENED — {} {} {:.2f}lots @ {:.5f} | SL {:.5f} | TP1 {:.5f} | TP2 {:.5f} | Score {}",
            direction,
            pair,
            order.lots,
            order.fill_price,
            signal.stop_loss,
            signal.tp1,
            signal.tp2,
            signal.score,
        )
        try:
            store = get_event_store()
            store.emit(
                event_type=TRADE_OPEN,
                severity="INFO",
                symbol=pair,
                correlation_id=getattr(self, "_current_cycle_id", None),
                parent_id=getattr(self, "_current_setup_id", None),
                source_module="platforms.main_loop",
                payload={
                    "direction": direction,
                    "order_id": order.order_id,
                    "fill_price": order.fill_price,
                    "stop_loss": signal.stop_loss,
                    "tp1": signal.tp1,
                    "tp2": signal.tp2,
                    "lots": order.lots,
                    "score": signal.score,
                    "session": session,
                },
            )
        except Exception as exc:
            logger.debug("TRADE_OPEN emit failed: {}", exc)
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
        except Exception as exc:
            logger.warning("[pending] broker position fetch failed, skipping pending order check: {}", exc)
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
                    confluences=list(sig.confluences),
                )
                pend_info = INSTRUMENT_REGISTRY.get(info["symbol"].upper())
                pend_pip_sz = pend_info.pip_size if pend_info else 0.0001
                pend_pip_val = pend_info.pip_value_per_lot if pend_info else 10.0
                pend_ctx = build_context_for_symbol(info["symbol"])
                pend_risk, pend_fb = compute_position_risk_dollars(
                    direction=info["direction"],
                    entry_price=bp.open_price,
                    sl=sig.stop_loss,
                    lots=bp.lots,
                    pip_size=pend_pip_sz,
                    pip_value_per_lot=pend_pip_val,
                    at_breakeven=False,
                    stake_usd=managed.stake_usd,
                    multiplier=managed.multiplier,
                    is_deriv_stake=pend_ctx.uses_stake,
                )
                managed.initial_risk_dollars = pend_risk if not pend_fb else None
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
                self._save_position_checked(managed)
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
                except Exception as exc:
                    logger.warning("[pending] MT5 order cancel failed for expired pending order: {}", exc)
                    pass
                expired.append(oid)
        for oid in expired:
            del self._pending_orders[oid]

    # ── Position management ──────────────────────────────────────────────

    def _update_positions(self) -> int:
        closed_count = 0
        to_remove: list[str] = []

        # ── STEP 1: Fetch broker state ONCE with per-platform confirmation ─
        # The broker is the source of truth for what positions exist and
        # their real P&L.  We require *positive confirmation* from a
        # position's own platform before inferring it was closed.
        # "Cannot see the position" ≠ "position is closed."
        snap = self.platforms.get_open_positions_snapshot()
        broker_map: dict[str, PositionInfo] = {
            p.order_id: p for p in snap.positions
        }

        # ── STEP 1.5: Margin-level guardian ──────────────────────────────
        if self.config.risk.margin_guardian_enabled and self.managed_positions:
            self._margin_guardian_check()

        # ── STEP 2: Detect broker-side closes (confirmed platforms only) ──
        # A managed position is removed ONLY when its own platform
        # positively confirmed its open-position list AND the position
        # is absent from that list.  If the platform failed or is
        # unconfirmed, the position is retained and marked for
        # revalidation — never auto-closed.
        max_unconfirmed = self.config.risk.reconcile_max_unconfirmed_cycles
        for oid, pos in list(self.managed_positions.items()):
            if oid in broker_map:
                if pos.revalidation_pending:
                    pos.revalidation_pending = False
                    pos.unconfirmed_cycles = 0
                    logger.info(
                        "[Reconcile] {} {} revalidation cleared — confirmed present on {}",
                        pos.direction, pos.symbol, pos.platform,
                    )
                continue

            if pos.platform in snap.confirmed_platforms:
                deal_info = self.platforms.get_deal_close_info(oid, pos.platform)
                if deal_info is not None:
                    broker_pnl = deal_info.pnl
                    exit_reason = deal_info.exit_reason
                    exit_reason_source = "deriv_poc" if pos.platform.startswith("deriv") else "mt5_deal"
                    raw_broker_reason = deal_info.raw_reason_code
                    raw_broker_comment = deal_info.raw_comment
                else:
                    realized = self.platforms.get_realized_pnl(oid, pos.platform)
                    broker_pnl = realized if realized is not None else pos.broker_pnl
                    exit_reason = "BROKER_CLOSED_UNKNOWN"
                    exit_reason_source = "unknown"
                    raw_broker_reason = None
                    raw_broker_comment = None
                if deal_info is not None and deal_info.close_price is not None:
                    close_price = deal_info.close_price
                else:
                    close_price = pos.entry_price
                    try:
                        tick = self.platforms.get_price(pos.symbol)
                        is_buy = pos.direction == "BUY"
                        close_price = tick.bid if is_buy else tick.ask
                    except Exception as exc:
                        logger.debug("[Reconcile] close-price fetch failed for {} {}, using fallback price: {}", pos.direction, pos.symbol, exc)
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
                    exit_reason,
                    close_result=fake_close if broker_pnl != 0.0 else None,
                    exit_reason_source=exit_reason_source,
                    raw_broker_reason=raw_broker_reason,
                    raw_broker_comment=raw_broker_comment,
                )
                to_remove.append(oid)
                closed_count += 1
                self._add_warning(
                    "info",
                    f"{pos.direction} {pos.symbol} closed by broker ({exit_reason})",
                    symbol=pos.symbol,
                )
                logger.info(
                    "📋 BROKER CLOSED — {} {} | reason={} | pnl={:.2f} — removed from management",
                    pos.direction,
                    pos.symbol,
                    exit_reason,
                    broker_pnl,
                )
            else:
                pos.revalidation_pending = True
                pos.unconfirmed_cycles += 1
                if pos.unconfirmed_cycles >= max_unconfirmed:
                    logger.critical(
                        "[Reconcile] CRITICAL — {} {} on {} unverifiable for {} cycles "
                        "— retaining under management, requires human review",
                        pos.direction, pos.symbol, pos.platform,
                        pos.unconfirmed_cycles,
                    )
                else:
                    logger.warning(
                        "[Reconcile] Cannot confirm {} {} on {} — retaining under "
                        "management, marked for revalidation (cycle {}/{})",
                        pos.direction, pos.symbol, pos.platform,
                        pos.unconfirmed_cycles, max_unconfirmed,
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
            except Exception as exc:
                logger.warning("[management] tick fetch failed for position, skipping cycle: {}", exc)
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
                except Exception as exc:
                    logger.debug("[management] M5 data fetch for stall analysis failed: {}", exc)
                    pass

            prev_sl = tm_trade.stop_loss
            prev_tp2 = tm_trade.tp2
            was_partial = tm_trade.partial_closed
            prev_remaining = tm_trade.remaining_size_lots
            prev_status = tm_trade.status
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
                        logger.error(
                            "🔴 TP1 PARTIAL CLOSE FAILED — {} {} oid={} | attempted {:.2f} lots — rolling back shadow state for retry",
                            pos.direction, pos.symbol, oid, partial_lots,
                        )
                        tm_trade.partial_closed = was_partial
                        tm_trade.remaining_size_lots = prev_remaining
                        tm_trade.status = prev_status
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
                    else:
                        logger.error(
                            "🔴 TP3 PARTIAL CLOSE FAILED — {} {} oid={} | attempted {:.2f} lots — rolling back shadow state for retry",
                            pos.direction, pos.symbol, oid, tp3_lots,
                        )
                        tm_trade.tp3_hit = was_tp3_hit
                        tm_trade.remaining_size_lots = prev_remaining

            sl_changed = tm_trade.stop_loss != prev_sl
            tp_changed = tm_trade.tp2 != prev_tp2
            if sl_changed or tp_changed:
                pos_ctx = build_context_for_symbol(pos.symbol)
                new_sl = tm_trade.stop_loss if sl_changed else None
                new_tp = tm_trade.tp2 if tp_changed else None
                if pos_ctx.supports_modify:
                    modified = self.platforms.modify_trade(oid, pos.platform, new_sl=new_sl, new_tp=new_tp)
                    if modified:
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
                        logger.error(
                            "🔴 SL/TP MODIFY FAILED — {} {} oid={} | attempted SL={} TP={} — broker rejected, keeping current SL={:.5f}",
                            pos.direction, pos.symbol, oid, new_sl, new_tp, pos.sl,
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

                # ── 5. Opportunity-cost exit detection (F4 shadow) ─────────
                if cfg.opportunity_cost_exit_mode != "off":
                    self._check_opportunity_cost_exit(oid, pos, now)
                    if oid not in self.managed_positions:
                        continue

                # ── 6. Scale-in on strength (wired to live scan) ───────────
                if cfg.scale_in_enabled:
                    self._check_scale_in_on_scan(oid, pos, scan_result)

            except Exception as exc:
                logger.debug("In-trade analysis error for {}: {}", pair, exc)


    def _submit_scale_in(
        self,
        pos: ManagedPosition,
        add_lots: float,
        new_sl: float,
        new_tp: float,
        score_tag: str,
    ) -> bool:
        idem_key = generate_idempotency_key(pos.symbol, pos.direction, add_lots)
        try:
            if self.position_store:
                self.position_store.record_in_flight(idem_key, pos.symbol, pos.direction, add_lots)
        except Exception as exc:
            logger.warning("[scale-in] in-flight record failed (proceeding): {}", exc)

        order = self.platforms.execute_entry(
            pos.symbol,
            pos.direction,
            add_lots,
            new_sl,
            new_tp,
            comment=build_order_comment("APEX", idem_key, pos.score, score_tag),
            idempotency_key=idem_key,
        )
        if order.success:
            try:
                if self.position_store:
                    self.position_store.resolve_in_flight(idem_key, order.order_id)
            except Exception as exc:
                logger.warning("[scale-in] resolve_in_flight failed: {}", exc)
            pos.scale_in_count += 1
            return True

        try:
            if self.position_store:
                self.position_store.cancel_in_flight(idem_key)
        except Exception as exc:
            logger.warning("[scale-in] cancel_in_flight failed: {}", exc)
        logger.error(
            "🔴 SCALE-IN FAILED — {} {} | attempted {:.2f} lots — broker rejected",
            pos.direction, pos.symbol, add_lots,
        )
        return False

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
        risk_distance = abs(tm_trade.entry_price - tm_trade.original_stop_loss)
        if risk_distance < 1e-8:
            return

        try:
            tick = self.platforms.get_price(pos.symbol)
            current = tick.bid if is_long_trade else tick.ask
        except Exception as exc:
            logger.warning("[management] tick fetch for profit-R calc failed, aborting: {}", exc)
            return

        profit_r = (
            (current - tm_trade.entry_price) / risk_distance if is_long_trade
            else (tm_trade.entry_price - current) / risk_distance
        )
        if profit_r < cfg.scale_in_min_profit_r:
            return

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

        if self._submit_scale_in(pos, add_lots, tm_trade.stop_loss, tm_trade.tp2, f"SCAN|{scan_result.score}"):
            logger.info(
                "📈 SCALE-IN (scan-wired) — {} {} | +{} lots (add #{}) | fresh score={}",
                pos.direction, pos.symbol, add_lots, pos.scale_in_count, scan_result.score,
            )


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
                can_open, _reason = self.correlation.can_open_trade(pos.symbol, pos.direction, open_trades_list)
                if not can_open:
                    continue
                if len(self.managed_positions) >= self.config.risk.max_open_trades:
                    continue
                if self.config.risk.margin_guardian_enabled:
                    ml = self._get_margin_level()
                    if ml is not None and ml < self.config.risk.margin_block_entry_pct:
                        continue
                add_lots = round(pos.lots * self.config.risk.scale_in_add_ratio, 2)
                add_lots = max(0.01, add_lots)
                if self._submit_scale_in(pos, add_lots, tm_trade.stop_loss, tm_trade.tp2, "SCALEIN"):
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
        exit_reason_source: str = "trade_manager",
        raw_broker_reason: Optional[int] = None,
        raw_broker_comment: Optional[str] = None,
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

        balance = self.platforms.get_platform_balance(pos.symbol)
        if not balance:
            logger.warning("⚠ Balance unavailable for {} at close — pnl_pct defaulted to 0.0", pos.symbol)
            pnl_pct = 0.0
        else:
            pnl_pct = pnl_dollars / balance

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

        swap_modeled = None
        swap_status = "unavailable"
        if self.config.risk.model_swap_costs:
            pos_ctx = build_context_for_symbol(pos.symbol)
            if pos_ctx.uses_stake:
                swap_modeled = None
                swap_status = "unavailable"
            else:
                rates = load_swap_rates(self.config.risk.swap_rates_path)
                swap_modeled, swap_status = estimate_swap(
                    pos.symbol,
                    pos.direction,
                    pos.lots,
                    pos.open_time,
                    datetime.now(timezone.utc),
                    rates=rates,
                    rollover_hour_utc=self.config.risk.swap_rollover_hour_utc,
                    triple_weekday=self.config.risk.swap_triple_weekday,
                )

        trade_record = TradeRecord(
            pair=pos.symbol,
            direction=pos.direction,
            entry=pos.entry_price,
            exit=close_price,
            pnl=pnl_pips,
            score=pos.score,
            confluences=list(pos.confluences),
            regime=pos.regime,
            session=pos.session,
            spread=0.0,
            slippage=0.0,
            entry_type=pos.entry_type,
            time_to_tp1=None,
            time_to_exit=hold_seconds / 60.0,
            outcome=outcome,
            pnl_dollars=pnl_dollars,
            swap_modeled=swap_modeled,
            swap_status=swap_status,
            risk_dollars=getattr(pos, "initial_risk_dollars", None),
        )
        self._run_journal_async(self.journal.log_trade(trade_record))
        self.ml.register_new_trade()

        try:
            store = get_event_store()
            store.emit(
                event_type=TRADE_CLOSE,
                severity="INFO",
                symbol=pos.symbol,
                correlation_id=getattr(self, "_current_cycle_id", None),
                source_module="platforms.main_loop",
                payload={
                    "order_id": getattr(pos, "order_id", None),
                    "direction": pos.direction,
                    "entry_price": pos.entry_price,
                    "close_price": close_price,
                    "exit_reason": outcome,
                    "exit_reason_source": exit_reason_source,
                    "raw_broker_reason": raw_broker_reason,
                    "raw_broker_comment": raw_broker_comment,
                    "pnl_pips": round(pnl_pips, 2),
                    "pnl_dollars": pnl_dollars,
                    "hold_seconds": round(hold_seconds, 1),
                    "lots": pos.lots,
                    "platform": pos.platform,
                },
            )
        except Exception as exc:
            logger.debug("TRADE_CLOSE emit failed: {}", exc)

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

    def _persist_shadow_contract(
        self, signal, rejecting_gate: str,
        entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        tp1: Optional[float] = None,
        tp2: Optional[float] = None,
    ) -> None:
        """Persist a shadow contract for a rejected setup (best-effort)."""
        try:
            ep = entry_price or getattr(signal, "entry_price", None)
            sl = stop_loss or getattr(signal, "stop_loss", None)
            t1 = tp1 or getattr(signal, "tp1", None)
            t2 = tp2 or getattr(signal, "tp2", None)

            if ep is None or sl is None or t1 is None or t2 is None:
                return

            pair = getattr(signal, "pair", None)
            direction = getattr(signal, "direction", None)
            if not pair or not direction:
                return

            pip_size = get_pip_size(pair)
            risk_distance = abs(ep - sl)
            is_long = direction.upper() in ("LONG", "BUY")
            tp3 = None
            if self.trade_manager.tp3_ladder_enabled and risk_distance > 0:
                candidate = (
                    ep + self.trade_manager.tp3_r_multiple * risk_distance
                    if is_long
                    else ep - self.trade_manager.tp3_r_multiple * risk_distance
                )
                beyond = (candidate > t2) if is_long else (candidate < t2)
                if beyond:
                    tp3 = candidate

            contract = ShadowContract(
                contract_id=new_contract_id(),
                symbol=pair,
                direction=direction,
                entry_price=ep,
                stop_loss=sl,
                tp1=t1,
                tp2=t2,
                tp3=tp3,
                pip_size=pip_size,
                position_size=getattr(signal, "position_size_lots", 0.01),
                entry_timeframe=getattr(signal, "entry_timeframe", "M5"),
                rejecting_gate=rejecting_gate,
                score=getattr(signal, "score", 0),
                ts_utc_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                correlation_id=getattr(self, "_current_cycle_id", None),
                setup_id=getattr(self, "_current_setup_id", None),
            )

            cid = self._shadow_store.insert_contract(contract)
            if cid:
                store = get_event_store()
                if store:
                    store.emit(
                        event_type=SHADOW_CONTRACT_CREATED,
                        severity="INFO",
                        symbol=pair,
                        correlation_id=getattr(self, "_current_cycle_id", None),
                        parent_id=getattr(self, "_current_setup_id", None),
                        source_module="platforms.main_loop",
                        payload={
                            "contract_id": cid,
                            "rejecting_gate": rejecting_gate,
                            "entry_price": ep,
                            "stop_loss": sl,
                            "tp1": t1,
                            "tp2": t2,
                            "direction": direction,
                        },
                    )
        except Exception:
            logger.debug("[ShadowContract] persist failed for {}", getattr(signal, "pair", "?"))

    def _log_rejection(self, pair: str, direction: str, score: int, reason: str,
                       entry_context: dict | None = None) -> None:
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
        try:
            store = get_event_store()
            payload = {
                "direction": direction,
                "score": score,
                "reason": reason,
            }
            if entry_context:
                payload.update(entry_context)
            store.emit(
                event_type=DECISION_REJECT,
                severity="INFO",
                symbol=pair,
                correlation_id=getattr(self, "_current_cycle_id", None),
                parent_id=getattr(self, "_current_setup_id", None),
                source_module="platforms.main_loop",
                payload=payload,
            )
        except Exception as exc:
            logger.debug("DECISION_REJECT emit failed: {}", exc)

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
                except Exception as exc:
                    logger.debug("[daily_reset] execution stats logging failed: {}", exc)
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
            except Exception as exc:
                logger.debug("[retrain] trade history assignment to scanner failed: {}", exc)
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
    "H1 OB": "ob_h1",
    "M5 OB": "ob_m5",
    "Order block": "ob_h1",
    "FVG": "fvg",
    "Multi-TF": "mtf_confluence",
    "Session": "session",
    "News": "news",
    "Currency strength": "currency_strength",
    "Liquidity sweep": "liquidity_sweep",
    "Liquidity": "liquidity_sweep",
    "Volume confirmed": "volume",
    "Inducement": "inducement",
    "Wyckoff": "wyckoff",
}


def _parse_confluence_tags(confluences: list) -> list[str]:
    """Map display confluence strings to canonical factor keys."""
    seen: set[str] = set()
    tags: list[str] = []
    for c in confluences:
        text = str(c)
        for prefix, tag in _CONFLUENCE_TO_TAG.items():
            if text.startswith(prefix) and tag not in seen:
                tags.append(tag)
                seen.add(tag)
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
