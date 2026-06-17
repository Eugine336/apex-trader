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
from management.exit_cause import ExitCause
from management.opportunity_executor import OpportunityExecutor
from brain.orchestrator import (
    Orchestrator,
    TradeProposal,
    PositionEvidence,
    PositionHealthReport,
    ManagementAction,
    gate_quality_multiplier as _gate_quality_multiplier,
)
from brain.outcome_feedback import OutcomeFeedback
from adaptive.post_close_tracker import PostCloseTracker
from brain.decision_trace import (
    DecisionTraceRecorder,
    STAGE_RANKER,
    STAGE_CORRELATION,
    STAGE_MARGIN,
    STAGE_MAX_TRADES,
    STAGE_ENTRY_ENGINE,
    STAGE_DECISION_ENGINE,
    STAGE_GOVERNOR,
    STAGE_PLANNER,
)
from management.trade_manager import (
    TradeManager,
    TradeStatus,
    TERMINAL_STATUSES,
    EntrySignal as TMEntrySignal,
)
from adaptive.optimizer import AdaptiveOptimizer as MLAdapter
from adaptive.gate_tuner import GateTuner
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
from risk.account_risk import AccountRiskManager
from risk import risk_accumulation as ra
from scanner import PairScanner, PairRanker, ScanScheduler
from trigger.entry_engine import EntryEngine, EntryRejection
from trigger.entry_validator import EntryValidator


from platforms.trading_loop.positions import ManagedPosition, _LockedPositions
from platforms.trading_loop.recovery_mixin import RecoveryReconciliationMixin
from platforms.trading_loop.risk_heat_mixin import RiskHeatMarginMixin
from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin
from platforms.trading_loop.shadow_live_mixin import ShadowLiveMixin

from persistence.event_store import get_event_store, new_cycle_id, new_setup_id
from persistence.domain_events import (
    DECISION_REJECT, ORDER_SENT, ORDER_FILLED, TRADE_OPEN, TRADE_CLOSE,
    SETUP_SKIPPED, SHADOW_CONTRACT_CREATED, BALANCE_UNAVAILABLE,
    PERSISTENCE_DEGRADED, CYCLE_FAILED, TRADING_LOOP_HALTED,
    ORCHESTRATOR_PROPOSAL, OUTCOME_FEEDBACK, POSITION_HEALTH,
)
from persistence.shadow_store import ShadowStore, ShadowContract, new_contract_id

from decision.context import EntryContext, TradeContext
from decision.situation import SituationEngine, SituationAssessment
from decision.actions import Action, EntryAction, ManagementDecision
from decision.engine import DecisionEngine, DecisionWeights, SEVERE_THESIS_CLOSE_PREFIX
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
    # Reject non-positive levels outright — a 0/negative SL would otherwise pass
    # the LONG check (0 < entry) and be sent to the broker as a NAKED order.
    if stop_loss <= 0:
        return False, f"invalid SL ({stop_loss}) — non-positive (would be naked)"
    if tp1 <= 0:
        return False, f"invalid TP1 ({tp1}) — non-positive"
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


def _regime_score_bump(
    optimal_score_threshold: int, sample_size: int, min_sample: int,
) -> int:
    """Defensive-only entry-bar bump derived from a regime's learned score
    threshold.

    The learner's threshold (~82–90) is calibrated around a neutral 85, so the
    meaningful signal is the DELTA above neutral, applied on top of the
    instrument's own bar. Returns 0 (no change) unless the regime is confident
    (≥ min_sample trades) AND wants a HIGHER bar — it never loosens, and is
    clamped to a small ceiling so a noisy regime can't choke off all trading.
    """
    if sample_size < min_sample:
        return 0
    return max(0, min(8, int(optimal_score_threshold) - 85))


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


class TradingLoop(RecoveryReconciliationMixin, RiskHeatMarginMixin, ExitChecksMixin, ShadowLiveMixin):
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
                ScoreOptimizer as _SO,
                ScoringWeights as _SW,
                ADAPTIVE_WEIGHT_ENVELOPE_PCT as _ENV_PCT,
            )
            if self.config.scoring.per_class_optimizer:
                # Per-class mode: load every persisted profile and hand the
                # scanner a {class: weights} payload it resolves per symbol.
                _so = _SO(config=self.config.scoring)
                _scanner_weights_dict = self._scanner_weight_payload(_so)
            else:
                _adaptive_weights = _load_weights()
                _clamped = _adaptive_weights.clamped_to_envelope(_SW(), _ENV_PCT)
                _scanner_weights_dict = _clamped.as_dict()
        self.scanner = PairScanner(self.config, scoring_weights=_scanner_weights_dict)
        self.ranker = PairRanker()
        # Opportunity executor — selects the live direction from the ranked
        # candidates when OpportunityRankerConfig.execute is on. Shadow no-op
        # otherwise (the scalar consensus direction is used unchanged).
        self._opportunity_executor = OpportunityExecutor(self.config.opportunity_ranker)
        # Decision trace recorder — threads one awareness record through every
        # entry-pipeline stage so each component sees (and can challenge) the
        # others' verdicts. Additive: records decisions, never changes them.
        self._trace_recorder = DecisionTraceRecorder(self.config.decision_trace)
        # Orchestrator — the round table. Collects every stage's evidence for an
        # entry and folds it into ONE bounded graded size multiplier (a dimmer,
        # not a kill switch): weak dimensions size the trade down, only physics
        # (handled by the existing risk gates) can veto. Records every proposal.
        self._orchestrator = Orchestrator(self.config.orchestrator)
        # Live-management state for the orchestrator round table on OPEN trades:
        #   * entry-health snapshot (the graded evidence baseline captured when a
        #     trade was opened) keyed by broker order id — lets management compare
        #     "then vs now" for thesis-integrity / situation-shift.
        #   * per-position management cycle counter + last-evaluated cycle, for the
        #     min-cycles-before-management and cooldown pacing.
        self._entry_health_snapshot: dict[str, dict] = {}
        self._mgmt_cycle_count: dict[str, int] = {}
        self._mgmt_last_eval_cycle: dict[str, int] = {}
        # Outcome feedback — closes the loop: links each placed trade's realised
        # R back to the modules / opportunity that drove it, for per-module
        # accuracy. Observational only; never changes a live decision.
        self._outcome_feedback = OutcomeFeedback(self.config.outcome_feedback)
        # ── Universal signal ledger + emitter feedback (observational) ────────
        # Records EVERY module's directional read each cycle (before any gate),
        # grades whether price actually moved the predicted way, and lets each
        # emitter ask how it is doing — traded vs blocked — so a later phase can
        # re-weight votes and detect over-filtering gates. Dormant unless the
        # SignalLedgerConfig flags are turned on; never changes a live decision.
        self._signal_ledger = None
        self._emitter_feedback = None
        try:
            sl_cfg = self.config.signal_ledger
            if getattr(sl_cfg, "signal_ledger_enabled", False):
                from adaptive.signal_ledger import SignalLedger
                from adaptive.emitter_feedback import EmitterFeedbackService

                self._signal_ledger = SignalLedger(
                    db_path=sl_cfg.signal_ledger_db_path,
                    grading_delay_minutes=sl_cfg.signal_grading_delay_minutes,
                    check_intervals=list(sl_cfg.signal_grading_check_intervals),
                    min_move_pct=sl_cfg.signal_min_move_pct,
                    accuracy_lookback=getattr(sl_cfg, "accuracy_lookback", 100),
                )
                if getattr(sl_cfg, "emitter_feedback_enabled", False):
                    self._emitter_feedback = EmitterFeedbackService(self._signal_ledger)
                logger.info(
                    "[signal-ledger] enabled (grading={}, db={})",
                    bool(sl_cfg.signal_grading_enabled), sl_cfg.signal_ledger_db_path,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[signal-ledger] init failed, disabled: {}", exc)
            self._signal_ledger = None
            self._emitter_feedback = None
        # ── Vote calibrator (learning layer #6) ──────────────────────────
        # Reads each module's graded accuracy (via the read-only EmitterFeedback
        # service) and turns it into a consensus-vote weight multiplier centred
        # on 1.0 — accurate modules vote louder, noisy ones softer. Wired into
        # the scanner so it scales the static ConsensusConfig weights. Inert
        # unless VoteCalibratorConfig.vote_calibration_enabled is on; a build
        # failure leaves it None and the scanner uses the static weights.
        self._vote_calibrator = None
        try:
            from adaptive.vote_calibrator import VoteCalibrator

            self._vote_calibrator = VoteCalibrator(
                self.config.vote_calibrator,
                emitter_feedback=self._emitter_feedback,
            )
            if getattr(self.config.vote_calibrator, "vote_calibration_enabled", False):
                logger.info(
                    "[vote-calibrator] enabled (method={}, feedback={})",
                    self.config.vote_calibrator.vote_weight_method,
                    self._emitter_feedback is not None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vote-calibrator] init failed, disabled: {}", exc)
            self._vote_calibrator = None
        # ── Module governor (L3 — shadow mode) ───────────────────────────
        # Watches each module's graded accuracy (via the read-only
        # EmitterFeedback service) and moves a struggling module into SHADOW —
        # it still runs and is still graded, but its vote weight is forced to
        # 0.0 so it cannot influence a live decision. Recovers → ACTIVE; stays
        # poor → DISABLED. Wired into the scanner so suppression is enforced in
        # the consensus path. Inert unless ModuleGovernorConfig.module_governor_
        # enabled is on; a build failure leaves it None (no shadowing).
        self._module_governor = None
        try:
            mg_cfg = getattr(self.config, "module_governor", None)
            if mg_cfg is not None and getattr(mg_cfg, "module_governor_enabled", False):
                from adaptive.module_governor import ModuleGovernor

                self._module_governor = ModuleGovernor(
                    mg_cfg,
                    emitter_feedback=self._emitter_feedback,
                    db_path=getattr(mg_cfg, "db_path", None),
                )
                logger.info(
                    "[module-governor] enabled (feedback={})",
                    self._emitter_feedback is not None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[module-governor] init failed, disabled: {}", exc)
            self._module_governor = None
        # ── Post-close price tracker (MFE/MAE attribution) ───────────────
        # Schedules forward price checks after every close to separate
        # entry-signal quality from management quality. Observational only —
        # the close/scan hooks below already call record_close /
        # process_pending_checks; it just needs to be instantiated. Fail-safe:
        # a construction error leaves it None and the guarded hooks no-op.
        self._post_close_tracker = None
        try:
            self._post_close_tracker = PostCloseTracker(
                db_path="data/post_close_tracker.db",
            )
            logger.info(
                "[post_close] tracker enabled (pending={})",
                self._post_close_tracker.pending_count,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[post_close] init failed, disabled: {}", exc)
            self._post_close_tracker = None
        # ── Counterfactual attribution engine (L4) ──────────────────────
        # Holds the exact vote panel + consensus config that opened each trade,
        # then replays that math leaving one module out at a time to measure
        # each module's MARGINAL contribution to the decisions taken. Purely
        # analytical — never changes a live decision. Dormant unless
        # CounterfactualConfig.counterfactual_enabled is on; a build failure
        # leaves it None and the guarded capture/complete hooks no-op.
        self._counterfactual = None
        try:
            cf_cfg = getattr(self.config, "counterfactual", None)
            if cf_cfg is not None and getattr(cf_cfg, "counterfactual_enabled", False):
                from adaptive.counterfactual import CounterfactualEngine

                self._counterfactual = CounterfactualEngine(
                    db_path=cf_cfg.counterfactual_db_path,
                    enabled=True,
                    attribution_lookback=cf_cfg.attribution_lookback,
                    attribution_interval=cf_cfg.attribution_interval,
                    min_trades_for_attribution=cf_cfg.min_trades_for_attribution,
                )
                logger.info(
                    "[counterfactual] engine enabled (lookback={}, interval={})",
                    cf_cfg.attribution_lookback, cf_cfg.attribution_interval,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[counterfactual] init failed, disabled: {}", exc)
            self._counterfactual = None
        # L5 — self-evolution / discovery engines (all read the counterfactual
        # closed-trade snapshots).  Each is gated by its own flag and left None
        # on any build failure so the guarded hooks simply no-op.
        self._init_evolution_engines()
        self.scheduler = ScanScheduler(config=self.config)
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
        self.drawdown = DrawdownGuard(
            rolling_window_days=risk_cfg.drawdown_rolling_window_days,
        )
        # The EntryEngine has its own DrawdownGuard that is never fed trade
        # results, so its risk_pct / score-floor gate would stay permanently at
        # NORMAL. Share the loop's guard (EntryEngine only READS it — it never
        # registers results — so there is no double-counting) so its sizing /
        # gating reflects the real drawdown mode.
        try:
            self.entry_engine.drawdown = self.drawdown
        except Exception as exc:
            logger.debug("[init] entry_engine drawdown share failed: {}", exc)
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
        self.ml = MLAdapter(config=self.config)
        # Wire the live PairLearner into the scanner so the opportunity ranker's
        # adaptive win-rate provider (learning layer #1) can read observed
        # per-pair win rates. Inert unless the ranker flag is on.
        try:
            self.scanner.set_pair_learner(self.ml.pair_learner)
        except Exception as exc:
            logger.warning("[main] could not wire PairLearner into scanner: {}", exc)
        # Wire the VoteCalibrator into the scanner so module votes are weighted
        # by their graded accuracy (learning layer #6). Inert unless the
        # vote_calibration flag is on; the scanner falls back to static weights.
        try:
            if self._vote_calibrator is not None:
                self.scanner.set_vote_calibrator(self._vote_calibrator)
        except Exception as exc:
            logger.warning("[main] could not wire VoteCalibrator into scanner: {}", exc)
        # Wire the ModuleGovernor into the scanner so a shadowed/disabled
        # module's vote is suppressed (weight 0.0) in the consensus path.
        # Inert unless the module_governor flag is on.
        try:
            if self._module_governor is not None:
                self.scanner.set_module_governor(self._module_governor)
        except Exception as exc:
            logger.warning("[main] could not wire ModuleGovernor into scanner: {}", exc)
        # Wire the post-close MFE/MAE tracker into the PairLearner so per-pair
        # learning can split entry quality from management quality (read-only;
        # only blends into sizing when the continuous split flag is on).
        try:
            if self._post_close_tracker is not None and hasattr(
                self.ml.pair_learner, "set_post_close_tracker"
            ):
                self.ml.pair_learner.set_post_close_tracker(self._post_close_tracker)
        except Exception as exc:
            logger.warning("[main] could not wire PostCloseTracker into PairLearner: {}", exc)
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
            structure_exit_tf_alignment_enabled=getattr(self.config.risk, "structure_exit_tf_alignment_enabled", False),
            structure_exit_tf_alignment_defer=getattr(self.config.risk, "structure_exit_tf_alignment_defer", 0.5),
            heat_trail_tighten_enabled=self.config.risk.heat_trail_tighten_enabled,
            heat_trail_factor_defensive=self.config.risk.heat_trail_factor_defensive,
            heat_trail_factor_reducing=self.config.risk.heat_trail_factor_reducing,
            heat_trail_factor_emergency=self.config.risk.heat_trail_factor_emergency,
        )
        self._journal_loop = asyncio.new_event_loop()
        self.position_store = PositionStore()
        self._shadow_store = ShadowStore()
        # Shadow contracts (counterfactual outcomes for rejected setups) are
        # resolved on a periodic cadence so the pipeline actually runs in
        # production instead of leaving contracts PENDING forever.
        self._shadow_resolve_interval_seconds = 900
        self._last_shadow_resolve_time = 0.0
        # Un-executed (PENDING) shadow contracts older than this are discarded
        # rather than replayed forever — keeps the backlog/ DB bounded.
        self._shadow_max_pending_age_seconds = 3 * 86400
        # ── Full-fidelity live shadow resolution ─────────────────────────
        # A dedicated *paper* TradeManager (same class/config as the real one)
        # so rejected-setup contracts are resolved on the LIVE feed using the
        # same management logic — never historical CSVs. Kept separate from the
        # real trade_manager so paper trades never touch broker reconciliation.
        self._shadow_tm = TradeManager(
            partial_close_ratio=0.5,
            breakeven_buffer_pips=2.0,
            tp_adjust_enabled=self.config.risk.tp_adjust_enabled,
            tp3_ladder_enabled=self.config.risk.tp3_ladder_enabled,
            tp3_r_multiple=self.config.risk.tp3_r_multiple,
            tp3_close_ratio=self.config.risk.tp3_close_ratio,
            breakeven_min_profit_r=self.config.risk.breakeven_min_profit_r,
            trailing_swing_lookback=self.config.risk.trailing_swing_lookback,
            structure_exit_tf_alignment_enabled=getattr(self.config.risk, "structure_exit_tf_alignment_enabled", False),
            structure_exit_tf_alignment_defer=getattr(self.config.risk, "structure_exit_tf_alignment_defer", 0.5),
        )
        self._active_shadows: dict = {}      # contract_id -> paper position namespace
        self._max_active_shadows = 20        # bound live re-scan cost
        # Shadow-outcome → quality-gate auto-tuner: loosens/tightens tunable
        # gate thresholds within bounded envelopes based on whether the gate's
        # rejected setups would have won (learned, not hardcoded).
        self._gate_tuner = GateTuner()
        self._last_gate_tune_time = 0.0
        self._gate_tune_interval_seconds = 6 * 3600
        # Latest per-gate counterfactual summary (observability; populated from
        # shadow outcomes during gate calibration, surfaced on the dashboard).
        self._last_gate_counterfactuals: dict = {}
        # Let the entry engine read the tuner's learned entry-score offset.
        try:
            self.entry_engine.gate_tuner = self._gate_tuner
        except Exception as exc:
            logger.debug("[init] entry_engine gate-tuner share failed: {}", exc)

        self.watchdog = HealthWatchdog()
        self.maintenance = DailyMaintenance()
        self._scan_breaker = CircuitBreaker("scan", failure_threshold=5, cooldown_seconds=300)
        self._execution_breaker = CircuitBreaker("execution", failure_threshold=3, cooldown_seconds=600)
        self._health_check_interval = 10
        # Track event-pipeline loss so silent audit-log drops become visible.
        self._last_event_drop_count = 0
        self._last_sink_error_count = 0

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
        self._absolute_be_protected: set[str] = set()          # oids profit-locked to BE on absolute floor
        self._last_market_data: dict = {}                       # most recent scan market data cache
        self._d1_cache: dict[str, pd.DataFrame] = {}             # D1 data cache (changes once/day)
        self._d1_cache_time: datetime | None = None              # when D1 cache was last refreshed
        self._last_slot_blocked_candidate: dict | None = None    # best foregone candidate when slots full (F4)
        self._last_skipped_state: dict[str, tuple[str, int]] = {}  # symbol → (status, score) for emit-on-change
        self._last_known_balance: float = 0.0
        # P3: last Decision Engine verdict per oid — lets the tick-level
        # TradeManager exit defer to a strategic HOLD/SCALE_IN.
        self._last_decision_action: dict[str, Action] = {}
        # Timestamp of each verdict so the tick-level exit ignores stale ones.
        self._last_decision_action_time: dict[str, datetime] = {}
        # A verdict older than this many seconds no longer defers exits.
        self._decision_verdict_max_age_seconds: float = 600.0
        # PR6/P0: positions running on legacy fallback because the strategic
        # engine raised — oid → consecutive degraded cycles. Escalates to a
        # forced SL→BE / CRITICAL alarm after _degraded_management_escalate_cycles.
        self._degraded_management: dict[str, int] = {}
        self._degraded_management_escalate_cycles: int = 3
        # P5: per-pair cooldown after a breakeven stop-out (symbol → datetime until).
        self._be_stop_cooldown: dict[str, datetime] = {}
        # PR10: consecutive management cycles the fast-evidence cluster (momentum
        # + M1 alignment) has opposed each open position — oid → streak. Feeds
        # the DecisionEngine's fast-opposition decay and the status snapshot.
        self._fast_opposition_streak: dict[str, int] = {}

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
        self._situation_engine = SituationEngine(
            adopted_observation_minutes=getattr(dcfg, "adopted_observation_minutes", 10.0),
        )
        self._decision_engine = DecisionEngine(
            DecisionWeights(
                conviction_htf=dcfg.conviction_htf_weight,
                conviction_structure=dcfg.conviction_structure_weight,
                conviction_momentum=dcfg.conviction_momentum_weight,
                conviction_confidence=dcfg.conviction_confidence_weight,
                enter_htf=dcfg.enter_htf_coeff,
                enter_structure=dcfg.enter_structure_coeff,
                enter_momentum=dcfg.enter_momentum_coeff,
                skip_htf=dcfg.skip_htf_coeff,
                skip_momentum=dcfg.skip_momentum_coeff,
            ),
            regime_weighting_enabled=dcfg.regime_weighting_enabled,
            regime_ranging_htf_scale=dcfg.regime_ranging_htf_scale,
            reversal_enabled=dcfg.reversal_trades_enabled,
            reversal_min_momentum=dcfg.reversal_min_momentum,
            reversal_required_evidence=dcfg.reversal_required_evidence,
            reversal_size_multiplier=dcfg.reversal_size_multiplier,
            reversal_no_evidence_skip_penalty=dcfg.reversal_no_evidence_skip_penalty,
            reversal_weighted_evidence=getattr(dcfg, "reversal_weighted_evidence", False),
            reversal_required_strength=getattr(dcfg, "reversal_required_strength", 2.0),
            reversal_momentum_full=getattr(dcfg, "reversal_momentum_full", 0.6),
            htf_aligned_size_bonus=dcfg.htf_aligned_size_bonus,
            htf_aligned_threshold=dcfg.htf_aligned_threshold,
            scalp_htf_scale=self.config.opportunity_ranker.scalp_htf_penalty_scale,
            swing_htf_scale=self.config.opportunity_ranker.swing_htf_penalty_scale,
            mixed_htf_scale=self.config.opportunity_ranker.mixed_htf_penalty_scale,
            thesis_secure_enabled=dcfg.thesis_secure_enabled,
            thesis_secure_min_profit_usd=dcfg.thesis_secure_min_profit_usd,
            thesis_secure_min_profit_pips=dcfg.thesis_secure_min_profit_pips,
            thesis_deterioration_threshold=dcfg.thesis_deterioration_threshold,
            thesis_close_threshold=dcfg.thesis_close_threshold,
            thesis_healthy_structure=dcfg.thesis_healthy_structure,
            thesis_healthy_momentum=dcfg.thesis_healthy_momentum,
            thesis_lock_fraction=dcfg.thesis_lock_fraction,
            thesis_struct_ref=dcfg.thesis_struct_ref,
            thesis_conviction_cycles=dcfg.thesis_conviction_cycles,
            thesis_conviction_drop=dcfg.thesis_conviction_drop,
            thesis_conviction_full_drop=dcfg.thesis_conviction_full_drop,
            oq_eq_decay_enabled=dcfg.oq_eq_decay_enabled,
            oq_floor=dcfg.oq_floor,
            eq_floor=dcfg.eq_floor,
            oq_decay_significant=dcfg.oq_decay_significant,
            fast_opposition_decay_enabled=dcfg.fast_opposition_decay_enabled,
            fast_opposition_min_streak=dcfg.fast_opposition_min_streak,
            fast_opposition_max_streak=dcfg.fast_opposition_max_streak,
            fast_opposition_decay_weight=dcfg.fast_opposition_decay_weight,
            fast_opposition_profit_threshold=dcfg.fast_opposition_profit_threshold,
            # #5: read the per-timeframe alignment vector (not just the collapsed
            # scalar) in enter/skip scoring when the orchestrator is the live
            # sizer, so a split HTF stack is no longer hidden by averaging.
            tf_conflict_aware=bool(getattr(self.config.orchestrator, "enabled", False)),
            # #6 — soften the enter/skip binary into a dimmer, but ONLY when the
            # orchestrator round table is enabled to make the final sizing call.
            # A mildly-negative margin then flows through (carrying a bounded
            # quality multiplier) instead of hard-killing the setup upstream of
            # the orchestrator; a margin below the safety floor still hard-SKIPs.
            soften_gate=bool(
                getattr(self.config.orchestrator, "enabled", False)
                and getattr(self.config.orchestrator, "soften_de_gate", False)
            ),
            gate_safety_margin=float(getattr(self.config.orchestrator, "de_safety_margin", -1.0)),
            gate_quality_floor=float(getattr(self.config.orchestrator, "de_gate_quality_floor", 0.15)),
            conviction_size_min=float(getattr(dcfg, "conviction_size_min", 0.5)),
            conviction_size_max=float(getattr(dcfg, "conviction_size_max", 1.5)),
            market_mode_threshold=float(getattr(dcfg, "market_mode_threshold", 0.40)),
            adopted_observation_minutes=float(getattr(dcfg, "adopted_observation_minutes", 10.0)),
        )
        # #24 — when the orchestrator round table is the live sizer, the risk
        # governor's entry review stops hard-vetoing on the first analytical
        # breach (heat / spread / R:R) and instead hands through a graded risk
        # multiplier the orchestrator sizes by; physics (position limits) stay a
        # hard veto. Inert (legacy first-breach veto) when the orchestrator is
        # off or accumulate_risk is disabled.
        _orch_cfg_gov = getattr(self.config, "orchestrator", None)
        _graded_risk = bool(
            _orch_cfg_gov is not None
            and getattr(_orch_cfg_gov, "enabled", False)
            and getattr(_orch_cfg_gov, "accumulate_risk", False)
        )
        self._risk_governor = (
            RiskGovernor(
                graded_risk=_graded_risk,
                risk_floor=float(getattr(_orch_cfg_gov, "risk_multiplier_floor", 0.15)),
            )
            if dcfg.governor_enabled else None
        )
        self._decision_journal = DecisionJournal(dcfg.journal_dir) if dcfg.journal_enabled else None

        # ── Trade Planner — coordinator between advisors and execution ───
        pcfg = getattr(self.config, "planner", None) or PlannerConfig()
        # Prefer a calibrated config persisted from a previous run.
        persisted = PlannerConfig.load()
        # Only adopt the persisted config when the feature is enabled in code config.
        planner_cfg = persisted if pcfg.enabled else pcfg
        planner_cfg.enabled = pcfg.enabled
        self._planner_enabled = planner_cfg.enabled
        # Phase 9: when the orchestrator is the live sizer, soften the planner's
        # conviction floor into a dimmer (it hands low-conviction setups through
        # as ENTER carrying a quality multiplier instead of SKIP — the governor
        # veto stays hard). Inert when the orchestrator is off.
        _orch_cfg_init = getattr(self.config, "orchestrator", None)
        if (
            _orch_cfg_init is not None
            and getattr(_orch_cfg_init, "enabled", False)
            and getattr(_orch_cfg_init, "soften_planner_gates", False)
        ):
            planner_cfg.soften_gates = True
            planner_cfg.gate_quality_floor = float(
                getattr(_orch_cfg_init, "gate_quality_floor", 0.15)
            )
            # #8: make the conviction gate dispersion-aware so a split advisor
            # panel reads below a united-but-mediocre one (the mean hides that).
            planner_cfg.dispersion_aware_agreement = True
        self._planner = TradePlanner(planner_cfg)
        self._outcome_logger = OutcomeLogger(planner_cfg.journal_path) if planner_cfg.enabled else None
        self._calibrator = Calibrator(planner_cfg) if planner_cfg.enabled else None

        # ── Portfolio Governor — portfolio-level risk limits ─────────────
        gcfg = getattr(self.config, "governor", None)
        # #24 — graded analytical concentration: when the orchestrator is the
        # live sizer, the portfolio governor's currency / sector / correlated
        # limits become a graded dimmer (verdict stays allowed, carries a risk
        # multiplier) instead of a first-breach hard block; physics (max
        # positions) and the daily-loss halt stay hard. Inert otherwise.
        if gcfg is not None and _graded_risk:
            try:
                gcfg.graded_exposure = True
                gcfg.risk_multiplier_floor = float(
                    getattr(_orch_cfg_gov, "risk_multiplier_floor", 0.15)
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[Governor] graded_exposure wiring skipped: {}", exc)
        self._governor = PortfolioGovernor(gcfg) if (gcfg is None or gcfg.enabled) else None
        if self._governor is not None:
            self._planner.set_governor(self._governor)

        # ── Per-account risk silos ───────────────────────────────────────
        # Each broker/account ($5, $10, MT5, Deriv, …) is managed independently:
        # its own balance, daily-loss cap and heat. A trade is sized/gated only
        # against its own account so the accounts never contaminate each other.
        self._account_risk = AccountRiskManager(
            daily_loss_cap_pct=getattr(gcfg, "daily_loss_cap_pct", 3.0) if gcfg else 3.0,
            daily_loss_recovery_pct=getattr(gcfg, "daily_loss_recovery_pct", 1.5) if gcfg else 1.5,
            heat_block_pct=getattr(self.config.risk, "portfolio_heat_block_pct", 2.0),
            daily_loss_flatten_pct=getattr(self.config.risk, "daily_loss_flatten_pct", 5.0),
        )
        self._account_key_cache: dict[str, str] = {}

        # ── Data backup ──────────────────────────────────────────────────
        self._last_data_backup_ts: float = 0.0

        # ── Tuner Agent — central auto-tuning coordinator ────────────────
        # One place that schedules, orders, validates, audits, and rolls back
        # every learner/tuner. When disabled (default) the legacy scattered
        # tuning triggers below run exactly as before; when enabled, those are
        # routed through the agent instead (see _check_daily_reset / scan loop /
        # _record_closed_trade). Instantiated last so all components exist.
        self._tuner_agent = None
        self._tuner_trade_cache: Optional[dict] = None
        self._tuner_trade_cache_gen: int = -1
        self._tuner_run_gen: int = 0
        try:
            self._setup_tuner_agent()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tuner-agent] setup skipped: {}", exc)
            self._tuner_agent = None

    # ── Tuner Agent wiring ────────────────────────────────────────────────

    def _init_evolution_engines(self) -> None:
        """Instantiate the L5 evolution / discovery engines (all gated, all
        reading the counterfactual closed-trade snapshots).  Any build failure
        leaves the attribute None so the guarded tuner hooks simply no-op."""
        self._param_evolver = None
        self._module_interaction = None
        self._signal_discovery = None
        if self._counterfactual is None:
            return  # they have nothing to read without the snapshot store

        pe_cfg = getattr(self.config, "param_evolution", None)
        if pe_cfg is not None and getattr(pe_cfg, "param_evolution_enabled", False):
            try:
                from adaptive.param_evolution import ParameterEvolver

                self._param_evolver = ParameterEvolver(
                    self._counterfactual,
                    enabled=True,
                    db_path=pe_cfg.param_evolution_db_path,
                    current_values_provider=self._param_evolution_current_values,
                    promote_callback=self._param_evolution_apply,
                    candidates_per_param=pe_cfg.candidates_per_param,
                    replay_lookback=pe_cfg.replay_lookback,
                    shadow_validation_trades=pe_cfg.shadow_validation_trades,
                    significance_threshold=pe_cfg.significance_threshold,
                    walk_forward_split=pe_cfg.walk_forward_split,
                    evolution_cooldown_hours=pe_cfg.evolution_cooldown_hours,
                    max_concurrent_shadows=pe_cfg.max_concurrent_shadows,
                    rollback_window=pe_cfg.rollback_window,
                    min_replay_trades=pe_cfg.min_replay_trades,
                )
                logger.info("[param-evolution] engine enabled (L5a)")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[param-evolution] init failed, disabled: {}", exc)
                self._param_evolver = None

        mi_cfg = getattr(self.config, "module_interaction", None)
        if mi_cfg is not None and getattr(mi_cfg, "module_interaction_enabled", False):
            try:
                from adaptive.module_interaction import ModuleInteractionEngine

                self._module_interaction = ModuleInteractionEngine(
                    self._counterfactual,
                    enabled=True,
                    db_path=mi_cfg.module_interaction_db_path,
                    lookback=mi_cfg.interaction_lookback,
                    interval=mi_cfg.interaction_interval,
                    min_trades=mi_cfg.min_trades_for_interaction,
                    significance_r=mi_cfg.interaction_significance_r,
                    max_modules=mi_cfg.max_modules,
                )
                logger.info("[module-interaction] engine enabled (L5b)")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[module-interaction] init failed, disabled: {}", exc)
                self._module_interaction = None

        sd_cfg = getattr(self.config, "signal_discovery", None)
        if sd_cfg is not None and getattr(sd_cfg, "signal_discovery_enabled", False):
            try:
                from adaptive.signal_discovery import SignalDiscoveryEngine

                self._signal_discovery = SignalDiscoveryEngine(
                    self._counterfactual,
                    enabled=True,
                    db_path=sd_cfg.signal_discovery_db_path,
                    lookback=sd_cfg.discovery_lookback,
                    interval=sd_cfg.discovery_interval,
                    min_trades=sd_cfg.min_trades_for_discovery,
                    min_support=sd_cfg.min_rule_support,
                    max_conditions=sd_cfg.max_rule_conditions,
                    min_edge_r=sd_cfg.min_edge_r,
                    walk_forward_split=sd_cfg.discovery_walk_forward_split,
                )
                logger.info("[signal-discovery] engine enabled (L5c)")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[signal-discovery] init failed, disabled: {}", exc)
                self._signal_discovery = None

    def _param_evolution_current_values(self) -> dict:
        """Live values of the evolvable consensus / ranker thresholds, so the
        evolver explores around where the system actually sits today."""
        cons = getattr(self.config, "consensus", None)
        rank = getattr(self.config, "opportunity_ranker", None)
        out: dict = {}
        if cons is not None:
            out["min_net_score"] = float(getattr(cons, "min_net_score", 1.5))
            out["min_agreement"] = float(getattr(cons, "min_agreement", 0.55))
            out["min_contributors"] = int(getattr(cons, "min_contributors", 2))
        if rank is not None:
            out["min_expected_value"] = float(getattr(rank, "min_expected_value", 0.0))
            out["min_cluster_confidence"] = float(getattr(rank, "min_cluster_confidence", 0.0))
            out["min_cluster_contributors"] = int(getattr(rank, "min_cluster_contributors", 1))
        return out

    def _param_evolution_apply(self, name: str, location: str, value: float) -> bool:
        """Apply a promoted parameter value to live config.  Gated by the
        evolver's own validation/shadow proof; returns True on success.  Int
        params are rounded; everything is exception-safe."""
        from adaptive.param_evolution import LOC_THRESHOLD

        try:
            target_cfg = self.config.consensus if location == LOC_THRESHOLD else self.config.opportunity_ranker
            if not hasattr(target_cfg, name):
                logger.warning("[param-evolution] unknown param '{}' — not applied", name)
                return False
            int_params = {"min_contributors", "min_cluster_contributors"}
            new_value = int(round(value)) if name in int_params else float(value)
            old_value = getattr(target_cfg, name)
            setattr(target_cfg, name, new_value)
            logger.info(
                "[param-evolution] APPLIED {}.{}: {} -> {}",
                type(target_cfg).__name__, name, old_value, new_value,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[param-evolution] apply '{}' failed: {}", name, exc)
            return False

    def _setup_tuner_agent(self) -> None:
        """Instantiate the TunerAgent and register every tunable (gated)."""
        tcfg = getattr(self.config, "tuner_agent", None)
        if tcfg is None or not getattr(tcfg, "enabled", False):
            return
        from adaptive.tuner_agent import TunerAgent
        from adaptive.tunable_adapters import (
            ScoreOptimizerTunable,
            RegimeLearnerTunable,
            PairLearnerTunable,
            SessionLearnerTunable,
            EVEstimatorTunable,
            GateTunerTunable,
            PlannerCalibratorTunable,
            SignalLedgerTunable,
            PostCloseTrackerTunable,
            VoteCalibratorTunable,
            CounterfactualTunable,
            ModuleGovernorTunable,
            ParameterEvolverTunable,
            ModuleInteractionTunable,
            SignalDiscoveryTunable,
            ConsumerTunable,
        )

        agent = TunerAgent(
            enabled=True,
            audit_db_path=tcfg.audit_db_path,
            max_tune_duration_seconds=tcfg.max_tune_duration_seconds,
            max_consecutive_failures=tcfg.max_consecutive_failures,
            log_all_skips=tcfg.log_all_skips,
        )

        agent.register(ScoreOptimizerTunable(
            self.ml.optimizer,
            self._tuner_ml_trades_provider,
            on_update=self._tuner_apply_scoring_weights,
        ))
        agent.register(RegimeLearnerTunable(
            self.ml.regime_learner, self._tuner_ml_trades_provider,
        ))
        agent.register(PairLearnerTunable(
            self.ml.pair_learner, self._tuner_ml_trades_provider,
        ))
        agent.register(SessionLearnerTunable(
            self.ml.session_learner, self._tuner_ml_trades_provider,
        ))
        agent.register(EVEstimatorTunable(
            getattr(self.scanner, "_ev_estimator", None),
            refresh=self._tuner_refresh_ev_history,
        ))
        agent.register(GateTunerTunable(
            self._gate_tuner,
            lambda: self._shadow_store.get_outcomes_by_gate(),
        ))
        if self._calibrator is not None:
            agent.register(PlannerCalibratorTunable(
                self._calibrator,
                self._tuner_completed_plans_provider,
                planner=self._planner,
                planner_enabled=self._planner_enabled,
            ))
        if self._signal_ledger is not None:
            agent.register(SignalLedgerTunable(
                self._signal_ledger, prices_provider=None,
            ))
        # Post-close MFE/MAE sampling (per-scan). Data source = live platforms.
        if self._post_close_tracker is not None:
            agent.register(PostCloseTrackerTunable(
                self._post_close_tracker,
                data_source_provider=lambda: self.platforms,
            ))
        # Vote calibrator (periodic) — consensus weights from graded accuracy.
        # Registered as an active tunable only when its flag is on; otherwise it
        # registers read-only below so the agent still sees it.
        vc_cfg = getattr(self.config, "vote_calibrator", None)
        vc_enabled = bool(getattr(vc_cfg, "vote_calibration_enabled", False))
        if self._vote_calibrator is not None and vc_enabled:
            agent.register(VoteCalibratorTunable(
                self._vote_calibrator,
                min_interval=float(
                    getattr(vc_cfg, "vote_calibration_min_interval_seconds", 3600.0)
                ),
            ))
        # Module governor (periodic) — shadow/reactivate/disable from graded
        # accuracy. Registered as an active tunable only when its flag is on.
        mg_cfg = getattr(self.config, "module_governor", None)
        mg_enabled = bool(getattr(mg_cfg, "module_governor_enabled", False))
        if self._module_governor is not None and mg_enabled:
            agent.register(ModuleGovernorTunable(self._module_governor))

        # Counterfactual attribution (trade-close batch) — periodic leave-one-out
        # module attribution. Pure analysis; recomputes on its own cadence.
        if self._counterfactual is not None:
            agent.register(CounterfactualTunable(
                self._counterfactual,
                min_trades=int(
                    getattr(
                        getattr(self.config, "counterfactual", None),
                        "min_trades_for_attribution", 50,
                    )
                ),
            ))

        # L5a — parameter evolution (periodic; depends on counterfactual).
        if self._param_evolver is not None:
            agent.register(ParameterEvolverTunable(
                self._param_evolver,
                min_trades=int(
                    getattr(
                        getattr(self.config, "param_evolution", None),
                        "min_replay_trades", 50,
                    )
                ),
            ))
        # L5b — module interaction discovery (trade-close batch).
        if self._module_interaction is not None:
            agent.register(ModuleInteractionTunable(
                self._module_interaction,
                min_trades=int(
                    getattr(
                        getattr(self.config, "module_interaction", None),
                        "min_trades_for_interaction", 50,
                    )
                ),
            ))
        # L5c — synthetic signal discovery (trade-close batch).
        if self._signal_discovery is not None:
            agent.register(SignalDiscoveryTunable(
                self._signal_discovery,
                min_trades=int(
                    getattr(
                        getattr(self.config, "signal_discovery", None),
                        "min_trades_for_discovery", 100,
                    )
                ),
            ))

        # ── Make the agent the single place the WHOLE system reports to ──────
        # Hard enforcement: the components that can self-tune get a reference so
        # any direct retrain/calibrate/grade call is blocked while the agent is
        # sole authority (it authorises only its own delegated calls).
        for _component in (
            self.ml, self._gate_tuner, self._calibrator, self._signal_ledger,
            self._vote_calibrator, self._module_governor,
        ):
            if _component is not None and hasattr(_component, "set_tuner_agent"):
                _component.set_tuner_agent(agent)

        # Consumers / observers (components 14-19): they don't self-tune — they
        # read tuned params or are dormant — but they register read-only so the
        # agent sees the whole system in get_system_tuning_status / validation.
        self._register_tuner_consumers(agent, ConsumerTunable)

        self._tuner_agent = agent
        logger.info(
            "🎛️ Tuner Agent ENABLED — coordinating {} tunables: {}",
            len(agent.registered_names), ", ".join(agent.registered_names),
        )
        # Startup validation: warn about any expected component that did not
        # register (flag off / wiring missing) and any invalid current params.
        try:
            agent.validate_registry()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] registry validation failed: {}", exc)

    def _register_tuner_consumers(self, agent, ConsumerTunable) -> None:
        """Register the read-only consumer/observer components (14-19) plus the
        coordinator-level reporters so every one of the 19 audited components is
        visible to the agent. Each entry exposes a current-config snapshot and
        is never auto-tuned. Fully guarded — a missing component is simply
        skipped, never fatal."""
        ml = getattr(self, "ml", None)

        def _reg(name, provider, *, dormant=False, note=""):
            try:
                agent.register(ConsumerTunable(name, provider, dormant=dormant, note=note))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[tuner-agent] consumer '{}' register skipped: {}", name, exc)

        # 1 — AdaptiveOptimizer (coordinator; sub-learners tuned individually).
        if ml is not None:
            _reg("adaptive_optimizer", lambda: {
                "recency_window_days": int(getattr(ml, "recency_window_days", 0) or 0),
                "recency_min_trades": int(getattr(ml, "recency_min_trades", 0) or 0),
                "losing_pattern_min_samples": int(getattr(ml, "losing_pattern_min_samples", 0) or 0),
                "losing_pattern_max_win_rate": float(getattr(ml, "losing_pattern_max_win_rate", 0.0) or 0.0),
            }, note="coordinator — sub-learners tuned individually")
            # 6 — TradeAnalyzer (losing-pattern source).
            analyzer = getattr(ml, "analyzer", None)
            if analyzer is not None:
                _reg("trade_analyzer", lambda: {
                    "cached_losing_patterns": len(getattr(ml, "_losing_patterns", []) or []),
                })

        # 9 — Win-rate provider (lives on the scanner; flag-gated).
        rc = getattr(self.config, "opportunity_ranker", None)
        _reg("win_rate_provider", lambda: {
            "enabled": bool(getattr(rc, "adaptive_win_rate_provider_enabled", False)),
            "built": getattr(self.scanner, "_win_rate_adapter", None) is not None,
        })

        # 11 — Emitter feedback (read-side service over the signal ledger).
        _reg("emitter_feedback", lambda: {
            "enabled": self._emitter_feedback is not None,
        })

        # 13b — Vote calibrator. Registered as an ACTIVE tunable above when its
        # flag is on; here we register it read-only when it is off (or unwired)
        # so the agent still sees it in get_system_tuning_status / validation.
        vc_cfg = getattr(self.config, "vote_calibrator", None)
        if not bool(getattr(vc_cfg, "vote_calibration_enabled", False)):
            _reg("vote_calibrator", lambda: {
                "enabled": False,
                "wired": self._vote_calibrator is not None,
                "method": str(getattr(vc_cfg, "vote_weight_method", "")),
            }, note="consumer — calibration disabled (static consensus weights)")

        # Module governor (L3 — shadow mode). Registered as an ACTIVE tunable
        # above when its flag is on; here we register it read-only when it is
        # off (or unwired) so the agent still sees it in the status / validation.
        mg_cfg = getattr(self.config, "module_governor", None)
        if not bool(getattr(mg_cfg, "module_governor_enabled", False)):
            _reg("module_governor", lambda: {
                "enabled": False,
                "wired": self._module_governor is not None,
            }, note="consumer — module governor disabled (no shadowing)")

        # 14 — RiskEngine.
        re = getattr(self, "risk_engine", None)
        if re is not None:
            _reg("risk_engine", lambda: {
                "mode": str(getattr(getattr(re, "drawdown_guard", None), "mode", "")),
                "balance": float(getattr(re, "balance", 0.0) or 0.0),
            })
            # 15 — PositionSizer (lives on the risk engine).
            ps = getattr(re, "position_sizer", None)
            if ps is not None:
                _reg("position_sizer", lambda: {
                    "max_risk_pct_per_trade": float(getattr(ps, "max_risk_pct_per_trade", 0.0) or 0.0),
                })

        # 16 — Orchestrator.
        orch = getattr(self, "_orchestrator", None)
        if orch is not None:
            _reg("orchestrator", lambda: {
                "enabled": bool(getattr(getattr(orch, "config", None), "enabled", False)),
                "apply_sizing": bool(getattr(getattr(orch, "config", None), "apply_sizing", False)),
            })

        # 17 — PortfolioGovernor.
        gov = getattr(self, "_governor", None)
        _reg("portfolio_governor", lambda: {
            "enabled": gov is not None,
        })

        # 18 — TradeManager.
        tm = getattr(self, "trade_manager", None)
        if tm is not None:
            _reg("trade_manager", lambda: {
                "open_trades": len(getattr(tm, "_trades", {}) or {}),
            })

        # 19 — RL stack (dormant until a trained checkpoint exists).
        _reg("rl_stack", lambda: {
            "bridge_present": getattr(self.scanner, "_rl", None) is not None,
        }, dormant=True, note="dormant — no trained checkpoint")

    def _tuner_agent_active(self) -> bool:
        return self._tuner_agent is not None and getattr(
            self.config.tuner_agent, "enabled", False
        )

    def _module_governor_active(self) -> bool:
        return self._module_governor is not None and getattr(
            getattr(self.config, "module_governor", None),
            "module_governor_enabled", False,
        )

    def _module_governor_evaluate(self) -> None:
        """Re-evaluate module shadow/reactivate/disable transitions.

        When the Tuner Agent is active it drives this through the registered
        ModuleGovernorTunable (periodic), so this only runs the governor
        directly when the agent is off — keeping a single source of truth and
        avoiding a double evaluation. Fully guarded; never blocks the loop.
        """
        if not self._module_governor_active() or self._tuner_agent_active():
            return
        try:
            self._module_governor.evaluate_transitions()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[module-governor] evaluate failed: {}", exc)

    def tuner_system_status(self) -> dict:
        """Whole-system tuning state for ops / the dashboard: every registered
        tunable, the expected components that did NOT register, and recent
        bypass attempts. Returns a disabled marker when the agent is off."""
        if self._tuner_agent is None:
            return {"agent_enabled": False, "is_sole_authority": False,
                    "registered_tunables": {}, "unregistered_expected": [],
                    "bypass_attempts": []}
        try:
            return self._tuner_agent.get_system_tuning_status()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] system status failed: {}", exc)
            return {"agent_enabled": self._tuner_agent_active(), "error": str(exc)}

    def _tuner_fetch_ml_trades(self) -> dict:
        """Fetch + tag-parse the journal once per agent run, cached by run gen.

        Returns ``{"raw": [...], "learner": [...]}`` where ``learner`` is the
        recency-windowed subset the AdaptiveOptimizer normally trains on. The
        cache makes the four ML adapters share one journal read per run.
        """
        if self._tuner_trade_cache_gen == self._tuner_run_gen and self._tuner_trade_cache is not None:
            return self._tuner_trade_cache
        raw_trades: list[dict] = []
        try:
            raw_trades = self._journal_loop.run_until_complete(
                self.journal.get_all_trades_as_dicts()
            ) or []
            for t in raw_trades:
                raw = t.pop("confluences_raw", [])
                if not isinstance(raw, list):
                    raw = []
                t["confluences_tags"] = _parse_confluence_tags(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] journal fetch failed: {}", exc)
            raw_trades = []
        try:
            learner_trades = MLAdapter._recent_trades(
                raw_trades, self.ml.recency_window_days, self.ml.recency_min_trades,
            )
        except Exception:  # noqa: BLE001
            learner_trades = raw_trades
        self._tuner_trade_cache = {"raw": raw_trades, "learner": learner_trades}
        self._tuner_trade_cache_gen = self._tuner_run_gen
        return self._tuner_trade_cache

    def _tuner_ml_trades_provider(self) -> list:
        return self._tuner_fetch_ml_trades().get("learner", [])

    def _tuner_completed_plans_provider(self) -> list:
        if self._outcome_logger is None:
            return []
        try:
            return self._outcome_logger.get_completed_trades(
                lookback=self._planner.config.calibration_lookback_trades
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] completed-plans fetch failed: {}", exc)
            return []

    def _tuner_refresh_ev_history(self) -> int:
        """Feed the latest trade history to the scanner's EVEstimator (faithful
        to the legacy retrain step) and return the snapshot size."""
        raw = self._tuner_fetch_ml_trades().get("raw", [])
        try:
            self.scanner._trade_history = raw[-500:] if len(raw) > 500 else list(raw)
            return len(self.scanner._trade_history)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] EV history refresh failed: {}", exc)
            return 0

    def _scanner_weight_payload(self, optimizer):
        """Build the scanner's weight payload from a ScoreOptimizer instance.

        Returns a per-class ``{class: weights_dict}`` payload (with a shared
        ``default``) when per-class mode is on, otherwise the legacy flat
        ``{factor: weight}`` dict. Every profile is envelope-clamped first.
        """
        from adaptive.score_optimizer import (
            ScoringWeights as _SW,
            ADAPTIVE_WEIGHT_ENVELOPE_PCT as _ENV_PCT,
            DEFAULT_CLASS as _DEF_CLS,
        )
        _base = _SW()
        if getattr(optimizer, "per_class", False):
            payload = {
                _DEF_CLS: optimizer.current_weights.clamped_to_envelope(_base, _ENV_PCT).as_dict()
            }
            for _cls, _w in (getattr(optimizer, "class_weights", {}) or {}).items():
                payload[_cls] = _w.clamped_to_envelope(_base, _ENV_PCT).as_dict()
            return payload
        return optimizer.current_weights.clamped_to_envelope(_base, _ENV_PCT).as_dict()

    def _tuner_apply_scoring_weights(self, weights) -> None:
        """Reload freshly optimised scoring weights into the live scanner —
        the same side effect the legacy _run_ml_optimization performed."""
        try:
            if not self.config.scoring.use_adaptive_scoring_weights:
                return
            # Rebuild from the optimizer instance so per-class profiles (not
            # just the global ``weights`` argument) propagate to the scanner.
            self.scanner._adaptive_weights = self._scanner_weight_payload(self.ml.optimizer)
            logger.info("[tuner-agent] live scanner scoring weights reloaded")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] scanner weight reload failed: {}", exc)

    def _tuner_make_context(self):
        from adaptive.tunable import TuneContext

        return TuneContext(
            total_trades=len(getattr(self.scanner, "_trade_history", []) or []),
            trades_since_last_tune=int(getattr(self.ml, "_trades_since_train", 0) or 0),
            seconds_since_last_tune=0.0,
        )

    def _tuner_run_trade_close(self) -> None:
        """Route trade-close tuning through the agent (ON_TRADE_CLOSE/BATCH +
        PERIODIC), then preserve the legacy losing-pattern cache refresh."""
        if not self._tuner_agent_active():
            return
        self._tuner_run_gen += 1
        self._tuner_trade_cache = None
        try:
            ctx = self._tuner_make_context()
            results = self._tuner_agent.on_trade_close(ctx)
            self._tuner_agent.on_periodic_tick(ctx)
            self._tuner_refresh_losing_patterns(results)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] trade-close run failed: {}", exc)

    def _tuner_refresh_losing_patterns(self, results) -> None:
        """Refresh AdaptiveOptimizer's losing-pattern cache when an ML learner
        actually ran — faithful to run_optimization, which the agent bypasses."""
        ml_names = {"score_optimizer", "regime_learner", "pair_learner", "session_learner"}
        ran = any(
            getattr(r, "tunable_name", "") in ml_names and not getattr(r, "skipped", True)
            for r in (results or [])
        )
        if not ran or self._tuner_trade_cache is None:
            return
        try:
            learner_trades = self._tuner_trade_cache.get("learner", [])
            self.ml._losing_patterns = self.ml.analyzer.get_losing_patterns(learner_trades)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] losing-pattern refresh failed: {}", exc)

    def _tuner_run_scan_cycle(self, market_data: dict) -> None:
        """Route per-scan-cycle tuning (signal grading) through the agent."""
        if not self._tuner_agent_active():
            return
        try:
            from adaptive.tunable import TuneContext

            prices: dict = {}
            for pair, frames in (market_data or {}).items():
                px = self._current_price_for(pair, {pair: frames})
                if px:
                    prices[pair] = px
            ctx = TuneContext(current_prices=prices)
            self._tuner_agent.on_scan_cycle(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tuner-agent] scan-cycle run failed: {}", exc)

    def _account_key(self, symbol: str) -> str:
        """Resolve the risk-silo key (broker + account login) for a symbol.

        Keyed by broker name AND account id so multiple MT5 brokers, multiple
        logins on the same broker, and Deriv are all separate silos. Cached
        once the account id is known (it is stable per symbol).
        """
        key = self._account_key_cache.get(symbol)
        if key:
            return key
        try:
            broker = self.platforms.get_broker_name(symbol) or "default"
        except Exception:
            broker = "default"
        try:
            acct_id = self.platforms.get_account_id(symbol) or ""
        except Exception:
            acct_id = ""
        key = f"{broker}:{acct_id}" if acct_id else broker
        # Only cache once the account id is resolved; otherwise keep retrying
        # so a not-yet-connected account doesn't get pinned to a broker-only key.
        if acct_id:
            self._account_key_cache[symbol] = key
        return key

    def _blocks_new_entry_near_weekend(self, symbol: str, now: datetime) -> bool:
        """True if opening a NEW position should be blocked because a
        weekend-closing instrument (FX/metals) is within the close buffer on
        Friday. 24/7 instruments (crypto, synthetics) are never blocked —
        holding fresh risk across the weekend gap is the thing we avoid."""
        if not getattr(self.config.risk, "weekend_protection_enabled", True):
            return False
        if now.weekday() != 4:  # Friday only
            return False
        sym = symbol.upper()
        try:
            from brain.currency_strength import CURRENCY_PAIRS
            weekend_closing = (
                sym in CURRENCY_PAIRS
                or sym in ("XAUUSD", "XAGUSD", "XBRUSD", "XTIUSD")
            )
        except Exception:
            weekend_closing = False
        if not weekend_closing:
            return False
        buf = int(getattr(self.config.risk, "weekend_close_buffer_minutes", 15))
        close_hour = int(getattr(self.config.risk, "friday_close_hour_utc", 21))
        close_dt = now.replace(hour=close_hour, minute=0, second=0, microsecond=0)
        return (close_dt - now).total_seconds() <= buf * 60


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

        self._log_management_configuration()

        self._install_signal_handlers()
        self._perform_startup_recovery()
        self._import_broker_history_once()

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

    def _import_broker_history_once(self) -> None:
        """Backfill the account's broker closed-trade history into the event
        store (reporting / equity reconstruction only — NOT fed to the learners).
        Idempotent by position_id, never blocks startup, gated by config."""
        if not getattr(self.config.risk, "broker_history_import_enabled", True):
            return
        try:
            from persistence.broker_history import import_broker_history
            lookback = int(getattr(self.config.risk, "broker_history_lookback_days", 365))
            store = get_event_store()
            for c in getattr(self.platforms, "mt5_connectors", []) or []:
                try:
                    if not c.is_connected():
                        continue
                except Exception:
                    continue
                acct = f"{getattr(c, '_broker_name', 'mt5')}:{getattr(c, '_login', 0)}"
                import_broker_history(c, store, account_key=acct, lookback_days=lookback)
        except Exception as exc:
            logger.warning("[startup] broker-history import skipped: {}", exc)

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
        _cycle_start_mono = _time.monotonic()
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

        # ── Post-close forward price checks ──────────────────────────────
        # Sample MFE/MAE for recently-closed trades when each check interval
        # comes due. Lightweight, fail-safe, and must never stall the cycle.
        try:
            tracker = getattr(self, "_post_close_tracker", None)
            if tracker is not None and tracker.pending_count:
                tracker.process_pending_checks(self.platforms)
        except Exception as exc:
            logger.debug("[post_close] pending check pass failed: {}", exc)

        self._log_periodic_management_status()

        if self.watchdog._cycles % self._health_check_interval == 0:
            report = self.watchdog.check_health()
            if not report.is_healthy:
                for w in report.warnings:
                    logger.warning("⚠️ HEALTH: {}", w)
            # Surface event-pipeline loss (dropped events / sink errors) so the
            # audit/analytics log silently losing data becomes visible.
            try:
                from persistence.event_sink import get_sink_error_count

                dropped = get_event_store().dropped_count
                sink_errs = get_sink_error_count()
                if dropped > self._last_event_drop_count or sink_errs > self._last_sink_error_count:
                    logger.warning(
                        "⚠️ EVENT PIPELINE — {} events dropped, {} sink errors (audit log loss)",
                        dropped, sink_errs,
                    )
                    self._add_warning(
                        "warning",
                        f"Event pipeline degraded: {dropped} dropped, {sink_errs} sink errors",
                    )
                    self._last_event_drop_count = dropped
                    self._last_sink_error_count = sink_errs
            except Exception as exc:
                logger.debug("[health] event-pipeline drop check failed: {}", exc)

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
                # Fail-safe: a thrown management pass must not silently skip a
                # whole cycle. Verify broker↔managed state is still sane; if
                # reconciliation also fails, make noise rather than continue
                # blind.
                try:
                    self._reconcile_positions()
                except Exception as recon_exc:
                    logger.critical(
                        "CRITICAL: management pass AND reconciliation both "
                        "failed — broker/position state may be unverified: {}",
                        recon_exc,
                    )

            cycle["positions_updated"] = len(self.managed_positions)
            cycle["positions_closed"] = closed_count

            # ── Full-fidelity live shadow resolution ─────────────────────
            # Advance rejected-setup paper trades one step on the live feed,
            # managed by the real engines (never historical CSVs). Fully
            # isolated + fail-safe: cannot affect real trading.
            try:
                self._advance_shadows(now)
            except Exception as exc:
                logger.debug("[shadow-live] advance cycle failed: {}", exc)

        # ── Periodic reconciliation heartbeat (H6) ───────────────────────
        if self._recovery_completed and self._last_reconcile_time is not None:
            elapsed = (now - self._last_reconcile_time).total_seconds()
            if elapsed >= self._reconcile_interval_seconds:
                try:
                    # Only advance the "last successful reconcile" clock when a
                    # platform was actually confirmed. Treating an unreachable-
                    # broker skip as success would mask a genuine reconcile
                    # outage from the emergency trigger (reconcile_age would
                    # never grow), defeating the safety net.
                    if self._reconcile_positions():
                        self._last_reconcile_time = datetime.now(timezone.utc)
                except Exception as exc:
                    logger.warning("Periodic reconciliation error: {}", exc)

        cycle["duration_ms"] = round((_time.monotonic() - _cycle_start_mono) * 1000.0, 1)
        self._record_cycle_timing(cycle["duration_ms"])
        return cycle

    def _record_cycle_timing(self, duration_ms: float) -> None:
        """Keep a small ring buffer of recent cycle durations for the dashboard
        performance panel. Never raises — pure observability."""
        try:
            buf = getattr(self, "_cycle_durations_ms", None)
            if buf is None:
                buf = self._cycle_durations_ms = []
            buf.append(float(duration_ms))
            if len(buf) > 200:
                del buf[:-200]
        except Exception:
            pass

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
        self._persist_scanner_rejections(report)
        # Record every module's directional read this cycle (before gates) and
        # grade prior signals against current prices. Observational — no-op
        # unless the signal ledger is enabled.
        self._record_scan_signals(report, market_data)
        if self._tuner_agent_active():
            # Signal grading is coordinated by the Tuner Agent (PER_SCAN_CYCLE).
            self._tuner_run_scan_cycle(market_data)
        else:
            self._run_signal_grading(market_data)

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
        # ── Capacity-aware dispatch cut (collapse #13 / #15) ──────────────
        # Legacy: a hardcoded reranked[:3] dropped the 4th+ best setup every
        # cycle regardless of quality or free slots. ``dispatch_top_n`` makes the
        # cut configurable (default 3 = legacy); ``slot_aware_dispatch`` instead
        # tracks real free trade slots so the ranked tail is only cut by
        # capacity, not a magic number. Every dispatched setup still runs every
        # downstream gate independently.
        _rc = getattr(self.config, "opportunity_ranker", None)
        _dispatch_n = int(getattr(_rc, "dispatch_top_n", 3) or 3) if _rc is not None else 3
        if _rc is not None and getattr(_rc, "slot_aware_dispatch", False):
            _free_slots = int(self.config.risk.max_open_trades) - len(self.managed_positions)
            _dispatch_n = max(0, min(int(getattr(_rc, "dispatch_max_n", 10) or 10), _free_slots))
            logger.debug(
                "[dispatch] slot-aware cut — {} free slot(s), dispatching up to {} of {} ranked setup(s)",
                _free_slots, _dispatch_n, len(reranked),
            )
        top_results = reranked[:_dispatch_n]
        top = [rank_map[r.pair] for r in top_results if r.pair in rank_map]

        self._last_slot_blocked_candidate = None
        for setup in top:
            result = setup.result
            if result.pair in open_pairs:
                continue

            # Open a fresh awareness trace for this setup. Every stage below
            # stamps its verdict onto it; it is finalised on rejection
            # (_log_rejection) or on a placed trade.
            self._trace_begin(result.pair)

            # ── Opportunity executor — graded direction selection ─────────
            # When execute is on, the ranked candidates (coherent vote clusters
            # scored by EV) choose the live direction instead of the scalar
            # consensus sum. The chosen direction then flows through EVERY gate
            # below (correlation/CP4, margin, max-trades, planner, governor)
            # unchanged. When execute is off this is a no-op: the scalar
            # direction stands and behaviour is identical to before.
            # The genuine scalar verdict (incl. NEUTRAL) is preserved on
            # ``consensus_direction``; ``result.direction`` may already have been
            # promoted by the scanner's NEUTRAL rescue. Trace against the genuine
            # verdict so a rescue is visible (scalar NEUTRAL → ranker direction)
            # rather than hidden behind the already-promoted direction.
            scalar_dir = getattr(result, "consensus_direction", "") or result.direction
            rescued_from_neutral = (
                scalar_dir not in ("LONG", "SHORT")
                and result.direction in ("LONG", "SHORT")
            )
            ranker_override = False
            selected_opp = None
            if self.config.opportunity_ranker.execute:
                # When the orchestrator is enabled, let the round table grade
                # every candidate and choose the most defensible one — instead of
                # blindly dispatching the ranker's top-EV candidate[0] (which
                # re-collapses the candidate set the ranker preserved). Legacy
                # top-1 selection stands when the orchestrator is off.
                _cand_scorer = None
                if getattr(self.config.orchestrator, "enabled", False):
                    def _cand_scorer(c):  # noqa: E306 — local, orchestrator-gated
                        return self._orchestrator.grade_candidate(
                            direction=getattr(c, "direction", ""),
                            horizon=getattr(c, "timeframe_class", ""),
                            ranker_ev=getattr(c, "expected_value", None),
                            ranker_coherence=getattr(c, "coherence", None),
                            ranker_confidence=getattr(c, "confidence", None),
                        )
                try:
                    _cands = getattr(result, "candidates", None)
                    opp = self._opportunity_executor.select(_cands, scorer=_cand_scorer)
                    if (
                        opp is not None
                        and _cands
                        and _cand_scorer is not None
                        and opp is not _cands[0]
                    ):
                        logger.info(
                            "[executor] {} ORCHESTRATOR PICK — chose {} {} over "
                            "top-EV {} {} (graded over {} candidate(s))",
                            result.pair, opp.direction, opp.timeframe_class,
                            _cands[0].direction, _cands[0].timeframe_class, len(_cands),
                        )
                except Exception as exc:
                    logger.error("[executor] {} selection failed — keeping scalar direction: {}", result.pair, exc)
                    opp = None
                if opp is not None and opp.direction in ("LONG", "SHORT"):
                    result.selected_horizon = opp.timeframe_class
                    selected_opp = opp
                    if opp.direction != scalar_dir:
                        ranker_override = True
                        logger.info(
                            "[executor] {} RANKER OVERRIDE — consensus={} → ranker={} "
                            "EV={:+.2f}R horizon={} | {}",
                            result.pair, scalar_dir or "NEUTRAL", opp.direction,
                            opp.expected_value, opp.timeframe_class, opp.summary,
                        )
                    else:
                        logger.info(
                            "[executor] {} ranker confirms {} EV={:+.2f}R horizon={}",
                            result.pair, opp.direction, opp.expected_value, opp.timeframe_class,
                        )
                    result.direction = opp.direction
                else:
                    # No qualifying candidate → the scalar consensus direction
                    # stands; no ranker horizon, so downstream HTF authority is
                    # unchanged (full authority).
                    result.selected_horizon = ""

            # Stamp the direction-selection verdict (ranker when it drove the
            # choice, else the scalar consensus that stood).
            if selected_opp is not None:
                if rescued_from_neutral:
                    _ranker_verb = f"RESCUED NEUTRAL consensus → {selected_opp.direction}"
                elif ranker_override:
                    _ranker_verb = f"OVERRODE consensus {scalar_dir or 'NEUTRAL'}"
                else:
                    _ranker_verb = "confirmed"
                self._trace_stamp(
                    STAGE_RANKER, "opportunity_ranker",
                    f"{selected_opp.direction}_{selected_opp.timeframe_class}",
                    (f"ranker {_ranker_verb} "
                     f"— best EV cluster {selected_opp.expected_value:+.2f}R on {selected_opp.timeframe_class} "
                     f"from {', '.join(selected_opp.contributors) or 'none'}"),
                    evidence={
                        "direction": selected_opp.direction,
                        "horizon": selected_opp.timeframe_class,
                        "ev_r": round(selected_opp.expected_value, 3),
                        "win_prob": round(selected_opp.win_prob, 3),
                        "coherence": round(selected_opp.coherence, 3),
                        "scalar_consensus": scalar_dir or "NEUTRAL",
                        "override": ranker_override,
                        "rescued_from_neutral": rescued_from_neutral,
                        "candidates": len(getattr(result, "candidates", []) or []),
                    },
                    confidence=float(getattr(selected_opp, "confidence", 0.0) or 0.0),
                )
            else:
                self._trace_stamp(
                    STAGE_RANKER, "directional_consensus",
                    result.direction or "NEUTRAL",
                    (f"scalar consensus {result.direction or 'NEUTRAL'} stands "
                     f"(score {int(result.score)}); no ranked candidate selected"),
                    evidence={
                        "direction": result.direction or "NEUTRAL",
                        "scan_score": int(result.score),
                        "ranker_execute": bool(self.config.opportunity_ranker.execute),
                        "candidates": len(getattr(result, "candidates", []) or []),
                    },
                    confidence=0.5,
                )

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
                self._trace_stamp(
                    STAGE_CORRELATION, "correlation_engine", "BLOCK", corr_reason,
                    evidence={"open_trades": len(open_trades), "direction": result.direction},
                    blocking=True,
                )
                self._log_rejection(result.pair, result.direction, result.score, corr_reason)
                continue
            self._trace_stamp(
                STAGE_CORRELATION, "correlation_engine", "PASS",
                f"no correlation/hedge conflict with {len(open_trades)} open position(s)",
                evidence={"open_trades": len(open_trades), "direction": result.direction},
            )

            if self.config.risk.margin_guardian_enabled:
                margin_ok, margin_reason = self._check_margin_for_entry(result.pair)
                if not margin_ok:
                    self._trace_stamp(
                        STAGE_MARGIN, "margin_guardian", "BLOCK", margin_reason,
                        blocking=True,
                    )
                    self._log_rejection(result.pair, result.direction, result.score, margin_reason)
                    continue
                self._trace_stamp(
                    STAGE_MARGIN, "margin_guardian", "PASS",
                    "sufficient free margin for this entry",
                )

            if len(self.managed_positions) >= self.config.risk.max_open_trades:
                self._last_slot_blocked_candidate = {
                    "pair": result.pair,
                    "direction": result.direction,
                    "score": result.score,
                }
                self._trace_stamp(
                    STAGE_MAX_TRADES, "risk_limits", "BLOCK",
                    f"max open trades reached ({len(self.managed_positions)}/{self.config.risk.max_open_trades})",
                    evidence={
                        "open": len(self.managed_positions),
                        "limit": self.config.risk.max_open_trades,
                    },
                    blocking=True,
                )
                self._log_rejection(result.pair, result.direction, result.score, "Max trades reached")
                break
            self._trace_stamp(
                STAGE_MAX_TRADES, "risk_limits", "PASS",
                f"trade-slot available ({len(self.managed_positions)}/{self.config.risk.max_open_trades} used)",
                evidence={
                    "open": len(self.managed_positions),
                    "limit": self.config.risk.max_open_trades,
                },
            )

            cycle["entries_attempted"] += 1
            filled = self._execute_entry(result, session_status.current_session, now)
            if filled:
                cycle["entries_filled"] += 1
                open_pairs.append(result.pair)
            else:
                # Safety net: if a downstream return path did not go through
                # _log_rejection (e.g. a circuit-breaker / in-flight skip), the
                # trace is still open — close it loudly rather than leak it.
                self._trace_finalize_abandoned("entry path returned without placing a trade")

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

        # ── P1: Re-validate OQ/EQ on fresh candles ───────────────────────
        # The scan-time OQ/EQ that gated this setup READY can be stale by the
        # time we actually enter (seconds-to-minutes later). Recompute both
        # from the fresh entry-time data and reject if either has decayed
        # below the re-validation floor — the market shifted since the scan.
        _ld_cfg = self.config.layered_decision
        # Entry-time OQ/EQ to stamp onto the trade for mid-trade decay tracking
        # (P3). Seeded from the scan result and upgraded to the fresh
        # re-validated values below when they are computed.
        entry_oq = float(getattr(result, "opportunity_quality", 0.0) or 0.0)
        entry_eq = float(getattr(result, "entry_quality", 0.0) or 0.0)
        if _ld_cfg.enabled and direction in ("LONG", "SHORT"):
            try:
                fresh_oq, fresh_eq = self.scanner.recompute_quality_for_entry(
                    pair=pair,
                    trade_dir=direction,
                    h1_df=h1_df,
                    m15_df=data.get("M15"),
                    m5_df=m5_df,
                    h4_df=data.get("H4"),
                    d1_df=data.get("D1"),
                    utc_now=now,
                )
            except Exception as exc:
                # Fail-safe: if re-validation itself errors we cannot confirm
                # the setup still holds, so skip the entry rather than trade blind.
                logger.error(
                    "[entry] OQ/EQ re-validation failed for {} — skipping entry "
                    "(fail-safe): {}", pair, exc,
                )
                self._log_rejection(
                    pair, direction, result.score,
                    "OQ/EQ re-validation error — fail-safe skip",
                )
                return False

            # ── Phase 9: soften the entry-time re-validation gate ─────────
            # The scan-stage scanner gate (#3) may have softened this setup to
            # READY; this re-validation floor would otherwise re-kill it from
            # the same kill pattern. When the orchestrator is the live sizer,
            # fold a below-floor (but above-safety) re-validation into the
            # setup's quality multiplier instead of dropping it. Truly decayed
            # setups (below the hard safety floors) still reject.
            _orch_cfg_rv = getattr(self.config, "orchestrator", None)
            _soften_reval = (
                _orch_cfg_rv is not None
                and getattr(_orch_cfg_rv, "enabled", False)
                and getattr(_orch_cfg_rv, "soften_scanner_gates", False)
            )
            _oq_floor = _ld_cfg.revalidate_opportunity_quality_min
            _eq_floor = _ld_cfg.revalidate_entry_quality_min
            _reval_softened = False
            if _soften_reval and (fresh_oq < _oq_floor or fresh_eq < _eq_floor):
                _safe_oq = float(getattr(_orch_cfg_rv, "scanner_safety_oq", 2.0))
                _safe_eq = float(getattr(_orch_cfg_rv, "scanner_safety_eq", 2.0))
                if fresh_oq >= _safe_oq and fresh_eq >= _safe_eq:
                    _floor = float(getattr(_orch_cfg_rv, "gate_quality_floor", 0.15))
                    _reval_mult = _gate_quality_multiplier(
                        [(fresh_oq, _oq_floor), (fresh_eq, _eq_floor)], _floor,
                    )
                    try:
                        result.gate_quality_multiplier = (
                            float(getattr(result, "gate_quality_multiplier", 1.0))
                            * _reval_mult
                        )
                    except Exception:
                        result.gate_quality_multiplier = _reval_mult
                    logger.info(
                        "[gate-soften] revalidation {} OQ={:.2f}/EQ={:.2f} below "
                        "floors but above safety — flowing ×{:.2f} (orchestrator sizes)",
                        pair, fresh_oq, fresh_eq, _reval_mult,
                    )
                    entry_oq, entry_eq = fresh_oq, fresh_eq
                    _reval_softened = True

            if not _reval_softened:
                if fresh_oq < _ld_cfg.revalidate_opportunity_quality_min:
                    logger.info(
                        "[entry] {} market conditions shifted since scan: "
                        "OQ {:.2f} < {:.2f} — rejecting entry",
                        pair, fresh_oq, _ld_cfg.revalidate_opportunity_quality_min,
                    )
                    self._log_rejection(
                        pair, direction, result.score,
                        f"OQ decayed since scan ({fresh_oq:.2f} < "
                        f"{_ld_cfg.revalidate_opportunity_quality_min:.2f})",
                    )
                    return False
                if fresh_eq < _ld_cfg.revalidate_entry_quality_min:
                    logger.info(
                        "[entry] {} entry geometry degraded since scan: "
                        "EQ {:.2f} < {:.2f} — rejecting entry",
                        pair, fresh_eq, _ld_cfg.revalidate_entry_quality_min,
                    )
                    self._log_rejection(
                        pair, direction, result.score,
                        f"EQ decayed since scan ({fresh_eq:.2f} < "
                        f"{_ld_cfg.revalidate_entry_quality_min:.2f})",
                    )
                    return False

                # Both floors cleared — these fresh, entry-time scores are the
                # baseline management measures decay against (P3).
                entry_oq, entry_eq = fresh_oq, fresh_eq

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

        # ── Per-account risk silo gate ───────────────────────────────────
        # Size/gate this entry against ITS OWN account only. A daily-loss halt
        # or hot heat on one account never blocks another.
        _acct = self._account_key(pair)
        self._account_risk.update_balance(_acct, balance)
        # Global drawdown backstop measures TOTAL portfolio equity (sum across
        # all account silos), not just the account that happens to be entering —
        # so the pooled daily/weekly P&L % is a real portfolio figure. Per-account
        # caps are enforced by the silos here; SIZING still uses this account's
        # own balance (passed as account_balance to risk_engine.assess below).
        self.risk_engine.balance = self._account_risk.total_balance() or balance
        if self._account_risk.daily_loss_halted(_acct):
            self._log_rejection(
                pair, direction, result.score,
                f"Account '{_acct}' daily-loss halt "
                f"({self._account_risk.daily_pnl_pct(_acct):.2f}%)",
            )
            return False
        if getattr(self.config.risk, "portfolio_heat_enabled", False) and self._account_risk.heat_blocked(_acct):
            self._log_rejection(
                pair, direction, result.score,
                f"Account '{_acct}' heat {self._account_risk.heat(_acct):.2f}% "
                f"≥ block {self._account_risk.heat_block_pct:.1f}%",
            )
            return False

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
            self._trace_stamp(
                STAGE_ENTRY_ENGINE, "entry_engine", "REJECT", signal.reason,
                evidence={
                    "horizon": getattr(result, "selected_horizon", "") or "",
                    "entry_price": signal.entry_price,
                    "stop_loss": signal.stop_loss,
                },
                blocking=True,
            )
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

        _entry_horizon = getattr(result, "selected_horizon", "") or "full-HTF"
        _entry_qmult = float(getattr(signal, "entry_quality_multiplier", 1.0) or 1.0)
        _scan_qmult = float(getattr(result, "gate_quality_multiplier", 1.0) or 1.0)
        self._trace_stamp(
            STAGE_ENTRY_ENGINE, "entry_engine", "PASS",
            (f"entry geometry built @ {signal.entry_price} SL {signal.stop_loss} "
             f"(horizon {_entry_horizon} — H4 penalty scaled to this horizon)"
             + (f" | entry-score gate softened ×{_entry_qmult:.2f}" if _entry_qmult < 1.0 else "")
             + (f" | scanner gate softened ×{_scan_qmult:.2f}" if _scan_qmult < 1.0 else "")),
            evidence={
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "entry_mode": getattr(signal, "entry_mode", ""),
                "horizon": getattr(result, "selected_horizon", "") or "",
                "entry_quality_multiplier": round(_entry_qmult, 3),
                "scanner_quality_multiplier": round(_scan_qmult, 3),
            },
        )

        spread = 0.0
        try:
            spread = self.platforms.get_spread(pair)
        except Exception as exc:
            logger.warning("[entry] spread fetch failed, proceeding with zero spread: {}", exc)
            pass

        # Feed the live spread into the RiskEngine's spread monitor so it builds
        # a real rolling baseline (its history was previously always empty,
        # leaving the assess()-time spread gate to fall back to the global
        # registry value).
        if spread > 0:
            try:
                self.risk_engine.spread_monitor.record_spread(pair, spread)
            except Exception as exc:
                logger.debug("[spread] record_spread failed: {}", exc)

        # ── Decision Engine entry path ────────────────────────────────────
        plan_to_store: tuple | None = None
        # Fresh DecisionEngine conviction (0–1) — drives RiskEngine sizing (P3).
        # Stays None when the Decision Engine is disabled, in which case the
        # engine falls back to the legacy stale-score scaler.
        entry_conviction: float | None = None
        # Hoisted so the orchestrator (below) can read the decision/planner
        # evidence regardless of which branch produced it. None when a stage
        # did not run — the orchestrator treats a missing dimension as neutral.
        _orch_sa = None
        _orch_decision = None
        _orch_plan = None
        _orch_plan_ctx = None
        if self._decision_enabled:
            try:
                entry_ctx = self._build_entry_context(
                    result, signal, data, balance, session, spread, ctx, _exec_risk, now,
                )
                sa = self._situation_engine.assess_entry(entry_ctx)
                entry_decision = self._decision_engine.decide_entry(entry_ctx, sa)
                _orch_sa = sa
                _orch_decision = entry_decision
                _pre_gov_action = getattr(getattr(entry_decision, "action", None), "value", str(getattr(entry_decision, "action", "")))
                _tf_align = getattr(sa, "tf_alignment", None)
                _de_horizon = getattr(result, "selected_horizon", "") or ""

                governor_changed = False
                if self._risk_governor is not None:
                    reviewed = self._risk_governor.review_entry(entry_decision, entry_ctx, sa)
                    if reviewed.action != entry_decision.action:
                        governor_changed = True
                    entry_decision = reviewed

                if self._decision_journal is not None:
                    self._decision_journal.log_entry(entry_ctx, sa, entry_decision, governor_changed)

                _de_action = getattr(getattr(entry_decision, "action", None), "value", str(getattr(entry_decision, "action", "")))
                _is_reversal = "[REVERSAL]" in getattr(entry_decision, "reason", "")
                _de_evidence = {
                    "action": _de_action,
                    "conviction": round(float(getattr(entry_decision, "conviction", 0.0) or 0.0), 3),
                    "size_mult": round(float(getattr(entry_decision, "size_multiplier", 1.0) or 1.0), 3),
                    "tf_alignment": (round(float(_tf_align), 3) if _tf_align is not None else None),
                    "horizon": _de_horizon,
                    "htf_scaled": bool(_de_horizon),
                    "reversal": _is_reversal,
                    "gate_softened": bool(getattr(entry_decision, "gate_softened", False)),
                    "de_quality_mult": round(float(getattr(entry_decision, "de_quality_multiplier", 1.0) or 1.0), 3),
                }
                self._trace_stamp(
                    STAGE_DECISION_ENGINE, "decision_engine",
                    _de_action if entry_decision.should_enter else f"SKIP:{_de_action}",
                    (getattr(entry_decision, "reason", "") or "decision engine verdict").strip(),
                    evidence=_de_evidence,
                    confidence=float(getattr(entry_decision, "conviction", 0.0) or 0.0),
                    blocking=not entry_decision.should_enter,
                )
                # The governor reviews the decision engine — record its verdict
                # and, when it overrode the engine, a formal challenge so the
                # disagreement is visible (the engine owner must justify it).
                self._trace_stamp(
                    STAGE_GOVERNOR, "risk_governor",
                    "CHANGED" if governor_changed else "PASS",
                    (f"governor overrode decision engine ({_pre_gov_action} → {_de_action})"
                     if governor_changed else
                     f"governor upheld decision engine verdict ({_de_action})"),
                    evidence={"pre": _pre_gov_action, "post": _de_action, "changed": governor_changed},
                )
                if governor_changed:
                    self._trace_challenge(
                        "governor", STAGE_DECISION_ENGINE,
                        f"risk governor disagreed with decision engine: changed {_pre_gov_action} → {_de_action}",
                    )

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

                # Awareness: a high-EV ranker pick that the decision engine only
                # weakly supports (low conviction) is worth flagging — the
                # downstream owner should be able to justify entering anyway.
                if (
                    getattr(result, "selected_horizon", "")
                    and float(getattr(entry_decision, "conviction", 1.0) or 1.0) < 0.35
                ):
                    self._trace_challenge(
                        "decision_engine", STAGE_RANKER,
                        (f"ranker selected {direction} {result.selected_horizon} but decision-engine "
                         f"conviction is low ({float(entry_decision.conviction):.2f})"),
                    )

                # Awareness: the DE enter/skip gate was softened (#6) — a setup
                # the legacy binary would have killed (margin <= 0) is flowing
                # through for the orchestrator to size. Flag it so the round
                # table's owner can justify (or dim) it; never a silent pass.
                if getattr(entry_decision, "gate_softened", False):
                    self._trace_challenge(
                        "orchestrator", STAGE_DECISION_ENGINE,
                        (f"decision-engine gate softened for {direction} {pair} "
                         f"(margin {float(getattr(entry_decision, 'entry_margin', 0.0)):+.2f}, "
                         f"quality ×{float(getattr(entry_decision, 'de_quality_multiplier', 1.0)):.2f}) "
                         f"— orchestrator sizes instead of a hard SKIP"),
                    )

                # Apply conviction-based sizing
                conviction_mult = entry_decision.size_multiplier
                entry_conviction = entry_decision.conviction
                if entry_decision.is_market:
                    signal.entry_mode = "MARKET"

                # Tag reversal trades (roadmap E) so their realized EV can be
                # tracked separately on the dashboard. The decision engine flags
                # a qualified counter-HTF reversal with "[REVERSAL]" in its
                # reason; persist it as a confluence marker, which flows into the
                # position and the trade journal via signal.confluences.
                try:
                    if (
                        "[REVERSAL]" in getattr(entry_decision, "reason", "")
                        and "REVERSAL_TRADE" not in signal.confluences
                    ):
                        signal.confluences.append("REVERSAL_TRADE")
                except Exception as exc:
                    logger.debug("[entry] reversal tag skipped: {}", exc)

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
                        _plan_advisors = {
                            "scan_score": int(getattr(result, "score", 0) or 0),
                            "de_conviction": round(float(getattr(entry_decision, "conviction", 0.0) or 0.0), 3),
                            "risk_pct": round(float(getattr(plan, "risk_pct", 0.0) or 0.0), 4),
                            "is_market": bool(getattr(plan, "is_market", False)),
                            "planner_quality_multiplier": round(float(getattr(plan, "gate_quality_multiplier", 1.0) or 1.0), 3),
                        }
                        if plan.action == "SKIP":
                            self._trace_stamp(
                                STAGE_PLANNER, "trade_planner", "SKIP",
                                (getattr(plan, "reasoning", "") or "planner skipped this setup").strip(),
                                evidence=_plan_advisors, blocking=True,
                            )
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
                            self._trace_stamp(
                                STAGE_PLANNER, "trade_planner", "WAIT",
                                (getattr(plan, "reasoning", "") or getattr(plan, "wait_reason", "") or "planner waiting").strip(),
                                evidence=_plan_advisors, blocking=True,
                            )
                            self._log_rejection(
                                pair, direction, result.score,
                                f"Planner WAIT: {plan.reasoning}",
                            )
                            # A perpetually WAIT-ed setup is effectively rejected,
                            # so persist a shadow contract (mirroring planner:SKIP)
                            # to keep it in the counterfactual / gate-tuner learning.
                            self._persist_shadow_contract(signal, rejecting_gate="planner:WAIT")
                            return False
                        # ENTER — adopt the plan's sizing and entry mode.
                        self._trace_stamp(
                            STAGE_PLANNER, "trade_planner", "ENTER",
                            (getattr(plan, "reasoning", "") or "planner approved entry timing/sizing").strip(),
                            evidence=_plan_advisors,
                        )
                        # The planner refines sizing/timing on the direction the
                        # ranker chose — flag when it sized down a ranker pick it
                        # only weakly agrees with (low scan score) so the
                        # disagreement is visible.
                        if (
                            getattr(result, "selected_horizon", "")
                            and int(getattr(result, "score", 0) or 0) < 50
                        ):
                            self._trace_challenge(
                                "planner", STAGE_RANKER,
                                (f"planner entered ranker's {direction} {result.selected_horizon} pick "
                                 f"but scanner score is weak ({int(result.score)})"),
                            )
                        base_pct = max(_exec_risk * 100.0, 1e-6)
                        conviction_mult = max(0.3, min(2.0, plan.risk_pct / base_pct))
                        signal.entry_mode = "MARKET" if plan.is_market else signal.entry_mode
                        plan_to_store = (plan, plan_ctx)
                        _orch_plan = plan
                        _orch_plan_ctx = plan_ctx
                    except Exception as exc:
                        logger.warning("[Planner] error — keeping decision-engine sizing: {}", exc)
                        # Surface so the 'trades open but no plan logged' symptom
                        # is diagnosable on the Activity panel instead of silent.
                        try:
                            self._add_warning("warning", f"Planner error for {pair}: {exc}")
                        except Exception:
                            pass
                        # A planner crash must not bypass the Portfolio Governor
                        # (its veto lives inside the planner). Run it directly.
                        gov = getattr(self, "_governor", None)
                        if gov is not None:
                            try:
                                book = [
                                    (p.symbol, p.direction)
                                    for p in self.managed_positions.values()
                                ]
                                gdir = "BUY" if direction in ("LONG", "BUY") else "SELL"
                                verdict = gov.check(result.pair, gdir, book, balance)
                                if verdict is not None and not getattr(verdict, "allowed", True):
                                    self._log_rejection(
                                        pair, direction, result.score,
                                        f"Governor (planner-fallback): "
                                        f"{getattr(verdict, 'reason', 'portfolio limit')}",
                                    )
                                    self._persist_shadow_contract(
                                        signal,
                                        rejecting_gate=f"governor:{getattr(verdict, 'blocked_by', 'portfolio')}",
                                    )
                                    return False
                            except Exception as gexc:
                                # Mirror the governor's fail-closed policy: a
                                # crashing portfolio veto must not silently let
                                # the trade through unless explicitly fail-open.
                                gov_fail_closed = getattr(
                                    getattr(gov, "config", None), "fail_closed", True
                                )
                                if gov_fail_closed:
                                    logger.error(
                                        "[Planner] governor fallback error — "
                                        "skipping entry (fail-closed): {}", gexc,
                                    )
                                    self._log_rejection(
                                        pair, direction, result.score,
                                        f"Governor (planner-fallback) error "
                                        f"(fail-closed): {gexc}",
                                    )
                                    try:
                                        self._persist_shadow_contract(
                                            signal,
                                            rejecting_gate="governor:error",
                                        )
                                    except Exception:
                                        pass
                                    return False
                                logger.debug("[Planner] governor fallback failed (fail-open): {}", gexc)
            except Exception as exc:
                # Authority hierarchy: if the decision/situation/governor-review
                # layer crashes, fail CLOSED (skip) rather than continuing to
                # execution and bypassing those gates entirely.
                logger.warning(
                    "[DecisionEngine] entry error — failing CLOSED (skipping entry): {}", exc,
                )
                self._log_rejection(
                    pair, direction, result.score,
                    f"Decision pipeline error (fail-closed): {exc}",
                )
                try:
                    self._persist_shadow_contract(signal, rejecting_gate="decision_engine:error")
                except Exception as shadow_exc:
                    logger.debug("[ShadowContract] persist on DE error failed: {}", shadow_exc)
                return False
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

        # ── Regime score-threshold (bounded context, not a hard veto) ────
        # When the RegimeLearner is confident this regime needs a higher bar,
        # a setup below that elevated bar is SIZED DOWN (bounded penalty) rather
        # than auto-killed — HTF/regime is context with bounded influence, never
        # a dictator. mode="veto" restores the legacy hard rejection.
        try:
            _regime = getattr(result, "regime", "") or ""
            _rl = self.ml.regime_learner
            _strat = _rl.get_strategy(_regime)
            _bump = _regime_score_bump(
                getattr(_strat, "optimal_score_threshold", 85),
                getattr(_strat, "sample_size", 0),
                _rl.MIN_SAMPLE,
            )
            if _bump > 0:
                from brain.instrument_profile import get_profile
                _base_bar = int(get_profile(pair).min_entry_score)
                if result.score < _base_bar + _bump:
                    _rmode = getattr(self.config.risk, "regime_score_threshold_mode", "penalty")
                    if _rmode == "veto":
                        self._log_rejection(
                            pair, direction, result.score,
                            f"regime '{_regime}' needs higher conviction "
                            f"(score {int(result.score)} < {_base_bar}+{_bump})",
                        )
                        self._persist_shadow_contract(
                            signal, rejecting_gate=f"regime_threshold:{_regime}",
                        )
                        return False
                    _rmult = max(0.0, min(1.0, float(getattr(
                        self.config.risk, "regime_below_threshold_size_mult", 0.7))))
                    conviction_mult *= _rmult
                    logger.info(
                        "[entry] regime '{}' below conviction bar "
                        "(score {} < {}+{}) — size ×{:.2f} (bounded, not vetoed)",
                        _regime, int(result.score), _base_bar, _bump, _rmult,
                    )
                    # Surface the bounded HTF/regime penalty on the dashboard.
                    # When this gate was a hard veto it appeared in the activity
                    # feed via _log_rejection; as a size penalty it would
                    # otherwise go dark — keep HTF context visible to the desk.
                    self._add_warning(
                        "info",
                        f"HTF/regime '{_regime}' below conviction bar "
                        f"(score {int(result.score)} < {_base_bar}+{_bump}) — "
                        f"size ×{_rmult:.2f} (bounded, not vetoed)",
                        symbol=pair,
                    )
        except Exception as exc:
            # Fail-safe: this gate normally sizes DOWN setups that fall short of
            # an elevated regime conviction bar. If it errors, apply the bounded
            # conservative size penalty rather than silently allowing full size.
            _rmult = max(0.0, min(1.0, float(getattr(
                self.config.risk, "regime_below_threshold_size_mult", 0.7))))
            conviction_mult *= _rmult
            logger.error(
                "[entry] regime threshold gate failed for {} — applying "
                "conservative size ×{:.2f} (fail-safe): {}", pair, _rmult, exc,
            )
        # Don't open fresh FX/metals risk right before the weekend close.
        if self._blocks_new_entry_near_weekend(pair, datetime.now(timezone.utc)):
            self._log_rejection(
                pair, direction, result.score,
                "weekend close buffer — no new FX/metals entries",
            )
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
            trade_history=getattr(self.scanner, "_trade_history", None),
            conviction=entry_conviction,
            portfolio_heat_pct=getattr(self, "_current_portfolio_heat", 0.0),
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
        # Learned, bounded EV cutoff (base 0.0). The gate auto-tuner lowers it
        # within an envelope when shadow outcomes show this gate keeps rejecting
        # winners; it stays at 0.0 by default.
        ev_cutoff = self._gate_tuner.threshold("ev_gate", 0.0)
        if ev_val < ev_cutoff:
            # Determine confidence from EVEstimator sample size indirectly via score
            # We gate on negative EV only when pair_mult is also below 1.0 (i.e. learner
            # has marked this pair as REDUCE_SIZE or worse) — belt + braces gate.
            # Belt + braces: we are already inside the negative-EV branch.
            # If the pair multiplier cannot be fetched we cannot confirm the
            # pair is healthy, so fail SAFE — treat it as a reducing learner
            # (pair_mult < 1.0) and let the gate block the negative-EV setup.
            pair_mult = 1.0
            try:
                pair_mult = self.ml.pair_learner.get_pair_multiplier(pair)
            except Exception as exc:
                logger.error(
                    "[entry] pair multiplier fetch for EV gate failed — "
                    "treating as reducing (fail-safe block): {}", exc,
                )
                pair_mult = 0.0
            if pair_mult < 1.0:
                _ev_mode = str(getattr(self.config.risk, "ev_gate_mode", "veto")).lower()
                if _ev_mode == "penalty":
                    _ev_mult = max(0.0, min(1.0, float(getattr(
                        self.config.risk, "ev_gate_below_size_mult", 0.5))))
                    conviction_mult *= _ev_mult
                    logger.info(
                        "[entry] EV gate (penalty) {} — negative EV ({:.4f}) + "
                        "pair_mult={:.2f} → size ×{:.2f} (bounded, not vetoed)",
                        pair, ev_val, pair_mult, _ev_mult,
                    )
                else:
                    self._log_rejection(
                        pair, direction, result.score, f"EV gate: negative EV ({ev_val:.4f}) + pair_mult={pair_mult:.2f}"
                    )
                    self._persist_shadow_contract(signal, rejecting_gate=f"ev_gate:ev={ev_val:.4f}")
                    return False

        # ── Losing-pattern gate (defensive) ──────────────────────────────
        # Block setups whose pair/session/regime/entry-type combination is a
        # statistically-confident loser in our own trade history (TradeAnalyzer
        # multi-dimensional patterns, refreshed each optimisation pass). Neutral
        # until enough history accumulates; only ever blocks. Fail-safe: any
        # error skips the entry rather than trading blind.
        if getattr(self.config.risk, "losing_pattern_block_enabled", True):
            try:
                _is_loser, _lp_reason = self.ml.is_losing_pattern(
                    pair=pair,
                    regime=getattr(result, "regime", ""),
                    session=session,
                    entry_type=getattr(signal, "entry_type", "") or "",
                )
                if _is_loser:
                    _lp_mode = str(getattr(self.config.risk, "losing_pattern_mode", "veto")).lower()
                    if _lp_mode == "penalty":
                        _lp_mult = max(0.0, min(1.0, float(getattr(
                            self.config.risk, "losing_pattern_size_mult", 0.5))))
                        conviction_mult *= _lp_mult
                        logger.info(
                            "[entry] losing-pattern gate (penalty) {} — {} → "
                            "size ×{:.2f} (bounded, not vetoed)",
                            pair, _lp_reason, _lp_mult,
                        )
                    else:
                        self._log_rejection(
                            pair, direction, result.score,
                            f"Losing pattern: {_lp_reason}",
                        )
                        self._persist_shadow_contract(
                            signal, rejecting_gate=f"losing_pattern:{_lp_reason}",
                        )
                        return False
            except Exception as exc:
                logger.error(
                    "[entry] losing-pattern gate failed for {} — skipping entry "
                    "(fail-safe): {}", pair, exc,
                )
                self._log_rejection(
                    pair, direction, result.score,
                    "Losing-pattern gate error — fail-safe skip",
                )
                return False

        # ── Orchestrator — the round table (graded sizing, dimmer not switch) ─
        # Every prior stage's evidence (ranker EV/coherence, HTF alignment,
        # decision-engine margin/conviction, planner advisor agreement, scan
        # score) is folded into ONE bounded size multiplier. Weak dimensions
        # size the trade DOWN; only physics (already enforced by the risk gates
        # above) can veto. When apply_sizing is on the multiplier scales the live
        # position — bounded [size_floor, 1.0] so it can only reduce a trade the
        # gates already approved, never create one or oversize it.
        orch_verdict = None
        if self.config.orchestrator.enabled:
            try:
                orch_verdict = self._evaluate_orchestrator(
                    result, _orch_sa, _orch_decision, _orch_plan, _orch_plan_ctx, signal,
                )
            except Exception as exc:
                logger.error(
                    "[orchestrator] {} evaluation failed — keeping pipeline sizing: {}",
                    pair, exc,
                )
                orch_verdict = None
            if orch_verdict is not None:
                applied = bool(self.config.orchestrator.apply_sizing)
                if applied and orch_verdict.size_multiplier > 0:
                    conviction_mult *= orch_verdict.size_multiplier
                self._record_orchestrator_proposal(result, orch_verdict, applied)
                self._trace_stamp(
                    "orchestrator", "orchestrator",
                    f"SIZE×{orch_verdict.size_multiplier:.2f}",
                    (f"round table graded size ×{orch_verdict.size_multiplier:.2f} "
                     f"{'(applied)' if applied else '(recorded only)'} — "
                     + "; ".join(f"{d.name}×{d.multiplier:.2f}" for d in orch_verdict.dimensions)),
                    evidence={
                        "size_multiplier": round(orch_verdict.size_multiplier, 3),
                        "applied": applied,
                        "horizon": orch_verdict.horizon or "",
                        "dimensions": {d.name: round(d.multiplier, 3) for d in orch_verdict.dimensions},
                    },
                    confidence=float(orch_verdict.size_multiplier),
                )

        try:
            adjustments = self.ml.get_trade_adjustments(
                pair=pair,
                regime=getattr(result, "regime", ""),
                session=session,
            )
            if not adjustments.should_trade:
                _ml_mode = str(getattr(self.config.risk, "ml_should_trade_mode", "veto")).lower()
                if _ml_mode == "penalty":
                    _ml_mult = max(0.0, min(1.0, float(getattr(
                        self.config.risk, "ml_should_trade_size_mult", 0.5))))
                    conviction_mult *= _ml_mult
                    logger.info(
                        "[entry] ML should_trade (penalty) {} — {} → size ×{:.2f} "
                        "(bounded, not vetoed)",
                        pair, adjustments.reason, _ml_mult,
                    )
                else:
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
            # Fail-safe: the ML adjustments include the should_trade veto and
            # the adaptive size multiplier. If this block errors we can neither
            # honour a potential veto nor size correctly, so skip the entry
            # rather than trade at full base size.
            logger.error(
                "[entry] ML adjustments/sizing failed for {} — skipping entry "
                "(fail-safe): {}", pair, exc,
            )
            self._log_rejection(
                pair, direction, result.score,
                "ML adjustments/sizing error — fail-safe skip",
            )
            return False

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
                self._record_entry_attribution(pending_id, result, orch_verdict, _orch_decision)
                self._snapshot_entry_health(pending_id, result, orch_verdict, _orch_sa)
                logger.info(
                    "📋 PENDING ORDER PLACED — {} {} @ {:.5f} | expires in {}min",
                    order_kind, pair, signal.entry_price, max_wait,
                )
                self._trace_finalize_success()
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

        # Outcome-feedback attribution — record which modules / opportunity drove
        # this fill, keyed by the broker order id so the realised R can be linked
        # back at close. Observational only.
        self._record_entry_attribution(order.order_id, result, orch_verdict, _orch_decision)
        self._snapshot_entry_health(order.order_id, result, orch_verdict, _orch_sa)

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
            platform=order.platform,
            plan_be_trigger_r=plan_be_trigger_r,
            plan_trail_activation_r=plan_trail_activation_r,
            plan_trail_strategy=plan_trail_strategy,
            plan_partial_ratio=plan_partial_ratio,
            entry_oq=entry_oq,
            entry_eq=entry_eq,
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

        # Link this pair's matching-direction signals to the opened trade so the
        # ledger can later join realised outcomes to the modules that called it.
        # No-op when the signal ledger is off.
        self._ledger_record_trade_opened(pair, direction, order.order_id)

        # Capture the decision snapshot (vote panel + consensus config) that
        # opened this trade so the counterfactual engine can later replay the
        # consensus leaving one module out at a time. No-op when disabled.
        self._counterfactual_record_open(result, order.order_id, direction)

        # Advance the RL live-trade counter so its authority-progression gate
        # (stages 6/7 require min_live_trades) reflects reality instead of
        # resetting to zero and never qualifying.
        try:
            rl_bridge = getattr(self.scanner, "_rl", None)
            if rl_bridge is not None:
                rl_bridge.record_live_trade()
        except Exception as exc:
            logger.debug("[RL] record_live_trade failed: {}", exc)

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
        self._trace_finalize_success()
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
                    platform="deriv" if pend_ctx.uses_stake else "mt5",
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
            self._absolute_be_protected.discard(oid)
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
                # Sync broker SL onto the managed position — but NEVER let a
                # broker-reported 0/absent SL silently erase a real protective
                # stop. If the broker shows no stop while we expect one, the
                # position is NAKED: alarm loudly and re-assert our SL.
                if bp.sl > 0 and abs(bp.sl - pos.sl) > 1e-8:
                    pos.sl = bp.sl
                elif bp.sl <= 0 and pos.sl > 0:
                    logger.critical(
                        "🚨 NAKED POSITION — {} {} has NO broker stop-loss; "
                        "re-asserting SL {:.5f}",
                        pos.direction, pos.symbol, pos.sl,
                    )
                    try:
                        if self.platforms.modify_trade(oid, pos.platform, new_sl=pos.sl):
                            logger.info(
                                "✅ SL re-asserted at broker — {} {} SL {:.5f}",
                                pos.direction, pos.symbol, pos.sl,
                            )
                        else:
                            logger.critical(
                                "🚨 SL RE-ASSERT FAILED — {} {} REMAINS NAKED at broker",
                                pos.direction, pos.symbol,
                            )
                    except Exception as exc:
                        logger.critical(
                            "🚨 SL re-assert error for {} {}: {}",
                            pos.direction, pos.symbol, exc,
                        )

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
            tm_trade = self.trade_manager.update(
                tm_trade, current, m5_df,
                portfolio_heat_state=self._current_heat_state_name(),
            )

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
                    # Ignore a verdict that has gone stale (e.g. strategic
                    # analysis was skipped for several cycles on missing data) —
                    # otherwise an old HOLD keeps deferring legitimate exits.
                    _verdict_ts = self._last_decision_action_time.get(oid)
                    _verdict_fresh = (
                        _verdict_ts is not None
                        and (datetime.now(timezone.utc) - _verdict_ts).total_seconds()
                        <= self._decision_verdict_max_age_seconds
                    )
                    de_wants_keep = self._decision_enabled and _verdict_fresh and last_act in (
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
                        else:
                            logger.error(
                                "🔴 BROKER CLOSE FAILED — {} {} oid={} | {} | {} — "
                                "position STILL OPEN at broker, retrying next cycle",
                                pos.direction, pos.symbol, oid,
                                tm_trade.close_reason or "exit",
                                getattr(result, "error", "unknown"),
                            )
                            self._add_warning(
                                "error",
                                f"Broker close FAILED for {pos.symbol} — still open, retrying",
                                pos.symbol,
                            )
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
                    else:
                        logger.error(
                            "🔴 BROKER CLOSE FAILED — {} {} oid={} | {} | {} — "
                            "position STILL OPEN at broker, retrying next cycle",
                            pos.direction, pos.symbol, oid,
                            tm_trade.close_reason or "exit",
                            getattr(result, "error", "unknown"),
                        )
                        self._add_warning(
                            "error",
                            f"Broker close FAILED for {pos.symbol} — still open, retrying",
                            pos.symbol,
                        )
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
                        _pip = get_pip_size(pos.symbol)
                        _pips = (
                            (result.close_price - pos.entry_price) / _pip
                            if pos.direction == "BUY"
                            else (pos.entry_price - result.close_price) / _pip
                        )
                        self._account_realized_pnl(pos, getattr(result, "pnl", 0.0), _pips, "TP1 partial")
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
                        self._record_closed_trade(pos, result.close_price, "TP1_FULL_CLOSE_REOPEN", close_result=result, exit_cause=ExitCause.TP1_PARTIAL)
                        to_remove.append(oid)
                        closed_count += 1
                        try:
                            half_stake = round(pos.stake_usd * 0.5, 2) if pos.stake_usd > 0 else None
                            # Guard the runner reopen with an idempotency key +
                            # in-flight intent so a reconnect/retry cannot place
                            # a duplicate runner (this was the only entry path
                            # without idempotency protection).
                            reopen_idem = generate_idempotency_key(
                                pos.symbol, pos.direction, half_stake or 0.0,
                            )
                            try:
                                if self.position_store:
                                    self.position_store.record_in_flight(
                                        reopen_idem, pos.symbol, pos.direction, half_stake or 0.0,
                                    )
                            except Exception as _ife:
                                logger.debug("[TP1-reopen] in-flight record failed (proceeding): {}", _ife)
                            reopen_order = self.platforms.execute_entry(
                                pos.symbol,
                                pos.direction,
                                0.0,
                                tm_trade.stop_loss,
                                tm_trade.tp2,
                                comment=build_order_comment("APEX", reopen_idem, pos.score, "TP1_REOPEN"),
                                stake_usd=half_stake if (half_stake is not None and half_stake > 0) else None,
                                idempotency_key=reopen_idem,
                            )
                            if reopen_order.success:
                                try:
                                    if self.position_store:
                                        self.position_store.resolve_in_flight(reopen_idem, reopen_order.order_id)
                                except Exception as _rfe:
                                    logger.debug("[TP1-reopen] resolve_in_flight failed: {}", _rfe)
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
                                    platform=pos.platform,
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
                            else:
                                try:
                                    if self.position_store:
                                        self.position_store.cancel_in_flight(reopen_idem)
                                except Exception as _cfe:
                                    logger.debug("[TP1-reopen] cancel_in_flight failed: {}", _cfe)
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
                        self.position_store.update_position(oid, lots=pos.lots, tp3_hit=True)
                        _pip = get_pip_size(pos.symbol)
                        _pips = (
                            (result.close_price - pos.entry_price) / _pip
                            if pos.direction == "BUY"
                            else (pos.entry_price - result.close_price) / _pip
                        )
                        self._account_realized_pnl(pos, getattr(result, "pnl", 0.0), _pips, "TP3 partial")
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
                else:
                    # Deriv stake contracts are all-or-nothing — there is no
                    # partial-close primitive for a TP3 bank. The runner keeps
                    # running under TP2 / trailing / decision-engine management.
                    # Log it so the skip is visible rather than silent.
                    logger.info(
                        "ℹ️ TP3 banking skipped for Deriv {} {} — stake contract has no "
                        "partial close; runner continues under TP2/trailing management",
                        pos.direction, pos.symbol,
                    )

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
                        # Roll the manager's intent back to the broker-confirmed
                        # values so it doesn't believe a tighter SL/TP than the
                        # broker holds, and so the modify is retried next cycle
                        # (sl_changed/tp_changed will fire again).
                        if sl_changed:
                            tm_trade.stop_loss = prev_sl
                        if tp_changed:
                            tm_trade.tp2 = prev_tp2
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

            # ── Absolute / early profit protection ──────────────────────
            # Independent of TP1 / R-multiple so adopted/orphan trades (which
            # have no reliable original risk) still get a modest open profit
            # locked to breakeven before it can round-trip into a loss.
            try:
                self._apply_absolute_profit_protection(
                    oid, pos, tm_trade, current, datetime.now(timezone.utc)
                )
            except Exception as exc:
                logger.warning(
                    "[management] absolute profit protection failed for {} ({}): {}",
                    pos.symbol, oid, exc,
                )

        for oid in to_remove:
            self.managed_positions.pop(oid, None)
            self.position_store.remove_position(oid)
            self._absolute_be_protected.discard(oid)

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
                            exit_cause=ExitCause.WEEKEND_PROTECTION,
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
                                    exit_cause=ExitCause.WEEKEND_PROTECTION,
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
                    de_action = self._run_decision_engine(
                        oid, pos, scan_result, sa_data={
                            "d1": d1, "h4": h4, "h1": h1, "m1": m1,
                            "m5": m5, "m15": m15,
                        },
                        hold_minutes=hold_minutes,
                        pressure=pressure,
                        opposing_boost=opposing_boost,
                        pressure_details=details,
                        now=now,
                    )
                    if oid not in self.managed_positions:
                        continue

                    # P1 (PR8): the legacy protective checks (C19–C22) used to
                    # run ONLY when the Decision Engine was disabled. Activate
                    # them as additive safety nets when the engine merely HELD
                    # (or observed) this cycle — they can only CLOSE a weak /
                    # opposed trade or TIGHTEN the stop in profit, never relax a
                    # stop or override a stronger verdict. When the engine
                    # actively closed/tightened/scaled, it already acted; and a
                    # degraded cycle (de_action is None) already ran the legacy
                    # fallback, so both are skipped to avoid double-managing.
                    if de_action in (Action.HOLD, Action.OBSERVE):
                        self._run_active_legacy_checks(
                            oid, pos, scan_result, h1, now,
                            hold_minutes=hold_minutes,
                            opposing_boost=opposing_boost,
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
                # Fail-safe: an analysis error must not leave the position
                # without at least its open-profit floor honoured this cycle.
                try:
                    tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                    if tm_trade is not None:
                        tick = self.platforms.get_price(pos.symbol)
                        is_long = pos.direction.upper() in ("BUY", "LONG")
                        current = tick.bid if is_long else tick.ask
                        self._apply_absolute_profit_protection(
                            oid, pos, tm_trade, current, now,
                        )
                except Exception as protect_exc:
                    logger.warning(
                        "[management] profit-protection fallback failed for {} ({}): {}",
                        pair, oid, protect_exc,
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
    ) -> Action | None:
        """Build context → assess situation → decide → governor review → execute.

        Returns the strategic verdict's :class:`Action` so the caller can layer
        the legacy protective checks as additive safety nets when the engine
        only HELD. Returns ``None`` when the engine degraded (its exception
        handler already ran the legacy fallback this cycle).
        """
        try:
            ctx = self._build_trade_context(
                oid, pos, scan_result, sa_data,
                hold_minutes=hold_minutes,
                pressure=pressure,
                opposing_boost=opposing_boost,
                pressure_details=pressure_details,
            )
            sa = self._situation_engine.assess_open_trade(ctx)
            # PR10: maintain the fast-cluster opposition streak for this position
            # and stamp it onto the context so decide_management can apply the
            # bounded close pressure when a non-winning trade is stuck against
            # the current.
            self._update_fast_opposition_streak(oid, ctx, sa)
            # Live-management round table: the orchestrator grades the position's
            # current evidence into a continuous health score and a bounded
            # management action — replacing the argmax collapse. Falls back to the
            # legacy decide_management() when disabled, pacing-gated, or on error,
            # so a position is never left unmanaged.
            decision = self._decide_management(oid, pos, ctx, sa)

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
            self._last_decision_action_time[oid] = datetime.now(timezone.utc)

            # P4: hand the strategic (multi-timeframe) structure read to the
            # mechanical manager so its M5-only structure-exit stays coherent
            # with the richer brain instead of re-deriving in isolation.
            try:
                tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                if tm_trade is not None:
                    self.trade_manager.record_strategic_assessment(
                        tm_trade,
                        structure_integrity=sa.structure_integrity,
                        tf_alignment=sa.tf_alignment,
                        assessed_at=datetime.now(timezone.utc),
                    )
            except Exception as exc:
                logger.debug(
                    "[DecisionEngine] strategic-structure record failed for {}: {}",
                    pos.symbol, exc,
                )

            self._execute_management_decision(oid, pos, decision, now)
            # Strategic engine completed — clear any degraded-mode tracking.
            self._degraded_management.pop(oid, None)
            return decision.action
        except Exception as exc:
            count = self._degraded_management.get(oid, 0) + 1
            self._degraded_management[oid] = count
            logger.warning(
                "DEGRADED_MANAGEMENT: strategic engine failed, running legacy "
                "fallback for {} ({}) — degraded cycle {}: {}",
                pos.symbol, oid, count, exc,
            )
            self._run_legacy_management_fallback(oid, pos, now)
            if count >= self._degraded_management_escalate_cycles:
                self._escalate_degraded_management(oid, pos, now)
            # Drop tracking for positions that are no longer open so the dict
            # cannot grow without bound across closed trades.
            for stale in [
                k for k in self._degraded_management if k not in self.managed_positions
            ]:
                self._degraded_management.pop(stale, None)

    # ── Orchestrator live-position management (round table for OPEN trades) ─
    def _decide_management(
        self,
        oid: str,
        pos: "ManagedPosition",
        ctx: "TradeContext",
        sa: "SituationAssessment",
    ) -> ManagementDecision:
        """Grade the open position's health → bounded action, or fall back.

        The orchestrator round table re-evaluates the position's current evidence
        into a continuous health score and maps it to a bounded management action
        (HOLD / TIGHTEN_SL / SCALE_DOWN / EXIT_PARTIAL / EXIT_FULL, plus opt-in
        SCALE_UP). This replaces the old argmax collapse in
        ``decide_management``.

        It is a strict superset of safety:
          * disabled, paced-out (first N cycles / cooldown), or any exception →
            fall back to the proven ``decide_management`` so a position is never
            left unmanaged;
          * the action is a one-way de-risk — it can only hold, tighten, trim or
            exit; SCALE_UP is recorded but never auto-adds exposure here (adds go
            through the existing governed scale-in path).
        """
        cfg = self.config.orchestrator
        if not getattr(cfg, "manage_open_positions", False):
            return self._decision_engine.decide_management(ctx, sa)

        # Pacing — don't manage immediately after entry, nor every cycle.
        count = self._mgmt_cycle_count.get(oid, 0) + 1
        self._mgmt_cycle_count[oid] = count
        if count <= int(getattr(cfg, "min_cycles_before_management", 0)):
            return self._decision_engine.decide_management(ctx, sa)
        cooldown = int(getattr(cfg, "management_cooldown_cycles", 0))
        last = self._mgmt_last_eval_cycle.get(oid, -(10**9))
        if cooldown > 0 and (count - last) < cooldown:
            return self._decision_engine.decide_management(ctx, sa)

        try:
            evidence = self._build_position_evidence(oid, pos, ctx, sa)
            report = self._orchestrator.evaluate_open_position(evidence)
            self._mgmt_last_eval_cycle[oid] = count
            self._record_position_health(pos, ctx, report)
            decision = self._management_decision_from_health(report, ctx)
            logger.info(
                "🧭 ORCH MANAGE — {} {} health={:.2f} (Δ{:+.2f}) → {} | {}",
                pos.direction, pos.symbol, report.health_score,
                report.health_delta, report.action.value,
                decision.action.value,
            )
            return decision
        except Exception as exc:
            logger.error(
                "[orchestrator/health] {} ({}) management evaluation failed — "
                "falling back to decide_management: {}",
                pos.symbol, oid, exc,
            )
            return self._decision_engine.decide_management(ctx, sa)

    def _build_position_evidence(
        self,
        oid: str,
        pos: "ManagedPosition",
        ctx: "TradeContext",
        sa: "SituationAssessment",
    ) -> PositionEvidence:
        """Assemble the re-evaluated evidence for one open position.

        Reuses the situation assessment already computed this cycle (so no
        re-analysis) and the entry-health snapshot captured at open.
        """
        cfg = self.config.orchestrator
        snap = self._entry_health_snapshot.get(oid, {}) or {}
        horizon = str(snap.get("horizon", "") or "").upper()
        if horizon == "SCALP":
            expected = float(getattr(cfg, "expected_hold_minutes_scalp", 30.0))
        else:
            # SWING / MIXED / unknown all use the slower reference.
            expected = float(getattr(cfg, "expected_hold_minutes_swing", 240.0))
        return PositionEvidence(
            pair=pos.symbol,
            direction=str(pos.direction),
            horizon=horizon,
            profit_r=ctx.profit_r,
            momentum=sa.momentum,
            structure_integrity=sa.structure_integrity,
            tf_alignment=sa.tf_alignment,
            tf_vector=sa.tf_vector(),
            read_confidence=sa.read_confidence,
            urgency=sa.urgency,
            hold_minutes=float(ctx.hold_minutes or 0.0),
            expected_hold_minutes=expected,
            portfolio_heat_pct=float(ctx.portfolio_heat_pct or 0.0),
            entry_health=snap.get("entry_health"),
            entry_structure_integrity=snap.get("structure_integrity"),
            entry_tf_alignment=snap.get("tf_alignment"),
        )

    def _management_decision_from_health(
        self, report: "PositionHealthReport", ctx: "TradeContext",
    ) -> ManagementDecision:
        """Translate a health report into an executable ManagementDecision.

        Maps the orchestrator's graded action onto the existing executor's
        ``Action`` vocabulary, reusing the decision engine's SL geometry for
        tightens. SCALE_UP is recorded on the report but executed as HOLD here —
        increasing exposure stays the responsibility of the governed scale-in
        path, keeping this translation a one-way de-risk.
        """
        action = report.action
        base = (
            f"orchestrator health {report.health_score:.2f} "
            f"(Δ{report.health_delta:+.2f})"
        )
        if report.thesis_changes:
            base += " — " + "; ".join(report.thesis_changes[:3])

        if action == ManagementAction.EXIT_FULL:
            return ManagementDecision(
                action=Action.CLOSE,
                reason=f"{base} → EXIT_FULL",
                confidence=round(1.0 - report.health_score, 4),
                evidence=list(report.thesis_changes),
            )
        if action in (ManagementAction.SCALE_DOWN, ManagementAction.EXIT_PARTIAL):
            ratio = float(report.recommended_size_pct or 0.0)
            if ratio <= 0.0:
                return ManagementDecision(action=Action.HOLD, reason=f"{base} → trim (no ratio)")
            return ManagementDecision(
                action=Action.PARTIAL_CLOSE,
                reason=f"{base} → {action.value} {int(ratio * 100)}%",
                confidence=round(1.0 - report.health_score, 4),
                partial_ratio=ratio,
                evidence=list(report.thesis_changes),
            )
        if action == ManagementAction.TIGHTEN_SL:
            new_sl = None
            try:
                new_sl = self._decision_engine._compute_tightened_sl(ctx)
            except Exception as exc:
                logger.debug("[orchestrator/health] tighten SL compute failed: {}", exc)
            if new_sl is None:
                return ManagementDecision(action=Action.HOLD, reason=f"{base} → tighten (SL unchanged)")
            return ManagementDecision(
                action=Action.TIGHTEN_SL,
                reason=f"{base} → TIGHTEN_SL",
                confidence=round(1.0 - report.health_score, 4),
                new_sl=new_sl,
                evidence=list(report.thesis_changes),
            )
        # HOLD and SCALE_UP (opt-in, recorded but not auto-added here).
        suffix = " → SCALE_UP (recorded; adds via governed scale-in)" if action == ManagementAction.SCALE_UP else " → HOLD"
        return ManagementDecision(action=Action.HOLD, reason=f"{base}{suffix}")

    def _record_position_health(
        self, pos: "ManagedPosition", ctx: "TradeContext", report: "PositionHealthReport",
    ) -> None:
        """Persist a POSITION_HEALTH event for the dashboard (best-effort)."""
        try:
            store = get_event_store()
            if store is None:
                return
            payload = report.to_dict()
            payload["order_id"] = str(getattr(pos, "order_id", "") or "")
            payload["profit_r"] = round(float(ctx.profit_r or 0.0), 4)
            payload["pnl_dollars"] = round(float(getattr(ctx, "pnl_dollars", 0.0) or 0.0), 4)
            payload["hold_minutes"] = round(float(ctx.hold_minutes or 0.0), 2)
            store.emit(
                event_type=POSITION_HEALTH,
                severity="INFO",
                symbol=pos.symbol,
                correlation_id=getattr(self, "_current_cycle_id", None),
                parent_id=str(getattr(pos, "order_id", "") or "") or None,
                source_module="brain.orchestrator",
                payload=payload,
            )
        except Exception as exc:
            logger.debug("[orchestrator/health] persist failed for {}: {}", pos.symbol, exc)

    def _update_fast_opposition_streak(
        self, oid: str, ctx: "TradeContext", sa: "SituationAssessment",
    ) -> None:
        """Maintain the fast-cluster opposition streak for one position (PR10).

        The fast-evidence cluster is considered opposing when the (direction-
        relative) momentum read is negative AND at most one of the last five M1
        candles is aligned with the trade. The streak increments on consecutive
        opposing cycles and resets to 0 the moment the fast cluster re-aligns.
        The result is stamped onto ``ctx`` so ``decide_management`` can apply the
        bounded close pressure. Fail-safe: any error resets to no pressure.
        """
        try:
            fast_opposing = sa.momentum < 0 and ctx.m1_aligned_count <= 1
            if fast_opposing:
                streak = self._fast_opposition_streak.get(oid, 0) + 1
            else:
                streak = 0
            self._fast_opposition_streak[oid] = streak
            ctx.fast_opposition_streak = streak
            # Prune streaks for positions that are no longer open.
            for stale in [
                k for k in self._fast_opposition_streak
                if k not in self.managed_positions
            ]:
                self._fast_opposition_streak.pop(stale, None)
        except Exception as exc:
            logger.debug(
                "[PR10] fast-opposition streak update failed for {}: {}",
                oid, exc,
            )
            ctx.fast_opposition_streak = 0

    def _run_active_legacy_checks(
        self,
        oid: str,
        pos: ManagedPosition,
        scan_result,
        h1,
        now: datetime,
        *,
        hold_minutes: float,
        opposing_boost: int,
    ) -> None:
        """Run the legacy protective checks (C19–C22) alongside the engine.

        These run only when the Decision Engine HELD this cycle, as additive
        safety nets. They mirror the gating of the DecisionEngine-disabled path
        and respect the same config flags, but each check is independently
        guarded so one failure does not suppress the rest. None of them can
        relax a stop or re-open a closed trade — they only CLOSE a weak/opposed
        trade or TIGHTEN the stop in profit, so they can never override a
        stronger strategic verdict.
        """
        cfg = self.config.risk

        # ── C19: opposing-setup / low-score invalidation ──────────────────
        if cfg.continuous_analysis_enabled and hold_minutes >= cfg.invalidation_min_hold_minutes:
            try:
                self._check_invalidation(
                    oid, pos, scan_result, now,
                    opposing_score_boost=opposing_boost,
                )
            except Exception as exc:
                logger.warning(
                    "[legacy-active] invalidation check failed for {} ({}): {}",
                    pos.symbol, oid, exc,
                )
            if oid not in self.managed_positions:
                return  # was closed

        # ── C20: conviction collapse ───────────────────────────────────────
        if cfg.conviction_monitoring_enabled and hold_minutes >= cfg.invalidation_min_hold_minutes:
            try:
                self._check_conviction_collapse(oid, pos, now)
            except Exception as exc:
                logger.warning(
                    "[legacy-active] conviction-collapse check failed for {} ({}): {}",
                    pos.symbol, oid, exc,
                )
            if oid not in self.managed_positions:
                return

        # ── C21: HTF candle-close reassessment ─────────────────────────────
        if cfg.htf_reassessment_enabled and cfg.htf_reassess_on_h1_close:
            try:
                self._check_htf_candle_close(oid, pos, h1, now)
            except Exception as exc:
                logger.warning(
                    "[legacy-active] HTF candle-close check failed for {} ({}): {}",
                    pos.symbol, oid, exc,
                )
            if oid not in self.managed_positions:
                return

        # ── C22: beyond-breakeven dynamic SL tightening (no DE equivalent) ─
        if cfg.dynamic_sl_tightening_enabled:
            try:
                self._apply_dynamic_sl_tightening(oid, pos)
            except Exception as exc:
                logger.warning(
                    "[legacy-active] dynamic SL tighten failed for {} ({}): {}",
                    pos.symbol, oid, exc,
                )

    def _run_legacy_management_fallback(
        self, oid: str, pos: ManagedPosition, now: datetime,
    ) -> None:
        """Run the legacy protective checks when the strategic engine fails.

        Mirrors the protective subset of the legacy (DecisionEngine-disabled)
        path so a strategic-engine exception does not leave a position governed
        by the mechanical TradeManager alone. Each check is independently
        guarded so one failure does not suppress the others.
        """
        # Lock open profit — covers adopted/orphan trades whose R-gates never
        # fire because their original risk is unknown.
        try:
            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            if tm_trade is not None:
                tick = self.platforms.get_price(pos.symbol)
                is_long = pos.direction.upper() in ("BUY", "LONG")
                current = tick.bid if is_long else tick.ask
                self._apply_absolute_profit_protection(oid, pos, tm_trade, current, now)
        except Exception as exc:
            logger.warning(
                "[degraded-mgmt] profit-protection fallback failed for {} ({}): {}",
                pos.symbol, oid, exc,
            )

        # Beyond-breakeven profit laddering — the one legacy check with no
        # DecisionEngine equivalent.
        try:
            self._apply_dynamic_sl_tightening(oid, pos)
        except Exception as exc:
            logger.warning(
                "[degraded-mgmt] dynamic SL tighten fallback failed for {} ({}): {}",
                pos.symbol, oid, exc,
            )

        # Spread-spike protection across the book.
        try:
            self._check_spread_deterioration()
        except Exception as exc:
            logger.warning(
                "[degraded-mgmt] spread deterioration fallback failed: {}", exc,
            )

    def _escalate_degraded_management(
        self, oid: str, pos: ManagedPosition, now: datetime,
    ) -> None:
        """Force-protect a position the strategic engine keeps failing on.

        After repeated degraded cycles, move the stop to breakeven if the trade
        is in profit; if it is in a loss there is nothing safe to tighten to, so
        raise a CRITICAL alarm for human attention.
        """
        cycles = self._degraded_management.get(oid, 0)
        try:
            moved = self._failsafe_move_to_breakeven(
                oid, pos, reason=f"degraded-mgmt-{cycles}-cycles",
            )
        except Exception as exc:
            logger.critical(
                "DEGRADED_MANAGEMENT: {} ({}) failing for {} cycles and broker "
                "state unreadable — manual review required: {}",
                pos.symbol, oid, cycles, exc,
            )
            return
        if not moved:
            logger.critical(
                "DEGRADED_MANAGEMENT: {} ({}) strategic engine failing for {} "
                "cycles while in a loss / unprotectable — manual review required",
                pos.symbol, oid, cycles,
            )

    @staticmethod
    def _scan_result_epoch(result) -> float:
        """Best-effort epoch seconds for a scan result's timestamp.

        Lets downstream layers measure how stale the scanner_score is. Falls
        back to the current time if the timestamp is missing/unparseable.
        """
        ts = getattr(result, "timestamp", None)
        try:
            if ts is not None:
                return float(ts.timestamp())
        except (AttributeError, TypeError, ValueError, OSError):
            pass
        return _time.time()

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
            oq=float(getattr(result, "opportunity_quality", 0.0) or 0.0),
            eq=float(getattr(result, "entry_quality", 0.0) or 0.0),
            scan_timestamp=self._scan_result_epoch(result),
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
            horizon=getattr(result, "selected_horizon", ""),
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

        # Portfolio heat carried from the EntryContext (already read from the
        # portfolio risk state machine) so the planner can size against live
        # book exposure rather than in isolation.
        portfolio_heat_pct = float(getattr(entry_ctx, "portfolio_heat_pct", 0.0) or 0.0)

        # ── Regime-adaptive trade shaping (Tier 2 #14) ───────────────────
        # Feed the RegimeLearner's learned TP stretch, SL buffer and partial
        # ratio into the planner — but only once the learner is CONFIDENT for
        # this regime (≥ MIN_SAMPLE trades). Until then these stay neutral
        # (mult 1.0 / buffer 0 / runner = planner default), so trade shaping is
        # unchanged. Buffers are expressed as deltas off the learner's baseline
        # and re-clamped inside the planner.
        regime_tp_mult = 1.0
        regime_sl_buffer_pips = 0.0
        regime_runner_pct = None
        try:
            _regime = getattr(result, "regime", "") or ""
            _rl = self.ml.regime_learner
            _strat = _rl.get_strategy(_regime)
            if getattr(_strat, "sample_size", 0) >= _rl.MIN_SAMPLE:
                regime_tp_mult = float(_strat.optimal_tp_multiplier)
                regime_sl_buffer_pips = float(_strat.optimal_sl_buffer_pips) - 2.0
                regime_runner_pct = 1.0 - float(_strat.optimal_partial_close_ratio)
        except Exception as exc:
            logger.debug("[planner] regime shaping unavailable, using neutral: {}", exc)

        plan_ctx = TradePlanContext(
            symbol=result.pair,
            pip_size=pip_size,
            spread_pips=spread,
            atr_pips=atr_pips,
            current_price=current_price,
            direction=result.direction,
            scanner_score=float(result.score),
            oq=float(getattr(entry_ctx, "oq", 0.0) or 0.0),
            eq=float(getattr(entry_ctx, "eq", 0.0) or 0.0),
            scan_timestamp=float(getattr(entry_ctx, "scan_timestamp", 0.0) or 0.0),
            zone_type=getattr(signal, "entry_type", ""),
            zone_quality=zone_quality,
            zone_entry_price=signal.entry_price,
            de_confidence=self._decision_engine.compute_conviction(sa),
            de_tf_alignment=sa.tf_alignment,
            de_structure_score=sa.structure_integrity,
            de_momentum_score=sa.momentum,
            sa_tf_alignment=sa.tf_alignment,
            sa_momentum=sa.momentum,
            sa_structure_integrity=sa.structure_integrity,
            sa_read_confidence=sa.read_confidence,
            rl_action=int(getattr(result, "rl_action", 0) or 0),
            rl_confidence=float(getattr(result, "rl_confidence", 0.0) or 0.0),
            rl_expected_r=float(getattr(result, "rl_expected_r", 0.0) or 0.0),
            rl_stage=int(getattr(result, "rl_stage", 1) or 1),
            pair_multiplier=entry_ctx.pair_multiplier,
            ev_estimate=entry_ctx.ev_estimate,
            pair_win_rate=pair_wr,
            session_win_rate=session_wr,
            regime_tp_mult=regime_tp_mult,
            regime_sl_buffer_pips=regime_sl_buffer_pips,
            regime_runner_pct=regime_runner_pct,
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
            portfolio_heat_pct=portfolio_heat_pct,
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

        # Full advisor context now reaches the planner — log it so every trade
        # decision records the complete picture (scanner score + OQ/EQ, the raw
        # situation dimensions behind conviction, and live portfolio state).
        try:
            scan_age = _time.time() - plan_ctx.scan_timestamp if plan_ctx.scan_timestamp else 0.0
            logger.info(
                "[Planner] context {} {}: score={:.0f} OQ={:.1f} EQ={:.1f} "
                "conviction={:.2f} SA[tf={:+.2f} mom={:+.2f} str={:.2f} conf={:.2f}] "
                "heat={:.2f}% corr={:.2f} dd={:.2f}% scan_age={:.1f}s",
                plan_ctx.direction, plan_ctx.symbol, plan_ctx.scanner_score,
                plan_ctx.oq, plan_ctx.eq, plan_ctx.de_confidence,
                plan_ctx.sa_tf_alignment, plan_ctx.sa_momentum,
                plan_ctx.sa_structure_integrity, plan_ctx.sa_read_confidence,
                plan_ctx.portfolio_heat_pct, plan_ctx.correlated_exposure,
                plan_ctx.current_drawdown_pct, scan_age,
            )
        except Exception as exc:
            logger.debug("[planner] context logging skipped: {}", exc)

        return plan_ctx

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
        trade_manager=None,
    ) -> TradeContext:
        """Assemble the full TradeContext from all available data sources."""
        tm = trade_manager or self.trade_manager
        tm_trade = tm.get_trade(pos.tm_trade_id)
        pnl_pips = tm_trade.pnl_pips if tm_trade else 0.0
        # Prefer the broker's live dollar P&L (synced onto the managed position
        # each cycle) — the real money the broker sees. Fall back to the local
        # pip-formula reconstruction only when broker truth is unavailable.
        broker_pnl = getattr(pos, "broker_pnl", 0.0) or 0.0
        if broker_pnl != 0.0:
            pnl_dollars = broker_pnl
        else:
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
            except Exception as exc:
                logger.debug("[ctx] {} structure extraction failed for {}: {}", tf_name, pos.symbol, exc)

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
            except Exception as exc:
                logger.debug("[ctx] H1 last-candle read failed for {}: {}", pos.symbol, exc)

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
            except Exception as exc:
                logger.debug("[ctx] M1 alignment count failed for {}: {}", pos.symbol, exc)

        # ── Session / News ───────────────────────────────────────────────
        try:
            sess = self.session_engine.get_status(datetime.now(timezone.utc))
            ctx.session_name = getattr(sess, 'name', 'UNKNOWN')
            ctx.session_tradeable = getattr(sess, 'is_tradeable', True)
        except Exception as exc:
            logger.debug("[ctx] session status read failed for {}: {}", pos.symbol, exc)

        # ── Portfolio heat ───────────────────────────────────────────────
        if self._portfolio_risk_sm is not None:
            try:
                ctx.portfolio_heat_pct = getattr(self._portfolio_risk_sm, '_last_heat_pct', 0.0) or 0.0
            except Exception as exc:
                logger.debug("[ctx] portfolio heat read failed for {}: {}", pos.symbol, exc)

        # ── P3: re-validate OQ/EQ on fresh candles ───────────────────────
        # OQ/EQ gated this trade READY at entry. Recompute them now (same two
        # scores, direction-correct for THIS trade) so the decision engine can
        # react when the conditions that justified the trade decay. Additive
        # context only — on failure we simply leave them None (no extra exit
        # pressure), so a recompute error never forces or suppresses an exit.
        _ld_cfg = self.config.layered_decision
        m5_live = sa_data.get("m5")
        m15_live = sa_data.get("m15")
        h1_live = sa_data.get("h1")
        if (
            _ld_cfg.enabled
            and m5_live is not None
            and m15_live is not None
            and h1_live is not None
        ):
            try:
                _dir = "LONG" if ctx.is_long else "SHORT"
                live_oq, live_eq = self.scanner.recompute_quality_for_entry(
                    pair=pos.symbol,
                    trade_dir=_dir,
                    h1_df=h1_live,
                    m15_df=m15_live,
                    m5_df=m5_live,
                    h4_df=sa_data.get("h4"),
                    d1_df=sa_data.get("d1"),
                    utc_now=datetime.now(timezone.utc),
                )
                ctx.live_oq = live_oq
                ctx.live_eq = live_eq
                e_oq = getattr(tm_trade, "entry_oq", None) if tm_trade else None
                e_eq = getattr(tm_trade, "entry_eq", None) if tm_trade else None
                ctx.entry_oq = e_oq
                ctx.entry_eq = e_eq
                if e_oq is not None:
                    ctx.oq_decay = round(e_oq - live_oq, 2)
                if e_eq is not None:
                    ctx.eq_decay = round(e_eq - live_eq, 2)
            except Exception as exc:
                logger.debug("[ctx] live OQ/EQ recompute failed for {}: {}", pos.symbol, exc)

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
                de_cause = (
                    ExitCause.THESIS_DECAY
                    if decision.reason.startswith(SEVERE_THESIS_CLOSE_PREFIX)
                    else ExitCause.STRATEGIC_CLOSE
                )
                # PR10: a CLOSE the engine attributed to a specific management
                # behaviour (e.g. fast-cluster opposition decay) carries an
                # explicit exit_cause tag — honour it over the generic default.
                explicit_cause = getattr(decision, "exit_cause", None)
                if explicit_cause:
                    try:
                        de_cause = ExitCause(explicit_cause)
                    except ValueError:
                        pass
                logger.info(
                    "🧠 DECISION CLOSE — {} {} | {}",
                    pos.direction, pos.symbol, reason,
                )
                self._record_closed_trade(pos, result.close_price, reason, close_result=result, exit_cause=de_cause)
                # Counterfactual audit of the severe-decay hard-close: persist a
                # shadow from the EXIT price using the protection that was in
                # force. The resolver then tells us whether price ran back to
                # that stop ("close was right") or continued ("sold a future
                # winner") — surfaced per-gate on the dashboard for validation.
                if decision.reason.startswith(SEVERE_THESIS_CLOSE_PREFIX):
                    self._audit_severe_thesis_close(pos, result.close_price)
                self.managed_positions.pop(oid, None)
                self.position_store.remove_position(oid)
                self._position_scores.pop(oid, None)
                self._degraded_management.pop(oid, None)
                self._fast_opposition_streak.pop(oid, None)
                self._entry_health_snapshot.pop(oid, None)
                self._mgmt_cycle_count.pop(oid, None)
                self._mgmt_last_eval_cycle.pop(oid, None)
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

        elif decision.action == Action.PARTIAL_CLOSE and decision.partial_ratio > 0:
            # Orchestrator-graded trim (SCALE_DOWN / EXIT_PARTIAL). Closes a
            # fraction of the position to de-risk while keeping a runner — a
            # bounded de-risk, never an oversize. Instruments without partial
            # support are skipped (a deeper health drop will close fully next
            # cycle); broker rejections roll local size back.
            pos_ctx = build_context_for_symbol(pos.symbol)
            if not pos_ctx.supports_partial_close:
                logger.info(
                    "🧭 ORCH TRIM skipped (no partial support) — {} {} | {}",
                    pos.direction, pos.symbol, decision.reason[:80],
                )
                return
            ratio = max(0.0, min(1.0, decision.partial_ratio))
            partial_lots = round(pos.lots * ratio, 2)
            # Keep at least the broker minimum on both sides — never close all
            # via the trim path (EXIT_FULL is the explicit close action).
            partial_lots = max(0.01, partial_lots)
            if partial_lots >= round(pos.lots - 0.01, 2):
                logger.info(
                    "🧭 ORCH TRIM skipped (would close whole position) — {} {}",
                    pos.direction, pos.symbol,
                )
                return
            prev_lots = pos.lots
            result = self.platforms.close_trade(oid, pos.platform, partial_lots)
            if result.success:
                pos.lots = round(pos.lots - partial_lots, 2)
                self.position_store.update_position(oid, lots=pos.lots)
                tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                if tm_trade is not None:
                    tm_trade.remaining_size_lots = pos.lots
                    tm_trade.partial_closed = True
                _pip = get_pip_size(pos.symbol)
                _pips = (
                    (result.close_price - pos.entry_price) / _pip
                    if pos.direction.upper() in ("BUY", "LONG")
                    else (pos.entry_price - result.close_price) / _pip
                )
                self._account_realized_pnl(pos, getattr(result, "pnl", 0.0), _pips, "orchestrator trim")
                logger.info(
                    "🧭 ORCH TRIM — {} {} | {:.2f}→{:.2f} lots ({:.0f}%) | {}",
                    pos.direction, pos.symbol, prev_lots, pos.lots, ratio * 100,
                    decision.reason[:80],
                )
            else:
                logger.warning(
                    "🔴 ORCH TRIM FAILED — {} {} oid={} | attempted {:.2f} lots — {}",
                    pos.direction, pos.symbol, oid, partial_lots,
                    getattr(result, "error", "unknown"),
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
            try:
                if self.position_store:
                    self.position_store.update_position(
                        pos.order_id, scale_in_count=pos.scale_in_count,
                    )
            except Exception as exc:
                logger.warning("[scale-in] scale_in_count persist failed: {}", exc)
            # Reflect the added broker volume on the managed position + the
            # TradeManager trade and persist it, so partial-close math and P&L
            # use the true size instead of waiting for reconcile to adopt the
            # add as a headless orphan.
            try:
                added = float(getattr(order, "lots", 0.0) or 0.0)
                if added > 0:
                    pos.lots = round(pos.lots + added, 2)
                    if self.position_store:
                        self.position_store.update_position(pos.order_id, lots=pos.lots)
                    tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                    if tm_trade is not None:
                        tm_trade.remaining_size_lots = round(tm_trade.remaining_size_lots + added, 2)
                        tm_trade.position_size_lots = round(
                            getattr(tm_trade, "position_size_lots", 0.0) + added, 2
                        )
            except Exception as exc:
                logger.warning("[scale-in] local lots/persist update failed: {}", exc)
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
        enforces the daily-loss-cap halt here.  Fail-safe: any error blocks the
        add (scale-in opens fresh risk, so never add on an unverified book).
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
            logger.error(
                "[Governor] add-check failed for {} — blocking scale-in "
                "(fail-safe): {}", pos.symbol, exc,
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

    def _persist_guard_state(self) -> None:
        """Persist all daily risk state as one payload so a mid-day restart
        keeps the day's loss budget / halt: the loop drawdown guard, the
        RiskEngine's own guard + balance, and the governor's daily tally/halt.
        """
        try:
            payload: dict = {"_v": 2, "drawdown": self.drawdown.to_state()}
            try:
                if getattr(self, "risk_engine", None) is not None and hasattr(self.risk_engine, "to_state"):
                    payload["risk_engine"] = self.risk_engine.to_state()
            except Exception as exc:
                logger.debug("[guard-state] risk_engine state failed: {}", exc)
            try:
                if getattr(self, "_governor", None) is not None and hasattr(self._governor, "to_state"):
                    payload["governor"] = self._governor.to_state()
            except Exception as exc:
                logger.debug("[guard-state] governor state failed: {}", exc)
            try:
                if getattr(self, "_account_risk", None) is not None:
                    payload["account_risk"] = self._account_risk.to_state()
            except Exception as exc:
                logger.debug("[guard-state] account_risk state failed: {}", exc)
            self.position_store.save_guard_state(payload)
        except Exception as exc:
            logger.error("Guard state persist failed: {}", exc)

    def _account_realized_pnl(
        self,
        pos: ManagedPosition,
        pnl_dollars: float,
        pnl_pips: float,
        label: str,
    ) -> None:
        """Fold a *partial* realised P&L into every risk tally.

        Full closes go through ``_record_closed_trade``; partials (TP1/TP3
        banks) previously only shrank ``pos.lots`` and were invisible to the
        daily-loss cap, drawdown guard and governor.  This books them once,
        mirroring the close-record accounting, without removing the position.
        """
        try:
            balance = self.platforms.get_platform_balance(pos.symbol)
            if balance:
                self._last_known_balance = balance
                pnl_pct = pnl_dollars / balance
            elif self._last_known_balance > 0:
                pnl_pct = pnl_dollars / self._last_known_balance
            else:
                pnl_pct = 0.0

            self.drawdown.register_trade_result(pnl_pct)

            self.risk_engine.record_trade_result(
                pnl_dollars=pnl_dollars,
                pnl_pips=pnl_pips,
                pair=pos.symbol,
                direction=pos.direction,
            )

            if getattr(self, "_governor", None) is not None:
                if balance:
                    self._governor.set_reference_balance(balance)
                self._governor.update_daily_pnl(pnl_dollars)

            # Per-account silo — book the partial against its own account too.
            _acct = self._account_key(pos.symbol)
            if balance:
                self._account_risk.update_balance(_acct, balance)
            self._account_risk.register_realized(_acct, pnl_dollars)

            self._persist_guard_state()

            logger.info(
                "💰 PARTIAL REALISED — {} {} | {} | ${:+.2f} folded into daily tally",
                pos.direction, pos.symbol, label, pnl_dollars,
            )
        except Exception as exc:
            logger.warning("[partial-pnl] accounting failed for {}: {}", pos.symbol, exc)

    def _current_heat_state_name(self) -> str:
        """Current portfolio-heat state name for the mechanical manager.

        Returns one of NORMAL/DEFENSIVE/REDUCING/EMERGENCY. Fail-safe: any error
        or a disabled heat machine reports NORMAL so the trail uses normal width
        rather than tightening on a stale/unknown state (P8).
        """
        try:
            sm = getattr(self, "_portfolio_risk_sm", None)
            if sm is not None and getattr(sm, "state", None) is not None:
                return sm.state.name
        except Exception as exc:
            logger.debug("[P8] heat-state read failed, defaulting NORMAL: {}", exc)
        return "NORMAL"

    def _log_management_configuration(self) -> None:
        """One-time INFO summary of the post-entry management configuration.

        Operators otherwise have no visibility into which management brains and
        protective layers are live versus dead/degraded. Logged once at startup
        as grep-friendly key=value lines under a MANAGEMENT_CONFIG marker (P9).
        """
        try:
            rcfg = self.config.risk
            dcfg = self.config.decision
            strategic = "ENABLED" if self._decision_enabled else "DISABLED"
            governor = "ENABLED" if self._risk_governor is not None else "DISABLED"
            # Legacy active-management checks (C19–C22). These run as the
            # DE-disabled path and as the degraded-mode fallback; surface their
            # per-feature config state so operators know what can fire.
            legacy = (
                f"invalidation={rcfg.continuous_analysis_enabled} "
                f"conviction_collapse={rcfg.conviction_monitoring_enabled} "
                f"htf_candle_close={rcfg.htf_reassessment_enabled} "
                f"dynamic_sl_tighten={rcfg.dynamic_sl_tightening_enabled}"
            )
            logger.info("=" * 60)
            logger.info("MANAGEMENT_CONFIG — post-entry management layer status")
            logger.info(
                "MANAGEMENT_CONFIG strategic_engine={} governor={}",
                strategic, governor,
            )
            logger.info("MANAGEMENT_CONFIG legacy_checks: {}", legacy)
            # Re-entry exists but the executor is not wired — it only LOGS
            # eligible re-entries today (no order is placed).
            logger.info(
                "MANAGEMENT_CONFIG re_entry=LOGGING_ONLY (detector active, no executor)",
            )
            logger.info(
                "MANAGEMENT_CONFIG oq_eq_revalidation={} two_brain_sync=ENABLED "
                "(structure_intact_threshold={} max_age_s={})",
                "ENABLED" if getattr(dcfg, "oq_eq_decay_enabled", False) else "DISABLED",
                self.trade_manager.strategic_structure_intact_threshold,
                self.trade_manager.strategic_structure_max_age_seconds,
            )
            logger.info(
                "MANAGEMENT_CONFIG heat_trail_tighten={} (defensive={} reducing={} emergency={}) "
                "portfolio_heat={}",
                "ENABLED" if rcfg.heat_trail_tighten_enabled else "DISABLED",
                rcfg.heat_trail_factor_defensive,
                rcfg.heat_trail_factor_reducing,
                rcfg.heat_trail_factor_emergency,
                "ENABLED" if rcfg.portfolio_heat_enabled else "DISABLED",
            )
            logger.info(
                "MANAGEMENT_CONFIG fast_opposition_decay={} (min_streak={} max_streak={} "
                "weight={} profit_threshold={}R)",
                "ENABLED" if getattr(dcfg, "fast_opposition_decay_enabled", False) else "DISABLED",
                getattr(dcfg, "fast_opposition_min_streak", 0),
                getattr(dcfg, "fast_opposition_max_streak", 0),
                getattr(dcfg, "fast_opposition_decay_weight", 0.0),
                getattr(dcfg, "fast_opposition_profit_threshold", 0.0),
            )
            logger.info("=" * 60)
        except Exception as exc:
            logger.warning("[P9] management-config summary failed: {}", exc)

    def _log_periodic_management_status(self) -> None:
        """Periodic INFO snapshot of live management mode (P9).

        Emitted every ``management_status_log_interval_cycles`` cycles so
        operators (and alerting) can see the current heat state, position count,
        whether the strategic engine is active/degraded, and the distribution of
        the last decision verdicts. Logging only — no behaviour change.
        """
        try:
            interval = int(getattr(self.config.risk, "management_status_log_interval_cycles", 0))
            if interval <= 0:
                return
            cycles = getattr(self.watchdog, "_cycles", 0)
            if cycles <= 0 or cycles % interval != 0:
                return

            positions = len(self.managed_positions)
            heat_state = self._current_heat_state_name()
            heat_pct = getattr(self, "_current_portfolio_heat", 0.0)

            if not self._decision_enabled:
                strategic_status = "disabled"
            elif self._degraded_management:
                strategic_status = f"degraded({len(self._degraded_management)})"
            else:
                strategic_status = "active"

            verdicts: dict[str, int] = {}
            for action in self._last_decision_action.values():
                key = getattr(action, "name", str(action))
                verdicts[key] = verdicts.get(key, 0) + 1
            verdict_str = (
                " ".join(f"{k}={v}" for k, v in sorted(verdicts.items())) or "none"
            )

            # PR10: visibility into positions stuck against the fast cluster.
            opp_streaks = [
                v for k, v in self._fast_opposition_streak.items()
                if k in self.managed_positions and v > 0
            ]
            fast_opp_count = len(opp_streaks)
            fast_opp_max = max(opp_streaks) if opp_streaks else 0

            logger.info(
                "MANAGEMENT_STATUS cycle={} positions={} heat_state={} heat_pct={:.1f} "
                "strategic={} fast_opp_positions={} fast_opp_max_streak={} last_verdicts: {}",
                cycles, positions, heat_state, heat_pct, strategic_status,
                fast_opp_count, fast_opp_max, verdict_str,
            )
        except Exception as exc:
            logger.debug("[P9] periodic management-status log failed: {}", exc)

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
        exit_cause: Optional[ExitCause] = None,
    ) -> None:
        pip_size = get_pip_size(pos.symbol)
        is_buy = pos.direction == "BUY"
        # P7: normalise the exit cause for the learners. Explicit sites pass an
        # ExitCause directly (tagged at the decision source); broker-side /
        # mechanical reasons that arrive only as strings fall back to a
        # best-effort classification of the free-text outcome.
        cause = exit_cause if isinstance(exit_cause, ExitCause) else ExitCause.from_reason(outcome)
        cause_value = cause.value
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

        # Per-account silo — book the realised P&L against this trade's own
        # account so its daily-loss cap is independent of the other accounts.
        try:
            _acct = self._account_key(pos.symbol)
            if balance:
                self._account_risk.update_balance(_acct, balance)
            self._account_risk.register_realized(_acct, pnl_dollars)
        except Exception as exc:
            logger.debug("[AccountRisk] realized update failed: {}", exc)

        # Persist the full daily risk state AFTER every tally has been updated
        # so a restart restores the post-close picture (not a pre-close one).
        self._persist_guard_state()

        hold_seconds = (datetime.now(timezone.utc) - pos.open_time).total_seconds()
        logger.info(
            "📊 TRADE CLOSED — {} {} | {:.1f}pip | {} | cause={} | {:.0f}s",
            pos.direction,
            pos.symbol,
            pnl_pips,
            outcome,
            cause_value,
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
            exit_cause=cause_value,
        )
        self._run_journal_async(self.journal.log_trade(trade_record))
        self.ml.register_new_trade(exit_cause=cause_value)
        # When the Tuner Agent owns scheduling, give it a chance to run the
        # trade-close-driven tuners (ML learners, EV, planner calibrator) plus
        # the periodic gate tuner — each gated by its own cadence. No-op when
        # the agent is disabled (legacy daily-reset tuning runs instead).
        if self._tuner_agent_active():
            self._tuner_run_trade_close()
        else:
            # Agent off: drive the module governor's transition evaluation
            # directly (the agent's periodic tunable handles it when on).
            self._module_governor_evaluate()
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
                "exit_cause": cause_value,
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

        # ── Outcome feedback — link realised R back to the entry attribution ──
        # Closes the module-accountability loop: per-module / per-horizon
        # accuracy is computed from these joins. Observational only.
        try:
            _plan_sl = float(getattr(pos, "plan_sl_pips", 0.0) or 0.0)
            _fb_pnl_r = (pnl_pips / _plan_sl) if _plan_sl > 1e-8 else (1.0 if pnl_dollars > 0 else -1.0)
            self._record_trade_outcome(pos, pnl_pips, pnl_dollars, _fb_pnl_r, outcome, cause_value)
        except Exception as exc:
            logger.debug("[outcome_feedback] close hook failed for {}: {}", pos.symbol, exc)

        # ── Counterfactual attribution — complete the entry snapshot ─────────
        # Attach the realised R to the decision snapshot captured at entry so
        # the leave-one-out replay can score each module's marginal P&L. Pure
        # analysis; no-op when the engine is disabled.
        try:
            _cf_plan_sl = float(getattr(pos, "plan_sl_pips", 0.0) or 0.0)
            _cf_pnl_r = (pnl_pips / _cf_plan_sl) if _cf_plan_sl > 1e-8 else (1.0 if pnl_dollars > 0 else -1.0)
            self._counterfactual_complete(pos, _cf_pnl_r, outcome, cause_value)
        except Exception as exc:
            logger.debug("[counterfactual] close hook failed for {}: {}", pos.symbol, exc)

        # ── Post-close price tracking — schedule forward MFE/MAE checks ──────
        # Separates entry-signal quality from management quality. Keyed by the
        # broker order id so checks survive a restart. Observational only.
        try:
            tracker = getattr(self, "_post_close_tracker", None)
            if tracker is not None and tracker.enabled:
                # R basis = original entry risk. pos.sl may have moved to BE /
                # trailed, so prefer the plan's entry SL distance when present.
                _plan_sl_pips = float(getattr(pos, "plan_sl_pips", 0.0) or 0.0)
                if _plan_sl_pips > 0:
                    _entry_sl = (
                        pos.entry_price - _plan_sl_pips * pip_size
                        if is_buy
                        else pos.entry_price + _plan_sl_pips * pip_size
                    )
                else:
                    _entry_sl = float(getattr(pos, "sl", 0.0) or 0.0)
                tracker.record_close(
                    trade_id=str(getattr(pos, "order_id", "") or ""),
                    pair=pos.symbol,
                    direction=pos.direction,
                    entry_price=pos.entry_price,
                    exit_price=close_price,
                    exit_cause=cause_value,
                    sl_price=_entry_sl,
                    tp_price=float(getattr(pos, "tp1", 0.0) or 0.0),
                    entry_timestamp=pos.open_time,
                    exit_timestamp=datetime.now(timezone.utc),
                    entry_score=int(getattr(pos, "score", 0) or 0),
                    entry_confluences=list(getattr(pos, "confluences", []) or []),
                )
        except Exception as exc:
            logger.debug("[post_close] schedule failed for {}: {}", pos.symbol, exc)

        # ── Link the realised outcome back to the signals that drove it ──────
        # Pushes PnL / R / exit cause onto every ledger signal tied to this
        # trade so EmitterFeedback can grade taken signals on real results.
        # No-op when the signal ledger is disabled.
        self._ledger_attach_trade_outcome(
            str(getattr(pos, "order_id", "") or ""),
            {
                "pair": pos.symbol,
                "direction": pos.direction,
                "outcome": outcome,
                "exit_cause": cause_value,
                "pnl_dollars": pnl_dollars,
                "pnl_pips": round(pnl_pips, 2),
            },
        )

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
                    "exit_cause": cause_value,
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

    def _audit_severe_thesis_close(self, pos, close_price: float) -> None:
        """Persist a counterfactual shadow after a severe-decay hard-close.

        Uses the EXIT price as the reference and the protective stop that was in
        force, with TP targets at 1.5R/2.5R of the exit-to-stop distance. The
        existing shadow resolver classifies the forward outcome:
          • LOSS / BREAKEVEN  → price returned to the stop ("close was correct")
          • WIN / PARTIAL     → price continued ("sold a future winner")
        Grouped under the 'severe_thesis_close' gate, this surfaces the
        false-positive rate and the R left on the table on the shadow dashboard.
        """
        try:
            if not pos.sl or not close_price:
                return
            risk = abs(close_price - pos.sl)
            if risk <= 0:
                return
            is_long = pos.direction.upper() in ("BUY", "LONG")
            tp1 = close_price + 1.5 * risk if is_long else close_price - 1.5 * risk
            tp2 = close_price + 2.5 * risk if is_long else close_price - 2.5 * risk
            self._persist_shadow_contract(
                SimpleNamespace(
                    pair=pos.symbol,
                    direction=pos.direction,
                    score=getattr(pos, "score", 0),
                    position_size_lots=getattr(pos, "lots", 0.01),
                    entry_timeframe="M5",
                ),
                rejecting_gate="severe_thesis_close",
                entry_price=close_price,
                stop_loss=pos.sl,
                tp1=tp1,
                tp2=tp2,
            )
        except Exception as exc:
            logger.debug("[thesis-audit] counterfactual persist failed: {}", exc)

    def _persist_shadow_contract(
        self, signal, rejecting_gate: str,
        entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        tp1: Optional[float] = None,
        tp2: Optional[float] = None,
        source: str = "planner",
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
                source=source,
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

    def _persist_scanner_rejections(self, report) -> None:
        """Persist shadow contracts for setups rejected at the scanner stage.

        Scanner-stage rejections (OQ/EQ/score thresholds) never reach the
        planner, so they would be invisible to the shadow engine and gate
        auto-tuner. Each carries an approximate trade (synthetic, coarse) so its
        counterfactual outcome can be resolved. Marked ``source=
        'scanner_approximation'`` to distinguish from planner-derived contracts.
        """
        rejected = getattr(report, "rejected_setups", None)
        if not rejected:
            return
        for rs in rejected:
            try:
                pip_size = get_pip_size(rs.symbol)
                contract = ShadowContract(
                    contract_id=new_contract_id(),
                    symbol=rs.symbol,
                    direction=rs.direction,
                    entry_price=rs.approximate_entry,
                    stop_loss=rs.approximate_sl,
                    tp1=rs.approximate_tp,
                    tp2=rs.approximate_tp,
                    tp3=None,
                    pip_size=pip_size,
                    entry_timeframe="M5",
                    rejecting_gate=rs.rejecting_gate,
                    score=int(rs.score) if rs.score is not None else 0,
                    ts_utc_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                    correlation_id=getattr(self, "_current_cycle_id", None),
                    source="scanner_approximation",
                )
                cid = self._shadow_store.insert_contract(contract)
                if cid:
                    store = get_event_store()
                    if store:
                        store.emit(
                            event_type=SHADOW_CONTRACT_CREATED,
                            severity="INFO",
                            symbol=rs.symbol,
                            correlation_id=getattr(self, "_current_cycle_id", None),
                            source_module="platforms.main_loop",
                            payload={
                                "contract_id": cid,
                                "rejecting_gate": rs.rejecting_gate,
                                "entry_price": rs.approximate_entry,
                                "stop_loss": rs.approximate_sl,
                                "tp1": rs.approximate_tp,
                                "tp2": rs.approximate_tp,
                                "direction": rs.direction,
                                "source": "scanner_approximation",
                            },
                        )
            except Exception as exc:
                logger.debug("[ShadowContract] scanner rejection persist failed for {}: {}", rs.symbol, exc)

    # ── Decision trace helpers (guarded — never break the live loop) ──────
    def _trace_begin(self, pair: str) -> None:
        try:
            self._trace_recorder.begin(
                pair,
                cycle_id=getattr(self, "_current_cycle_id", "") or "",
                setup_id=getattr(self, "_current_setup_id", "") or "",
            )
        except Exception as exc:
            logger.debug("[decision_trace] begin failed for {}: {}", pair, exc)

    def _trace_stamp(self, stage: str, owner: str, verdict: str, justification: str,
                     *, evidence: dict | None = None, confidence: float = 1.0,
                     blocking: bool = False) -> None:
        try:
            self._trace_recorder.stamp(
                stage, owner, verdict, justification,
                evidence=evidence, confidence=confidence, blocking=blocking,
            )
        except Exception as exc:
            logger.debug("[decision_trace] stamp '{}' failed: {}", stage, exc)

    def _trace_challenge(self, challenger: str, target_stage: str, reason: str) -> None:
        try:
            self._trace_recorder.challenge(challenger, target_stage, reason)
        except Exception as exc:
            logger.debug("[decision_trace] challenge failed: {}", exc)

    def _trace_finalize_success(self) -> None:
        try:
            self._trace_recorder.finalize_success()
        except Exception as exc:
            logger.debug("[decision_trace] finalize_success failed: {}", exc)

    def _trace_finalize_abandoned(self, reason: str = "") -> None:
        try:
            self._trace_recorder.finalize_abandoned(reason)
        except Exception as exc:
            logger.debug("[decision_trace] finalize_abandoned failed: {}", exc)

    # ── Orchestrator + outcome-feedback helpers (guarded) ─────────────────
    def _select_candidate(self, result):
        """The ranker opportunity driving this entry (matching dir + horizon)."""
        candidates = getattr(result, "candidates", None) or []
        if not candidates:
            return None
        direction = getattr(result, "direction", "")
        horizon = getattr(result, "selected_horizon", "") or ""
        for c in candidates:
            if getattr(c, "direction", "") == direction and (
                not horizon or getattr(c, "timeframe_class", "") == horizon
            ):
                return c
        for c in candidates:
            if getattr(c, "direction", "") == direction:
                return c
        return candidates[0]

    def _risk_headroom_multiplier(self, result, entry_decision) -> float:
        """Accumulated analytical-risk multiplier for the orchestrator (#24/#23).

        Folds the risk governor's graded entry review (heat / spread / R:R,
        carried on ``entry_decision.risk_multiplier``) together with the live
        trade-slot headroom (how full the position book is) into one bounded
        ``[risk_multiplier_floor, 1.0]`` factor. The hard physics gates
        (correlation conflict, margin, the max-trades cap) already ran upstream
        as absolute blocks; this only sizes a *near-limit* survivor DOWN so a
        trade taken with little headroom rides smaller. Returns 1.0 (neutral) on
        any failure — never blocks the entry.
        """
        floor = float(getattr(self.config.orchestrator, "risk_multiplier_floor", 0.15))
        try:
            review_mult = (
                float(getattr(entry_decision, "risk_multiplier", 1.0) or 1.0)
                if entry_decision is not None else 1.0
            )
            dims = []
            try:
                max_trades = int(self.config.risk.max_open_trades)
                open_count = len(self.managed_positions)
                if max_trades > 0:
                    dims.append(ra.dimension(
                        "trade_slots", open_count + 1, max_trades,
                        f"slot {open_count + 1}/{max_trades}",
                    ))
            except Exception:  # noqa: BLE001
                pass
            headroom = ra.accumulate(dims, floor=floor).multiplier if dims else 1.0
            return max(floor, min(1.0, headroom * review_mult))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[orchestrator] risk headroom unavailable: {}", exc)
            return 1.0

    def _evaluate_orchestrator(self, result, sa, entry_decision, plan, plan_ctx, signal=None):
        """Build a TradeProposal from the collected evidence and grade it."""
        opp = self._select_candidate(result)
        advisor_agreement = None
        advisor_vector: dict = {}
        if plan is not None:
            agree = getattr(plan, "advisor_agreement", None)
            advisor_agreement = float(agree) if agree is not None else None
        if plan_ctx is not None:
            try:
                av = self._planner.advisor_vector(plan_ctx)
                advisor_vector = av.get("advisors", {}) or {}
                if advisor_agreement is None:
                    advisor_agreement = float(av.get("agreement")) if av.get("agreement") is not None else None
            except Exception as exc:
                logger.debug("[orchestrator] advisor vector unavailable: {}", exc)

        # Phase 9 gate-softening multipliers (1.0 = the gate passed cleanly).
        gate_mult = float(getattr(result, "gate_quality_multiplier", 1.0) or 1.0)
        planner_mult = float(getattr(plan, "gate_quality_multiplier", 1.0) or 1.0) if plan is not None else 1.0
        entry_mult = float(getattr(signal, "entry_quality_multiplier", 1.0) or 1.0) if signal is not None else 1.0

        # #24 / #23 — accumulated analytical-risk dimmer: the risk governor's
        # graded entry review (heat / spread / R:R) and the portfolio governor's
        # graded concentration limits (currency / sector / correlated), combined
        # with the live trade-slot headroom. Physics already enforced as hard
        # gates above; this only sizes a near-limit trade DOWN.
        risk_mult = self._risk_headroom_multiplier(result, entry_decision)
        gov_risk = float(getattr(plan, "governor_risk_multiplier", 1.0) or 1.0) if plan is not None else 1.0
        risk_mult = max(
            float(getattr(self.config.orchestrator, "risk_multiplier_floor", 0.15)),
            min(1.0, risk_mult * gov_risk),
        )

        proposal = TradeProposal(
            pair=result.pair,
            direction=getattr(result, "direction", ""),
            horizon=getattr(result, "selected_horizon", "") or "",
            ranker_ev=(float(opp.expected_value) if opp is not None else None),
            ranker_coherence=(float(opp.coherence) if opp is not None else None),
            ranker_confidence=(float(opp.confidence) if opp is not None else None),
            candidate_count=len(getattr(result, "candidates", []) or []),
            tf_alignment=(float(getattr(sa, "tf_alignment", 0.0)) if sa is not None else None),
            tf_vector=(sa.tf_vector() if sa is not None and hasattr(sa, "tf_vector") else {}),
            de_margin=(float(getattr(entry_decision, "entry_margin", 0.0)) if entry_decision is not None else None),
            de_conviction=(float(getattr(entry_decision, "conviction", 0.0)) if entry_decision is not None else None),
            de_quality_multiplier=(
                float(getattr(entry_decision, "de_quality_multiplier", 1.0))
                if entry_decision is not None and getattr(entry_decision, "gate_softened", False)
                else None
            ),
            advisor_agreement=advisor_agreement,
            advisor_vector=advisor_vector,
            scan_score=float(getattr(result, "score", 0) or 0),
            risk_multiplier=risk_mult,
            gate_quality_multiplier=gate_mult,
            planner_quality_multiplier=planner_mult,
            entry_quality_multiplier=entry_mult,
        )
        verdict = self._orchestrator.evaluate(proposal)
        # Stash the proposal alongside the verdict so the recorder/feedback can
        # serialise the full evidence without recomputing it.
        verdict._proposal = proposal  # type: ignore[attr-defined]
        return verdict

    def _record_orchestrator_proposal(self, result, verdict, applied: bool) -> None:
        try:
            store = get_event_store()
            if store is None:
                return
            payload = verdict.to_dict()
            payload["applied"] = bool(applied)
            proposal = getattr(verdict, "_proposal", None)
            if proposal is not None:
                payload["proposal"] = proposal.to_dict()
            store.emit(
                event_type=ORCHESTRATOR_PROPOSAL,
                severity="INFO",
                symbol=getattr(result, "pair", ""),
                correlation_id=getattr(self, "_current_cycle_id", None),
                parent_id=getattr(self, "_current_setup_id", None),
                source_module="brain.orchestrator",
                payload=payload,
            )
        except Exception as exc:
            logger.debug("[orchestrator] proposal persist failed: {}", exc)

    def _record_entry_attribution(self, trade_key, result, verdict, entry_decision) -> None:
        """Persist which modules/opportunity drove a placed trade (feedback loop)."""
        fb = getattr(self, "_outcome_feedback", None)
        if fb is None or not fb.enabled or not trade_key:
            return
        try:
            opp = self._select_candidate(result)
            votes_map: dict = {}
            for v in getattr(result, "votes", None) or []:
                module = str(getattr(v, "module", "") or "")
                direction = str(getattr(v, "direction", "NEUTRAL") or "NEUTRAL")
                if module and direction in ("LONG", "SHORT"):
                    votes_map[module] = [direction, round(float(getattr(v, "confidence", 0.0) or 0.0), 4)]
            attribution = {
                "pair": getattr(result, "pair", ""),
                "direction": getattr(result, "direction", ""),
                "horizon": getattr(result, "selected_horizon", "") or "",
                "scan_score": int(getattr(result, "score", 0) or 0),
                "ranker_ev": (round(float(opp.expected_value), 4) if opp is not None else None),
                "ranker_confidence": (round(float(opp.confidence), 4) if opp is not None else None),
                "contributors": (list(getattr(opp, "contributors", []) or []) if opp is not None else []),
                "votes": votes_map,
                "de_conviction": (round(float(getattr(entry_decision, "conviction", 0.0) or 0.0), 4) if entry_decision is not None else None),
                "orchestrator_size_multiplier": (round(float(verdict.size_multiplier), 4) if verdict is not None else None),
                "cycle_id": getattr(self, "_current_cycle_id", "") or "",
                "setup_id": getattr(self, "_current_setup_id", "") or "",
            }
            fb.record_entry(str(trade_key), attribution)
        except Exception as exc:
            logger.debug("[outcome_feedback] entry attribution failed: {}", exc)

    def _snapshot_entry_health(self, trade_key, result, verdict, sa) -> None:
        """Capture the entry-evidence baseline so live management can compare.

        Stored keyed by broker order id: the orchestrator's graded entry size
        multiplier (a proxy for entry conviction/health), the situation read at
        open (structure integrity + HTF alignment) and the selected horizon.
        Read back by ``_build_position_evidence`` for thesis-integrity and
        situation-shift dimensions. Best-effort — never blocks the entry.
        """
        if not trade_key:
            return
        try:
            snap = {
                "horizon": getattr(result, "selected_horizon", "") or "",
                "entry_health": (
                    round(float(verdict.size_multiplier), 4) if verdict is not None else None
                ),
                "structure_integrity": (
                    round(float(getattr(sa, "structure_integrity", 0.0)), 4) if sa is not None else None
                ),
                "tf_alignment": (
                    round(float(getattr(sa, "tf_alignment", 0.0)), 4) if sa is not None else None
                ),
            }
            self._entry_health_snapshot[str(trade_key)] = snap
        except Exception as exc:
            logger.debug("[orchestrator/health] entry snapshot failed: {}", exc)

    def _record_trade_outcome(self, pos, pnl_pips, pnl_dollars, pnl_r, outcome, cause_value) -> None:
        """Link a closed trade's realised R back to its entry attribution."""
        fb = getattr(self, "_outcome_feedback", None)
        if fb is None or not fb.enabled:
            return
        try:
            key = getattr(pos, "order_id", "")
            if not key:
                return
            payload = {
                "pair": pos.symbol,
                "direction": pos.direction,
                "pnl_r": round(float(pnl_r), 4),
                "pnl_pips": round(float(pnl_pips), 2),
                "pnl_dollars": round(float(pnl_dollars), 2),
                "won": float(pnl_dollars) > 0,
                "outcome": outcome,
                "exit_cause": cause_value,
            }
            fb.record_outcome(str(key), payload)
            store = get_event_store()
            if store is not None:
                store.emit(
                    event_type=OUTCOME_FEEDBACK,
                    severity="INFO",
                    symbol=pos.symbol,
                    source_module="brain.outcome_feedback",
                    payload={"trade_key": str(key), **payload},
                )
        except Exception as exc:
            logger.debug("[outcome_feedback] outcome record failed: {}", exc)

    def _signal_ledger_active(self) -> bool:
        return self._signal_ledger is not None and getattr(
            self.config.signal_ledger, "signal_ledger_enabled", False
        )

    @staticmethod
    def _current_price_for(pair: str, market_data: dict) -> Optional[float]:
        """Latest close for a pair from the freshest intraday frame available."""
        frames = (market_data or {}).get(pair)
        if not frames:
            return None
        for tf in ("M1", "M5", "M15", "H1"):
            df = frames.get(tf)
            try:
                if df is not None and len(df) > 0:
                    return float(df["close"].iloc[-1])
            except Exception:  # noqa: BLE001
                continue
        return None

    def _record_scan_signals(self, report, market_data: dict) -> None:
        """Record every module's directional read this cycle, before any gate.

        Each result carries the raw per-module ``votes`` plus the scalar
        ``consensus_direction`` that fed both the consensus and the ranker. We
        log every non-NEUTRAL vote (emitter = module name) and the consensus
        verdict (emitter = "consensus") with the price at emission, so blocked
        signals are graded too — removing the learning layer's selection bias.
        Fully guarded: any failure is swallowed and never blocks the scan.
        """
        if not self._signal_ledger_active():
            return
        try:
            from adaptive.signal_ledger import SignalRecord

            results = getattr(report, "results", None) or []
            ts = _time.time()
            for result in results:
                pair = getattr(result, "pair", None)
                if not pair:
                    continue
                price = self._current_price_for(pair, market_data)
                if not price:
                    continue
                for vote in getattr(result, "votes", []) or []:
                    direction = getattr(vote, "direction", "NEUTRAL")
                    if direction not in ("LONG", "SHORT"):
                        continue
                    module = getattr(vote, "module", "unknown")
                    ctx = {"weight": float(getattr(vote, "weight", 0.0) or 0.0)}
                    # Tag signals from a shadowed/disabled module so they are
                    # distinguishable in the ledger (still graded, but the vote
                    # was suppressed in the live decision).
                    gov = self._module_governor
                    if gov is not None:
                        try:
                            if gov.is_suppressed(module):
                                ctx["shadow"] = True
                                ctx["module_mode"] = gov.mode_for(module).value
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("[module-governor] tag failed for {}: {}", module, exc)
                    self._signal_ledger.record_signal(SignalRecord(
                        pair=pair,
                        emitter=module,
                        direction=direction,
                        strength=float(getattr(vote, "confidence", 0.0) or 0.0),
                        price_at_signal=price,
                        context=ctx,
                        timestamp=ts,
                    ))
                consensus_dir = getattr(result, "consensus_direction", "") or getattr(result, "direction", "")
                if consensus_dir in ("LONG", "SHORT"):
                    self._signal_ledger.record_signal(SignalRecord(
                        pair=pair,
                        emitter="consensus",
                        direction=consensus_dir,
                        strength=float(getattr(result, "consensus_agreement", 0.0) or 0.0),
                        price_at_signal=price,
                        context={
                            "net": float(getattr(result, "consensus_net", 0.0) or 0.0),
                            "score": int(getattr(result, "score", 0) or 0),
                        },
                        timestamp=ts,
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[signal-ledger] record_scan_signals failed: {}", exc)

    def _run_signal_grading(self, market_data: dict) -> None:
        """Grade outstanding signals against this cycle's prices (guarded)."""
        if not self._signal_ledger_active():
            return
        if not getattr(self.config.signal_ledger, "signal_grading_enabled", False):
            return
        try:
            prices: dict = {}
            for pair, frames in (market_data or {}).items():
                px = self._current_price_for(pair, {pair: frames})
                if px:
                    prices[pair] = px
            if prices:
                self._signal_ledger.run_grading_cycle(prices)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[signal-ledger] grading cycle failed: {}", exc)

    def _ledger_record_gate_block(self, pair: str, reason: str) -> None:
        """Attribute a gate rejection to this pair's emitted signals (guarded)."""
        if not self._signal_ledger_active() or not pair:
            return
        try:
            self._signal_ledger.record_gate_block_for_pair(pair, reason)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[signal-ledger] gate-block hook failed: {}", exc)

    def _ledger_record_trade_opened(self, pair: str, direction: str, trade_id: str) -> None:
        """Link a pair's matching-direction signals to the opened trade (guarded)."""
        if not self._signal_ledger_active() or not pair or not trade_id:
            return
        try:
            self._signal_ledger.record_trade_opened_for_pair(pair, str(trade_id), direction)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[signal-ledger] trade-open hook failed: {}", exc)

    def _ledger_attach_trade_outcome(self, trade_id: str, outcome: dict) -> None:
        """Merge a closed trade's realised result onto its ledger signals so the
        emitter-feedback layer can grade taken signals on real PnL (guarded)."""
        if not self._signal_ledger_active() or not trade_id:
            return
        try:
            self._signal_ledger.attach_trade_outcome(str(trade_id), outcome or {})
        except Exception as exc:  # noqa: BLE001
            logger.debug("[signal-ledger] attach-outcome hook failed: {}", exc)

    # ── Counterfactual attribution hooks ─────────────────────────────────

    def _counterfactual_active(self) -> bool:
        return self._counterfactual is not None and getattr(
            self._counterfactual, "enabled", False
        )

    def _counterfactual_record_open(self, result, trade_id: str, direction: str) -> None:
        """Snapshot the vote panel + consensus config that opened a trade so the
        attribution engine can replay the consensus leave-one-out (guarded)."""
        if not self._counterfactual_active() or not trade_id or result is None:
            return
        try:
            from adaptive.counterfactual import TradeAttribution

            votes_snapshot: list[dict] = []
            for v in getattr(result, "votes", []) or []:
                votes_snapshot.append({
                    "module": str(getattr(v, "module", "")),
                    "direction": str(getattr(v, "direction", "NEUTRAL")),
                    "confidence": float(getattr(v, "confidence", 0.0) or 0.0),
                    "weight": float(getattr(v, "weight", 0.0) or 0.0),
                })
            if not votes_snapshot:
                return  # nothing to attribute (legacy single-module path)

            cc = self.config.consensus
            rc = getattr(self.config, "opportunity_ranker", None)
            thresholds = {
                "min_net_score": float(cc.min_net_score),
                "min_agreement": float(cc.min_agreement),
                "high_authority_modules": list(cc.high_authority_modules or []),
                "high_authority_oppose_confidence": float(cc.high_authority_oppose_confidence),
                "min_contributors": int(cc.min_contributors),
            }
            ranker_kwargs: dict = {}
            if rc is not None:
                ranker_kwargs = {
                    "execute": bool(getattr(rc, "execute", False)),
                    "rescue_neutral_consensus": bool(getattr(rc, "rescue_neutral_consensus", False)),
                    "scalp_modules": list(getattr(rc, "scalp_modules", []) or []),
                    "swing_modules": list(getattr(rc, "swing_modules", []) or []),
                    "scalp_reward_risk": float(getattr(rc, "scalp_reward_risk", 1.5)),
                    "swing_reward_risk": float(getattr(rc, "swing_reward_risk", 2.5)),
                    "base_win_rate": float(getattr(rc, "base_win_rate", 0.40)),
                    "confidence_win_rate_gain": float(getattr(rc, "confidence_win_rate_gain", 0.40)),
                    "min_expected_value": float(getattr(rc, "min_expected_value", 0.0)),
                    "min_cluster_confidence": float(getattr(rc, "min_cluster_confidence", 0.0)),
                    "min_cluster_contributors": int(getattr(rc, "min_cluster_contributors", 1)),
                }
            self._counterfactual.record_open(TradeAttribution(
                trade_id=str(trade_id),
                pair=str(getattr(result, "pair", "")),
                direction=str(direction),
                timeframe_class=str(getattr(result, "selected_horizon", "") or ""),
                votes=votes_snapshot,
                consensus_direction=str(getattr(result, "consensus_direction", "") or ""),
                consensus_net=float(getattr(result, "consensus_net", 0.0) or 0.0),
                consensus_agreement=float(getattr(result, "consensus_agreement", 0.0) or 0.0),
                thresholds=thresholds,
                ranker_kwargs=ranker_kwargs,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[counterfactual] record_open hook failed: {}", exc)

    def _counterfactual_complete(self, pos, pnl_r: float, outcome: str, cause_value: str) -> None:
        """Attach a closed trade's realised R to its entry snapshot (guarded)."""
        if not self._counterfactual_active():
            return
        try:
            trade_id = str(getattr(pos, "order_id", "") or "")
            if not trade_id:
                return
            self._counterfactual.complete(trade_id, {
                "pnl_r": round(float(pnl_r), 4),
                "won": bool(float(pnl_r) > 0),
                "outcome": str(outcome or ""),
                "exit_cause": str(cause_value or ""),
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("[counterfactual] complete hook failed: {}", exc)

    def _log_rejection(self, pair: str, direction: str, score: int, reason: str,
                       entry_context: dict | None = None) -> None:
        logger.debug("❌ REJECTED {} {} (score {}) — {}", direction, pair, score, reason)
        # Attribute this rejection to the pair's emitted signals so blocked
        # signals are graded and gate over-filtering is measurable. No-op when
        # the ledger is off.
        self._ledger_record_gate_block(pair, reason)
        # Close out the awareness trace for this setup (if one is open) so the
        # rejection is attributed to the gate that blocked it. No-op when
        # tracing is off or no trace is active.
        try:
            self._trace_recorder.finalize_rejection(reason)
        except Exception as exc:
            logger.debug("[decision_trace] finalize_rejection failed: {}", exc)
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
            # Keep the RiskEngine's own daily tally / drawdown mode in lock-step
            # with the governor — otherwise a FROZEN mode never lifts at the
            # day boundary and blocks all entries indefinitely.
            try:
                self.risk_engine.reset_daily()
            except Exception as exc:
                logger.debug("[RiskEngine] daily reset failed: {}", exc)
            # Reset every per-account daily tally / loss-cap halt too.
            try:
                self._account_risk.reset_daily()
            except Exception as exc:
                logger.debug("[AccountRisk] daily reset failed: {}", exc)
            # The loop's primary DrawdownGuard only un-freezes inside
            # register_trade_result → _roll_day_if_needed, i.e. when a trade
            # closes. A freeze with no open positions left to close (margin /
            # daily-loss flatten, or a -5% day that flattened the book) would
            # otherwise persist across days and block EVERY new entry forever.
            # Roll its day here so a stale FROZEN lifts to RECOVERY at the
            # boundary, then persist so a later restart doesn't reload the
            # stale freeze.
            try:
                from brain.drawdown_guard import DrawdownMode
                _dd_was_frozen = self.drawdown.mode == DrawdownMode.FROZEN
                self.drawdown.reset_daily(datetime.now(timezone.utc))
                if _dd_was_frozen and self.drawdown.mode != DrawdownMode.FROZEN:
                    logger.info(
                        "[DrawdownGuard] NEW DAY: stale FROZEN lifted → {}",
                        self.drawdown.mode.value,
                    )
                    self._persist_guard_state()
            except Exception as exc:
                logger.debug("[DrawdownGuard] daily reset failed: {}", exc)
            if self.maintenance.should_run():
                try:
                    maint_result = self.maintenance.run()
                    logger.info("🧹 Daily maintenance — {}", maint_result)
                except Exception as exc:
                    logger.warning("Maintenance error: {}", exc)
                # Bound the audit/event DB — retention was never wired in, so
                # apex_events.db had grown into the hundreds of MB. Prune daily.
                try:
                    from persistence.event_store import get_event_store

                    pruned = get_event_store().prune()
                    if pruned:
                        logger.info("🧹 Event store pruned — {} old events removed", pruned)
                        # Reclaim the freed disk space (DELETE alone does not
                        # shrink the file). Runs in daily maintenance only.
                        get_event_store().vacuum()
                except Exception as exc:
                    logger.debug("[maintenance] event-store prune failed: {}", exc)

        if self._tuner_agent_active():
            # All tuning is coordinated by the Tuner Agent on trade close / scan
            # cycle; the legacy scattered triggers are skipped to avoid double
            # tuning. Non-tuning maintenance (shadows, backup) still runs.
            self._maybe_resolve_shadows()
            self._maybe_backup_data()
            return

        if self.ml.should_retrain():
            self._run_ml_optimization()

        self._maybe_calibrate_planner()

        self._maybe_calibrate_gates()

        self._maybe_resolve_shadows()

        self._maybe_backup_data()

    def _maybe_resolve_shadows(self) -> None:
        """Periodically discard stale un-executed shadow contracts.

        Resolution itself now happens LIVE, every cycle, in `_advance_shadows`
        (full-fidelity: real TradeManager + decision engine + governor on the
        live feed — no historical CSV replay). This method only ages out old
        PENDING contracts that were never managed forward, keeping the DB bounded.
        """
        now = _time.time()
        if now - self._last_shadow_resolve_time < self._shadow_resolve_interval_seconds:
            return
        self._last_shadow_resolve_time = now
        try:
            cutoff_ms = int((now - self._shadow_max_pending_age_seconds) * 1000)
            discarded = self._shadow_store.discard_stale_pending(cutoff_ms)
            if discarded:
                logger.info(
                    "👻 Shadow — discarded {} stale pending contracts (older than {:.0f}d)",
                    discarded, self._shadow_max_pending_age_seconds / 86400,
                )
        except Exception as exc:
            logger.debug("[shadow] discard stale pending failed: {}", exc)

    def _maybe_calibrate_gates(self) -> None:
        """Tune quality-gate thresholds from shadow outcomes (bounded, logged).

        If a tunable quality gate's rejected setups would have won, loosen it a
        little within its envelope; if they would have lost, tighten back toward
        neutral. Safety/physical gates are never touched.
        """
        now = _time.time()
        if now - self._last_gate_tune_time < self._gate_tune_interval_seconds:
            return
        self._last_gate_tune_time = now
        try:
            outcomes = self._shadow_store.get_outcomes_by_gate()
            changes = self._gate_tuner.calibrate(outcomes)
            if changes:
                logger.info(
                    "🎛️ Gate auto-tune — {} quality gate(s) adjusted from shadow outcomes",
                    len(changes),
                )
            # Observability: surface counterfactual stats for the high-authority
            # gates too (decision_engine, planner, regime_threshold, risk_engine,
            # governor, ...). These are never auto-tuned, but knowing whether
            # their rejected setups would have won is the key to spotting an
            # alpha-suppressing bottleneck.
            self._last_gate_counterfactuals = self._gate_tuner.summarize(outcomes)
            high_auth = {
                f: s for f, s in self._last_gate_counterfactuals.items()
                if not s["auto_tuned"] and s["rejected_resolved"] >= GateTuner.MIN_SAMPLES
            }
            if high_auth:
                logger.info(
                    "📊 Gate counterfactual review (observability-only) — {}",
                    high_auth,
                )
        except Exception as exc:
            logger.debug("[gate-tuner] calibration failed: {}", exc)

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

            # Re-inject freshly optimized scoring weights into the live scanner
            # so the new weights take effect this run instead of only after a
            # restart (otherwise re-optimized weights sit stale on disk).
            try:
                if self.config.scoring.use_adaptive_scoring_weights:
                    self.scanner._adaptive_weights = self._scanner_weight_payload(
                        self.ml.optimizer
                    )
                    logger.info("[retrain] live scanner scoring weights reloaded")
            except Exception as exc:
                logger.debug("[retrain] scanner weight reload failed: {}", exc)
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
