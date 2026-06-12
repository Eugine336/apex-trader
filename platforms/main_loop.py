"""
APEX TRADER — Main Trading Loop
The heartbeat of the system.
Scan → Analyse → Trigger → Manage → Repeat.
Always watching. Always ready. In and out like a sniper.
"""

import asyncio
import math
import signal
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional, Any

import pandas as pd
from loguru import logger

from persistence.position_store import PositionStore, STORE_UNAVAILABLE
from platforms.circuit_breaker import CircuitBreaker
from platforms.health_watchdog import HealthWatchdog
from platforms.maintenance import DailyMaintenance
from platforms.startup_check import StartupCheck

from brain import (
    CorrelationEngine,
    DrawdownGuard,
    ExecutionMonitor,
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
    PERSISTENCE_DEGRADED, CYCLE_FAILED, TRADING_LOOP_HALTED,
)
from persistence.shadow_store import ShadowStore, ShadowContract, new_contract_id

from decision.context import EntryContext, TradeContext
from decision.situation import SituationEngine, SituationAssessment
from decision.actions import Action, EntryAction, ManagementDecision
from decision.engine import DecisionEngine
from decision.governor import RiskGovernor
from decision.journal import DecisionJournal
from planning import (
    Calibrator,
    OutcomeLogger,
    PlannerConfig,
    TradePlanContext,
    TradePlanner,
)
from governor import PortfolioGovernor


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


_HARD_LEVEL_REASONS = {"SL", "TP", "STOP_OUT"}
_BENIGN_BROKER_REASONS = {
    "BROKER_CLOSED_UNKNOWN", "ALGO", "MANUAL",
    "ROLLOVER", "VARIATION_MARGIN", "SPLIT",
}
_DISCRETIONARY_KEYWORDS = ("stall", "structure", "spread", "news", "session", "opportunity")
_SIMULATED_SL_KEYWORDS = ("stop loss", "stop_loss")
_SIMULATED_TP_KEYWORDS = ("tp2", "tp1", "take profit", "take_profit")


def _exit_reasons_conflict(broker_reason: str, manager_reason: str) -> bool:
    if not manager_reason:
        return False
    br = broker_reason.upper().strip()
    mr = manager_reason.lower().strip()
    if br in _BENIGN_BROKER_REASONS:
        return False
    is_discretionary = any(kw in mr for kw in _DISCRETIONARY_KEYWORDS)
    if is_discretionary and br in _HARD_LEVEL_REASONS:
        return True
    is_sim_sl = any(kw in mr for kw in _SIMULATED_SL_KEYWORDS)
    is_sim_tp = any(kw in mr for kw in _SIMULATED_TP_KEYWORDS)
    if is_sim_sl and br in _HARD_LEVEL_REASONS and br != "SL":
        return True
    if is_sim_tp and br in _HARD_LEVEL_REASONS and br != "TP":
        return True
    return False


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
        risk_cfg = self.config.risk
        self.entry_engine = EntryEngine(
            config=self.config,
            volatility_stop_mode=risk_cfg.volatility_stop_mode,
            atr_stop_period=risk_cfg.atr_stop_period,
            atr_stop_mult=risk_cfg.atr_stop_mult,
            atr_stop_ratio_min=risk_cfg.atr_stop_ratio_min,
            atr_stop_ratio_max=risk_cfg.atr_stop_ratio_max,
            atr_stop_max_risk_mult=risk_cfg.atr_stop_max_risk_mult,
        )
        self.drawdown = DrawdownGuard()
        self.correlation = CorrelationEngine(
            max_correlated_trades=risk_cfg.max_correlated_trades,
            max_cluster_same_direction=risk_cfg.max_cluster_same_direction,
            allow_intentional_hedge=risk_cfg.allow_intentional_hedge,
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
            breakeven_min_profit_r=self.config.risk.breakeven_min_profit_r,
            trailing_swing_lookback=self.config.risk.trailing_swing_lookback,
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
        self._consecutive_cycle_failures = 0
        self._last_scan_time: Optional[datetime] = None
        self._daily_trades = 0
        self._last_reset_day: Optional[str] = None
        self._last_backup_time: float = 0.0
        # Live reconciliation heartbeat
        self._last_reconcile_time: Optional[datetime] = None
        self._reconcile_interval_seconds: int = 30
        self._recovery_completed: bool = False
        # In-trade analysis state — keyed by order_id
        # Tracks score history for conviction monitoring and HTF reassessment
        self._position_scores: dict[str, list[int]] = {}       # recent N scores per position
        self._position_last_h1_close: dict[str, datetime] = {} # last H1 candle time seen
        self._news_exit_protected: set[str] = set()            # oids already tightened for news
        self._last_market_data: dict = {}                       # most recent scan market data cache
        self._d1_cache: dict[str, pd.DataFrame] = {}             # D1 data cache (changes once/day)
        self._d1_cache_time: datetime | None = None              # when D1 cache was last refreshed
        self._last_slot_blocked_candidate: dict | None = None    # best foregone candidate when slots full (F4)
        self._last_skipped_state: dict[str, tuple[str, int]] = {}  # symbol → (status, score) for emit-on-change
        self._last_known_balance: float = 0.0
        # P3: last Decision Engine verdict per oid — lets the tick-level
        # TradeManager exit defer to a strategic HOLD/SCALE_IN.
        self._last_decision_action: dict[str, Action] = {}
        # P5: per-pair cooldown after a breakeven stop-out (symbol → datetime until).
        self._be_stop_cooldown: dict[str, datetime] = {}

        # ── D1 cache — daily candles change once/day, refresh hourly ──
        self._d1_cache: dict[str, "pd.DataFrame"] = {}
        self._d1_cache_ts: float = 0.0
        self._d1_cache_ttl: float = 3600.0

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

        # ── Decision Intelligence System ────────────────────────────────
        dcfg = self.config.decision
        self._decision_enabled = dcfg.enabled
        self._situation_engine = SituationEngine()
        self._decision_engine = DecisionEngine()
        self._risk_governor = RiskGovernor() if dcfg.governor_enabled else None
        self._decision_journal = DecisionJournal(dcfg.journal_dir) if dcfg.journal_enabled else None

        # ── Trade Planner — coordinator between advisors and execution ───
        pcfg = getattr(self.config, "planner", None) or PlannerConfig()
        # Prefer a calibrated config persisted from a previous run.
        persisted = PlannerConfig.load()
        # Only adopt the persisted config when the feature is enabled in code config.
        planner_cfg = persisted if pcfg.enabled else pcfg
        planner_cfg.enabled = pcfg.enabled
        self._planner_enabled = planner_cfg.enabled
        self._planner = TradePlanner(planner_cfg)
        self._outcome_logger = OutcomeLogger(planner_cfg.journal_path) if planner_cfg.enabled else None
        self._calibrator = Calibrator(planner_cfg) if planner_cfg.enabled else None

        # ── Portfolio Governor — portfolio-level risk limits ─────────────
        gcfg = getattr(self.config, "governor", None)
        self._governor = PortfolioGovernor(gcfg) if (gcfg is None or gcfg.enabled) else None
        if self._governor is not None:
            self._planner.set_governor(self._governor)

        # ── Data backup ──────────────────────────────────────────────────
        self._last_data_backup_ts: float = 0.0

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
                self._run_supervised_cycle()
                interval = self._get_sleep_interval()
                _time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Shutdown signal received")
        finally:
            self.stop()

    def _run_supervised_cycle(self) -> None:
        """Run exactly one cycle with per-cycle isolation.

        Absorbs non-fatal exceptions so a single bad cycle cannot kill
        the entire trading loop.  After *max_consecutive_cycle_failures*
        back-to-back failures the loop is halted to protect capital.
        """
        try:
            self._check_daily_reset()
            self.run_once()
            self._consecutive_cycle_failures = 0
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            self._consecutive_cycle_failures += 1
            logger.exception(
                "🔴 Cycle exception ({}/{} consecutive): {}",
                self._consecutive_cycle_failures,
                self.config.max_consecutive_cycle_failures,
                exc,
            )
            try:
                get_event_store().emit(
                    event_type=CYCLE_FAILED,
                    severity="ERROR",
                    symbol=None,
                    correlation_id=getattr(self, "_current_cycle_id", None),
                    source_module="platforms.main_loop",
                    payload={
                        "consecutive_failures": self._consecutive_cycle_failures,
                        "max_allowed": self.config.max_consecutive_cycle_failures,
                        "error": str(exc),
                    },
                )
            except Exception as emit_exc:
                logger.debug("CYCLE_FAILED emit failed: {}", emit_exc)

            if self._consecutive_cycle_failures >= self.config.max_consecutive_cycle_failures:
                logger.critical(
                    "🚨 TRADING LOOP HALTED — {} consecutive cycle failures exceeded threshold ({})",
                    self._consecutive_cycle_failures,
                    self.config.max_consecutive_cycle_failures,
                )
                try:
                    get_event_store().emit(
                        event_type=TRADING_LOOP_HALTED,
                        severity="CRITICAL",
                        symbol=None,
                        correlation_id=getattr(self, "_current_cycle_id", None),
                        source_module="platforms.main_loop",
                        payload={
                            "consecutive_failures": self._consecutive_cycle_failures,
                        },
                    )
                except Exception as emit_exc:
                    logger.debug("TRADING_LOOP_HALTED emit failed: {}", emit_exc)
                self.running = False

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

        blind, blind_reason = self.watchdog.is_scanner_blind()
        if blind:
            logger.error("🔴 HEALTH BLOCK: scanner blind ({}), new entries suspended this cycle", blind_reason)
            cycle["health_blocked"] = True

        session_status = self.session_engine.get_status(now)
        news_status = self.news_guard.check(self.config.enabled_pairs, now)

        can_trade, reason = self.drawdown.can_trade(now)
        if not can_trade:
            logger.debug("Trading paused: {}", reason)
            self._check_pending_orders()
            self._check_weekend_protection()
            self._update_positions()
        else:
            health_blocked = cycle.get("health_blocked", False)

            if not health_blocked:
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
                    open_trade_data = self._fetch_open_trade_market_data(now)
                    if open_trade_data:
                        self._analyse_open_trades(open_trade_data, now)
                    else:
                        logger.warning(
                            "[management] open-trade strategic analysis skipped — fresh full-timeframe data unavailable",
                        )
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

    def _get_d1_cached(self, symbols: list[str]) -> dict:
        """Return cached D1 DataFrames, refreshing at most once per hour."""
        import time as _time

        now_mono = _time.monotonic()
        if self._d1_cache and (now_mono - self._d1_cache_ts) < self._d1_cache_ttl:
            return self._d1_cache

        try:
            d1_data = self.platforms.fetch_all_market_data(
                symbols=symbols, timeframes=["D1"],
            )
            fresh: dict = {}
            for sym, frames in d1_data.items():
                df = frames.get("D1")
                if df is not None and len(df) >= 5:
                    fresh[sym] = df
            if fresh:
                self._d1_cache = fresh
                self._d1_cache_ts = now_mono
                logger.debug("D1 cache refreshed — {} symbols", len(fresh))
        except Exception as exc:
            logger.warning("D1 cache refresh failed (using stale): {}", exc)

        return self._d1_cache

    def _scan_and_enter(self, session_status, news_status, now: datetime, cycle: dict) -> None:
        d1_data = self._get_d1_cached(self.config.enabled_pairs)

        try:
            market_data = self.platforms.fetch_all_market_data(now_utc=now)
        except Exception as exc:
            logger.error("Market data fetch failed: {}", exc)
            return

        if not market_data:
            return

        # ── D1 cache: fetch once per hour, merge into market_data ─────
        d1_stale = (
            self._d1_cache_time is None
            or (now - self._d1_cache_time).total_seconds() > 3600
        )
        if d1_stale:
            try:
                d1_data = self.platforms.fetch_all_market_data(
                    timeframes=["D1"], count=100, now_utc=now,
                )
                if d1_data:
                    self._d1_cache = {
                        sym: frames["D1"]
                        for sym, frames in d1_data.items()
                        if "D1" in frames
                    }
                    self._d1_cache_time = now
                    logger.info("[D1 cache] refreshed {} symbols", len(self._d1_cache))
            except Exception as exc:
                logger.warning("[D1 cache] fetch failed, using stale cache: {}", exc)

        for sym, d1_df in self._d1_cache.items():
            if sym in market_data:
                market_data[sym]["D1"] = d1_df

        # Store for in-trade analysis this cycle
        self._last_market_data = market_data

        # Build currency_data for the strength meter — H1 data keyed by symbol
        currency_data = {pair: frames["H1"] for pair, frames in market_data.items() if "H1" in frames}

        report = self.scanner.scan_all(market_data, currency_data=currency_data, utc_now=now)
        ready = self.scanner.get_ready_setups(report)
        self._emit_setup_skipped(report)

        qf, qt = self.scanner.get_quality_failure_stats()
        if qf > 0:
            self.watchdog.record_quality_failures(qf, qt)
            if qf == qt and qt > 0:
                self._add_warning(
                    "error",
                    f"OQ/EQ quality computation failed on ALL {qf} directional setups — zero trades can reach READY",
                )
        self.scanner.reset_quality_failure_stats()

        # Track opportunity density — feeds into position sizing
        self.density_tracker.record_scan([r.pair for r in ready], utc_now=now)

        # Update system-wide volatility state — reduce all sizes during market vol spikes
        try:
            from brain.regime_detector import RegimeDetector

            _regime_det = RegimeDetector()
            _vol_analyses = []
            for pair, frames in market_data.items():
                vol_df = self._select_vol_timeframe(frames)
                if vol_df is not None:
                    try:
                        _vol_analyses.append(_regime_det.analyze(vol_df))
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
        prsm: Optional[PortfolioRiskStateMachine] = getattr(self, '_portfolio_risk_sm', None)
        if prsm is None:
            pass
        elif prsm.state in (PortfolioRiskState.DEFENSIVE, PortfolioRiskState.REDUCING):
            logger.info(
                "[PortfolioRisk] {} — new entries frozen (heat={:.2f}%)",
                prsm.state.name,
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

            # P5: per-pair cooldown after a breakeven stop-out. In chop a pair
            # can cycle enter → BE → stopped at BE → re-enter, bleeding spread
            # each loop. Skip re-entry while the cooldown is active.
            cd_until = self._be_stop_cooldown.get(result.pair)
            if cd_until is not None:
                if now < cd_until:
                    mins_left = (cd_until - now).total_seconds() / 60.0
                    self._log_rejection(
                        result.pair, result.direction, result.score,
                        f"BE-stop cooldown active ({mins_left:.0f}min left)",
                    )
                    continue
                # cooldown expired — clear it
                self._be_stop_cooldown.pop(result.pair, None)

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

    def _fetch_open_trade_market_data(self, now: datetime) -> dict[str, dict[str, pd.DataFrame]]:
        if not self.managed_positions:
            return {}

        symbols = sorted({pos.symbol for pos in self.managed_positions.values()})
        if not symbols:
            return {}

        try:
            base_data = self.platforms.fetch_all_market_data(
                symbols=symbols,
                timeframes=["D1", "H4", "H1", "M15", "M5"],
                count=200,
                now_utc=now,
            )
        except Exception as exc:
            logger.error(
                "[management] fresh open-trade base data fetch failed for {} symbols: {}",
                len(symbols), exc,
            )
            return {}

        try:
            m1_data = self.platforms.fetch_all_market_data(
                symbols=symbols,
                timeframes=["M1"],
                count=400,
                now_utc=now,
            )
        except Exception as exc:
            logger.error(
                "[management] fresh open-trade M1 fetch failed for {} symbols: {}",
                len(symbols), exc,
            )
            return {}

        merged: dict[str, dict[str, pd.DataFrame]] = {}
        for symbol in symbols:
            frames: dict[str, pd.DataFrame] = {}
            if symbol in base_data:
                frames.update(base_data[symbol])
            else:
                logger.warning(
                    "[management] fresh open-trade data missing base frames for {}",
                    symbol,
                )

            if symbol in m1_data:
                frames.update(m1_data[symbol])
            else:
                logger.warning(
                    "[management] fresh open-trade data missing M1 for {}",
                    symbol,
                )

            if frames:
                merged[symbol] = frames

        return merged

    def _compute_in_trade_context_pressure(
        self,
        pos: ManagedPosition,
        d1_df: Optional[pd.DataFrame],
        m1_df: Optional[pd.DataFrame],
    ) -> tuple[int, int, list[str]]:
        pressure = 0
        opposing_boost = 0
        details: list[str] = []
        is_long = pos.direction == "BUY"

        if d1_df is not None:
            try:
                d1 = self.scanner.structure.analyze(d1_df)
                d1_trend = getattr(d1.trend, "value", str(d1.trend))
                d1_conf = float(getattr(d1, "confidence", 0.0) or 0.0)
                if d1_trend in ("BULLISH", "BEARISH"):
                    aligned = (is_long and d1_trend == "BULLISH") or (not is_long and d1_trend == "BEARISH")
                    if aligned:
                        support = 2 if d1_conf >= 0.6 else 1
                        pressure -= support
                        details.append(f"D1 support {d1_trend} ({d1_conf:.2f})")
                    else:
                        d1_pressure = 8 if d1_conf >= 0.65 else 5
                        pressure += d1_pressure
                        opposing_boost += max(1, d1_pressure // 2)
                        details.append(f"D1 pressure {d1_trend} ({d1_conf:.2f})")
            except Exception as exc:
                logger.warning("[management] D1 context analysis failed for {}: {}", pos.symbol, exc)

        if m1_df is not None:
            try:
                m1 = self.scanner.structure.analyze(m1_df)
                m1_trend = getattr(m1.trend, "value", str(m1.trend))
                m1_conf = float(getattr(m1, "confidence", 0.0) or 0.0)
                if m1_trend in ("BULLISH", "BEARISH"):
                    aligned = (is_long and m1_trend == "BULLISH") or (not is_long and m1_trend == "BEARISH")
                    if aligned:
                        pressure -= 1
                        details.append(f"M1 trend support {m1_trend} ({m1_conf:.2f})")
                    else:
                        m1_trend_pressure = 4 if m1_conf >= 0.55 else 2
                        pressure += m1_trend_pressure
                        opposing_boost += 2
                        details.append(f"M1 trend pressure {m1_trend} ({m1_conf:.2f})")

                event = getattr(getattr(m1, "last_event", None), "value", "NONE")
                opposing_event = (is_long and event in ("BOS_BEARISH", "CHOCH_BEARISH")) or (
                    (not is_long) and event in ("BOS_BULLISH", "CHOCH_BULLISH")
                )
                supporting_event = (is_long and event in ("BOS_BULLISH", "CHOCH_BULLISH")) or (
                    (not is_long) and event in ("BOS_BEARISH", "CHOCH_BEARISH")
                )
                if opposing_event:
                    pressure += 4
                    opposing_boost += 3
                    details.append(f"M1 adverse event {event}")
                elif supporting_event:
                    pressure -= 2
                    details.append(f"M1 supportive event {event}")

                if len(m1_df) >= 6:
                    last5 = m1_df.tail(5)
                    if "open" in last5.columns and "close" in last5.columns:
                        if is_long:
                            aligned_count = int((last5["close"] > last5["open"]).sum())
                        else:
                            aligned_count = int((last5["close"] < last5["open"]).sum())
                        momentum_pressure_map = {5: -3, 4: -2, 3: 0, 2: 3, 1: 6, 0: 8}
                        momentum_pressure = momentum_pressure_map.get(aligned_count, 0)
                        pressure += momentum_pressure
                        if momentum_pressure > 0:
                            opposing_boost += max(1, momentum_pressure // 2)
                        details.append(f"M1 momentum {aligned_count}/5 ({momentum_pressure:+d})")
            except Exception as exc:
                logger.warning("[management] M1 context analysis failed for {}: {}", pos.symbol, exc)

        pressure = max(-6, min(18, pressure))
        opposing_boost = max(0, min(10, opposing_boost))
        return pressure, opposing_boost, details

    def _select_vol_timeframe(self, frames: dict) -> "pd.DataFrame | None":
        """Pick the timeframe for SYSTEM-WIDE volatility detection.
        Prefer M15 (responsive to intraday shocks); fall back to H1 then H4
        if a faster frame is unavailable for this symbol."""
        for tf in ("M15", "H1", "H4"):
            df = frames.get(tf)
            if df is not None and len(df) >= 50:
                return df
        return None

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

    @staticmethod
    def _levels_after_fill(
        direction: str,
        planned_entry: float,
        fill_price: float,
        planned_sl: float,
        planned_tp1: float,
        planned_tp2: float,
        pip_size: float,
        spread_pips: float,
    ) -> tuple[float, float, float]:
        """Re-anchor SL/TP to the actual fill price (P1) and spread-adjust (P2).

        P1: the planned SL/TP were computed off the planned (mid-zone) entry.
        After an adverse fill the offsets must be preserved relative to the
        ACTUAL fill, otherwise every slipped fill silently compresses R:R.

        P2: a SHORT's SL triggers on ASK and a LONG's TP fills on BID, so the
        spread skims every trade. Widen the spread-exposed leg by the spread:
          • BUY  → push TP further out (fills on BID)
          • SELL → push SL further out (triggers on ASK)
        """
        is_buy = direction.upper() in ("BUY", "LONG")
        d_sl = planned_sl - planned_entry
        d_tp1 = planned_tp1 - planned_entry
        d_tp2 = planned_tp2 - planned_entry
        sl = fill_price + d_sl
        tp1 = fill_price + d_tp1
        tp2 = fill_price + d_tp2

        spread_price = max(0.0, spread_pips) * pip_size
        if spread_price > 0:
            if is_buy:
                tp1 += spread_price
                tp2 += spread_price
            else:
                sl += spread_price
        return sl, tp1, tp2

    def _execute_entry(self, result, session: str, now: datetime) -> bool:
        setup_id = new_setup_id()
        with logger.contextualize(setup_id=setup_id):
            return self._execute_entry_inner(result, session, now, setup_id)

    def _execute_entry_inner(self, result, session: str, now: datetime, setup_id: str) -> bool:
        self._current_setup_id = setup_id
        pair = result.pair
        direction = result.direction

        # P9: refuse to trade instruments missing from INSTRUMENT_REGISTRY.
        # A silent pip_size/pip_value fallback (0.0001 / 10.0) mis-sizes whole
        # instrument categories (JPY 100×, Gold 10×). Skip loudly instead.
        if INSTRUMENT_REGISTRY.get(pair.upper()) is None:
            logger.critical(
                "INSTRUMENT NOT FOUND IN REGISTRY: {} — trade SKIPPED. Add this "
                "symbol to INSTRUMENT_REGISTRY (pip_size/pip_value would be guessed).",
                pair,
            )
            self._log_rejection(pair, direction, result.score, "Instrument not in registry")
            return False

        # Fetch more M1 bars than other timeframes — CHoCH detection needs
        # sufficient swing structure. 200 M1 bars = 3.3hrs, too few for Gold.
        # Fetch H4/H1/M15/M5 at 200, M1 at 400 (6.5hrs of micro structure).
        # D1 for higher-timeframe bias context in the decision engine.
        base_data = self.platforms.fetch_market_data(pair, ["D1", "H4", "H1", "M15", "M5"])
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
        self._last_known_balance = balance
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
            d1_df=data.get("D1"),
        )

        if isinstance(signal, EntryRejection):
            entry_context: dict[str, Any] = {}
            if signal.entry_price is not None:
                entry_context["entry_price"] = signal.entry_price
            if signal.stop_loss is not None:
                entry_context["stop_loss"] = signal.stop_loss
            self._log_rejection(pair, direction, result.score, signal.reason,
                                entry_context=entry_context or None)
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

        # ── Decision Engine entry path ────────────────────────────────────
        plan_to_store: tuple | None = None
        if self._decision_enabled:
            try:
                entry_ctx = self._build_entry_context(
                    result, signal, data, balance, session, spread, ctx, _exec_risk, now,
                )
                sa = self._situation_engine.assess_entry(entry_ctx)
                entry_decision = self._decision_engine.decide_entry(entry_ctx, sa)

                governor_changed = False
                if self._risk_governor is not None:
                    reviewed = self._risk_governor.review_entry(entry_decision, entry_ctx, sa)
                    if reviewed.action != entry_decision.action:
                        governor_changed = True
                    entry_decision = reviewed

                if self._decision_journal is not None:
                    self._decision_journal.log_entry(entry_ctx, sa, entry_decision, governor_changed)

                if not entry_decision.should_enter:
                    self._log_rejection(
                        pair, direction, result.score,
                        f"Decision Engine: {entry_decision.reason}",
                    )
                    self._persist_shadow_contract(
                        signal,
                        rejecting_gate=f"decision_engine:{entry_decision.action.value}",
                    )
                    return False

                # Apply conviction-based sizing
                conviction_mult = entry_decision.size_multiplier
                if entry_decision.is_market:
                    signal.entry_mode = "MARKET"

                # ── Trade Planner coordinator ────────────────────────────
                # The planner reads every advisor (scanner, DE, RL, adaptive,
                # portfolio, timing) and produces a complete plan. It refines
                # sizing/entry-mode and can SKIP or WAIT with a logged reason.
                if self._planner_enabled:
                    try:
                        plan_ctx = self._build_plan_context(
                            result, signal, entry_ctx, sa, spread, _exec_risk, now,
                        )
                        plan = self._planner.plan_trade(plan_ctx)
                        if plan.action == "SKIP":
                            self._log_rejection(
                                pair, direction, result.score,
                                f"Planner SKIP: {plan.reasoning}",
                            )
                            self._persist_shadow_contract(signal, rejecting_gate="planner:SKIP")
                            return False
                        if plan.action == "WAIT":
                            logger.info(
                                "[Planner] WAIT {} {} — {} (re-evaluated next scan)",
                                direction, pair, plan.wait_reason,
                            )
                            self._log_rejection(
                                pair, direction, result.score,
                                f"Planner WAIT: {plan.reasoning}",
                            )
                            return False
                        # ENTER — adopt the plan's sizing and entry mode.
                        base_pct = max(_exec_risk * 100.0, 1e-6)
                        conviction_mult = max(0.3, min(2.0, plan.risk_pct / base_pct))
                        signal.entry_mode = "MARKET" if plan.is_market else signal.entry_mode
                        plan_to_store = (plan, plan_ctx)
                    except Exception as exc:
                        logger.warning("[Planner] error — keeping decision-engine sizing: {}", exc)
            except Exception as exc:
                logger.warning("[DecisionEngine] entry error — falling back to legacy gates: {}", exc)
                conviction_mult = 1.0
        else:
            conviction_mult = 1.0

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
            exec_mult = (
                self.execution_monitor.get_size_multiplier(pair)
                if self.config.risk.execution_quality_sizing_enabled
                else 1.0
            )
            adjusted_lots = round(
                signal.position_size_lots * adjustments.position_size_multiplier * density_mult * vol_mult * exec_mult * conviction_mult,
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
            pending = self.position_store.find_pending_in_flight(pair, direction)
            if pending is STORE_UNAVAILABLE:
                logger.warning(
                    "[entry] in-flight store unavailable — skipping {} {} this cycle to avoid double-submit",
                    pair, direction,
                )
                return False
            if not isinstance(pending, list):
                return False
            stale = [p for p in pending if p.get("idempotency_key") != idem_key]
            if stale:
                stale_keys = [p.get("idempotency_key", "?") for p in stale]
                logger.warning(
                    "[entry] unresolved in-flight intent(s) for {} {} — skipping to avoid double-submit (stale keys: {})",
                    pair, direction, stale_keys,
                )
                return False

        if self.position_store:
            self.position_store.record_in_flight(idem_key, pair, direction, adjusted_lots)

        use_pending = False
        # ── Intelligent entry mode ────────────────────────────────────────
        # entry_mode is set by EntryEngine._decide_entry_mode() based on:
        #   • whether price is already inside the FVG/OB zone
        #   • M1 confirmation strength (choch_bos, engulfing, sweep, etc.)
        #   • liquidity sweep detected at the zone
        # "MARKET" = enter immediately; "PENDING" = wait for retrace to zone.
        brain_wants_market = getattr(signal, "entry_mode", "PENDING") == "MARKET"

        if self.config.risk.pending_orders_enabled and not ctx.uses_stake and not brain_wants_market:
            try:
                tick = self.platforms.get_price(pair)
                current = tick.ask if direction == "LONG" else tick.bid
                pip_size = get_pip_size(pair)
                distance_pips = abs(current - signal.entry_price) / pip_size
                is_buy = direction == "LONG"
                # Pending orders: only when price hasn't already moved past the zone
                # in the trade direction.  If price IS past the zone, a limit order
                # would bet on retrace against the thesis — fall through to market.
                if distance_pips > 3.0:
                    if is_buy and current < signal.entry_price:
                        order_kind = "BUY_STOP"
                        use_pending = True
                    elif not is_buy and current > signal.entry_price:
                        order_kind = "SELL_STOP"
                        use_pending = True
                    elif is_buy and current > signal.entry_price:
                        logger.info(
                            "[entry] {} LONG — price {:.5f} already above entry "
                            "{:.5f}, using market instead of BUY_LIMIT",
                            pair, current, signal.entry_price,
                        )
                    elif not is_buy and current < signal.entry_price:
                        logger.info(
                            "[entry] {} SHORT — price {:.5f} already below entry "
                            "{:.5f}, using market instead of SELL_LIMIT",
                            pair, current, signal.entry_price,
                        )
            except Exception as exc:
                logger.debug("[entry] pending order distance check failed, using market order: {}", exc)

        if brain_wants_market:
            logger.info(
                "[entry] {} {} — brain decided MARKET entry (mode={}, confirmation={})",
                pair, direction,
                getattr(signal, "entry_mode", "?"),
                getattr(signal, "micro_confirmation", "?"),
            )

        if use_pending:
            order = self.platforms.place_pending_entry(
                pair,
                order_kind,
                signal.entry_price,
                adjusted_lots,
                signal.stop_loss,
                signal.tp1,
                comment=build_order_comment("APND", idem_key),
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
                symbol=pair,
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

        # ── P1 + P2: re-anchor SL/TP to the actual fill and spread-adjust ──
        # The order was placed with planned-entry SL/TP1. Now that we know the
        # real fill, recompute SL/TP1/TP2 off the fill price (P1) and widen the
        # spread-exposed leg (P2), then push the correction to the broker.
        pip_sz_live = get_pip_size(pair)
        eff_sl, eff_tp1, eff_tp2 = self._levels_after_fill(
            direction=direction,
            planned_entry=signal.entry_price,
            fill_price=order.fill_price,
            planned_sl=signal.stop_loss,
            planned_tp1=signal.tp1,
            planned_tp2=signal.tp2,
            pip_size=pip_sz_live,
            spread_pips=spread,
        )
        levels_changed = (
            abs(eff_sl - signal.stop_loss) > pip_sz_live * 0.1
            or abs(eff_tp1 - signal.tp1) > pip_sz_live * 0.1
        )
        if levels_changed and all(
            v is not None and math.isfinite(v) for v in (eff_sl, eff_tp1)
        ):
            try:
                ok = self.platforms.modify_trade(
                    order.order_id, order.platform, new_sl=eff_sl, new_tp=eff_tp1
                )
                if ok:
                    order.sl = eff_sl
                    order.tp = eff_tp1
                    logger.info(
                        "🔧 LEVELS RE-ANCHORED — {} {} fill {:.5f} | SL {:.5f}→{:.5f} "
                        "TP1 {:.5f}→{:.5f} (slip {:.1f}pip, spread {:.1f}pip)",
                        direction, pair, order.fill_price,
                        signal.stop_loss, eff_sl, signal.tp1, eff_tp1,
                        order.slippage_pips, spread,
                    )
                else:
                    logger.warning(
                        "🔧 LEVEL RE-ANCHOR modify rejected — {} {}; keeping broker SL/TP as sent",
                        direction, pair,
                    )
                    eff_sl, eff_tp1, eff_tp2 = signal.stop_loss, signal.tp1, signal.tp2
            except Exception as exc:
                logger.warning("🔧 LEVEL RE-ANCHOR modify failed for {}: {}", pair, exc)
                eff_sl, eff_tp1, eff_tp2 = signal.stop_loss, signal.tp1, signal.tp2

        managed = ManagedPosition(
            order=order,
            tp1=eff_tp1,
            tp2=eff_tp2,
            score=signal.score,
            regime=getattr(result, "regime", ""),
            session=session,
            entry_type=signal.entry_type,
            stake_usd=stake_usd or 0.0,
            multiplier=self._get_deriv_multiplier(pair) if ctx.uses_stake else 100,
            idempotency_key=idem_key,
            confluences=list(signal.confluences),
        )
        # P4: persist the entry execution quality for the close record.
        managed.entry_spread = float(spread or 0.0)
        managed.entry_slippage_pips = float(getattr(order, "slippage_pips", 0.0) or 0.0)

        info_risk = INSTRUMENT_REGISTRY.get(pair.upper())
        pip_sz = info_risk.pip_size if info_risk else 0.0001
        pip_val = info_risk.pip_value_per_lot if info_risk else 10.0
        risk_d, is_fb = compute_position_risk_dollars(
            direction=direction,
            entry_price=order.fill_price,
            sl=eff_sl,
            lots=order.lots,
            pip_size=pip_sz,
            pip_value_per_lot=pip_val,
            at_breakeven=False,
            stake_usd=managed.stake_usd,
            multiplier=managed.multiplier,
            is_deriv_stake=ctx.uses_stake,
        )
        managed.initial_risk_dollars = risk_d if not is_fb else None

        # ── Derive per-trade management overrides from the plan (if any) ──
        # These are primitive values handed to the TradeManager so management
        # follows the plan's BE/trailing/partial rules instead of globals.
        # A position without a plan leaves them None → global config applies.
        plan_be_trigger_r = None
        plan_trail_activation_r = None
        plan_trail_strategy = None
        plan_partial_ratio = None
        if plan_to_store is not None:
            try:
                _mplan = plan_to_store[0]
                plan_be_trigger_r = float(_mplan.be_trigger_r)
                plan_trail_activation_r = float(_mplan.trail_activation_r)
                plan_trail_strategy = _mplan.trail_strategy
                # runner_pct = fraction left to run; the partial close at TP1
                # banks (1 - runner_pct). A zero runner means "no explicit
                # partial plan" → fall back to the global ratio.
                runner = float(_mplan.runner_pct or 0.0)
                if runner > 0.0:
                    plan_partial_ratio = max(0.1, min(1.0, 1.0 - runner))
            except Exception as exc:
                logger.debug("[Planner] management-param derive failed for {}: {}", pair, exc)

        tm_signal = TMEntrySignal(
            pair=pair,
            direction=direction,
            entry_price=order.fill_price,
            stop_loss=eff_sl,
            tp1=eff_tp1,
            tp2=eff_tp2,
            risk_reward_1=signal.risk_reward_1,
            risk_reward_2=signal.risk_reward_2,
            # P7: seed TradeManager with the ACTUAL filled lots, not the
            # requested amount — partial fills otherwise break TP1/trailing math.
            position_size_lots=order.lots,
            score=signal.score,
            confluences=list(signal.confluences),
            entry_zone=signal.entry_zone,
            entry_timeframe=signal.entry_timeframe,
            plan_be_trigger_r=plan_be_trigger_r,
            plan_trail_activation_r=plan_trail_activation_r,
            plan_trail_strategy=plan_trail_strategy,
            plan_partial_ratio=plan_partial_ratio,
        )
        tm_trade = self.trade_manager.open_trade(tm_signal)
        managed.tm_trade_id = tm_trade.trade_id

        # ── Link this position to its trade plan for outcome learning ────
        if plan_to_store is not None:
            try:
                _plan, _plan_ctx = plan_to_store
                managed.plan_id = _plan.plan_id
                managed.plan_sl_pips = float(_plan.sl_pips or 0.0)
                managed.plan_scale_in_allowed = bool(_plan.scale_in_allowed)
                if self._outcome_logger is not None:
                    self._outcome_logger.log_plan(_plan, _plan_ctx)
            except Exception as exc:
                logger.debug("[Planner] plan link/log failed for {}: {}", pair, exc)

        self.managed_positions[order.order_id] = managed
        self._save_position_checked(managed)
        self._daily_trades += 1

        logger.info(
            "🎯 TRADE OPENED — {} {} {:.2f}lots @ {:.5f} | SL {:.5f} | TP1 {:.5f} | TP2 {:.5f} | Score {}",
            direction,
            pair,
            order.lots,
            order.fill_price,
            eff_sl,
            eff_tp1,
            eff_tp2,
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
                    "stop_loss": eff_sl,
                    "tp1": eff_tp1,
                    "tp2": eff_tp2,
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
                        mt5.order_send({  # type: ignore[attr-defined]
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
                    pnl_from_broker = True
                    exit_reason = deal_info.exit_reason
                    exit_reason_source = "deriv_poc" if pos.platform.startswith("deriv") else "mt5_deal"
                    raw_broker_reason = deal_info.raw_reason_code
                    raw_broker_comment = deal_info.raw_comment
                else:
                    realized = self.platforms.get_realized_pnl(oid, pos.platform)
                    pnl_from_broker = realized is not None
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
                manager_intent = None
                exit_reason_discrepancy = False
                tm_trade = self.trade_manager.get_trade(pos.tm_trade_id) if pos.tm_trade_id else None
                if tm_trade is not None and tm_trade.close_reason:
                    manager_intent = tm_trade.close_reason
                    exit_reason_discrepancy = _exit_reasons_conflict(exit_reason, tm_trade.close_reason)
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
                    close_result=fake_close if pnl_from_broker else None,
                    exit_reason_source=exit_reason_source,
                    raw_broker_reason=raw_broker_reason,
                    raw_broker_comment=raw_broker_comment,
                    manager_intent=manager_intent,
                    exit_reason_discrepancy=exit_reason_discrepancy,
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
                    # P3: the tick-level stall/structure exit must stay coherent
                    # with the strategic Decision Engine. Defer the discretionary
                    # close when the engine's last verdict was HOLD/SCALE_IN, or
                    # when the trade is already working (pnl_r > 0.3) — don't kill
                    # trades that haven't had a chance to play out. Hard SL/TP2
                    # remain broker-enforced and are unaffected by this guard.
                    last_act = self._last_decision_action.get(oid)
                    de_wants_keep = self._decision_enabled and last_act in (
                        Action.HOLD, Action.SCALE_IN,
                        Action.MOVE_TO_BREAKEVEN, Action.TIGHTEN_SL,
                        Action.SET_PROTECTIVE_STOP, Action.OBSERVE,
                    )
                    _pip = get_pip_size(pos.symbol)
                    _risk_pips = abs(pos.entry_price - getattr(pos, "sl_original", pos.sl)) / _pip
                    pnl_r = (tm_trade.pnl_pips / _risk_pips) if _risk_pips > 1e-8 else 0.0
                    if de_wants_keep or pnl_r > 0.3:
                        logger.info(
                            "🧠 STALL/STRUCTURE EXIT DEFERRED — {} {} | de_verdict={} pnl_r={:.2f}",
                            pos.direction, pos.symbol,
                            last_act.value if last_act else "none", pnl_r,
                        )
                        tm_trade.status = (
                            TradeStatus.TRAILING if tm_trade.breakeven_active else TradeStatus.OPEN
                        )
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
                                stake_usd=half_stake if (half_stake is not None and half_stake > 0) else None,
                            )
                            if reopen_order.success:
                                # P6: register the reopened runner with the
                                # TradeManager + position store. Without this the
                                # reopened half ran headless — no BE protection,
                                # no trailing, no exit management.
                                runner_sl = tm_trade.stop_loss
                                runner_tp = tm_trade.tp2
                                new_managed = ManagedPosition(
                                    order=reopen_order,
                                    tp1=runner_tp,
                                    tp2=runner_tp,
                                    score=pos.score,
                                    regime=pos.regime,
                                    session=pos.session,
                                    entry_type=pos.entry_type,
                                    stake_usd=(half_stake or 0.0),
                                    multiplier=pos.multiplier,
                                    confluences=list(pos.confluences),
                                )
                                # Runner inherits post-TP1 state: SL at BE, TP1 done.
                                new_managed.sl = runner_sl
                                new_managed.tp1_hit = True
                                new_managed.at_breakeven = True
                                new_managed.entry_spread = getattr(pos, "entry_spread", 0.0)
                                new_managed.entry_slippage_pips = getattr(pos, "entry_slippage_pips", 0.0)
                                runner_signal = TMEntrySignal(
                                    pair=pos.symbol,
                                    direction=pos.direction,
                                    entry_price=reopen_order.fill_price,
                                    stop_loss=runner_sl,
                                    tp1=runner_tp,
                                    tp2=runner_tp,
                                    risk_reward_1=getattr(tm_trade, "risk_reward_1", 0.0),
                                    risk_reward_2=getattr(tm_trade, "risk_reward_2", 0.0),
                                    position_size_lots=reopen_order.lots,
                                    score=pos.score,
                                    confluences=list(pos.confluences),
                                )
                                runner_tm = self.trade_manager.open_trade(runner_signal)
                                runner_tm.partial_closed = True
                                runner_tm.breakeven_active = True
                                new_managed.tm_trade_id = runner_tm.trade_id
                                self.managed_positions[reopen_order.order_id] = new_managed
                                self._save_position_checked(new_managed)
                                logger.info(
                                    "✅ TP1 HIT (Deriv reopen) — {} {} | runner reopened at "
                                    "half stake, managed (SL@BE {:.5f}, TP {:.5f})",
                                    pos.direction, pos.symbol, runner_sl, runner_tp,
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
                        # P10: don't trust the local state blindly — verify the
                        # broker accepted the SL→BE modify. A silent failure would
                        # leave the original wide SL exposed to a Monday gap.
                        ok = self.platforms.modify_trade(
                            oid, pos.platform, new_sl=be_price, new_tp=None
                        )
                        if not ok:
                            ok = self.platforms.modify_trade(
                                oid, pos.platform, new_sl=be_price, new_tp=None
                            )
                        if not ok:
                            logger.critical(
                                "🌙 WEEKEND DE-RISK FAILED — {} could not move SL→BE; "
                                "closing position to avoid weekend gap on a wide SL.",
                                pos.symbol,
                            )
                            close_res = self.platforms.close_trade(oid, pos.platform)
                            if close_res.success:
                                self._record_closed_trade(
                                    pos, close_res.close_price,
                                    "WEEKEND_DERISK_CLOSE_FALLBACK", close_result=close_res,
                                )
                                self.managed_positions.pop(oid, None)
                                self.position_store.remove_position(oid)
                            else:
                                logger.critical(
                                    "🌙 WEEKEND DE-RISK fallback close ALSO failed for {} — "
                                    "position remains open with original SL.", pos.symbol,
                                )
                            continue
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
                logger.warning(
                    "[management] skipping strategic analysis for {} ({}) — no fresh frames returned",
                    pair, oid,
                )
                continue

            h4 = frames.get("H4")
            h1 = frames.get("H1")
            m15 = frames.get("M15")
            m5 = frames.get("M5")
            d1 = frames.get("D1")
            m1 = frames.get("M1")

            required_frames = {
                "H4": h4,
                "H1": h1,
                "M15": m15,
                "M5": m5,
            }
            missing = [tf for tf, df in required_frames.items() if df is None]
            if missing:
                logger.warning(
                    "[management] skipping strategic analysis for {} ({}) — missing frames: {}",
                    pair, oid, ",".join(missing),
                )
                continue

            if d1 is None:
                logger.warning(
                    "[management] {} ({}) fresh analysis running without D1 context",
                    pair, oid,
                )
            if m1 is None:
                logger.warning(
                    "[management] {} ({}) fresh analysis running without M1 context",
                    pair, oid,
                )

            try:
                # ── Re-score this instrument with fresh data ───────────────
                scan_result = self.scanner.scan_pair(
                    pair, h4, h1, m15, m5, currency_data, now, d1_df=d1,
                )

                pressure, opposing_boost, details = self._compute_in_trade_context_pressure(
                    pos=pos,
                    d1_df=d1,
                    m1_df=m1,
                )
                raw_score = int(scan_result.score)
                adjusted_score = max(0, min(100, raw_score - pressure))
                if adjusted_score != raw_score:
                    details.insert(0, f"In-trade context pressure {pressure:+d} ({raw_score}→{adjusted_score})")
                    scan_result.score = adjusted_score
                if details:
                    scan_result.confluences.extend(details)

                # Track score history for conviction monitoring
                if oid not in self._position_scores:
                    self._position_scores[oid] = []
                self._position_scores[oid].append(scan_result.score)
                # Keep only last N cycles
                max_cycles = cfg.conviction_decline_cycles + 2
                self._position_scores[oid] = self._position_scores[oid][-max_cycles:]

                hold_minutes = (now - pos.open_time).total_seconds() / 60

                # ── Decision Intelligence System ───────────────────────
                if self._decision_enabled:
                    self._run_decision_engine(
                        oid, pos, scan_result, sa_data={
                            "d1": d1, "h4": h4, "h1": h1, "m1": m1,
                        },
                        hold_minutes=hold_minutes,
                        pressure=pressure,
                        opposing_boost=opposing_boost,
                        pressure_details=details,
                        now=now,
                    )
                    if oid not in self.managed_positions:
                        continue
                else:
                    # ── Legacy rule-based checks (fallback) ────────────
                    if cfg.continuous_analysis_enabled and hold_minutes >= cfg.invalidation_min_hold_minutes:
                        self._check_invalidation(
                            oid,
                            pos,
                            scan_result,
                            now,
                            opposing_score_boost=opposing_boost,
                        )
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

                # ── Opportunity-cost exit + scale-in (P3) ──────────────────
                # These run regardless of whether the Decision Engine is
                # enabled. The engine has no executor for SCALE_IN and never
                # emits an opportunity-cost verdict, so without this they were
                # silently dead whenever the engine was on.
                if cfg.opportunity_cost_exit_mode != "off":
                    self._check_opportunity_cost_exit(oid, pos, now)
                    if oid not in self.managed_positions:
                        continue

                if cfg.scale_in_enabled:
                    self._check_scale_in_on_scan(oid, pos, scan_result)

            except Exception as exc:
                logger.warning(
                    "In-trade analysis error for {} (oid={}): {}",
                    pair, oid, exc,
                )

    # ── Decision Intelligence System helpers ─────────────────────────────

    def _run_decision_engine(
        self,
        oid: str,
        pos: ManagedPosition,
        scan_result,
        sa_data: dict,
        hold_minutes: float,
        pressure: int,
        opposing_boost: int,
        pressure_details: list[str],
        now: datetime,
    ) -> None:
        """Build context → assess situation → decide → governor review → execute."""
        try:
            ctx = self._build_trade_context(
                oid, pos, scan_result, sa_data,
                hold_minutes=hold_minutes,
                pressure=pressure,
                opposing_boost=opposing_boost,
                pressure_details=pressure_details,
            )
            sa = self._situation_engine.assess_open_trade(ctx)
            decision = self._decision_engine.decide_management(ctx, sa)

            governor_changed = False
            if self._risk_governor is not None:
                reviewed = self._risk_governor.review(decision, ctx, sa)
                if reviewed.action != decision.action:
                    governor_changed = True
                decision = reviewed

            if self._decision_journal is not None:
                self._decision_journal.log(ctx, sa, decision, governor_changed)

            # P3: record the strategic verdict so the tick-level TradeManager
            # exit can defer to a HOLD/SCALE_IN instead of overriding it.
            self._last_decision_action[oid] = decision.action

            self._execute_management_decision(oid, pos, decision, now)
        except Exception as exc:
            logger.warning(
                "[DecisionEngine] error for {} ({}) — falling back to legacy: {}",
                pos.symbol, oid, exc,
            )

    def _build_entry_context(
        self,
        result,
        signal,
        data: dict,
        balance: float,
        session: str,
        spread: float,
        platform_ctx,
        exec_risk: float,
        now,
    ) -> EntryContext:
        """Build rich EntryContext from scan result + signal + market data."""
        ctx = EntryContext(
            symbol=result.pair,
            direction=result.direction,
            scan_score=result.score,
            scan_direction=result.direction,
            entry_type=getattr(signal, "entry_type", ""),
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            risk_reward_1=signal.risk_reward_1,
            risk_reward_2=signal.risk_reward_2,
            risk_pips=signal.risk_pips,
            entry_mode=getattr(signal, "entry_mode", "PENDING"),
            micro_confirmation=getattr(signal, "micro_confirmation", ""),
            base_lots=signal.position_size_lots,
            account_balance=balance,
            risk_pct=exec_risk,
            session_name=session,
            open_trade_count=len(self.managed_positions),
            max_open_trades=self.config.risk.max_open_trades,
            current_spread=spread,
            typical_spread=platform_ctx.typical_spread(result.pair, fallback=2.0) if platform_ctx else 2.0,
            regime=getattr(result, "regime", ""),
            ev_estimate=getattr(result, "ev_estimate", 0.0),
            confluences=getattr(result, "confluences", []),
        )

        try:
            ctx.pair_multiplier = self.ml.pair_learner.get_pair_multiplier(result.pair)
        except Exception:
            pass

        try:
            prsm = getattr(self, "_portfolio_risk_sm", None)
            if prsm is not None:
                ctx.portfolio_heat_pct = getattr(prsm, "_last_heat_pct", 0.0)
        except Exception:
            pass

        try:
            session_status = self.session_engine.get_status(now)
            ctx.session_tradeable = session_status.is_tradeable
        except Exception:
            pass

        try:
            open_pairs = [p.symbol for p in self.managed_positions.values()]
            ns = self.news_guard.check(open_pairs + [result.pair], now)
            if hasattr(ns, "upcoming_events"):
                for evt in ns.upcoming_events:
                    if result.pair in getattr(evt, "affected_pairs", []):
                        mins = getattr(evt, "minutes_until", 999.0)
                        impact = getattr(evt, "impact", "LOW")
                        if mins < ctx.minutes_to_high_impact_news:
                            ctx.minutes_to_high_impact_news = mins
                            ctx.news_impact = impact
        except Exception:
            pass

        # Multi-TF structure analysis
        for tf_key, attr_prefix in [("D1", "d1"), ("H4", "h4"), ("H1", "h1"), ("M1", "m1")]:
            df = data.get(tf_key)
            if df is None or len(df) < 5:
                continue
            try:
                analysis = self.scanner.structure.analyze(df)
                setattr(ctx, f"{attr_prefix}_trend", analysis.trend)
                setattr(ctx, f"{attr_prefix}_confidence", analysis.confidence)
                event = "NONE"
                if analysis.last_choch:
                    event = f"CHOCH_{analysis.trend}"
                elif analysis.last_bos:
                    event = f"BOS_{analysis.trend}"
                setattr(ctx, f"{attr_prefix}_event", event)
            except Exception:
                pass

        # M1 aligned count
        if "M1" in data and data["M1"] is not None and len(data["M1"]) >= 5:
            try:
                m1_df = data["M1"]
                recent = m1_df.tail(5)
                is_long = result.direction.upper() in ("BUY", "LONG")
                aligned = sum(
                    1 for _, c in recent.iterrows()
                    if (is_long and float(c.get("close", 0)) > float(c.get("open", 0)))
                    or (not is_long and float(c.get("close", 0)) < float(c.get("open", 0)))
                )
                ctx.m1_aligned_count = aligned
            except Exception:
                pass

        return ctx

    def _build_plan_context(
        self,
        result,
        signal,
        entry_ctx: EntryContext,
        sa: SituationAssessment,
        spread: float,
        exec_risk: float,
        now,
    ) -> TradePlanContext:
        """Gather every advisor's analysis into one TradePlanContext.

        Reuses the rich EntryContext + SituationAssessment that the decision
        engine already produced (no duplicate analysis), and folds in the RL
        signal carried on the scan result plus portfolio/timing state.
        """
        pip_size = get_pip_size(result.pair)
        # ATR in pips from the entry engine's risk distance when available.
        atr_pips = 0.0
        try:
            atr_pips = float(getattr(signal, "risk_pips", 0.0) or 0.0)
        except (TypeError, ValueError):
            atr_pips = 0.0

        try:
            tick = self.platforms.get_price(result.pair)
            current_price = tick.ask if entry_ctx.is_long else tick.bid
        except Exception:
            current_price = signal.entry_price

        # Daily P&L in R from the risk engine's running tally (best-effort).
        daily_pnl_r = 0.0
        try:
            daily_pnl_r = float(getattr(self.risk_engine, "daily_pnl_r", 0.0) or 0.0)
        except (TypeError, ValueError):
            daily_pnl_r = 0.0

        # Correlated exposure: fraction of open trades sharing a currency leg.
        correlated = 0.0
        try:
            open_syms = [p.symbol for p in self.managed_positions.values()]
            if open_syms:
                legs = {result.pair[:3], result.pair[3:6]}
                shared = sum(
                    1 for s in open_syms
                    if {s[:3], s[3:6]} & legs
                )
                correlated = min(1.0, shared / max(1, len(open_syms)))
        except Exception:
            correlated = 0.0

        dd_pct = 0.0
        try:
            # DrawdownGuard exposes drawdown via get_status().drawdown_from_peak_pct
            # (a 0–1 fraction). The planner compares against a percent threshold
            # (drawdown_size_reduction_threshold defaults to 5.0), so scale ×100.
            dd_status = self.drawdown.get_status(now)
            dd_pct = abs(float(dd_status.drawdown_from_peak_pct)) * 100.0
        except Exception:
            dd_pct = 0.0

        # Minutes until the next session boundary — drives the planner's
        # WAIT-for-better-session logic. Without this it stays at its 999
        # default and the WAIT path can never trigger.
        mins_to_session = 999
        try:
            mins_to_session = int(self.session_engine.get_status(now).minutes_to_next_session)
        except Exception:
            mins_to_session = 999

        session_wr = 0.5
        try:
            prof = self.ml.session_learner.get_session_aggression(entry_ctx.session_name)
            session_wr = {"AGGRESSIVE": 0.6, "NORMAL": 0.5, "CAUTIOUS": 0.42, "AVOID": 0.3}.get(prof, 0.5)
        except Exception:
            session_wr = 0.5

        pair_wr = 0.5
        try:
            profile = self.ml.pair_learner._profiles.get(result.pair)
            if profile is not None and profile.total_trades > 0:
                pair_wr = float(profile.win_rate)
        except Exception:
            pair_wr = 0.5

        zone_quality = max(0.0, min(1.0, sa.structure_integrity))

        return TradePlanContext(
            symbol=result.pair,
            pip_size=pip_size,
            spread_pips=spread,
            atr_pips=atr_pips,
            current_price=current_price,
            direction=result.direction,
            scanner_score=float(result.score),
            zone_type=getattr(signal, "entry_type", ""),
            zone_quality=zone_quality,
            zone_entry_price=signal.entry_price,
            de_confidence=self._decision_engine.compute_conviction(sa),
            de_tf_alignment=sa.tf_alignment,
            de_structure_score=sa.structure_integrity,
            de_momentum_score=sa.momentum,
            rl_action=int(getattr(result, "rl_action", 0) or 0),
            rl_confidence=float(getattr(result, "rl_confidence", 0.0) or 0.0),
            rl_expected_r=float(getattr(result, "rl_expected_r", 0.0) or 0.0),
            rl_stage=int(getattr(result, "rl_stage", 1) or 1),
            pair_multiplier=entry_ctx.pair_multiplier,
            ev_estimate=entry_ctx.ev_estimate,
            pair_win_rate=pair_wr,
            session_win_rate=session_wr,
            proposed_sl_price=signal.stop_loss,
            proposed_sl_pips=float(getattr(signal, "risk_pips", 0.0) or 0.0),
            proposed_tp1_price=signal.tp1,
            proposed_tp2_price=signal.tp2,
            risk_reward_1=signal.risk_reward_1,
            risk_reward_2=signal.risk_reward_2,
            structure_sl_available=getattr(signal, "entry_type", "") not in ("", None),
            micro_confirmation=getattr(signal, "micro_confirmation", ""),
            brain_entry_mode=getattr(signal, "entry_mode", "PENDING"),
            open_positions=len(self.managed_positions),
            correlated_exposure=correlated,
            daily_pnl_r=daily_pnl_r,
            max_positions=self.config.risk.max_open_trades,
            open_position_book=[
                (p.symbol, p.direction) for p in self.managed_positions.values()
            ],
            session=entry_ctx.session_name,
            day_of_week=now.weekday(),
            minutes_to_session_change=mins_to_session,
            minutes_to_news=entry_ctx.minutes_to_high_impact_news,
            is_news_window=entry_ctx.minutes_to_high_impact_news < 15,
            account_balance=float(entry_ctx.account_balance or 0.0),
            base_risk_pct=exec_risk * 100.0,
            current_drawdown_pct=dd_pct,
            situation_label=sa.primary_label,
        )

    def _build_trade_context(
        self,
        oid: str,
        pos: ManagedPosition,
        scan_result,
        sa_data: dict,
        hold_minutes: float = 0.0,
        pressure: int = 0,
        opposing_boost: int = 0,
        pressure_details: list[str] | None = None,
    ) -> TradeContext:
        """Assemble the full TradeContext from all available data sources."""
        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        pnl_pips = tm_trade.pnl_pips if tm_trade else 0.0
        pnl_dollars = tm_trade.pnl_dollars if tm_trade and hasattr(tm_trade, 'pnl_dollars') else 0.0
        partial_closed = tm_trade.partial_closed if tm_trade else False

        try:
            tick = self.platforms.get_price(pos.symbol)
            current_price = tick.bid if pos.direction == "BUY" else tick.ask
        except Exception:
            current_price = pos.entry_price

        _pip_sz = get_pip_size(pos.symbol)
        original_risk = abs(pos.entry_price - getattr(pos, 'sl_original', pos.sl)) / _pip_sz
        if original_risk < 1e-8:
            original_risk = abs(pos.entry_price - pos.sl) / _pip_sz

        ctx = TradeContext(
            symbol=pos.symbol,
            order_id=oid,
            direction=pos.direction,
            entry_type=getattr(pos, 'entry_type', ''),
            entry_price=pos.entry_price,
            current_price=current_price,
            current_sl=pos.sl,
            pnl_pips=pnl_pips,
            pnl_dollars=pnl_dollars,
            hold_minutes=hold_minutes,
            at_breakeven=pos.at_breakeven,
            tp1_hit=pos.tp1_hit,
            trailing=pos.trailing,
            partial_closed=partial_closed,
            lots=pos.lots,
            original_risk_pips=original_risk,
            scan_score=int(scan_result.score),
            scan_direction=getattr(scan_result, 'direction', ''),
            score_history=list(self._position_scores.get(oid, [])),
            open_trade_count=len(self.managed_positions),
            max_open_trades=self.config.risk.max_open_trades,
            context_pressure=pressure,
            opposing_boost=opposing_boost,
            pressure_details=list(pressure_details or []),
            confluences=list(getattr(scan_result, 'confluences', [])),
        )

        # ── Extract structure from analysis DataFrames ───────────────────
        for tf_key, tf_name in [("d1", "d1"), ("h4", "h4"), ("h1", "h1"), ("m1", "m1")]:
            df = sa_data.get(tf_key)
            if df is None:
                continue
            try:
                analysis = self.scanner.structure.analyze(df)
                trend = getattr(analysis.trend, "value", str(analysis.trend))
                conf = float(getattr(analysis, "confidence", 0.0) or 0.0)
                event = getattr(analysis.last_event, "value", "NONE")
                setattr(ctx, f"{tf_name}_trend", trend)
                setattr(ctx, f"{tf_name}_confidence", conf)
                setattr(ctx, f"{tf_name}_event", event)
                if hasattr(analysis, "swing_high") and analysis.swing_high is not None:
                    setattr(ctx, f"{tf_name}_swing_high", analysis.swing_high)
                if hasattr(analysis, "swing_low") and analysis.swing_low is not None:
                    setattr(ctx, f"{tf_name}_swing_low", analysis.swing_low)
            except Exception:
                pass

        # H1 last candle direction
        h1_df = sa_data.get("h1")
        if h1_df is not None and len(h1_df) >= 3:
            try:
                last_closed = h1_df.iloc[-2]
                c_open = float(last_closed.get("open", 0))
                c_close = float(last_closed.get("close", 0))
                if c_open > 0 and c_close > 0:
                    body = abs(c_close - c_open)
                    rng = float(last_closed.get("high", c_close)) - float(last_closed.get("low", c_open))
                    ctx.h1_last_candle_doji = rng > 0 and (body / rng) < 0.3
                    ctx.h1_last_candle_bearish = c_close < c_open
            except Exception:
                pass

        # M1 aligned candle count
        m1_df = sa_data.get("m1")
        if m1_df is not None and len(m1_df) >= 5:
            try:
                recent = m1_df.tail(5)
                is_long = pos.direction == "BUY"
                aligned = sum(
                    1 for _, row in recent.iterrows()
                    if (is_long and float(row.get("close", 0)) > float(row.get("open", 0)))
                    or (not is_long and float(row.get("close", 0)) < float(row.get("open", 0)))
                )
                ctx.m1_aligned_count = aligned
            except Exception:
                pass

        # ── Session / News ───────────────────────────────────────────────
        try:
            sess = self.session_engine.get_status(datetime.now(timezone.utc))
            ctx.session_name = getattr(sess, 'name', 'UNKNOWN')
            ctx.session_tradeable = getattr(sess, 'is_tradeable', True)
        except Exception:
            pass

        # ── Portfolio heat ───────────────────────────────────────────────
        if self._portfolio_risk_sm is not None:
            try:
                ctx.portfolio_heat_pct = getattr(self._portfolio_risk_sm, '_last_heat_pct', 0.0) or 0.0
            except Exception:
                pass

        return ctx

    def _execute_management_decision(
        self,
        oid: str,
        pos: ManagedPosition,
        decision: ManagementDecision,
        now: datetime,
    ) -> None:
        """Map a ManagementDecision to execution actions."""
        if decision.action == Action.CLOSE:
            result = self.platforms.close_trade(oid, pos.platform)
            if result.success:
                reason = f"DECISION_ENGINE({decision.reason[:100]})"
                logger.info(
                    "🧠 DECISION CLOSE — {} {} | {}",
                    pos.direction, pos.symbol, reason,
                )
                self._record_closed_trade(pos, result.close_price, reason, close_result=result)
                self.managed_positions.pop(oid, None)
                self.position_store.remove_position(oid)
                self._position_scores.pop(oid, None)
            else:
                logger.warning(
                    "🧠 DECISION CLOSE FAILED — {} {} oid={} | {}",
                    pos.direction, pos.symbol, oid, getattr(result, "error", "unknown"),
                )

        elif decision.action == Action.TIGHTEN_SL and decision.new_sl is not None:
            success = self.platforms.modify_trade(oid, pos.platform, new_sl=decision.new_sl)
            if success:
                old_sl = pos.sl
                pos.sl = decision.new_sl
                tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                if tm_trade:
                    tm_trade.stop_loss = decision.new_sl
                self.position_store.update_position(oid, sl=decision.new_sl)
                logger.info(
                    "🧠 DECISION TIGHTEN — {} {} | SL {:.5f}→{:.5f} | {}",
                    pos.direction, pos.symbol, old_sl, decision.new_sl,
                    decision.reason[:80],
                )

        elif decision.action == Action.SET_PROTECTIVE_STOP and decision.new_sl is not None:
            success = self.platforms.modify_trade(oid, pos.platform, new_sl=decision.new_sl)
            if success:
                old_sl = pos.sl
                pos.sl = decision.new_sl
                tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                if tm_trade:
                    tm_trade.stop_loss = decision.new_sl
                self.position_store.update_position(oid, sl=decision.new_sl)
                logger.info(
                    "🧠 PROTECTIVE STOP — {} {} | SL {:.5f}→{:.5f} | {}",
                    pos.direction, pos.symbol, old_sl, decision.new_sl,
                    decision.reason[:80],
                )

        elif decision.action == Action.MOVE_TO_BREAKEVEN and not pos.at_breakeven:
            be_level = pos.entry_price
            success = self.platforms.modify_trade(oid, pos.platform, new_sl=be_level)
            if success:
                pos.sl = be_level
                pos.at_breakeven = True
                tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                if tm_trade:
                    tm_trade.stop_loss = be_level
                    tm_trade.breakeven_active = True
                self.position_store.update_position(oid, sl=be_level, at_breakeven=True)
                logger.info(
                    "🧠 DECISION BE — {} {} | SL→{:.5f} | {}",
                    pos.direction, pos.symbol, be_level, decision.reason[:80],
                )

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

    def _scale_in_allowed_for(self, pos: ManagedPosition) -> bool:
        """Per-position scale-in gate.

        The trade plan can refine the global ``scale_in_enabled`` switch for a
        specific position.  ``plan_scale_in_allowed`` of ``None`` means the plan
        gave no directive — defer to the global behaviour (already gated by the
        caller).  ``False`` blocks scale-in for this position; ``True`` permits
        it (still subject to the usual safety checks downstream).
        """
        plan_flag = getattr(pos, "plan_scale_in_allowed", None)
        if plan_flag is None:
            return True
        if not plan_flag:
            logger.debug("[scale-in] plan disallows scale-in for {} — skipping", pos.symbol)
        return bool(plan_flag)

    def _governor_allows_add(self, pos: ManagedPosition) -> bool:
        """Portfolio Governor gate for adding to an existing position.

        Excludes the position itself from the book so a same-symbol add is not
        blocked by its own currency/sector footprint — the governor mainly
        enforces the daily-loss-cap halt here.  Fail-open on any error.
        """
        gov = getattr(self, "_governor", None)
        if gov is None:
            return True
        try:
            balance = self._last_known_balance or 0.0
            others = [
                p for p in self.managed_positions.values()
                if getattr(p, "order_id", None) != getattr(pos, "order_id", None)
            ]
            verdict = gov.check(pos.symbol, pos.direction, others, balance)
            if not getattr(verdict, "allowed", True):
                logger.info(
                    "[Governor] add to {} {} blocked — {}",
                    pos.direction, pos.symbol, getattr(verdict, "reason", ""),
                )
                return False
            return True
        except Exception as exc:
            logger.debug("[Governor] add-check failed (allowing): {}", exc)
            return True

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
        prsm: Optional[PortfolioRiskStateMachine] = getattr(self, '_portfolio_risk_sm', None)
        if prsm is None:
            pass
        elif prsm.state in (PortfolioRiskState.DEFENSIVE, PortfolioRiskState.REDUCING):
            return
        cfg = self.config.risk
        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        if tm_trade is None:
            return
        if not tm_trade.partial_closed or not tm_trade.breakeven_active:
            return
        if pos.scale_in_count >= cfg.scale_in_max_adds:
            return

        # Plan-aware gate: the trade plan can disallow scale-in for this
        # specific position (None = no directive, defer to global behaviour).
        if not self._scale_in_allowed_for(pos):
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
        dd_status = self.drawdown.get_status()
        effective_min = max(self.config.scoring.min_entry_score, dd_status.current_score_threshold)
        if scan_result.score < effective_min:
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

        # Portfolio Governor — respect daily-loss-cap halt / exposure limits.
        if not self._governor_allows_add(pos):
            return

        if self._submit_scale_in(pos, add_lots, tm_trade.stop_loss, tm_trade.tp2, f"SCAN|{scan_result.score}"):
            logger.info(
                "📈 SCALE-IN (scan-wired) — {} {} | +{} lots (add #{}) | fresh score={}",
                pos.direction, pos.symbol, add_lots, pos.scale_in_count, scan_result.score,
            )


    def _check_scale_in(self):
        if not self.config.risk.scale_in_enabled:
            return
        prsm: Optional[PortfolioRiskStateMachine] = getattr(self, '_portfolio_risk_sm', None)
        if prsm is None:
            pass
        elif prsm.state in (PortfolioRiskState.DEFENSIVE, PortfolioRiskState.REDUCING):
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
                if not self._scale_in_allowed_for(pos):
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
                if not self._governor_allows_add(pos):
                    continue
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
        manager_intent: Optional[str] = None,
        exit_reason_discrepancy: bool = False,
    ) -> None:
        pip_size = get_pip_size(pos.symbol)
        is_buy = pos.direction == "BUY"
        pnl_pips = (close_price - pos.entry_price) / pip_size if is_buy else (pos.entry_price - close_price) / pip_size

        if close_result is not None:
            pnl_dollars = round(close_result.pnl, 2)
        else:
            pos_ctx = build_context_for_symbol(pos.symbol)
            if pos_ctx.uses_stake:
                price_move_pct = abs(close_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0
                signed_move = price_move_pct if is_buy == (close_price >= pos.entry_price) else -price_move_pct
                pnl_dollars = round(pos.stake_usd * signed_move * pos.multiplier, 2)
            else:
                info = INSTRUMENT_REGISTRY.get(pos.symbol.upper())
                if info is None:
                    # P9: never silently fall back to a guessed pip value — flag it.
                    logger.critical(
                        "INSTRUMENT NOT FOUND IN REGISTRY: {} — pnl fallback used "
                        "default pip_value=10.0, P&L may be WRONG. Add this symbol "
                        "to INSTRUMENT_REGISTRY.", pos.symbol,
                    )
                pip_value = info.pip_value_per_lot if info else 10.0
                pnl_dollars = round(pnl_pips * pip_value * pos.lots, 2)
            logger.warning(
                "[pnl] Broker PnL unavailable for {} {} — using formula fallback (pnl_dollars={:.2f})",
                pos.direction, pos.symbol, pnl_dollars,
            )

        balance = self.platforms.get_platform_balance(pos.symbol)
        if balance:
            self._last_known_balance = balance
            pnl_pct = pnl_dollars / balance
        elif self._last_known_balance > 0:
            pnl_pct = pnl_dollars / self._last_known_balance
            logger.warning(
                "⚠ Balance unavailable for {} at close — using last known balance {:.2f}",
                pos.symbol, self._last_known_balance,
            )
        else:
            logger.warning(
                "⚠ Balance unavailable for {} at close and no last known balance — "
                "drawdown guard did NOT see this trade's result", pos.symbol,
            )
            pnl_pct = 0.0

        self.drawdown.register_trade_result(pnl_pct)
        try:
            self.position_store.save_guard_state(self.drawdown.to_state())
        except Exception as exc:
            logger.error("Guard state persist after trade close failed: {}", exc)
        self.risk_engine.record_trade_result(
            pnl_dollars=pnl_dollars,
            pnl_pips=pnl_pips,
            pair=pos.symbol,
            direction=pos.direction,
        )

        # Portfolio Governor — fold realised P&L into the daily tally so the
        # daily-loss-cap halt can engage / lift.
        if getattr(self, "_governor", None) is not None:
            try:
                if balance:
                    self._governor.set_reference_balance(balance)
                self._governor.update_daily_pnl(pnl_dollars)
            except Exception as exc:
                logger.debug("[Governor] daily pnl update failed: {}", exc)

        hold_seconds = (datetime.now(timezone.utc) - pos.open_time).total_seconds()
        logger.info(
            "📊 TRADE CLOSED — {} {} | {:.1f}pip | {} | {:.0f}s",
            pos.direction,
            pos.symbol,
            pnl_pips,
            outcome,
            hold_seconds,
        )

        # P5: arm a per-pair cooldown when a trade is stopped out at breakeven
        # (profit ≈ 0), to break the enter→BE→stopped→re-enter chop loop.
        try:
            cd_min = getattr(self.config.risk, "be_stop_cooldown_minutes", 0.0)
            if cd_min > 0:
                is_be_stop = ("breakeven" in (outcome or "").lower()) or (
                    getattr(pos, "at_breakeven", False) and abs(pnl_pips) <= 2.0
                )
                if is_be_stop:
                    self._be_stop_cooldown[pos.symbol] = (
                        datetime.now(timezone.utc) + timedelta(minutes=cd_min)
                    )
                    logger.info(
                        "[P5] BE-stop cooldown armed for {} — {:.0f}min",
                        pos.symbol, cd_min,
                    )
        except Exception as exc:
            logger.debug("[P5] BE cooldown record failed: {}", exc)

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
            # P4: persist the real entry spread (pips) and entry slippage (pips)
            # captured at fill time instead of the previous hardcoded 0.0,
            # so ML / analysis can learn their cost impact.
            spread=float(getattr(pos, "entry_spread", 0.0) or 0.0),
            slippage=float(getattr(pos, "entry_slippage_pips", 0.0) or 0.0),
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
            trade_summary = {
                "pair": pos.symbol,
                "direction": pos.direction,
                "regime": pos.regime,
                "session": pos.session,
                "pnl_dollars": pnl_dollars,
                "risk_dollars": getattr(pos, "initial_risk_dollars", None),
                "pnl": round(pnl_pips, 2),
                "outcome": outcome,
            }
            self.scanner._trade_history.append(trade_summary)
            if len(self.scanner._trade_history) > 500:
                self.scanner._trade_history = self.scanner._trade_history[-500:]
        except Exception as exc:
            logger.debug("[record_trade] scanner trade history append failed: {}", exc)

        # ── Link the realised outcome back to its trade plan ─────────────
        if self._outcome_logger is not None and getattr(pos, "plan_id", ""):
            try:
                plan_sl = float(getattr(pos, "plan_sl_pips", 0.0) or 0.0)
                pnl_r = round(pnl_pips / plan_sl, 3) if plan_sl > 1e-8 else 0.0
                self._outcome_logger.log_outcome(
                    pos.plan_id,
                    {
                        "pnl_r": pnl_r,
                        "pnl_pips": round(pnl_pips, 2),
                        "pnl_dollars": pnl_dollars,
                        "outcome": outcome,
                        "duration_minutes": round(hold_seconds / 60.0, 1),
                        "symbol": pos.symbol,
                        "direction": pos.direction,
                    },
                )
            except Exception as exc:
                logger.debug("[Planner] outcome log failed for {}: {}", pos.symbol, exc)

        if exit_reason_discrepancy:
            logger.warning(
                "⚠️ EXIT ATTRIBUTION DISCREPANCY — {} {}: broker={} but manager intended '{}'",
                pos.direction, pos.symbol, outcome, manager_intent,
            )

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
                    "manager_intent": manager_intent,
                    "exit_reason_discrepancy": exit_reason_discrepancy,
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

            if getattr(self, "_governor", None) is not None:
                try:
                    self._governor.reset_daily()
                except Exception as exc:
                    logger.debug("[Governor] daily reset failed: {}", exc)
            if self.maintenance.should_run():
                try:
                    maint_result = self.maintenance.run()
                    logger.info("🧹 Daily maintenance — {}", maint_result)
                except Exception as exc:
                    logger.warning("Maintenance error: {}", exc)

        if self.ml.should_retrain():
            self._run_ml_optimization()

        self._maybe_calibrate_planner()

        self._maybe_backup_data()

    def _maybe_calibrate_planner(self) -> None:
        """Evolve PlannerConfig from realised plan→outcome data."""
        if self._calibrator is None or self._outcome_logger is None:
            return
        try:
            completed = self._outcome_logger.get_completed_trades(
                lookback=self._planner.config.calibration_lookback_trades
            )
            if not self._calibrator.should_calibrate(len(completed)):
                return
            new_cfg = self._calibrator.calibrate(completed)
            new_cfg.enabled = self._planner_enabled
            self._planner.update_config(new_cfg)
            new_cfg.save()
            logger.info("🎯 Planner calibrated from {} completed plans", len(completed))
        except Exception as exc:
            logger.warning("Planner calibration failed: {}", exc)

    def _maybe_backup_data(self) -> None:
        """Push data/ to GitHub every ``interval_hours`` if backup is enabled."""
        cfg = self.config.data_backup
        if not cfg.enabled:
            return
        now = _time.time()
        interval_secs = cfg.interval_hours * 3600
        if now - self._last_backup_time < interval_secs:
            return
        self._last_backup_time = now
        try:
            from scripts.backup_data import run_backup

            result = run_backup(
                max_file_size_mb=cfg.max_file_size_mb,
                exclude_patterns=cfg.exclude_patterns,
            )
            logger.info("💾 Data backup — {}", result)
        except Exception as exc:
            logger.warning("Data backup failed: {}", exc)

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
                # Re-entry must respect the Portfolio Governor — a daily-loss
                # halt or exposure limit blocks re-entering just like a fresh entry.
                if not self._governor_allows_add(pos):
                    logger.info(
                        "🔄 RE-ENTRY suppressed by governor — {} {}",
                        pos.direction, pos.symbol,
                    )
                    return
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
