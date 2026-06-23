"""APEX TRADER — System Context.

Shared subsystem container that provides the event-driven system with access
to the same risk, decision, and lifecycle subsystems that TradingLoop uses.

Phase 1: risk layer (DrawdownGuard, PortfolioRiskStateMachine,
PortfolioGovernor, AccountRiskManager, CorrelationEngine, RiskEngine,
RiskReporter).
Phase 2: decision intelligence (DecisionEngine, SituationEngine,
RiskGovernor, DecisionJournal, SessionEngine, NewsGuard).
Phase 3: scan pipeline + sizing (OpportunityExecutor,
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
from typing import TYPE_CHECKING, Any, Optional

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
    from compliance.division import ComplianceDivision
    from config import AppConfig
    from decision.engine import DecisionEngine
    from decision.governor import RiskGovernor
    from decision.journal import DecisionJournal
    from decision.situation import SituationEngine
    from governance.division import GovernanceDivision
    from governor.portfolio_governor import PortfolioGovernor
    from management.opportunity_executor import OpportunityExecutor
    from platforms.platform_manager import PlatformManager
    from portfolio.division import PortfolioDivision
    from risk.account_risk import AccountRiskManager
    from risk.portfolio_risk_state import PortfolioRiskStateMachine
    from risk.risk_engine import RiskEngine
    from risk.risk_reporter import RiskReporter
    from trigger.entry_engine import EntryEngine

    from adaptive.counterfactual import CounterfactualEngine
    from adaptive.emitter_feedback import EmitterFeedbackService
    from adaptive.gate_tuner import GateTuner
    from adaptive.interaction_discovery import InteractionAnalyzer
    from adaptive.module_governor import ModuleGovernor
    from adaptive.optimizer import AdaptiveOptimizer
    from adaptive.post_close_tracker import PostCloseTracker
    from adaptive.recommendations import RecommendationGateway
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
    # Portfolio Division — the cohesive capital-allocation/exposure layer that
    # owns sizing (replaces the inline fresh-PositionSizer + multiplier chain).
    portfolio: Optional["PortfolioDivision"] = None

    # ── Decision intelligence ────────────────────────────────────────
    decision_engine: Optional[DecisionEngine] = None
    situation_engine: Optional[SituationEngine] = None
    risk_governor: Optional[RiskGovernor] = None
    decision_journal: Optional[DecisionJournal] = None
    session_engine: Optional[SessionEngine] = None
    news_guard: Optional[NewsGuard] = None

    # ── Compliance (Department 3 — pure permit layer) ─────────────────
    compliance: Optional[ComplianceDivision] = None

    # ── Governance (Department 8 — authorise Learning + contain) ──────
    governance: Optional["GovernanceDivision"] = None

    # ── Scan pipeline + sizing (Phase 3) ──────────────────────────────
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
    symbol_conviction: Optional[Any] = None
    post_close_tracker: Optional[PostCloseTracker] = None
    gate_tuner: Optional[GateTuner] = None
    counterfactual_engine: Optional[CounterfactualEngine] = None
    interaction_analyzer: Optional[InteractionAnalyzer] = None
    shadow_store: Optional[ShadowStore] = None
    tuner_agent: Optional[TunerAgent] = None
    recommendation_gateway: Optional[RecommendationGateway] = None
    ml_adapter: Optional[AdaptiveOptimizer] = None
    win_rate_provider: Optional[Any] = None
    rl_bridge: Optional[Any] = None

    # ── Ops / Dashboard / Persistence (Phase 5) ─────────────────────
    trade_journal: Optional[Any] = None
    process_watchdog: Optional[Any] = None
    daily_maintenance: Optional[Any] = None
    health_watchdog: Optional[Any] = None

    # ── Evolution engines (Phase 6) ──────────────────────────────────
    capital_allocator: Optional[Any] = None
    execution_profiles: Optional[Any] = None
    regime_detector: Optional[Any] = None
    behavior_discovery: Optional[Any] = None
    signal_discovery: Optional[Any] = None
    virtual_module_registry: Optional[Any] = None
    virtual_signal_manager: Optional[Any] = None
    param_evolver: Optional[Any] = None

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

        # ── PortfolioDivision ────────────────────────────────────────
        # The cohesive capital-allocation layer. Reuses the RiskEngine's own
        # PositionSizer (so the per-trade risk ceiling stays authoritative —
        # no fresh sizer that could bypass it) and the live CorrelationEngine
        # for currency-decomposition exposure.
        try:
            from portfolio.division import PortfolioDivision

            sizer = getattr(ctx.risk_engine, "position_sizer", None)
            if sizer is None:
                from risk.position_sizer import PositionSizer
                sizer = PositionSizer(
                    micro_account_threshold_usd=risk_cfg.micro_account_threshold_usd,
                    deriv_min_stake_usd=risk_cfg.deriv_min_stake_usd,
                    max_risk_pct_per_trade=risk_cfg.max_risk_pct_per_trade,
                )
            ctx.portfolio = PortfolioDivision(
                position_sizer=sizer,
                correlation_engine=ctx.correlation_engine,
                max_pair_concentration=getattr(
                    risk_cfg, "max_pair_concentration", 0,
                ),
                max_broker_positions=getattr(
                    risk_cfg, "max_broker_positions", 0,
                ),
            )
        except Exception as exc:
            logger.warning("[SystemContext] PortfolioDivision init failed: {}", exc)

        logger.info(
            "[SystemContext] risk layer initialized — drawdown={} corr={} "
            "risk_engine={} portfolio_sm={} governor={} account_risk={} "
            "reporter={} portfolio={}",
            ctx.drawdown_guard is not None,
            ctx.correlation_engine is not None,
            ctx.risk_engine is not None,
            ctx.portfolio_risk_sm is not None,
            ctx.portfolio_governor is not None,
            ctx.account_risk is not None,
            ctx.risk_reporter is not None,
            ctx.portfolio is not None,
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

        # ── ComplianceDivision (Department 3 — pure permit layer) ────
        # Built with the subsystem references it owns.  The broker/platform-
        # bound callables (market-open, broker-health, live spread) are bound
        # later by the event-driven bootstrap via ``compliance.bind_runtime``,
        # since those depend on the PlatformManager + broker-truth helpers.
        try:
            from compliance.division import ComplianceDivision as _Compliance

            gcfg = getattr(config, "governor", None)
            max_pos = int(getattr(gcfg, "max_open_positions", 8)) if gcfg else 8
            ctx.compliance = _Compliance(
                drawdown_guard=ctx.drawdown_guard,
                portfolio_risk_sm=ctx.portfolio_risk_sm,
                account_risk=ctx.account_risk,
                news_guard=ctx.news_guard,
                spread_monitor=getattr(ctx.risk_engine, "spread_monitor", None),
                max_open_positions=max_pos,
            )
        except Exception as exc:
            logger.warning("[SystemContext] ComplianceDivision init failed: {}", exc)

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
                    gate_safety_margin=getattr(
                        de_cfg, "gate_safety_margin", -0.3,
                    ),
                    reversal_weighted_evidence=getattr(
                        de_cfg, "reversal_weighted_evidence", True,
                    ),
                    reversal_required_strength=getattr(
                        de_cfg, "reversal_required_strength", 2.0,
                    ),
                    reversal_momentum_full=getattr(
                        de_cfg, "reversal_momentum_full", 0.6,
                    ),
                    tf_conflict_aware=getattr(de_cfg, "tf_conflict_aware", True),
                    range_edge_required=getattr(
                        de_cfg, "range_edge_required", True,
                    ),
                    range_edge_htf_min=getattr(
                        de_cfg, "range_edge_htf_min", 0.20,
                    ),
                    range_edge_consensus_min=getattr(
                        de_cfg, "range_edge_consensus_min", 0.40,
                    ),
                    fast_opposition_decay_enabled=getattr(
                        de_cfg, "fast_opposition_decay_enabled", True,
                    ),
                    fast_opposition_min_streak=getattr(
                        de_cfg, "fast_opposition_min_streak", 3,
                    ),
                    fast_opposition_max_streak=getattr(
                        de_cfg, "fast_opposition_max_streak", 8,
                    ),
                    fast_opposition_decay_weight=getattr(
                        de_cfg, "fast_opposition_decay_weight", 0.30,
                    ),
                    fast_opposition_profit_threshold=getattr(
                        de_cfg, "fast_opposition_profit_threshold", 0.3,
                    ),
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
            "[SystemContext] scan/sizing layer initialized — "
            "opp_exec={} orchestrator={} vol_mon={} density={} "
            "entry_engine={} exec_mon={}",
            ctx.opportunity_executor is not None,
            ctx.orchestrator is not None,
            ctx.system_volatility_monitor is not None,
            ctx.opportunity_density_tracker is not None,
            ctx.entry_engine is not None,
            ctx.execution_monitor is not None,
        )

        # ── Learning + Feedback (Phase 4) ────────────────────────────

        # ── OutcomeFeedback ─────────────────────────────────────────
        # Reads enabled/journal_path/accuracy_lookback off the config handed in.
        # Those live on the nested OutcomeFeedbackConfig — pass that, not the
        # top-level AppConfig, or every field falls through to its constructor
        # default and the configured values never take effect.
        try:
            from brain.outcome_feedback import OutcomeFeedback as _OutcomeFeedback
            ctx.outcome_feedback = _OutcomeFeedback(
                config=getattr(config, "outcome_feedback", config),
            )
        except Exception as exc:
            logger.warning("[SystemContext] OutcomeFeedback init failed: {}", exc)

        # ── SignalLedger ────────────────────────────────────────────
        try:
            from adaptive.signal_ledger import SignalLedger as _SignalLedger
            # SignalLedger took no config, so its grading delay / intervals /
            # min-move / lookback used the constructor defaults and the
            # SignalLedgerConfig values were silently ignored. Wire the nested
            # config so operator settings actually take effect.
            sl_cfg = getattr(config, "signal_ledger", None)
            if sl_cfg is not None:
                ctx.signal_ledger = _SignalLedger(
                    grading_delay_minutes=float(
                        getattr(sl_cfg, "signal_grading_delay_minutes", 30.0)
                    ),
                    check_intervals=list(
                        getattr(sl_cfg, "signal_grading_check_intervals", [5, 15, 30, 60])
                    ),
                    min_move_pct=float(getattr(sl_cfg, "signal_min_move_pct", 0.1)),
                    accuracy_lookback=int(getattr(sl_cfg, "accuracy_lookback", 100)),
                )
            else:
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
        # The nested CounterfactualConfig names its fields counterfactual_enabled
        # / attribution_lookback / attribution_interval — reading enabled /
        # lookback / interval silently missed all three and used the getattr
        # fallbacks. Read the real field names so the config is authoritative.
        try:
            from adaptive.counterfactual import CounterfactualEngine as _Counterfactual
            cf_cfg = getattr(config, "counterfactual", None)
            ctx.counterfactual_engine = _Counterfactual(
                enabled=getattr(cf_cfg, "counterfactual_enabled", True) if cf_cfg else True,
                attribution_lookback=getattr(cf_cfg, "attribution_lookback", 500) if cf_cfg else 500,
                attribution_interval=getattr(cf_cfg, "attribution_interval", 100) if cf_cfg else 100,
            )
        except Exception as exc:
            logger.warning("[SystemContext] CounterfactualEngine init failed: {}", exc)

        # ── RecommendationGateway (Learning ⑦ → Governance ⑧ boundary) ──
        # The single authorisation chokepoint every Learning recommendation
        # passes through. Auto-approves while governance is not required (the
        # default), so routing learners through it is behaviour-neutral; Phase 7
        # flips governance_required on and injects an authoriser.
        try:
            from adaptive.recommendations import RecommendationGateway as _RecGateway
            lg_cfg = getattr(config, "learning_governance", None)
            ctx.recommendation_gateway = _RecGateway(
                governance_required=bool(
                    getattr(lg_cfg, "governance_required", False) if lg_cfg else False
                ),
                history_limit=int(
                    getattr(lg_cfg, "recommendation_history_limit", 500) if lg_cfg else 500
                ),
            )
        except Exception as exc:
            logger.warning("[SystemContext] RecommendationGateway init failed: {}", exc)

        # ── VoteCalibrator ──────────────────────────────────────────
        # The calibrator reads its master switch and hyperparameters off the
        # config object handed in (vote_calibration_enabled, vote_weight_*, …).
        # Those live on the nested ``VoteCalibratorConfig`` — pass that, not the
        # top-level AppConfig, or ``enabled`` resolves to its False default and
        # the calibrator stays inert despite the config saying it is on.
        try:
            from adaptive.vote_calibrator import VoteCalibrator as _VoteCalib
            ctx.vote_calibrator = _VoteCalib(
                config=getattr(config, "vote_calibrator", config),
                emitter_feedback=ctx.emitter_feedback,
            )
            if ctx.counterfactual_engine is not None:
                ctx.vote_calibrator.set_counterfactual(ctx.counterfactual_engine)
            if ctx.outcome_feedback is not None:
                try:
                    ctx.vote_calibrator.set_outcome_feedback(ctx.outcome_feedback)
                except Exception as exc:
                    logger.warning(
                        "[SystemContext] VoteCalibrator→outcome_feedback wire failed: {}",
                        exc,
                    )
            # Route weight-multiplier publication through the recommendation
            # gateway (Learning recommends → Governance authorises). Auto-approved
            # until Phase 7, so calibration behaviour is unchanged.
            if ctx.recommendation_gateway is not None:
                try:
                    ctx.vote_calibrator.set_recommendation_gateway(
                        ctx.recommendation_gateway
                    )
                except Exception as exc:
                    logger.warning(
                        "[SystemContext] VoteCalibrator→gateway wire failed: {}", exc
                    )
        except Exception as exc:
            logger.warning("[SystemContext] VoteCalibrator init failed: {}", exc)

        # ── ModuleGovernor ──────────────────────────────────────────
        # The governor reads ``module_governor_enabled`` and every transition
        # threshold off the config it is handed. Those fields live on the nested
        # ``ModuleGovernorConfig`` (config.module_governor), NOT the top-level
        # AppConfig — passing the AppConfig made ``enabled`` fall through to its
        # False default, so the governor was permanently inert (no module ever
        # shadowed) even though the config flag was True. Pass the nested config.
        try:
            from adaptive.module_governor import ModuleGovernor as _ModGov
            ctx.module_governor = _ModGov(
                config=getattr(config, "module_governor", config),
                emitter_feedback=ctx.emitter_feedback,
                counterfactual=ctx.counterfactual_engine,
            )
            if ctx.module_governor is not None:
                logger.info(
                    "[SystemContext] ModuleGovernor enabled={}",
                    ctx.module_governor.enabled,
                )
        except Exception as exc:
            logger.warning("[SystemContext] ModuleGovernor init failed: {}", exc)

        # ── SymbolConvictionStore (1B — symbol-relative conviction) ──
        # Learning ⑦ produces the per-symbol conviction distribution; Consensus
        # ② consumes the normalized value at form_thesis. Cold-start neutral
        # (raw passthrough until a symbol warms up) and per-user isolated.
        try:
            cn_cfg = getattr(config, "conviction_normalization", None)
            if cn_cfg is None or bool(getattr(cn_cfg, "enabled", True)):
                from adaptive.symbol_conviction import SymbolConvictionStore as _SymConv
                ctx.symbol_conviction = _SymConv(
                    enabled=bool(getattr(cn_cfg, "enabled", True)) if cn_cfg else True,
                    min_samples=int(getattr(cn_cfg, "min_samples", 30)) if cn_cfg else 30,
                    max_history=int(getattr(cn_cfg, "max_history", 300)) if cn_cfg else 300,
                    blend=float(getattr(cn_cfg, "blend", 0.5)) if cn_cfg else 0.5,
                    persist=bool(getattr(cn_cfg, "persist", True)) if cn_cfg else True,
                )
        except Exception as exc:
            logger.warning("[SystemContext] SymbolConvictionStore init failed: {}", exc)
        # ── PostCloseTracker ────────────────────────────────────────
        # Reads enabled/check_intervals_minutes/max_retries off the config it is
        # handed. Those live on the nested PostCloseTrackerConfig — passing the
        # top-level AppConfig made every field fall through to its constructor
        # default (so the tracker ran with the in-code default enabled=True and
        # ignored the configured intervals/retries). Pass the nested config.
        try:
            from adaptive.post_close_tracker import PostCloseTracker as _PostClose
            ctx.post_close_tracker = _PostClose(
                config=getattr(config, "post_close_tracker", config),
            )
        except Exception as exc:
            logger.warning("[SystemContext] PostCloseTracker init failed: {}", exc)

        # ── GateTuner ───────────────────────────────────────────────
        try:
            from adaptive.gate_tuner import GateTuner as _GateTuner
            ctx.gate_tuner = _GateTuner()
            # Wire tuned gate offsets into the live entry engine — without this
            # the EntryEngine's gate_tuner stays None and the learned offsets
            # never reach live entry-gate thresholds.
            if ctx.entry_engine is not None:
                try:
                    ctx.entry_engine.gate_tuner = ctx.gate_tuner
                except Exception as exc:
                    logger.warning("[SystemContext] GateTuner→EntryEngine wire failed: {}", exc)
        except Exception as exc:
            logger.warning("[SystemContext] GateTuner init failed: {}", exc)

        # ── InteractionAnalyzer ─────────────────────────────────────
        try:
            from adaptive.interaction_discovery import InteractionAnalyzer as _Interaction
            if ctx.counterfactual_engine is not None:
                ia_cfg = getattr(config, "interaction", None)
                # InteractionConfig fields are interaction_discovery_enabled /
                # interaction_lookback / interaction_interval — reading enabled /
                # lookback / interval missed them and used the fallbacks, so the
                # config never reached the analyzer. Read the real field names.
                ctx.interaction_analyzer = _Interaction(
                    ctx.counterfactual_engine,
                    enabled=getattr(ia_cfg, "interaction_discovery_enabled", True) if ia_cfg else True,
                    lookback=getattr(ia_cfg, "interaction_lookback", 500) if ia_cfg else 500,
                    interval=getattr(ia_cfg, "interaction_interval", 500) if ia_cfg else 500,
                )
                # Toxic module-pair findings flow to Governance as recommendations.
                if ctx.recommendation_gateway is not None:
                    try:
                        ctx.interaction_analyzer.set_recommendation_gateway(
                            ctx.recommendation_gateway
                        )
                    except Exception as exc:
                        logger.warning(
                            "[SystemContext] InteractionAnalyzer→gateway wire failed: {}",
                            exc,
                        )
        except Exception as exc:
            logger.warning("[SystemContext] InteractionAnalyzer init failed: {}", exc)

        # ── ShadowStore ─────────────────────────────────────────────
        try:
            from persistence.shadow_store import ShadowStore as _ShadowStore
            ctx.shadow_store = _ShadowStore()
        except Exception as exc:
            logger.warning("[SystemContext] ShadowStore init failed: {}", exc)

        # ── RL bridge ───────────────────────────────────────────────
        # Dormant unless a trained checkpoint exists (the bridge self-disables
        # otherwise), so this is default-neutral on a fresh install. Shares the
        # Phase 4 ShadowStore so RL shadow contracts land in the same place.
        try:
            from rl.bridge import build_rl_bridge
            ctx.rl_bridge = build_rl_bridge()
            if ctx.rl_bridge is not None and ctx.shadow_store is not None:
                ctx.rl_bridge._shadow_store = ctx.shadow_store
        except Exception as exc:
            logger.warning("[SystemContext] RLBridge init failed: {}", exc)

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

        # ── AdaptiveWinRateProvider ─────────────────────────────────
        # Closes the opportunity-ranker's ``win_rate_provider`` hook: feeds the
        # ranker (and the orchestrator EV sizing that consumes it) a calibrated
        # per-pair win probability from the learned PairLearner / EVEstimator
        # instead of the hardcoded 0.40 prior. Read-only over both learners;
        # gated by ``adaptive_win_rate_provider_enabled`` and behaviour-neutral
        # at cold start (resolves to the same 0.40 prior until real history
        # exists).
        try:
            # ``adaptive_win_rate_provider_enabled`` lives on the nested
            # OpportunityRankerConfig, NOT the top-level AppConfig — reading it
            # off AppConfig always hit the True fallback, so the configured flag
            # (e.g. set False to fall back to the cold-start prior) was ignored.
            _rank_cfg = getattr(config, "opportunity_ranker", None)
            if bool(getattr(_rank_cfg, "adaptive_win_rate_provider_enabled", True)):
                from adaptive.win_rate_provider import AdaptiveWinRateProvider as _WRP
                pair_learner = (
                    getattr(ctx.ml_adapter, "pair_learner", None)
                    if ctx.ml_adapter is not None else None
                )
                ev_estimator = (
                    getattr(ctx.risk_engine, "ev_estimator", None)
                    if ctx.risk_engine is not None else None
                )
                ctx.win_rate_provider = _WRP(
                    pair_learner=pair_learner,
                    ev_estimator=ev_estimator,
                )
        except Exception as exc:
            logger.warning("[SystemContext] AdaptiveWinRateProvider init failed: {}", exc)

        # ── TunerAgent ──────────────────────────────────────────────
        # The nested config lives on AppConfig.tuner_agent (TunerAgentConfig) —
        # the old getattr(config, "tuner", …) key never matched, so tuner_cfg was
        # always None and every field (duration cap, failure cap, audit path, …)
        # used the constructor defaults. Read the correct key and wire all fields.
        try:
            from adaptive.tuner_agent import TunerAgent as _TunerAgent
            tuner_cfg = getattr(config, "tuner_agent", None)
            ctx.tuner_agent = _TunerAgent(
                enabled=getattr(tuner_cfg, "enabled", True) if tuner_cfg else True,
                audit_db_path=getattr(tuner_cfg, "audit_db_path", None) if tuner_cfg else None,
                max_tune_duration_seconds=float(
                    getattr(tuner_cfg, "max_tune_duration_seconds", 30.0)
                ) if tuner_cfg else 30.0,
                max_consecutive_failures=int(
                    getattr(tuner_cfg, "max_consecutive_failures", 3)
                ) if tuner_cfg else 3,
                log_all_skips=bool(
                    getattr(tuner_cfg, "log_all_skips", False)
                ) if tuner_cfg else False,
            )
        except Exception as exc:
            logger.warning("[SystemContext] TunerAgent init failed: {}", exc)

        # ── GovernanceDivision (Department 8 — authorise + contain) ──
        # Learning recommends; Governance authorises. Wired as the authoriser on
        # the RecommendationGateway and given the enforcement arms: the
        # ModuleGovernor (to shadow a harmful module) and the TunerAgent (to
        # freeze a runaway tunable). Permissive-but-bounded by default, so
        # turning governance on is behaviour-neutral — it only adds the explicit
        # gate. If construction fails the gateway keeps no authoriser and
        # auto-approves (fail-safe — trading is never blocked by a governance
        # wiring fault).
        try:
            from governance.division import GovernanceDivision as _Governance

            lg_cfg = getattr(config, "learning_governance", None)
            ctx.governance = _Governance(
                module_governor=ctx.module_governor,
                tuner_agent=ctx.tuner_agent,
                min_size_multiplier=getattr(lg_cfg, "min_size_multiplier", 0.0) if lg_cfg else 0.0,
                max_size_multiplier=getattr(lg_cfg, "max_size_multiplier", 5.0) if lg_cfg else 5.0,
                max_weight_multiplier=getattr(lg_cfg, "max_weight_multiplier", 10.0) if lg_cfg else 10.0,
                enforce_toxic_pairs=getattr(lg_cfg, "enforce_toxic_pairs", False) if lg_cfg else False,
                validation_min_signals=getattr(lg_cfg, "promotion_validation_min_signals", 20) if lg_cfg else 20,
                validation_min_accuracy=getattr(lg_cfg, "promotion_validation_min_accuracy", 0.50) if lg_cfg else 0.50,
                limited_min_signals=getattr(lg_cfg, "promotion_limited_min_signals", 40) if lg_cfg else 40,
                limited_min_accuracy=getattr(lg_cfg, "promotion_limited_min_accuracy", 0.52) if lg_cfg else 0.52,
                full_min_signals=getattr(lg_cfg, "promotion_full_min_signals", 80) if lg_cfg else 80,
                full_min_accuracy=getattr(lg_cfg, "promotion_full_min_accuracy", 0.55) if lg_cfg else 0.55,
                full_min_marginal_r=getattr(lg_cfg, "promotion_full_min_marginal_r", 0.0) if lg_cfg else 0.0,
                history_limit=int(getattr(lg_cfg, "recommendation_history_limit", 500) if lg_cfg else 500),
            )
            # Install Governance as the authoriser on the Learning→Governance
            # gateway and require authorisation (per config; default on).
            if ctx.recommendation_gateway is not None:
                ctx.recommendation_gateway.set_authorizer(ctx.governance.authorize)
                ctx.recommendation_gateway.set_governance_required(
                    bool(getattr(lg_cfg, "governance_required", True) if lg_cfg else True)
                )
        except Exception as exc:
            logger.warning("[SystemContext] GovernanceDivision init failed: {}", exc)

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
            db_cfg = getattr(config, "data_backup", None)
            ctx.daily_maintenance = _DailyMaint(
                auto_sync_data_repo=bool(
                    getattr(db_cfg, "auto_sync_data_repo", True) if db_cfg else True
                ),
                sync_branch=str(
                    getattr(db_cfg, "sync_branch", "main") if db_cfg else "main"
                ),
                sync_orphan_branch=bool(
                    getattr(db_cfg, "sync_orphan_branch", True) if db_cfg else True
                ),
                sync_exclude_patterns=list(
                    getattr(db_cfg, "exclude_patterns", ["*.csv"]) if db_cfg else ["*.csv"]
                ),
            )
        except Exception as exc:
            logger.warning("[SystemContext] DailyMaintenance init failed: {}", exc)

        # ── HealthWatchdog (carries RL subsystem health) ────────────
        try:
            from platforms.health_watchdog import HealthWatchdog as _HealthWatchdog
            ctx.health_watchdog = _HealthWatchdog()
            rl = getattr(ctx, "rl_bridge", None)
            if rl is not None:
                ctx.health_watchdog.record_rl_status(
                    enabled=bool(getattr(rl, "enabled", False)),
                    stage=int(getattr(getattr(rl, "authority", None), "stage", 1) or 1),
                    checkpoint_loaded=bool(getattr(rl, "checkpoint_exists", False)),
                )
        except Exception as exc:
            logger.warning("[SystemContext] HealthWatchdog init failed: {}", exc)

        logger.info(
            "[SystemContext] ops layer initialized — journal={} watchdog={} maintenance={}",
            ctx.trade_journal is not None,
            ctx.process_watchdog is not None,
            ctx.daily_maintenance is not None,
        )

        # ── Evolution Engines (Phase 6) ──────────────────────────────

        # ── CapitalAllocator (L5.5a) ───────────────────────────────
        # Took no config, so CapitalAllocationConfig (horizons, weights,
        # rebalance cadence, enabled flag) was ignored and the constructor
        # defaults were used. Wire the nested config so operator tuning of the
        # capital-allocation sizing multiplier actually takes effect. Defaults
        # coincide with the constructor, so this is behaviour-neutral by default.
        try:
            from adaptive.capital_allocator import CapitalAllocator as _CapAlloc
            ca_cfg = getattr(config, "capital_allocation", None)
            if ca_cfg is not None:
                ctx.capital_allocator = _CapAlloc(
                    db_path=getattr(ca_cfg, "capital_allocation_db_path", None),
                    enabled=bool(getattr(ca_cfg, "enabled", True)),
                    short_horizon_trades=int(getattr(ca_cfg, "short_horizon_trades", 50)),
                    medium_horizon_trades=int(getattr(ca_cfg, "medium_horizon_trades", 500)),
                    long_horizon_trades=int(getattr(ca_cfg, "long_horizon_trades", 5000)),
                    short_weight=float(getattr(ca_cfg, "short_weight", 0.2)),
                    medium_weight=float(getattr(ca_cfg, "medium_weight", 0.3)),
                    long_weight=float(getattr(ca_cfg, "long_weight", 0.5)),
                    rebalance_interval_trades=int(getattr(ca_cfg, "rebalance_interval_trades", 25)),
                    max_allocation_shift=float(getattr(ca_cfg, "max_allocation_shift", 0.10)),
                    min_allocation=float(getattr(ca_cfg, "min_allocation", 0.05)),
                    min_trades_for_scoring=int(getattr(ca_cfg, "min_trades_for_scoring", 50)),
                    bayesian_prior_trades=int(getattr(ca_cfg, "bayesian_prior_trades", 100)),
                    allocation_temperature=float(getattr(ca_cfg, "allocation_temperature", 0.5)),
                )
            else:
                ctx.capital_allocator = _CapAlloc()
        except Exception as exc:
            logger.warning("[SystemContext] CapitalAllocator init failed: {}", exc)

        # ── ExecutionProfileManager (L5.5b) ────────────────────────
        # Took no config, so ExecutionProfileConfig was ignored and the
        # constructor defaults were used. Wire the nested config (defaults
        # coincide → behaviour-neutral) so profile selection is config-driven.
        try:
            from adaptive.execution_profiles import ExecutionProfileManager as _ExecProf
            ep_cfg = getattr(config, "execution_profiles", None)
            if ep_cfg is not None:
                ctx.execution_profiles = _ExecProf(
                    db_path=getattr(ep_cfg, "execution_profiles_db_path", None),
                    enabled=bool(getattr(ep_cfg, "enabled", True)),
                    default_profile=str(getattr(ep_cfg, "default_profile", "standard_swing")),
                    allow_profile_creation=bool(getattr(ep_cfg, "allow_profile_creation", True)),
                    max_active_profiles=int(getattr(ep_cfg, "max_active_profiles", 10)),
                    min_trades_for_scoring=int(getattr(ep_cfg, "min_trades_for_scoring", 30)),
                    strong_consensus_threshold=float(getattr(ep_cfg, "strong_consensus_threshold", 0.75)),
                    weak_consensus_threshold=float(getattr(ep_cfg, "weak_consensus_threshold", 0.45)),
                )
            else:
                ctx.execution_profiles = _ExecProf()
        except Exception as exc:
            logger.warning("[SystemContext] ExecutionProfileManager init failed: {}", exc)

        # ── RegimeDetector (L7 adaptive) ───────────────────────────
        # AppConfig exposes this as ``regime_detection`` (RegimeDetectionConfig);
        # the old getattr(config, "regime_detector", …) key never matched, so
        # rd_cfg was always None and lookback/hysteresis used the constructor
        # defaults instead of the configured values. Read the correct key.
        try:
            from adaptive.regime_detector import RegimeDetector as _RegimeDetL7
            rd_cfg = getattr(config, "regime_detection", None)
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
        # Took no config, so its enabled flag used the constructor default
        # (False) and every mining parameter (lookback, interval, support,
        # thresholds) ignored SignalDiscoveryConfig. Wire the nested config so
        # the advisory miner is config-authoritative. enabled is sourced from
        # signal_discovery_enabled which defaults OFF (per the config docstring),
        # so the engine stays dormant until an operator enables it.
        try:
            from adaptive.signal_discovery import SignalDiscoveryEngine as _SigDisc
            if ctx.counterfactual_engine is not None:
                sd_cfg = getattr(config, "signal_discovery", None)
                if sd_cfg is not None:
                    ctx.signal_discovery = _SigDisc(
                        ctx.counterfactual_engine,
                        enabled=bool(getattr(sd_cfg, "signal_discovery_enabled", False)),
                        db_path=getattr(sd_cfg, "signal_discovery_db_path", None),
                        lookback=int(getattr(sd_cfg, "discovery_lookback", 1000)),
                        interval=int(getattr(sd_cfg, "discovery_interval", 200)),
                        min_trades=int(getattr(sd_cfg, "min_trades_for_discovery", 100)),
                        min_support=int(getattr(sd_cfg, "min_rule_support", 15)),
                        max_conditions=int(getattr(sd_cfg, "max_rule_conditions", 3)),
                        min_edge_r=float(getattr(sd_cfg, "min_edge_r", 0.10)),
                        walk_forward_split=float(getattr(sd_cfg, "discovery_walk_forward_split", 0.7)),
                        bonferroni_alpha=float(getattr(sd_cfg, "bonferroni_alpha", 0.05)),
                        walk_forward_ratio_threshold=float(
                            getattr(sd_cfg, "walk_forward_ratio_threshold", 0.6)
                        ),
                        score_decay_rate=float(getattr(sd_cfg, "score_decay_rate", 0.05)),
                        max_active_signals=int(getattr(sd_cfg, "max_active_signals", 5)),
                    )
                else:
                    ctx.signal_discovery = _SigDisc(ctx.counterfactual_engine)
        except Exception as exc:
            logger.warning("[SystemContext] SignalDiscoveryEngine init failed: {}", exc)

        # ── ParameterEvolver (L5a) — SHADOW ONLY ───────────────────
        # Explores consensus/ranker thresholds, replay + shadow validates
        # candidates, then RECOMMENDS promotions through the RecommendationGateway
        # (Learning ⑦ → Governance ⑧).  The promote callback NEVER mutates live
        # config: it submits the proven candidate as a PARAM_PROMOTE recommendation
        # for audit/authorisation and returns False, so the engine records a
        # "recommended" decision and the parameter is never changed by it.  A
        # fault here can never affect trading (construction + run are exception-safe).
        try:
            from adaptive.param_evolution import ParameterEvolver as _ParamEvolver
            from adaptive.recommendations import (
                LearningRecommendation as _LearnRec,
                RecommendationType as _RecType,
            )
            pe_cfg = getattr(config, "param_evolution", None)
            if pe_cfg is not None and ctx.counterfactual_engine is not None:
                _gateway = ctx.recommendation_gateway

                def _param_current_values() -> dict:
                    """Live centre-point for candidate generation (read-only)."""
                    cons = getattr(config, "consensus", None)
                    rank = getattr(config, "opportunity_ranker", None)
                    vals: dict[str, float] = {}
                    if cons is not None:
                        vals["min_net_score"] = float(getattr(cons, "min_net_score", 1.5))
                        vals["min_agreement"] = float(getattr(cons, "min_agreement", 0.55))
                        vals["min_contributors"] = float(getattr(cons, "min_contributors", 2))
                    if rank is not None:
                        vals["min_expected_value"] = float(getattr(rank, "min_expected_value", 0.0))
                        vals["min_cluster_confidence"] = float(
                            getattr(rank, "min_cluster_confidence", 0.0)
                        )
                        vals["min_cluster_contributors"] = float(
                            getattr(rank, "min_cluster_contributors", 1)
                        )
                    return vals

                def _param_promote_recommend(name: str, location: str, value: float) -> bool:
                    """SHADOW-ONLY promotion hook: submit a recommendation through
                    the gateway (audited + governance-authorised) and ALWAYS return
                    False so the parameter is never mutated by the evolver."""
                    if _gateway is None:
                        return False
                    try:
                        _gateway.submit(_LearnRec(
                            source="param_evolver",
                            recommendation_type=_RecType.PARAM_PROMOTE,
                            payload={
                                "param_name": name,
                                "location": location,
                                "proposed_value": float(value),
                            },
                            confidence=0.0,
                            evidence={"mode": "shadow"},
                        ))
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[SystemContext] param promote submit failed: {}", exc)
                    return False  # never apply — shadow mode only

                ctx.param_evolver = _ParamEvolver(
                    ctx.counterfactual_engine,
                    enabled=bool(getattr(pe_cfg, "param_evolution_enabled", False)),
                    db_path=getattr(pe_cfg, "param_evolution_db_path", None),
                    current_values_provider=_param_current_values,
                    promote_callback=_param_promote_recommend,
                    candidates_per_param=int(getattr(pe_cfg, "candidates_per_param", 10)),
                    replay_lookback=int(getattr(pe_cfg, "replay_lookback", 500)),
                    shadow_validation_trades=int(getattr(pe_cfg, "shadow_validation_trades", 50)),
                    significance_threshold=float(getattr(pe_cfg, "significance_threshold", 0.05)),
                    walk_forward_split=float(getattr(pe_cfg, "walk_forward_split", 0.7)),
                    evolution_cooldown_hours=float(getattr(pe_cfg, "evolution_cooldown_hours", 48.0)),
                    max_concurrent_shadows=int(getattr(pe_cfg, "max_concurrent_shadows", 3)),
                    rollback_window=int(getattr(pe_cfg, "rollback_window", 100)),
                    min_replay_trades=int(getattr(pe_cfg, "min_replay_trades", 50)),
                )
        except Exception as exc:
            logger.warning("[SystemContext] ParameterEvolver init failed: {}", exc)

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
                # The manager reads virtual_promotion_enabled /
                # signal_discovery_enabled / feedback_lookback off the config it
                # is handed. Those live on the nested SignalDiscoveryConfig — the
                # old config=config (top-level AppConfig) made both flags fall
                # through to False, so the whole virtual shadow→promote→retire
                # lifecycle was permanently inert (same class of bug as the
                # Governor). Pass the nested config so it is config-authoritative.
                # NOTE: SignalDiscoveryConfig defaults these flags OFF (per its
                # docstring), so this is behaviour-neutral — the cluster stays
                # dormant until an operator explicitly flips the flags on.
                ctx.virtual_signal_manager = _VSM(
                    registry=ctx.virtual_module_registry,
                    config=getattr(config, "signal_discovery", config),
                    signal_discovery=ctx.signal_discovery,
                    emitter_feedback=ctx.emitter_feedback,
                    counterfactual=ctx.counterfactual_engine,
                )
                # Governance signs off virtual-module promotions (no module
                # reaches live weight without authorisation). No-op while
                # virtual promotion is disabled (default) — behaviour-neutral.
                if ctx.governance is not None:
                    try:
                        ctx.virtual_signal_manager.set_governance(ctx.governance)
                        ctx.governance.bind_runtime(
                            virtual_registry=ctx.virtual_module_registry
                        )
                    except Exception as exc:
                        logger.warning(
                            "[SystemContext] VirtualSignalManager→governance wire failed: {}",
                            exc,
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
            "sig_disc={} virt_reg={} virt_mgr={} param_evolver={} "
            "planner={} outcome_log={} calibrator={} re_entry={}",
            ctx.capital_allocator is not None,
            ctx.execution_profiles is not None,
            ctx.regime_detector is not None,
            ctx.behavior_discovery is not None,
            ctx.signal_discovery is not None,
            ctx.virtual_module_registry is not None,
            ctx.virtual_signal_manager is not None,
            ctx.param_evolver is not None,
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
