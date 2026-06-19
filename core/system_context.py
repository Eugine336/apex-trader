"""APEX TRADER — System Context.

Shared subsystem container that provides the event-driven system with access
to the same risk, decision, and lifecycle subsystems that TradingLoop uses.

Phase 1: risk layer (DrawdownGuard, PortfolioRiskStateMachine,
PortfolioGovernor, AccountRiskManager, CorrelationEngine, RiskEngine,
RiskReporter).
Phase 2: decision intelligence (DecisionEngine, SituationEngine,
RiskGovernor, DecisionJournal, SessionEngine, NewsGuard).
Phase 3: scan pipeline + sizing (PairRanker, OpportunityExecutor,
Orchestrator, SystemVolatilityMonitor, OpportunityDensityTracker,
EntryEngine, ExecutionMonitor).
Phase 4: learning + feedback (OutcomeFeedback, SignalLedger,
EmitterFeedbackService, VoteCalibrator, ModuleGovernor, PostCloseTracker,
GateTuner, CounterfactualEngine, InteractionAnalyzer, ShadowStore,
TunerAgent, AdaptiveOptimizer).

Usage::

    ctx = SystemContext.create(config, platform_manager)
    ed_system = EventDrivenSystem(config, platform_manager, ctx)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from brain.correlation_engine import CorrelationEngine
    from brain.drawdown_guard import DrawdownGuard
    from brain.execution_monitor import ExecutionMonitor
    from brain.opportunity_density import OpportunityDensityTracker
    from brain.orchestrator import Orchestrator
    from brain.outcome_feedback import OutcomeFeedback
    from brain.regime_detector import SystemVolatilityMonitor
    from brain.session_engine import NewsGuard, SessionEngine
    from config import AppConfig
    from decision.engine import DecisionEngine
    from decision.governor import RiskGovernor
    from decision.journal import DecisionJournal
    from decision.situation import SituationEngine
    from governor.portfolio_governor import PortfolioGovernor
    from management.opportunity_executor import OpportunityExecutor
    from platforms.platform_manager import PlatformManager
    from risk.account_risk import AccountRiskManager
    from risk.portfolio_risk_state import PortfolioRiskStateMachine
    from risk.risk_engine import RiskEngine
    from risk.risk_reporter import RiskReporter
    from scanner.pair_ranker import PairRanker
    from trigger.entry_engine import EntryEngine

    from adaptive.counterfactual import CounterfactualEngine
    from adaptive.emitter_feedback import EmitterFeedbackService
    from adaptive.gate_tuner import GateTuner
    from adaptive.interaction_discovery import InteractionAnalyzer
    from adaptive.module_governor import ModuleGovernor
    from adaptive.optimizer import AdaptiveOptimizer
    from adaptive.post_close_tracker import PostCloseTracker
    from adaptive.signal_ledger import SignalLedger
    from adaptive.tuner_agent import TunerAgent
    from adaptive.vote_calibrator import VoteCalibrator
    from persistence.shadow_store import ShadowStore


@dataclass
class SystemContext:
    """Shared subsystem references consumed by the event-driven system.

    Each subsystem is Optional — a missing subsystem disables the
    corresponding safety gate (logged at startup, never crashes).
    """

    # ── Risk layer ───────────────────────────────────────────────────
    risk_engine: Optional[RiskEngine] = None
    drawdown_guard: Optional[DrawdownGuard] = None
    correlation_engine: Optional[CorrelationEngine] = None
    portfolio_risk_sm: Optional[PortfolioRiskStateMachine] = None
    account_risk: Optional[AccountRiskManager] = None
    portfolio_governor: Optional[PortfolioGovernor] = None
    risk_reporter: Optional[RiskReporter] = None

    # ── Decision intelligence ────────────────────────────────────────
    decision_engine: Optional[DecisionEngine] = None
    situation_engine: Optional[SituationEngine] = None
    risk_governor: Optional[RiskGovernor] = None
    decision_journal: Optional[DecisionJournal] = None
    session_engine: Optional[SessionEngine] = None
    news_guard: Optional[NewsGuard] = None

    # ── Scan pipeline + sizing (Phase 3) ──────────────────────────────
    pair_ranker: Optional[PairRanker] = None
    opportunity_executor: Optional[OpportunityExecutor] = None
    orchestrator: Optional[Orchestrator] = None
    system_volatility_monitor: Optional[SystemVolatilityMonitor] = None
    opportunity_density_tracker: Optional[OpportunityDensityTracker] = None
    entry_engine: Optional[EntryEngine] = None
    execution_monitor: Optional[ExecutionMonitor] = None

    # ── Learning + feedback (Phase 4) ────────────────────────────────
    outcome_feedback: Optional[OutcomeFeedback] = None
    signal_ledger: Optional[SignalLedger] = None
    emitter_feedback: Optional[EmitterFeedbackService] = None
    vote_calibrator: Optional[VoteCalibrator] = None
    module_governor: Optional[ModuleGovernor] = None
    post_close_tracker: Optional[PostCloseTracker] = None
    gate_tuner: Optional[GateTuner] = None
    counterfactual_engine: Optional[CounterfactualEngine] = None
    interaction_analyzer: Optional[InteractionAnalyzer] = None
    shadow_store: Optional[ShadowStore] = None
    tuner_agent: Optional[TunerAgent] = None
    ml_adapter: Optional[AdaptiveOptimizer] = None

    # ── Ops / Dashboard / Persistence (Phase 5) ─────────────────────
    trade_journal: Optional[Any] = None
    process_watchdog: Optional[Any] = None
    daily_maintenance: Optional[Any] = None

    # ── Evolution engines (Phase 6) ──────────────────────────────────
    capital_allocator: Optional[Any] = None
    execution_profiles: Optional[Any] = None
    regime_detector: Optional[Any] = None
    behavior_discovery: Optional[Any] = None
    signal_discovery: Optional[Any] = None
    virtual_module_registry: Optional[Any] = None
    virtual_signal_manager: Optional[Any] = None

    # ── Planning + shadow (Phase 6) ──────────────────────────────────
    trade_planner: Optional[Any] = None
    outcome_logger: Optional[Any] = None
    calibrator: Optional[Any] = None
    re_entry_manager: Optional[Any] = None

    # ── Account key cache (symbol → broker:account_id) ───────────────
    _account_key_cache: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        config: AppConfig,
        platform_manager: PlatformManager,
    ) -> SystemContext:
        """Build a SystemContext with all risk-layer subsystems.

        Mirrors the initialization in ``TradingLoop.__init__`` for the risk
        subsystems so both code paths share identical business-rule parameters.
        Any subsystem that fails to build is left ``None`` (logged, never fatal).
        """
        from brain.correlation_engine import CorrelationEngine
        from brain.drawdown_guard import DrawdownGuard
        from governor import PortfolioGovernor
        from risk.account_risk import AccountRiskManager
        from risk.portfolio_risk_state import PortfolioRiskStateMachine
        from risk.risk_engine import RiskEngine
        from risk.risk_reporter import RiskReporter

        risk_cfg = config.risk
        ctx = cls()

        # ── DrawdownGuard ────────────────────────────────────────────
        try:
            ctx.drawdown_guard = DrawdownGuard(
                rolling_window_days=risk_cfg.drawdown_rolling_window_days,
            )
        except Exception as exc:
            logger.warning("[SystemContext] DrawdownGuard init failed: {}", exc)

        # ── CorrelationEngine ────────────────────────────────────────
        try:
            ctx.correlation_engine = CorrelationEngine(
                max_correlated_trades=risk_cfg.max_correlated_trades,
                max_cluster_same_direction=risk_cfg.max_cluster_same_direction,
                allow_intentional_hedge=risk_cfg.allow_intentional_hedge,
            )
        except Exception as exc:
            logger.warning("[SystemContext] CorrelationEngine init failed: {}", exc)

        # ── RiskEngine ───────────────────────────────────────────────
        try:
            ctx.risk_engine = RiskEngine(config=config)
        except Exception as exc:
            logger.warning("[SystemContext] RiskEngine init failed: {}", exc)

        # ── PortfolioRiskStateMachine ────────────────────────────────
        try:
            if risk_cfg.portfolio_risk_engine_enabled:
                ctx.portfolio_risk_sm = PortfolioRiskStateMachine(
                    heat_defensive_pct=risk_cfg.heat_defensive_pct,
                    heat_recovery_pct=risk_cfg.heat_recovery_pct,
                    recovery_dwell_seconds=risk_cfg.recovery_dwell_seconds,
                    heat_reduction_pct=risk_cfg.heat_reduction_pct,
                    reduction_persist_seconds=risk_cfg.reduction_persist_seconds,
                    heat_emergency_pct=risk_cfg.heat_emergency_pct,
                )
        except Exception as exc:
            logger.warning("[SystemContext] PortfolioRiskStateMachine init failed: {}", exc)

        # ── PortfolioGovernor ────────────────────────────────────────
        try:
            gcfg = getattr(config, "governor", None)
            if gcfg is None or gcfg.enabled:
                ctx.portfolio_governor = PortfolioGovernor(gcfg)
        except Exception as exc:
            logger.warning("[SystemContext] PortfolioGovernor init failed: {}", exc)

        # ── AccountRiskManager ───────────────────────────────────────
        try:
            gcfg = getattr(config, "governor", None)
            ctx.account_risk = AccountRiskManager(
                daily_loss_cap_pct=getattr(gcfg, "daily_loss_cap_pct", 3.0) if gcfg else 3.0,
                daily_loss_recovery_pct=getattr(gcfg, "daily_loss_recovery_pct", 1.5) if gcfg else 1.5,
                heat_block_pct=getattr(risk_cfg, "portfolio_heat_block_pct", 2.0),
                daily_loss_flatten_pct=getattr(risk_cfg, "daily_loss_flatten_pct", 5.0),
            )
        except Exception as exc:
            logger.warning("[SystemContext] AccountRiskManager init failed: {}", exc)

        # ── RiskReporter ─────────────────────────────────────────────
        try:
            ctx.risk_reporter = RiskReporter()
        except Exception as exc:
            logger.warning("[SystemContext] RiskReporter init failed: {}", exc)

        logger.info(
            "[SystemContext] risk layer initialized — drawdown={} corr={} "
            "risk_engine={} portfolio_sm={} governor={} account_risk={} reporter={}",
            ctx.drawdown_guard is not None,
            ctx.correlation_engine is not None,
            ctx.risk_engine is not None,
            ctx.portfolio_risk_sm is not None,
            ctx.portfolio_governor is not None,
            ctx.account_risk is not None,
            ctx.risk_reporter is not None,
        )

        # ── Decision Intelligence (Phase 2) ──────────────────────────

        # ── SessionEngine ───────────────────────────────────────────
        try:
            from brain.session_engine import SessionEngine as _SessionEngine
            ctx.session_engine = _SessionEngine()
        except Exception as exc:
            logger.warning("[SystemContext] SessionEngine init failed: {}", exc)

        # ── NewsGuard ───────────────────────────────────────────────
        try:
            from brain.session_engine import NewsGuard as _NewsGuard
            ctx.news_guard = _NewsGuard()
        except Exception as exc:
            logger.warning("[SystemContext] NewsGuard init failed: {}", exc)

        # ── SituationEngine ─────────────────────────────────────────
        try:
            from decision.situation import SituationEngine as _SituationEngine
            ctx.situation_engine = _SituationEngine()
        except Exception as exc:
            logger.warning("[SystemContext] SituationEngine init failed: {}", exc)

        # ── DecisionEngine ──────────────────────────────────────────
        try:
            from decision.engine import DecisionEngine as _DecisionEngine
            de_cfg = getattr(config, "decision", None)
            if de_cfg is not None:
                ctx.decision_engine = _DecisionEngine(
                    soften_gate=getattr(de_cfg, "soften_gate", True),
                )
            else:
                ctx.decision_engine = _DecisionEngine(soften_gate=True)
        except Exception as exc:
            logger.warning("[SystemContext] DecisionEngine init failed: {}", exc)

        # ── RiskGovernor ────────────────────────────────────────────
        try:
            from decision.governor import RiskGovernor as _RiskGovernor
            ctx.risk_governor = _RiskGovernor(graded_risk=True)
        except Exception as exc:
            logger.warning("[SystemContext] RiskGovernor init failed: {}", exc)

        # ── DecisionJournal ─────────────────────────────────────────
        try:
            from decision.journal import DecisionJournal as _DecisionJournal
            ctx.decision_journal = _DecisionJournal()
        except Exception as exc:
            logger.warning("[SystemContext] DecisionJournal init failed: {}", exc)

        logger.info(
            "[SystemContext] decision layer initialized — session={} news={} "
            "situation={} decision={} governor={} journal={}",
            ctx.session_engine is not None,
            ctx.news_guard is not None,
            ctx.situation_engine is not None,
            ctx.decision_engine is not None,
            ctx.risk_governor is not None,
            ctx.decision_journal is not None,
        )

        # ── Scan Pipeline + Sizing (Phase 3) ─────────────────────────

        # ── PairRanker ──────────────────────────────────────────────
        try:
            from scanner.pair_ranker import PairRanker as _PairRanker
            ctx.pair_ranker = _PairRanker()
        except Exception as exc:
            logger.warning("[SystemContext] PairRanker init failed: {}", exc)

        # ── OpportunityExecutor ─────────────────────────────────────
        try:
            from management.opportunity_executor import OpportunityExecutor as _OppExec
            opp_cfg = getattr(config, "opportunity_ranker", None)
            ctx.opportunity_executor = _OppExec(opp_cfg)
        except Exception as exc:
            logger.warning("[SystemContext] OpportunityExecutor init failed: {}", exc)

        # ── Orchestrator ────────────────────────────────────────────
        try:
            from brain.orchestrator import Orchestrator as _Orchestrator
            orch_cfg = getattr(config, "orchestrator", None)
            ctx.orchestrator = _Orchestrator(config=orch_cfg)
        except Exception as exc:
            logger.warning("[SystemContext] Orchestrator init failed: {}", exc)

        # ── SystemVolatilityMonitor ─────────────────────────────────
        try:
            from brain.regime_detector import SystemVolatilityMonitor as _VolMon
            ctx.system_volatility_monitor = _VolMon()
        except Exception as exc:
            logger.warning("[SystemContext] SystemVolatilityMonitor init failed: {}", exc)

        # ── OpportunityDensityTracker ───────────────────────────────
        try:
            from brain.opportunity_density import OpportunityDensityTracker as _DensityTracker
            ctx.opportunity_density_tracker = _DensityTracker(window_minutes=60)
        except Exception as exc:
            logger.warning("[SystemContext] OpportunityDensityTracker init failed: {}", exc)

        # ── EntryEngine ─────────────────────────────────────────────
        try:
            from trigger.entry_engine import EntryEngine as _EntryEngine
            ctx.entry_engine = _EntryEngine(
                config=config,
                volatility_stop_mode=risk_cfg.volatility_stop_mode,
                atr_stop_period=risk_cfg.atr_stop_period,
                atr_stop_mult=risk_cfg.atr_stop_mult,
                atr_stop_ratio_min=getattr(risk_cfg, "atr_stop_ratio_min", None),
                atr_stop_ratio_max=getattr(risk_cfg, "atr_stop_ratio_max", None),
                atr_stop_max_risk_mult=getattr(risk_cfg, "atr_stop_max_risk_mult", None),
            )
            if ctx.drawdown_guard is not None:
                ctx.entry_engine.drawdown = ctx.drawdown_guard
        except Exception as exc:
            logger.warning("[SystemContext] EntryEngine init failed: {}", exc)

        # ── ExecutionMonitor ────────────────────────────────────────
        try:
            from brain.execution_monitor import ExecutionMonitor as _ExecMon
            ctx.execution_monitor = _ExecMon()
        except Exception as exc:
            logger.warning("[SystemContext] ExecutionMonitor init failed: {}", exc)

        logger.info(
            "[SystemContext] scan/sizing layer initialized — ranker={} "
            "opp_exec={} orchestrator={} vol_mon={} density={} "
            "entry_engine={} exec_mon={}",
            ctx.pair_ranker is not None,
            ctx.opportunity_executor is not None,
            ctx.orchestrator is not None,
            ctx.system_volatility_monitor is not None,
            ctx.opportunity_density_tracker is not None,
            ctx.entry_engine is not None,
            ctx.execution_monitor is not None,
        )

        # ── Learning + Feedback (Phase 4) ────────────────────────────

        # ── OutcomeFeedback ─────────────────────────────────────────
        try:
            from brain.outcome_feedback import OutcomeFeedback as _OutcomeFeedback
            ctx.outcome_feedback = _OutcomeFeedback(config=config)
        except Exception as exc:
            logger.warning("[SystemContext] OutcomeFeedback init failed: {}", exc)

        # ── SignalLedger ────────────────────────────────────────────
        try:
            from adaptive.signal_ledger import SignalLedger as _SignalLedger
            ctx.signal_ledger = _SignalLedger()
        except Exception as exc:
            logger.warning("[SystemContext] SignalLedger init failed: {}", exc)

        # ── EmitterFeedbackService ──────────────────────────────────
        try:
            from adaptive.emitter_feedback import EmitterFeedbackService as _EmitterFB
            if ctx.signal_ledger is not None:
                ctx.emitter_feedback = _EmitterFB(ctx.signal_ledger)
        except Exception as exc:
            logger.warning("[SystemContext] EmitterFeedbackService init failed: {}", exc)

        # ── CounterfactualEngine ────────────────────────────────────
        try:
            from adaptive.counterfactual import CounterfactualEngine as _Counterfactual
            cf_cfg = getattr(config, "counterfactual", None)
            ctx.counterfactual_engine = _Counterfactual(
                enabled=getattr(cf_cfg, "enabled", True) if cf_cfg else True,
                attribution_lookback=getattr(cf_cfg, "lookback", 500) if cf_cfg else 500,
                attribution_interval=getattr(cf_cfg, "interval", 100) if cf_cfg else 100,
            )
        except Exception as exc:
            logger.warning("[SystemContext] CounterfactualEngine init failed: {}", exc)

        # ── VoteCalibrator ──────────────────────────────────────────
        try:
            from adaptive.vote_calibrator import VoteCalibrator as _VoteCalib
            ctx.vote_calibrator = _VoteCalib(
                config=config,
                emitter_feedback=ctx.emitter_feedback,
            )
            if ctx.counterfactual_engine is not None:
                ctx.vote_calibrator.set_counterfactual(ctx.counterfactual_engine)
        except Exception as exc:
            logger.warning("[SystemContext] VoteCalibrator init failed: {}", exc)

        # ── ModuleGovernor ──────────────────────────────────────────
        try:
            from adaptive.module_governor import ModuleGovernor as _ModGov
            ctx.module_governor = _ModGov(
                config=config,
                emitter_feedback=ctx.emitter_feedback,
                counterfactual=ctx.counterfactual_engine,
            )
        except Exception as exc:
            logger.warning("[SystemContext] ModuleGovernor init failed: {}", exc)

        # ── PostCloseTracker ────────────────────────────────────────
        try:
            from adaptive.post_close_tracker import PostCloseTracker as _PostClose
            ctx.post_close_tracker = _PostClose(config=config)
        except Exception as exc:
            logger.warning("[SystemContext] PostCloseTracker init failed: {}", exc)

        # ── GateTuner ───────────────────────────────────────────────
        try:
            from adaptive.gate_tuner import GateTuner as _GateTuner
            ctx.gate_tuner = _GateTuner()
        except Exception as exc:
            logger.warning("[SystemContext] GateTuner init failed: {}", exc)

        # ── InteractionAnalyzer ─────────────────────────────────────
        try:
            from adaptive.interaction_discovery import InteractionAnalyzer as _Interaction
            if ctx.counterfactual_engine is not None:
                ia_cfg = getattr(config, "interaction", None)
                ctx.interaction_analyzer = _Interaction(
                    ctx.counterfactual_engine,
                    enabled=getattr(ia_cfg, "enabled", True) if ia_cfg else True,
                    lookback=getattr(ia_cfg, "lookback", 500) if ia_cfg else 500,
                    interval=getattr(ia_cfg, "interval", 500) if ia_cfg else 500,
                )
        except Exception as exc:
            logger.warning("[SystemContext] InteractionAnalyzer init failed: {}", exc)

        # ── ShadowStore ─────────────────────────────────────────────
        try:
            from persistence.shadow_store import ShadowStore as _ShadowStore
            ctx.shadow_store = _ShadowStore()
        except Exception as exc:
            logger.warning("[SystemContext] ShadowStore init failed: {}", exc)

        # ── AdaptiveOptimizer (ML adapter) ──────────────────────────
        try:
            from adaptive.optimizer import AdaptiveOptimizer as _MLAdapter
            ctx.ml_adapter = _MLAdapter(config=config)
            if ctx.post_close_tracker is not None:
                try:
                    ctx.ml_adapter.pair_learner.set_post_close_tracker(ctx.post_close_tracker)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("[SystemContext] AdaptiveOptimizer init failed: {}", exc)

        # ── TunerAgent ──────────────────────────────────────────────
        try:
            from adaptive.tuner_agent import TunerAgent as _TunerAgent
            tuner_cfg = getattr(config, "tuner", None)
            ctx.tuner_agent = _TunerAgent(
                enabled=getattr(tuner_cfg, "enabled", True) if tuner_cfg else True,
            )
        except Exception as exc:
            logger.warning("[SystemContext] TunerAgent init failed: {}", exc)

        logger.info(
            "[SystemContext] learning layer initialized — outcome_fb={} "
            "ledger={} emitter_fb={} vote_cal={} mod_gov={} post_close={} "
            "gate_tuner={} counterfactual={} interaction={} shadow={} "
            "tuner={} ml={}",
            ctx.outcome_feedback is not None,
            ctx.signal_ledger is not None,
            ctx.emitter_feedback is not None,
            ctx.vote_calibrator is not None,
            ctx.module_governor is not None,
            ctx.post_close_tracker is not None,
            ctx.gate_tuner is not None,
            ctx.counterfactual_engine is not None,
            ctx.interaction_analyzer is not None,
            ctx.shadow_store is not None,
            ctx.tuner_agent is not None,
            ctx.ml_adapter is not None,
        )

        # ── Ops / Dashboard / Persistence (Phase 5) ──────────────────

        # ── TradeJournal ────────────────────────────────────────────
        try:
            from brain.trade_journal import TradeJournal as _TradeJournal
            ctx.trade_journal = _TradeJournal()
        except Exception as exc:
            logger.warning("[SystemContext] TradeJournal init failed: {}", exc)

        # ── ProcessWatchdog ─────────────────────────────────────────
        try:
            from ops.watchdog import ProcessWatchdog as _Watchdog
            ops_cfg = getattr(config, "ops", None)
            if ops_cfg is not None:
                ctx.process_watchdog = _Watchdog(ops_cfg)
            else:
                from types import SimpleNamespace
                ctx.process_watchdog = _Watchdog(SimpleNamespace(
                    heartbeat_file="data/.heartbeat",
                    heartbeat_interval_seconds=10,
                    max_tick_duration_seconds=60,
                ))
        except Exception as exc:
            logger.warning("[SystemContext] ProcessWatchdog init failed: {}", exc)

        # ── DailyMaintenance ────────────────────────────────────────
        try:
            from platforms.maintenance import DailyMaintenance as _DailyMaint
            ctx.daily_maintenance = _DailyMaint()
        except Exception as exc:
            logger.warning("[SystemContext] DailyMaintenance init failed: {}", exc)

        logger.info(
            "[SystemContext] ops layer initialized — journal={} watchdog={} maintenance={}",
            ctx.trade_journal is not None,
            ctx.process_watchdog is not None,
            ctx.daily_maintenance is not None,
        )

        # ── Evolution Engines (Phase 6) ──────────────────────────────

        # ── CapitalAllocator (L5.5a) ───────────────────────────────
        try:
            from adaptive.capital_allocator import CapitalAllocator as _CapAlloc
            ctx.capital_allocator = _CapAlloc()
        except Exception as exc:
            logger.warning("[SystemContext] CapitalAllocator init failed: {}", exc)

        # ── ExecutionProfileManager (L5.5b) ────────────────────────
        try:
            from adaptive.execution_profiles import ExecutionProfileManager as _ExecProf
            ctx.execution_profiles = _ExecProf()
        except Exception as exc:
            logger.warning("[SystemContext] ExecutionProfileManager init failed: {}", exc)

        # ── RegimeDetector (L7 adaptive) ───────────────────────────
        try:
            from adaptive.regime_detector import RegimeDetector as _RegimeDetL7
            rd_cfg = getattr(config, "regime_detector", None)
            ctx.regime_detector = _RegimeDetL7(
                lookback_bars=getattr(rd_cfg, "lookback_bars", 50) if rd_cfg else 50,
                hysteresis_bars=getattr(rd_cfg, "hysteresis_bars", 5) if rd_cfg else 5,
            )
        except Exception as exc:
            logger.warning("[SystemContext] RegimeDetector init failed: {}", exc)

        # ── BehaviorDiscoveryEngine (L6) ───────────────────────────
        try:
            from adaptive.behavior_discovery import BehaviorDiscoveryEngine as _BehavDisc
            ctx.behavior_discovery = _BehavDisc()
        except Exception as exc:
            logger.warning("[SystemContext] BehaviorDiscoveryEngine init failed: {}", exc)

        # ── SignalDiscoveryEngine (L5c) ────────────────────────────
        try:
            from adaptive.signal_discovery import SignalDiscoveryEngine as _SigDisc
            if ctx.counterfactual_engine is not None:
                ctx.signal_discovery = _SigDisc(ctx.counterfactual_engine)
        except Exception as exc:
            logger.warning("[SystemContext] SignalDiscoveryEngine init failed: {}", exc)

        # ── VirtualModuleRegistry (L5c) ────────────────────────────
        try:
            from adaptive.virtual_modules import VirtualModuleRegistry as _VMReg
            virt_cfg = getattr(config, "virtual", None)
            ctx.virtual_module_registry = _VMReg(
                enabled=getattr(virt_cfg, "kill_switch", True) if virt_cfg else False,
            )
        except Exception as exc:
            logger.warning("[SystemContext] VirtualModuleRegistry init failed: {}", exc)

        # ── VirtualSignalManager (L5c lifecycle) ───────────────────
        try:
            from adaptive.virtual_promotion import VirtualSignalManager as _VSM
            if ctx.virtual_module_registry is not None:
                ctx.virtual_signal_manager = _VSM(
                    registry=ctx.virtual_module_registry,
                    config=config,
                    signal_discovery=ctx.signal_discovery,
                    emitter_feedback=ctx.emitter_feedback,
                    counterfactual=ctx.counterfactual_engine,
                )
        except Exception as exc:
            logger.warning("[SystemContext] VirtualSignalManager init failed: {}", exc)

        # ── Planning + Shadow (Phase 6) ──────────────────────────────

        # ── TradePlanner ───────────────────────────────────────────
        try:
            from planning.trade_planner import TradePlanner as _TradePlanner
            ctx.trade_planner = _TradePlanner(governor=ctx.portfolio_governor)
        except Exception as exc:
            logger.warning("[SystemContext] TradePlanner init failed: {}", exc)

        # ── OutcomeLogger ──────────────────────────────────────────
        try:
            from planning.outcome_logger import OutcomeLogger as _OutcomeLogger
            ctx.outcome_logger = _OutcomeLogger()
        except Exception as exc:
            logger.warning("[SystemContext] OutcomeLogger init failed: {}", exc)

        # ── Calibrator ─────────────────────────────────────────────
        try:
            from planning.calibrator import Calibrator as _Calibrator
            ctx.calibrator = _Calibrator()
        except Exception as exc:
            logger.warning("[SystemContext] Calibrator init failed: {}", exc)

        # ── ReEntryManager ─────────────────────────────────────────
        try:
            from management.re_entry import ReEntryManager as _ReEntry
            ctx.re_entry_manager = _ReEntry()
        except Exception as exc:
            logger.warning("[SystemContext] ReEntryManager init failed: {}", exc)

        logger.info(
            "[SystemContext] evolution+planning initialized — "
            "cap_alloc={} exec_prof={} regime={} behavior={} "
            "sig_disc={} virt_reg={} virt_mgr={} "
            "planner={} outcome_log={} calibrator={} re_entry={}",
            ctx.capital_allocator is not None,
            ctx.execution_profiles is not None,
            ctx.regime_detector is not None,
            ctx.behavior_discovery is not None,
            ctx.signal_discovery is not None,
            ctx.virtual_module_registry is not None,
            ctx.virtual_signal_manager is not None,
            ctx.trade_planner is not None,
            ctx.outcome_logger is not None,
            ctx.calibrator is not None,
            ctx.re_entry_manager is not None,
        )

        return ctx

    def account_key(
        self,
        symbol: str,
        platform_manager: PlatformManager,
    ) -> str:
        """Resolve the risk-silo key (broker:account_id) for a symbol.

        Mirrors ``TradingLoop._account_key``.
        """
        key = self._account_key_cache.get(symbol)
        if key:
            return key
        try:
            broker = platform_manager.get_broker_name(symbol) or "default"
        except Exception:
            broker = "default"
        try:
            acct_id = platform_manager.get_account_id(symbol) or ""
        except Exception:
            acct_id = ""
        key = f"{broker}:{acct_id}" if acct_id else broker
        if acct_id:
            self._account_key_cache[symbol] = key
        return key
