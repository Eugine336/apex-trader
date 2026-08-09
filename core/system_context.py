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
EmitterFeedbackService, PostCloseTracker, GateTuner, ShadowStore,
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
    from platforms.platform_manager import PlatformManager
    from portfolio.division import PortfolioDivision
    from risk.account_risk import AccountRiskManager
    from risk.portfolio_risk_state import PortfolioRiskStateMachine
    from risk.risk_engine import RiskEngine
    from risk.risk_reporter import RiskReporter
    from trigger.entry_engine import EntryEngine

    from adaptive.emitter_feedback import EmitterFeedbackService
    from adaptive.gate_tuner import GateTuner
    from adaptive.optimizer import AdaptiveOptimizer
    from adaptive.post_close_tracker import PostCloseTracker
    from adaptive.recommendations import RecommendationGateway
    from adaptive.signal_ledger import SignalLedger
    from adaptive.tuner_agent import TunerAgent
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
    # Aggregate-health thermostat read by Governance to pause/resume learning.
    health_assessor: Optional[Any] = None
    # Per-gate parameter attribution — which learned gate adjustment opened a
    # trade (DECISIVE) vs merely supported one that would have passed anyway.
    gate_attributor: Optional[Any] = None
    # Persistent competing Long/Short/Flat theses per symbol (Gap 1a). Runs
    # observationally alongside the consensus pipeline; surfaced via Governance.
    thesis_engine: Optional[Any] = None
    # Session 29 — anti-ping-pong gate for atomic reversals. Consulted when a
    # ``thesis_flip`` fires to decide whether to close + reverse (vs plain exit).
    reversal_manager: Optional[Any] = None
    # Evolving market campaigns — the lifetime of a directional thesis on a
    # symbol that individual orders (open/scale/partial/re-entry/reversal) are
    # legs of. Observational (default OFF); surfaced via Governance.
    campaign_registry: Optional[Any] = None
    # LLM reasoning subsystem — an additional evidence-emitting reasoner over the
    # structured theses/votes. Provider-agnostic (env-driven), default OFF,
    # fail-safe; surfaced via Governance. Never overrides physics vetoes.
    llm_reasoner: Optional[Any] = None
    # Autonomous Action Layer (Composio) — the single governed gateway through
    # which Brain-authored objectives become external actions. Default OFF +
    # dry-run; never reasons or originates objectives. Surfaced via Governance.
    action_orchestrator: Optional[Any] = None
    # Action Planner (Part IX Art 11) — turns the Brain's semantic objectives
    # into provider-bound, governed Composio actions. The Brain never names a
    # provider; the planner selects capability + provider. Default-off.
    action_planner: Optional[Any] = None
    # Knowledge source (Part IX v3.0) — Composio-backed external market context,
    # research and AI advisors turned into advisory Evidence for the Brain.
    # Read-only, gated, throttled and self-measuring. Default-off.
    knowledge_source: Optional[Any] = None
    # Reasoning Orchestrator (Part XVII) — lets the one Brain consult several
    # reasoning engines; each opinion becomes advisory Evidence (never a vote).
    # Default-off (consult_multi); the Brain remains the sole decision-maker.
    reasoning_orchestrator: Optional[Any] = None
    # Provider Registry / Manager (Part XXI) — the catalogue of every reasoning
    # provider and its constitutional state (AVAILABLE/CONFIGURED/UNAVAILABLE).
    # Observability only: it maintains provider identity + readiness, never
    # reasons or decides. Surfaced via Governance.
    provider_registry: Optional[Any] = None
    # Consultation Ledger (Part XXI Art 9/10/11) — records every Advisory-Council
    # consultation and grades each advisor (records + per-domain scorecards).
    # Observability/learning only; never authority. Surfaced via Governance.
    consultation_ledger: Optional[Any] = None
    # The AI Cognitive Brain (Single Reasoner) + its background cognition loop.
    # Consumes consolidated Evidence, emits DecisionPackages/CampaignSpecs. Runs
    # in shadow by default (observational); surfaced via Governance.
    cognitive_brain: Optional[Any] = None
    cognition_loop: Optional[Any] = None
    # Brain entry gate (Step C) — one-way authority that can veto (never
    # originate) a proposed entry when the Brain does not back it. Fail-open.
    cognition_gate: Optional[Any] = None
    # Brain management gate (Phase F) — governs exposure-adding management actions
    # (scale-in/re-entry); de-risking is never gated. Fail-safe.
    management_gate: Optional[Any] = None
    # Institutional memory (Phase H, Part VII) — persists campaign state
    # fingerprints + realised outcomes so the Brain can consult analogous
    # history. Observational; fail-safe; surfaced via cognition status.
    campaign_memory: Optional[Any] = None
    # Operational-intelligence author (Phase I, Part IX Art 9) — turns campaign
    # outcomes into operational objectives (issue/notify/report) for the Action
    # Planner. Ecosystem-only, never broker orders. Default-off.
    operations_author: Optional[Any] = None
    # Adaptive influence + Brain calibration (Phase J, Part VIII). The ledger
    # grades each evidence source by realised outcome; the tracker grades the
    # Brain's confidence-vs-reality. Learning is always on; applying the weights
    # to consolidation is gated (shadow by default). Surfaced via cognition.
    influence_ledger: Optional[Any] = None
    brain_calibration: Optional[Any] = None
    # Cognition observability (Phase L, Part XII) — read-only aggregator of the
    # cognitive components' status into derived rollout metrics. Surfaced via
    # governance cognition.observability.
    cognition_observability: Optional[Any] = None

    # ── Scan pipeline + sizing (Phase 3) ──────────────────────────────
    opportunity_executor: Optional[Any] = None  # RETIRED ranking executor — always None
    orchestrator: Optional[Any] = None  # RETIRED legacy decider — always None
    system_volatility_monitor: Optional[SystemVolatilityMonitor] = None
    opportunity_density_tracker: Optional[OpportunityDensityTracker] = None
    entry_engine: Optional[EntryEngine] = None
    execution_monitor: Optional[ExecutionMonitor] = None

    # ── Learning + feedback (Phase 4) ────────────────────────────────
    outcome_feedback: Optional[OutcomeFeedback] = None
    signal_ledger: Optional[SignalLedger] = None
    emitter_feedback: Optional[EmitterFeedbackService] = None
    post_close_tracker: Optional[PostCloseTracker] = None
    gate_tuner: Optional[GateTuner] = None
    shadow_store: Optional[ShadowStore] = None
    tuner_agent: Optional[TunerAgent] = None
    recommendation_gateway: Optional[RecommendationGateway] = None
    # ── Phase 6: continuous-learning closure ─────────────────────────
    adaptive_weight_provider: Optional[Any] = None
    recommendation_applier: Optional[Any] = None
    # ── Cross-instrument opportunity layer (GAP 1-5) ──────────────────
    # Pure helpers consumed at gated call sites; default-disabled behaviour is an
    # identity no-op. The queue + proactive scanner live on the event-driven
    # system (they need runtime dispatch / event-bus callables).
    cross_instrument_ranker: Optional[Any] = None
    opportunity_quality_sizer: Optional[Any] = None
    position_displacer: Optional[Any] = None
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

    # ── Safety degradation tracking ──────────────────────────────────
    # Names of CRITICAL safety subsystems that failed to initialise. When
    # non-empty the system runs WITHOUT one or more risk gates; new OPEN
    # entries are refused (existing-position management still runs) until
    # restarted with a clean init.
    _safety_degraded_subsystems: list = field(default_factory=list)

    def _mark_safety_degraded(self, name: str) -> None:
        """Record that a CRITICAL safety subsystem failed to initialise."""
        if name not in self._safety_degraded_subsystems:
            self._safety_degraded_subsystems.append(name)

    @property
    def safety_degraded(self) -> bool:
        """True when any CRITICAL safety subsystem failed to initialise."""
        return bool(self._safety_degraded_subsystems)

    @property
    def safety_degraded_reason(self) -> str:
        """Comma-separated names of the failed CRITICAL safety subsystems."""
        return ", ".join(self._safety_degraded_subsystems)

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
            logger.critical("[SystemContext] DrawdownGuard init failed — SAFETY DEGRADED: {}", exc)
            ctx._mark_safety_degraded("DrawdownGuard")

        # ── CorrelationEngine ────────────────────────────────────────
        try:
            ctx.correlation_engine = CorrelationEngine(
                max_correlated_trades=risk_cfg.max_correlated_trades,
                max_cluster_same_direction=risk_cfg.max_cluster_same_direction,
                allow_intentional_hedge=risk_cfg.allow_intentional_hedge,
            )
        except Exception as exc:
            logger.critical(
                "[SystemContext] CorrelationEngine init failed — SAFETY "
                "DEGRADED (correlation/cluster exposure gate unavailable): {}", exc,
            )
            ctx._mark_safety_degraded("CorrelationEngine")

        # ── RiskEngine ───────────────────────────────────────────────
        try:
            ctx.risk_engine = RiskEngine(config=config)
        except Exception as exc:
            logger.critical("[SystemContext] RiskEngine init failed — SAFETY DEGRADED: {}", exc)
            ctx._mark_safety_degraded("RiskEngine")

        # ── PortfolioRiskStateMachine ────────────────────────────────
        try:
            if risk_cfg.portfolio_risk_engine_enabled:
                # Adaptive heat thresholds scale with account size, so the SM
                # needs the balance available at construction time. Prefer the
                # pooled live balance; fall back to the RiskEngine's balance.
                account_equity: Optional[float] = None
                try:
                    bal = platform_manager.get_total_balance()
                    if bal and bal > 0:
                        account_equity = float(bal)
                except Exception:
                    account_equity = None
                if account_equity is None and ctx.risk_engine is not None:
                    try:
                        rb = float(getattr(ctx.risk_engine, "balance", 0.0) or 0.0)
                        if rb > 0:
                            account_equity = rb
                    except Exception:
                        account_equity = None

                ctx.portfolio_risk_sm = PortfolioRiskStateMachine(
                    heat_defensive_pct=risk_cfg.heat_defensive_pct,
                    heat_recovery_pct=risk_cfg.heat_recovery_pct,
                    recovery_dwell_seconds=risk_cfg.recovery_dwell_seconds,
                    heat_reduction_pct=risk_cfg.heat_reduction_pct,
                    reduction_persist_seconds=risk_cfg.reduction_persist_seconds,
                    heat_emergency_pct=risk_cfg.heat_emergency_pct,
                    reference_balance=risk_cfg.reference_balance,
                    account_equity=account_equity,
                )
        except Exception as exc:
            logger.critical(
                "[SystemContext] PortfolioRiskStateMachine init failed — "
                "SAFETY DEGRADED (portfolio risk-state gate unavailable): {}", exc,
            )
            ctx._mark_safety_degraded("PortfolioRiskStateMachine")

        # ── PortfolioGovernor ────────────────────────────────────────
        try:
            gcfg = getattr(config, "governor", None)
            if gcfg is None or gcfg.enabled:
                ctx.portfolio_governor = PortfolioGovernor(gcfg)
        except Exception as exc:
            logger.critical(
                "[SystemContext] PortfolioGovernor init failed — SAFETY "
                "DEGRADED (portfolio concentration/allocation gate unavailable): {}",
                exc,
            )
            ctx._mark_safety_degraded("PortfolioGovernor")

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
            logger.critical("[SystemContext] AccountRiskManager init failed — SAFETY DEGRADED: {}", exc)
            ctx._mark_safety_degraded("AccountRiskManager")

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
                    allow_min_lot_over_risk=getattr(
                        risk_cfg, "allow_min_lot_over_risk", False
                    ),
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
            logger.critical("[SystemContext] ComplianceDivision init failed — SAFETY DEGRADED: {}", exc)
            ctx._mark_safety_degraded("ComplianceDivision")

        # ── SituationEngine ─────────────────────────────────────────
        try:
            from decision.situation import SituationEngine as _SituationEngine
            ctx.situation_engine = _SituationEngine()
        except Exception as exc:
            logger.warning("[SystemContext] SituationEngine init failed: {}", exc)

        # ── DecisionEngine ──────────────────────────────────────────
        # NOTE: the DecisionEngine's in-trade MANAGEMENT path is RETIRED — the
        # single Cognitive Brain is the sole market manager (Constitution Part
        # VI/X); the deterministic protectors remain the Part X safety floor.
        # The engine is still constructed here for the entry/decision paths that
        # continue to consume it.
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

        # ── OpportunityExecutor — RETIRED (directional ranking entry path)
        # The ranking executor turned module votes into ranked opportunities for
        # the retired zone/queue entry path; the single Cognitive Brain is now
        # the sole originator, so it is deleted. ctx.opportunity_executor stays
        # None and its consumers are None-guarded.

        # ── Orchestrator — RETIRED (Single Reasoner cutover, Part III.2) ──
        # The legacy graded-sizing / physics-veto round table was a market
        # decision authority; it is deleted. ctx.orchestrator stays None and
        # every consumer (grade_candidate / evaluate) is None-guarded, so sizing
        # degrades to neutral (×1.0) with no veto.
        ctx.orchestrator = None

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
            logger.critical(
                "[SystemContext] EntryEngine init failed — SAFETY DEGRADED "
                "(SL/TP refinement + sizing gate unavailable): {}", exc,
            )
            ctx._mark_safety_degraded("EntryEngine")

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

        # L4 attribution engine RETIRED - severed from the live path.

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

        # Adaptive authority engines RETIRED - the directional-decision and
        # offline-adaptive subsystem is severed from the live path; the one
        # Cognitive Brain is the sole decider.
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

        # Module-interaction analyzer RETIRED - severed from the live path.

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
            # Wire the PairLearner into the live entry engine so it can RAISE the
            # entry-score bar for cold-start (unproven) symbols. Without this the
            # EntryEngine's pair_learner stays None and the cold-start boost never
            # reaches the live entry gate.
            if ctx.entry_engine is not None:
                try:
                    ctx.entry_engine.pair_learner = ctx.ml_adapter.pair_learner
                except Exception as exc:
                    logger.warning(
                        "[SystemContext] PairLearner→EntryEngine wire failed: {}", exc
                    )
        except Exception as exc:
            logger.warning("[SystemContext] AdaptiveOptimizer init failed: {}", exc)

        # ── Event-driven retrain: DrawdownGuard → AdaptiveOptimizer ──
        # The guard is constructed before the optimiser, so wire the
        # mode-transition callback here (now both exist). A drawdown escalation
        # (NORMAL→CAUTION, CAUTION→RECOVERY, …) arms an out-of-band retrain.
        # No-op when either subsystem is absent; never raises.
        try:
            if ctx.drawdown_guard is not None and ctx.ml_adapter is not None:
                ctx.drawdown_guard.set_mode_change_callback(
                    ctx.ml_adapter.notify_drawdown_escalation
                )
        except Exception as exc:
            logger.warning(
                "[SystemContext] DrawdownGuard→optimizer retrain wire failed: {}", exc
            )

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
        # the RecommendationGateway and given the enforcement arm: the TunerAgent
        # (to freeze a runaway tunable). Permissive-but-bounded by default, so
        # turning governance on is behaviour-neutral — it only adds the explicit
        # gate. If construction fails the gateway keeps no authoriser and
        # auto-approves (fail-safe — trading is never blocked by a governance
        # wiring fault).
        try:
            from governance.division import GovernanceDivision as _Governance

            lg_cfg = getattr(config, "learning_governance", None)

            # ── HealthAssessor (the thermostat over all learning) ──────
            # Built first so it can be handed to Governance. Aggregate health
            # is what Governance reads to pause/resume the whole learning layer
            # — distinct from the per-recommendation authorisation it already
            # does. Behaviour-neutral by default (only freezes on CRITICAL,
            # which needs negative rolling EV AND a strong secondary signal).
            try:
                from governance.health_assessor import HealthAssessor as _HealthAssessor

                ha_cfg = getattr(lg_cfg, "health_assessor", None) if lg_cfg else None
                if ha_cfg is None or bool(getattr(ha_cfg, "enabled", True)):
                    ctx.health_assessor = _HealthAssessor(
                        enabled=bool(getattr(ha_cfg, "enabled", True)) if ha_cfg else True,
                        window_size=int(getattr(ha_cfg, "window_size", 20)) if ha_cfg else 20,
                        max_history=int(getattr(ha_cfg, "max_history", 200)) if ha_cfg else 200,
                        critical_entry_rate_threshold=float(
                            getattr(ha_cfg, "critical_entry_rate_threshold", 1.3)
                        ) if ha_cfg else 1.3,
                        critical_learner_loss_rate=float(
                            getattr(ha_cfg, "critical_learner_loss_rate", 0.65)
                        ) if ha_cfg else 0.65,
                        degraded_learner_loss_rate=float(
                            getattr(ha_cfg, "degraded_learner_loss_rate", 0.55)
                        ) if ha_cfg else 0.55,
                    )
            except Exception as exc:
                logger.warning("[SystemContext] HealthAssessor init failed: {}", exc)

            # ── GateAttributor (per-gate parameter attribution) ─────
            # Snapshots the GateTuner offsets at entry and replays the gate
            # decision on the operator's default thresholds at close, so
            # Governance can attribute which learned loosening *opened* a trade
            # (DECISIVE) vs which merely supported one that would have passed
            # anyway. Observational — it never freezes or blocks. Reads the
            # entry-config defaults (min_entry_ev / min_entry_score /
            # min_htf_alignment) the live EntryGate also defaults to.
            try:
                from adaptive.gate_attribution import GateAttributor as _GateAttributor
                from entry.models import EntryConfig as _EntryConfig

                ctx.gate_attributor = _GateAttributor(
                    gate_tuner=ctx.gate_tuner,
                    config=_EntryConfig(),
                )
            except Exception as exc:
                logger.warning("[SystemContext] GateAttributor init failed: {}", exc)

            # ── ThesisEngine — RETIRED (Single Reasoner cutover, Part III.2) ──
            # The legacy competing-thesis decider (`brain/thesis_engine.py`) was
            # a market-decision authority (`should_act` entry gate,
            # `evaluate_open_position` exit/flip). It is deleted: the one AI
            # Cognitive Brain now owns entry/exit judgment, and the thesis reads
            # it used to consume flow to the Brain as Evidence via
            # `cognition/evidence_adapters`. `ctx.thesis_engine` stays None; every
            # consumer is None-guarded and degrades to the Brain + mechanical
            # stops (no legacy thesis gate, no legacy evidence-exit).
            ctx.thesis_engine = None

            # ── Reversal Manager (Session 29) ─────────────────────────────
            # Anti-ping-pong gate: when a thesis_flip fires, decide whether to
            # close + reverse (atomic) or just exit. Fail-safe — a construction
            # fault leaves ``reversal_manager`` None, so the flip falls back to
            # the Session-28 evidence exit (flat).
            try:
                from management.reversal_manager import ReversalManager as _ReversalManager

                thesis_cfg = getattr(config, "thesis", None)
                ctx.reversal_manager = _ReversalManager(
                    enabled=bool(
                        getattr(thesis_cfg, "atomic_reversal_enabled", True)
                        if thesis_cfg is not None else True
                    ),
                    min_thesis_ev=float(
                        getattr(thesis_cfg, "reversal_min_thesis_ev", 0.1)
                        if thesis_cfg is not None else 0.1
                    ),
                    cooldown_seconds=float(
                        getattr(thesis_cfg, "reversal_cooldown", 300.0)
                        if thesis_cfg is not None else 300.0
                    ),
                    max_reversals_per_session=int(
                        getattr(thesis_cfg, "max_reversals_per_session", 3)
                        if thesis_cfg is not None else 3
                    ),
                    threshold_escalation=float(
                        getattr(thesis_cfg, "reversal_threshold_escalation", 0.5)
                        if thesis_cfg is not None else 0.5
                    ),
                )
            except Exception as exc:
                logger.warning("[SystemContext] ReversalManager init failed: {}", exc)

            # ── Campaign Registry (evolving market campaigns) ─────────────
            # Tracks the lifetime of a directional thesis per symbol — the
            # continuing idea that individual orders (open / scale-in / partial
            # / re-entry / reversal) are legs of. Observational (default OFF):
            # it records the campaign narrative and is surfaced via Governance
            # ``get_status`` without altering any execution decision.
            try:
                from brain.campaign import CampaignRegistry as _CampaignRegistry

                camp_cfg = getattr(config, "campaign", None)
                ctx.campaign_registry = _CampaignRegistry(
                    enabled=bool(
                        getattr(camp_cfg, "enabled", False)
                        if camp_cfg is not None else False
                    ),
                    dormant_after_seconds=float(
                        getattr(camp_cfg, "dormant_after_seconds", 900.0)
                        if camp_cfg is not None else 900.0
                    ),
                    invalidate_after_seconds=float(
                        getattr(camp_cfg, "invalidate_after_seconds", 3600.0)
                        if camp_cfg is not None else 3600.0
                    ),
                    history_limit=int(
                        getattr(camp_cfg, "history_limit", 500)
                        if camp_cfg is not None else 500
                    ),
                    postmortem_enabled=bool(
                        getattr(camp_cfg, "postmortem_enabled", True)
                        if camp_cfg is not None else True
                    ),
                    sound_evidence_threshold=float(
                        getattr(camp_cfg, "sound_evidence_threshold", 0.5)
                        if camp_cfg is not None else 0.5
                    ),
                    evidence_full_refreshes=int(
                        getattr(camp_cfg, "evidence_full_refreshes", 5)
                        if camp_cfg is not None else 5
                    ),
                )
            except Exception as exc:
                logger.warning("[SystemContext] CampaignRegistry init failed: {}", exc)

            # ── LLM Reasoner (provider-agnostic reasoning subsystem) ──────
            # Builds a provider client purely from config/env (no hardcoded
            # vendor): self-hosted (Ollama / vLLM / any OpenAI-compatible
            # endpoint) or commercial API (OpenAI / Anthropic / Gemini). Default
            # OFF and fail-safe: with no provider set the client is None and the
            # reasoner is inert. Emits opinions as evidence only — governed by
            # the same authority layer and never overriding physics vetoes.
            try:
                from llm.client import build_client as _build_llm_client
                from llm.model_manager import build_model_manager as _build_model_manager
                from llm.reasoner import LLMReasoner as _LLMReasoner

                llm_cfg = getattr(config, "llm", None)
                _llm_client = None
                if llm_cfg is not None and bool(getattr(llm_cfg, "enabled", False)):
                    # Part XVI Art 9 — prefer the Model Manager (policy-driven
                    # multi-model selection + failover). It is a drop-in for a
                    # single client and degrades to the primary model when only
                    # one candidate is configured. Fall back to a lone client if
                    # the manager cannot be built.
                    _llm_client = _build_model_manager(llm_cfg)
                    if _llm_client is None:
                        _llm_client = _build_llm_client(llm_cfg)
                ctx.llm_reasoner = _LLMReasoner(
                    client=_llm_client,
                    enabled=bool(
                        getattr(llm_cfg, "enabled", False)
                        if llm_cfg is not None else False
                    ),
                    drive_decisions=bool(
                        getattr(llm_cfg, "drive_decisions", False)
                        if llm_cfg is not None else False
                    ),
                    min_interval_seconds=float(
                        getattr(llm_cfg, "min_interval_seconds", 30.0)
                        if llm_cfg is not None else 30.0
                    ),
                    # §8 — the Brain's strategic reasoner requests a compute CLASS
                    # (default "deep"); a class-aware ModelManager serves it from a
                    # matching, healthy provider. Untagged rosters are unaffected.
                    default_compute_class=str(
                        getattr(llm_cfg, "reasoning_compute_class", "")
                        if llm_cfg is not None else ""
                    ),
                )
                if ctx.llm_reasoner.available:
                    logger.info(
                        "[SystemContext] LLM reasoner ONLINE — provider '{}' model '{}'",
                        getattr(llm_cfg, "provider", "?"),
                        getattr(llm_cfg, "model", "?"),
                    )
            except Exception as exc:
                logger.warning("[SystemContext] LLMReasoner init failed: {}", exc)

            # ── Action Orchestrator (Autonomous Action Layer / Composio) ──
            # The single governed gateway for external actions. It NEVER reasons
            # or originates objectives — the Brain authors them, governance policy
            # authorises, an adapter executes. Default OFF + dry-run (mock
            # adapter, no external calls) until explicitly configured. Fail-safe.
            try:
                from action.composio import build_adapter as _build_action_adapter
                from action.orchestrator import (
                    ActionOrchestrator as _ActionOrchestrator,
                    GovernancePolicy as _GovernancePolicy,
                    tier_from as _tier_from,
                )

                comp_cfg = getattr(config, "composio", None)
                _policy = _GovernancePolicy(
                    enabled=bool(getattr(comp_cfg, "enabled", False)
                                 if comp_cfg is not None else False),
                    require_source=bool(getattr(comp_cfg, "require_source", True)
                                        if comp_cfg is not None else True),
                    min_confidence=float(getattr(comp_cfg, "min_confidence", 0.2)
                                         if comp_cfg is not None else 0.2),
                    auto_max_risk=_tier_from(
                        getattr(comp_cfg, "auto_max_risk", "low")
                        if comp_cfg is not None else "low"
                    ),
                    medium_confidence_threshold=float(
                        getattr(comp_cfg, "medium_confidence_threshold", 0.7)
                        if comp_cfg is not None else 0.7
                    ),
                )
                _adapter = _build_action_adapter(comp_cfg)
                ctx.action_orchestrator = _ActionOrchestrator(_policy, _adapter)
                # Part IX Article 11 — the Action Planner: turns the Brain's
                # semantic objectives into provider-bound, governed Composio
                # actions. Enabled only when the action layer is; default-off is
                # doubly safe (planner.submit is inert while disabled).
                try:
                    from action.capabilities import default_registry as _default_registry
                    from action.planner import ActionPlanner as _ActionPlanner
                    _capability_registry = _default_registry()
                    ctx.action_planner = _ActionPlanner(
                        ctx.action_orchestrator,
                        _capability_registry,
                        enabled=bool(getattr(comp_cfg, "enabled", False)
                                     if comp_cfg is not None else False),
                        available_providers=(comp_cfg.available_providers_list()
                                             if comp_cfg is not None else None),
                        provider_preferences=(comp_cfg.provider_preferences_map()
                                              if comp_cfg is not None else None),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SystemContext] ActionPlanner init failed: {}", exc)
                    ctx.action_planner = None
                    _capability_registry = None
                # Part IX v3.0 — the Operational Intelligence READ layer. Turns
                # external market context / research / AI advisors (via Composio)
                # into advisory Evidence for the Brain. Gated by both the action
                # layer being enabled AND its own knowledge switch; default-off.
                try:
                    from cognition.knowledge_source import KnowledgeSource as _KnowledgeSource
                    _know_on = bool(
                        getattr(comp_cfg, "enabled", False)
                        and getattr(comp_cfg, "knowledge_enabled", False)
                    ) if comp_cfg is not None else False
                    ctx.knowledge_source = _KnowledgeSource(
                        _adapter,
                        _capability_registry,
                        enabled=_know_on,
                        interval_seconds=float(
                            getattr(comp_cfg, "knowledge_interval_seconds", 300.0)
                            if comp_cfg is not None else 300.0
                        ),
                        max_items=int(
                            getattr(comp_cfg, "knowledge_max_items", 5)
                            if comp_cfg is not None else 5
                        ),
                        advisor_enabled=bool(
                            getattr(comp_cfg, "advisor_enabled", False)
                            if comp_cfg is not None else False
                        ),
                        available_providers=(
                            comp_cfg.available_providers_list()
                            if comp_cfg is not None else None
                        ),
                        knowledge_provider=(
                            comp_cfg.provider_preferences_map().get("knowledge.retrieve", "")
                            if comp_cfg is not None else ""
                        ),
                        advisor_provider=(
                            comp_cfg.provider_preferences_map().get("advisor.consult", "")
                            if comp_cfg is not None else ""
                        ),
                        arg_overrides=(
                            comp_cfg.knowledge_arg_overrides_map()
                            if comp_cfg is not None
                            and hasattr(comp_cfg, "knowledge_arg_overrides_map") else None
                        ),
                    )
                    logger.info(
                        "[SystemContext] Knowledge source ready — enabled={} advisor={}",
                        _know_on,
                        getattr(comp_cfg, "advisor_enabled", False) if comp_cfg else False,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SystemContext] KnowledgeSource init failed: {}", exc)
                    ctx.knowledge_source = None
                logger.info(
                    "[SystemContext] Action layer ready — enabled={} adapter={} "
                    "(dry_run={})",
                    getattr(comp_cfg, "enabled", False) if comp_cfg else False,
                    getattr(_adapter, "name", type(_adapter).__name__),
                    getattr(comp_cfg, "dry_run", True) if comp_cfg else True,
                )
            except Exception as exc:
                logger.warning("[SystemContext] ActionOrchestrator init failed: {}", exc)

            # ── AI Cognitive Brain (Single Reasoner) + cognition loop ─────
            # The one subsystem permitted to reason (Constitution Part I Art 4 /
            # Part II). Consumes consolidated Evidence, emits decisions. Runs in
            # SHADOW by default: it produces/surfaces decisions for observability
            # without driving execution, so the legacy path stays authoritative
            # until the cutover is validated (Parts XIII/XV). Fail-safe.
            try:
                from cognition.brain import CognitiveBrain as _CognitiveBrain
                from cognition.loop import (
                    BrainActionBridge as _BrainActionBridge,
                    CognitionLoop as _CognitionLoop,
                    EvidenceConsolidator as _EvidenceConsolidator,
                )

                cog_cfg = getattr(config, "cognition", None)
                # Part VII — institutional memory (Phase H). Best-effort: a
                # store fault leaves memory None (observational, fail-open).
                _memory = None
                try:
                    if bool(getattr(cog_cfg, "memory_enabled", True)
                            if cog_cfg is not None else True):
                        from cognition.memory import get_campaign_memory as _get_memory
                        _memory = _get_memory()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SystemContext] campaign memory init failed: {}", exc)
                    _memory = None
                ctx.campaign_memory = _memory
                # Phase J (Part VIII) — adaptive influence ledger + Brain
                # calibration. The ledger always learns from outcomes; whether its
                # weights are APPLIED to consolidation is gated by influence_enabled
                # (default shadow). Fail-safe: a fault leaves them None.
                _influence = None
                _calibration = None
                try:
                    from cognition.influence import (
                        CalibrationTracker as _CalibrationTracker,
                        InfluenceLedger as _InfluenceLedger,
                    )
                    _influence = _InfluenceLedger(
                        min_samples=int(getattr(cog_cfg, "influence_min_samples", 20)
                                        if cog_cfg is not None else 20),
                        min_weight=float(getattr(cog_cfg, "influence_min_weight", 0.5)
                                         if cog_cfg is not None else 0.5),
                        max_weight=float(getattr(cog_cfg, "influence_max_weight", 1.5)
                                         if cog_cfg is not None else 1.5),
                    )
                    _calibration = _CalibrationTracker()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SystemContext] influence/calibration init failed: {}", exc)
                    _influence = _calibration = None
                ctx.influence_ledger = _influence
                ctx.brain_calibration = _calibration
                # Art XXXI — applying the learned weights to live consolidation is
                # safe by default: the ledger's significance floor leaves any
                # under-sampled source neutral (1.0) and only attenuates
                # demonstrated poor performers. Wire it True unless config or env
                # explicitly disables it.
                _influence_enabled = bool(
                    getattr(cog_cfg, "influence_weighting_enabled", True)
                    if cog_cfg is not None else True
                )
                ctx.cognitive_brain = _CognitiveBrain(
                    reasoner=ctx.llm_reasoner,
                    min_confidence_to_act=float(
                        getattr(cog_cfg, "min_confidence_to_act", 0.55)
                        if cog_cfg is not None else 0.55
                    ),
                    max_uncertainty_to_act=float(
                        getattr(cog_cfg, "max_uncertainty_to_act", 0.6)
                        if cog_cfg is not None else 0.6
                    ),
                    allow_scale_in=bool(
                        getattr(cog_cfg, "allow_scale_in", False)
                        if cog_cfg is not None else False
                    ),
                    reverse_confidence=float(
                        getattr(cog_cfg, "manage_reverse_confidence", 0.7)
                        if cog_cfg is not None else 0.7
                    ),
                    exit_floor=float(
                        getattr(cog_cfg, "manage_exit_floor", 0.3)
                        if cog_cfg is not None else 0.3
                    ),
                    # Part IX Art 1/7 — payoff geometry for the Brain's expected
                    # value (EV in R). Reuses the same reward multiple the
                    # origination sink targets so the strategic EV and the
                    # execution-cost-adjusted EV share one reward-to-risk basis.
                    reward_r_default=float(
                        getattr(cog_cfg, "origination_reward_multiple", 2.0)
                        if cog_cfg is not None else 2.0
                    ),
                    # Part IX Q35/Q36 — act only when expected value clears this
                    # threshold (in R). Default 0.0 ⇒ decline non-positive-EV
                    # opportunities even when confident. Set to None in config to
                    # disable the EV gate (not recommended).
                    min_expected_value=(
                        None if (cog_cfg is not None
                                 and getattr(cog_cfg, "min_expected_value", 0.0) is None)
                        else float(getattr(cog_cfg, "min_expected_value", 0.0)
                                   if cog_cfg is not None else 0.0)
                    ),
                    # Q40 — management re-reasons an OPEN position on its own
                    # tighter cadence (independent throttle bucket); origination
                    # and the advisory council keep the global rate.
                    manage_min_interval_seconds=float(
                        getattr(cog_cfg, "manage_min_interval_seconds", 8.0)
                        if cog_cfg is not None else 8.0
                    ),
                    # Art XXXI — close the learning loop: the Brain's stated
                    # confidence is corrected by its demonstrated calibration
                    # (Decision → Outcome → Attribution → Calibration → future
                    # reasoning). Neutral until the tracker has enough samples.
                    calibration=_calibration,
                    # Article XX — advisor quorum: a campaign must be backed by at
                    # least this many advisors that actually responded.
                    min_advisors_for_action=int(
                        getattr(cog_cfg, "min_advisors_for_action", 2)
                        if cog_cfg is not None else 2
                    ),
                    # Article XXXIV — minimum evidence-domain coverage to act.
                    min_evidence_domains=int(
                        getattr(cog_cfg, "min_evidence_domains", 2)
                        if cog_cfg is not None else 2
                    ),
                    # Article XXI — attenuate confidence under degraded cognition.
                    degraded_confidence_multiplier=float(
                        getattr(cog_cfg, "degraded_confidence_multiplier", 0.7)
                        if cog_cfg is not None else 0.7
                    ),
                    # Violation V9 (Part XXIV/XXV) — fallback round-trip execution
                    # cost in R subtracted from the Brain's EV when no live
                    # EXECUTION_QUALITY evidence is present. 0.05 ≈ 5% of R.
                    default_cost_r=float(
                        getattr(cog_cfg, "default_execution_cost_r", 0.05)
                        if cog_cfg is not None else 0.05
                    ),
                )
                # Part XVII — Reasoning Orchestrator: the one Brain may consult
                # several reasoning engines whose opinions become advisory
                # Evidence (never a vote). Built only when LLM is enabled AND
                # consult_multi is on. Reliability-informed by the Phase VIII
                # influence ledger (measured usefulness, not assumption — Art 11).
                _reasoning_orch = None
                try:
                    _llm_cfg = getattr(config, "llm", None)
                    if (_llm_cfg is not None and bool(getattr(_llm_cfg, "enabled", False))
                            and bool(getattr(_llm_cfg, "consult_multi", False))):
                        from llm.reasoning_orchestrator import (
                            build_reasoning_orchestrator as _build_reasoning_orch,
                        )
                        _rel = _influence.weight_for if _influence is not None else None
                        _reasoning_orch = _build_reasoning_orch(
                            _llm_cfg, reliability_provider=_rel)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SystemContext] ReasoningOrchestrator init failed: {}", exc)
                    _reasoning_orch = None
                ctx.reasoning_orchestrator = _reasoning_orch
                # §7/§9 — start the background recovery prober so a benched
                # (circuit-OPEN) advisor whose failure was transient is retried
                # quietly OFF the consult path and rejoins the council the moment
                # it heals. Daemon thread (dies with the process); fail-safe and a
                # strict no-op when the cadence is 0 or recovery is unsupported.
                if _reasoning_orch is not None:
                    try:
                        _start = getattr(_reasoning_orch, "start_recovery", None)
                        if callable(_start):
                            _start()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "[SystemContext] recovery prober start skipped: {}", exc)
                # Part XXI — Provider Registry / Manager. The catalogue of every
                # reasoning provider and its constitutional state. Built whenever
                # an LLM config exists (independent of consult_multi) so it also
                # surfaces CONFIGURED providers awaiting only credentials — the
                # "ready to activate" picture (Art 5 / 16 / 17). Observability
                # only; fail-safe (empty registry on any fault).
                try:
                    _llm_cfg = getattr(config, "llm", None)
                    if _llm_cfg is not None:
                        from llm.provider_registry import (
                            build_provider_registry as _build_provider_registry,
                        )
                        ctx.provider_registry = _build_provider_registry(_llm_cfg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SystemContext] ProviderRegistry init failed: {}", exc)
                    ctx.provider_registry = None
                # Part XXI Art 9/10/11 — Consultation Ledger. Records every
                # council consultation and grades each advisor (records + per-
                # domain scorecards). Built only when the multi-engine
                # orchestrator exists (there is nothing to record otherwise).
                # In-memory by default; observability/learning only, fail-safe.
                try:
                    if _reasoning_orch is not None:
                        from cognition.consultation_ledger import (
                            build_consultation_ledger as _build_consult_ledger,
                        )
                        ctx.consultation_ledger = _build_consult_ledger(enabled=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SystemContext] ConsultationLedger init failed: {}", exc)
                    ctx.consultation_ledger = None
                # Phase I (Part IX Art 9) — operational-intelligence author.
                # Turns terminated-campaign outcomes into operational objectives
                # (issue on recurring loss, notify on a validated win, periodic
                # report). Default-off; ecosystem-only, never broker orders.
                _ops_author = None
                try:
                    from cognition.operations import OperationsAuthor as _OperationsAuthor
                    _ops_author = _OperationsAuthor(
                        enabled=bool(getattr(cog_cfg, "operations_enabled", False)
                                     if cog_cfg is not None else False),
                        loss_streak_threshold=int(
                            getattr(cog_cfg, "operations_loss_streak", 3)
                            if cog_cfg is not None else 3),
                        report_period_seconds=float(
                            getattr(cog_cfg, "operations_report_period_seconds", 86_400.0)
                            if cog_cfg is not None else 86_400.0),
                        cooldown_seconds=float(
                            getattr(cog_cfg, "operations_cooldown_seconds", 3_600.0)
                            if cog_cfg is not None else 3_600.0),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SystemContext] OperationsAuthor init failed: {}", exc)
                    _ops_author = None
                ctx.operations_author = _ops_author

                # Part VII — the registry writes terminal outcomes to memory, and
                # (Part IX Art 9) feeds the same outcomes to the operations author,
                # and (Part VIII) grades evidence sources + Brain calibration.
                def _campaign_close_sink(camp: Any) -> None:
                    stitched = None
                    if _memory is not None:
                        try:
                            stitched = _memory.record_close(camp)
                        except Exception:  # noqa: BLE001
                            stitched = None
                    data = {}
                    pm = {}
                    try:
                        data = camp.to_dict() if hasattr(camp, "to_dict") else {}
                        pm = data.get("postmortem") or {}
                    except Exception:  # noqa: BLE001
                        data, pm = {}, {}
                    if _ops_author is not None:
                        try:
                            _ops_author.observe_campaign_outcome(
                                symbol=str(data.get("symbol", "") or ""),
                                direction=str(data.get("direction", "") or ""),
                                verdict=str(pm.get("verdict", "") or ""),
                                reasoning_quality=float(pm.get("reasoning_quality", 0.0) or 0.0),
                                realized_pnl=float(data.get("realized_pnl", 0.0) or 0.0),
                                evidence_ref=str(data.get("campaign_id", "") or ""),
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    # Part VIII — credit/debit the evidence sources that backed
                    # this campaign, and record the Brain's confidence-vs-outcome.
                    if stitched is not None:
                        won = bool(stitched.get("won", False))
                        if _influence is not None:
                            try:
                                _influence.observe_many(
                                    stitched.get("supporting_sources") or [], won)
                            except Exception:  # noqa: BLE001
                                pass
                        if _calibration is not None:
                            ec = stitched.get("entry_confidence")
                            if ec is not None:
                                try:
                                    _calibration.observe(ec, won)
                                except Exception:  # noqa: BLE001
                                    pass

                if (_memory is not None or _ops_author is not None
                        or _influence is not None) \
                        and ctx.campaign_registry is not None:
                    try:
                        ctx.campaign_registry.set_memory_sink(_campaign_close_sink)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[SystemContext] close sink wiring failed: {}", exc)

                # Violation V9 — per-symbol round-trip execution-cost estimate in
                # R for the Brain's EV. Uses the instrument's typical spread and a
                # nominal stop distance: cost_r ≈ spread_pips / stop_distance_pips
                # (spread + a slippage allowance). Falls back to the Brain's
                # default_cost_r when the instrument is unknown. Fail-safe.
                _default_cost_r = float(
                    getattr(cog_cfg, "default_execution_cost_r", 0.05)
                    if cog_cfg is not None else 0.05
                )
                _stop_distance_pips = float(
                    getattr(cog_cfg, "execution_cost_stop_distance_pips", 20.0)
                    if cog_cfg is not None else 20.0
                )

                def _execution_cost_source(symbol: str) -> dict:
                    try:
                        from config import INSTRUMENT_REGISTRY
                        info = INSTRUMENT_REGISTRY.get(str(symbol or ""))
                        if info is None:
                            return {"estimated_total_cost_r": _default_cost_r}
                        spread_pips = float(getattr(info, "typical_spread_pips", 0.0) or 0.0)
                        stop_pips = _stop_distance_pips if _stop_distance_pips > 0 else 20.0
                        if spread_pips <= 0.0:
                            return {"estimated_total_cost_r": _default_cost_r}
                        spread_r = min(0.5, spread_pips / stop_pips)
                        # Slippage allowance ≈ half the spread cost.
                        slippage_r = 0.5 * spread_r
                        return {
                            "estimated_spread_r": round(spread_r, 6),
                            "estimated_slippage_r": round(slippage_r, 6),
                            "estimated_total_cost_r": round(spread_r + slippage_r, 6),
                        }
                    except Exception:  # noqa: BLE001 — cost estimate must never break the loop
                        return {"estimated_total_cost_r": _default_cost_r}

                _consolidator = _EvidenceConsolidator(
                    ctx=ctx,
                    per_module=bool(
                        getattr(cog_cfg, "per_module_evidence", True)
                        if cog_cfg is not None else True
                    ),
                    memory=_memory,
                    max_analogues=int(
                        getattr(cog_cfg, "memory_max_analogues", 5)
                        if cog_cfg is not None else 5
                    ),
                    influence=_influence,
                    influence_enabled=_influence_enabled,
                    reasoning=_reasoning_orch,
                    knowledge=ctx.knowledge_source,
                    execution_cost_source=_execution_cost_source,
                )
                # Part XXI — record + grade every council consultation.
                if ctx.consultation_ledger is not None:
                    try:
                        _consolidator.set_consultation_ledger(ctx.consultation_ledger)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[SystemContext] consultation-ledger wiring failed: {}", exc)
                _bridge = _BrainActionBridge(
                    ctx.action_orchestrator,
                    notify_enabled=bool(
                        getattr(cog_cfg, "emit_operator_notifications", True)
                        if cog_cfg is not None else True
                    ),
                )

                def _cognition_symbols() -> list:
                    try:
                        eng = ctx.thesis_engine
                        if eng is not None:
                            st = eng.get_status() or {}
                            syms = list((st.get("theses") or {}).keys())
                            if syms:
                                return syms
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        return list(config.enabled_pairs)
                    except Exception:  # noqa: BLE001
                        return []

                def _cognition_positions() -> list:
                    # Constitution Part VI — the Brain manages the ACTUAL open
                    # broker book (every live position, independently), NOT the
                    # campaign ledger. Brain-originated entries reach the broker
                    # through the execution plane and are never registered as
                    # "campaigns", so sourcing from the registry left the Brain
                    # with nothing to manage (open-and-forget). Read the live
                    # positions and present each as a PositionView; enrich with
                    # campaign_id/entry_confidence when a matching campaign
                    # happens to exist. Fully fail-safe.
                    try:
                        from cognition.brain import PositionView as _PositionView
                        from cognition.position_adapter import (
                            canonical_side as _canon,
                            broker_profit_r as _profit_r,
                            hold_seconds_from as _hold,
                        )
                        try:
                            positions = (
                                platform_manager.get_all_open_positions() or []
                            )
                        except Exception:  # noqa: BLE001
                            positions = []
                        # Best-effort campaign enrichment, keyed by (symbol, side).
                        camp_by_key: dict = {}
                        reg = ctx.campaign_registry
                        if reg is not None:
                            try:
                                for c in (reg.get_status() or {}).get("live", []) or []:
                                    key = (
                                        str(c.get("symbol", "") or ""),
                                        _canon(c.get("direction", "")),
                                    )
                                    camp_by_key[key] = c
                            except Exception:  # noqa: BLE001
                                camp_by_key = {}
                        out = []
                        for p in positions:
                            side = _canon(getattr(p, "direction", ""))
                            if side not in ("LONG", "SHORT"):
                                continue
                            sym = str(getattr(p, "symbol", "") or "")
                            if not sym:
                                continue
                            camp = camp_by_key.get((sym, side)) or {}
                            ec = camp.get("entry_confidence") if isinstance(camp, dict) else None
                            out.append(_PositionView(
                                symbol=sym,
                                direction=side,
                                profit_r=_profit_r(
                                    side,
                                    getattr(p, "open_price", 0.0),
                                    getattr(p, "current_price", 0.0),
                                    getattr(p, "sl", 0.0),
                                ),
                                hold_seconds=_hold(getattr(p, "open_time", None)),
                                size=float(getattr(p, "lots", 0.0) or 0.0),
                                campaign_id=(
                                    str(camp.get("campaign_id", "") or "")
                                    if isinstance(camp, dict) else ""
                                ),
                                entry_confidence=(
                                    float(ec) if ec is not None else None
                                ),
                            ))
                        return out
                    except Exception:  # noqa: BLE001
                        return []

                def _cognition_balance(symbol: str) -> float:
                    # Per-symbol platform balance for Brain-originated sizing.
                    try:
                        return float(platform_manager.get_platform_balance(symbol) or 0.0)
                    except Exception:  # noqa: BLE001
                        return 0.0

                ctx.cognition_loop = _CognitionLoop(
                    ctx.cognitive_brain, _consolidator, _cognition_symbols,
                    interval_seconds=float(
                        getattr(cog_cfg, "loop_interval_seconds", 30.0)
                        if cog_cfg is not None else 30.0
                    ),
                    max_symbols_per_cycle=int(
                        getattr(cog_cfg, "max_symbols_per_cycle", 12)
                        if cog_cfg is not None else 12
                    ),
                    shadow_mode=bool(
                        getattr(cog_cfg, "shadow_mode", True)
                        if cog_cfg is not None else True
                    ),
                    action_bridge=_bridge,
                    position_source=_cognition_positions,
                    origination_mode=str(
                        getattr(cog_cfg, "origination_mode", "shadow")
                        if cog_cfg is not None else "shadow"
                    ),
                    origination_risk_fraction=float(
                        getattr(cog_cfg, "origination_risk_fraction", 0.01)
                        if cog_cfg is not None else 0.01
                    ),
                    origination_max_exposure=float(
                        getattr(cog_cfg, "origination_max_exposure", 1.0)
                        if cog_cfg is not None else 1.0
                    ),
                    balance_provider=_cognition_balance,
                    memory=_memory,
                    operations_author=_ops_author,
                    operations_sink=(ctx.action_planner.submit
                                     if ctx.action_planner is not None else None),
                    management_mode=str(
                        getattr(cog_cfg, "management_mode", "shadow")
                        if cog_cfg is not None else "shadow"
                    ),
                    event_driven=bool(
                        getattr(cog_cfg, "event_driven", False)
                        if cog_cfg is not None else False
                    ),
                    event_min_interval_seconds=float(
                        getattr(cog_cfg, "event_min_interval_seconds", 8.0)
                        if cog_cfg is not None else 8.0
                    ),
                    event_confidence_delta=float(
                        getattr(cog_cfg, "event_confidence_delta", 0.15)
                        if cog_cfg is not None else 0.15
                    ),
                    campaign_registry=ctx.campaign_registry,
                    reallocation_enabled=bool(
                        getattr(cog_cfg, "reallocation_enabled", False)
                        if cog_cfg is not None else False
                    ),
                    reallocation_max_per_component=int(
                        getattr(cog_cfg, "reallocation_max_per_component", 2)
                        if cog_cfg is not None else 2
                    ),
                    reallocation_concentration_limit=float(
                        getattr(cog_cfg, "reallocation_concentration_limit", 0.6)
                        if cog_cfg is not None else 0.6
                    ),
                    reallocation_trim_fraction=float(
                        getattr(cog_cfg, "reallocation_trim_fraction", 0.5)
                        if cog_cfg is not None else 0.5
                    ),
                )
                from cognition.gate import CognitionGate as _CognitionGate
                ctx.cognition_gate = _CognitionGate(
                    ctx.cognitive_brain,
                    mode=str(getattr(cog_cfg, "gate_mode", "authoritative")
                             if cog_cfg is not None else "authoritative"),
                    max_decision_age_seconds=float(
                        getattr(cog_cfg, "max_decision_age_seconds", 300.0)
                        if cog_cfg is not None else 300.0
                    ),
                )
                from cognition.management_gate import ManagementGate as _ManagementGate
                ctx.management_gate = _ManagementGate(
                    ctx.cognitive_brain,
                    mode=str(getattr(cog_cfg, "gate_mode", "authoritative")
                             if cog_cfg is not None else "authoritative"),
                    max_decision_age_seconds=float(
                        getattr(cog_cfg, "max_decision_age_seconds", 300.0)
                        if cog_cfg is not None else 300.0
                    ),
                )
                # Phase L (Part XII) — observability aggregator over the cognitive
                # components. Read-only; surfaces derived metrics (throughput,
                # veto/authorise rates, reasoning-quality score) for the rollout.
                try:
                    from cognition.observability import (
                        CognitionObservability as _CognitionObservability,
                    )
                    ctx.cognition_observability = _CognitionObservability(
                        brain=ctx.cognitive_brain,
                        loop=ctx.cognition_loop,
                        gate=ctx.cognition_gate,
                        management_gate=ctx.management_gate,
                        calibration=ctx.brain_calibration,
                        memory=ctx.campaign_memory,
                        min_calibration_samples=int(
                            getattr(cog_cfg, "influence_min_samples", 20)
                            if cog_cfg is not None else 20),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[SystemContext] CognitionObservability init failed: {}", exc)
                    ctx.cognition_observability = None
                logger.info(
                    "[SystemContext] Cognitive Brain ready — reasoner_available={} "
                    "shadow={} gate_mode={}",
                    ctx.cognitive_brain.available,
                    getattr(cog_cfg, "shadow_mode", False) if cog_cfg else False,
                    getattr(cog_cfg, "gate_mode", "authoritative") if cog_cfg else "authoritative",
                )
            except Exception as exc:
                logger.warning("[SystemContext] CognitiveBrain init failed: {}", exc)

            ctx.governance = _Governance(
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
                health_assessor=ctx.health_assessor,
                health_auto_freeze=bool(
                    getattr(getattr(lg_cfg, "health_assessor", None), "auto_freeze_on_critical", True)
                    if lg_cfg else True
                ),
                health_auto_release=bool(
                    getattr(getattr(lg_cfg, "health_assessor", None), "auto_release_on_healthy", True)
                    if lg_cfg else True
                ),
                gate_attributor=ctx.gate_attributor,
                thesis_engine=ctx.thesis_engine,
                campaign_registry=ctx.campaign_registry,
                llm_reasoner=ctx.llm_reasoner,
                action_orchestrator=ctx.action_orchestrator,
                cognitive_brain=ctx.cognitive_brain,
                cognition_loop=ctx.cognition_loop,
                cognition_gate=ctx.cognition_gate,
                management_gate=ctx.management_gate,
                campaign_memory=ctx.campaign_memory,
                action_planner=ctx.action_planner,
                operations_author=ctx.operations_author,
                influence_ledger=ctx.influence_ledger,
                brain_calibration=ctx.brain_calibration,
                reasoning_orchestrator=ctx.reasoning_orchestrator,
                provider_registry=ctx.provider_registry,
                consultation_ledger=ctx.consultation_ledger,
                cognition_observability=ctx.cognition_observability,
            )
            # Install Governance as the authoriser on the Learning→Governance
            # gateway and require authorisation (per config; default on).
            if ctx.recommendation_gateway is not None:
                ctx.recommendation_gateway.set_authorizer(ctx.governance.authorize)
                ctx.recommendation_gateway.set_governance_required(
                    bool(getattr(lg_cfg, "governance_required", True) if lg_cfg else True)
                )
        except Exception as exc:
            logger.critical(
                "[SystemContext] GovernanceDivision init failed — SAFETY DEGRADED; "
                "Learning recommendations will be REJECTED (fail-closed): {}", exc,
            )
            ctx._mark_safety_degraded("GovernanceDivision")
            # Fail-CLOSED: with no authoriser the gateway would otherwise
            # auto-approve every learning recommendation (fail-OPEN). Install a
            # rejecting authoriser and require authorisation so a governance
            # wiring fault cannot silently let unvetted recommendations through.
            gw = ctx.recommendation_gateway
            if gw is not None:
                try:
                    gw.set_authorizer(
                        lambda rec: (False, "governance unavailable — fail-closed")
                    )
                    gw.set_governance_required(True)
                except Exception as wire_exc:  # noqa: BLE001
                    logger.error(
                        "[SystemContext] failed to set fail-closed gateway authoriser: {}",
                        wire_exc,
                    )

        logger.info(
            "[SystemContext] learning layer initialized - outcome_fb={} "
            "ledger={} emitter_fb={} post_close={} gate_tuner={} shadow={} "
            "tuner={} ml={}",
            ctx.outcome_feedback is not None,
            ctx.signal_ledger is not None,
            ctx.emitter_feedback is not None,
            ctx.post_close_tracker is not None,
            ctx.gate_tuner is not None,
            ctx.shadow_store is not None,
            ctx.tuner_agent is not None,
            ctx.ml_adapter is not None,
        )

        # ── Phase 6: continuous-learning closure ─────────────────────
        # (a) AdaptiveWeightProvider — bounded, learned override of the static
        #     probabilistic-bias evidence weights, registered on decision_core
        #     so compute_bias reads it (falls back to static defaults on any
        #     fault or until the min-trades floor is reached).
        at_cfg = getattr(config, "adaptive_tuner", None)
        try:
            from adaptive.adaptive_weight_provider import (
                AdaptiveWeightProvider as _AWP,
            )
            from brain import decision_core as _dc
            if at_cfg is not None and bool(getattr(at_cfg, "enabled", True)):
                ctx.adaptive_weight_provider = _AWP(
                    enabled=bool(getattr(at_cfg, "weight_adaptation_enabled", True)),
                    min_trades=int(getattr(at_cfg, "weight_min_trades", 30)),
                    min_weight=float(getattr(at_cfg, "weight_min", 0.05)),
                    max_weight=float(getattr(at_cfg, "weight_max", 0.40)),
                    max_shift_per_cycle=float(
                        getattr(at_cfg, "weight_max_shift_per_cycle", 0.03)
                    ),
                    window_size=int(getattr(at_cfg, "weight_window_size", 200)),
                    adapt_gain=float(getattr(at_cfg, "weight_adapt_gain", 0.5)),
                    state_path=getattr(at_cfg, "weight_state_path", None),
                )
                _dc.set_evidence_weight_provider(ctx.adaptive_weight_provider)
        except Exception as exc:
            logger.warning("[SystemContext] AdaptiveWeightProvider init failed: {}", exc)

        # (b) RecommendationApplier — applies APPROVED PARAM_PROMOTE
        #     recommendations to a registered, bounded set of live config
        #     parameters (consensus / ranker thresholds). Wired as the gateway
        #     applier so it only fires on an authorised approval.
        try:
            from adaptive.recommendation_applier import (
                RecommendationApplier as _RecApplier,
            )
            if (
                at_cfg is not None
                and bool(getattr(at_cfg, "recommendation_apply_enabled", True))
                and ctx.recommendation_gateway is not None
            ):
                applier = _RecApplier(
                    enabled=True,
                    max_change_pct=float(
                        getattr(at_cfg, "recommendation_max_change_pct", 0.20)
                    ),
                )
                cons = getattr(config, "consensus", None)
                rank = getattr(config, "opportunity_ranker", None)
                if cons is not None:
                    applier.register_target(
                        "min_net_score",
                        lambda c=cons: float(getattr(c, "min_net_score", 1.5)),
                        lambda v, c=cons: setattr(c, "min_net_score", float(v)),
                        lo=0.5, hi=5.0,
                    )
                    applier.register_target(
                        "min_agreement",
                        lambda c=cons: float(getattr(c, "min_agreement", 0.55)),
                        lambda v, c=cons: setattr(c, "min_agreement", float(v)),
                        lo=0.3, hi=0.9,
                    )
                if rank is not None:
                    applier.register_target(
                        "min_expected_value",
                        lambda r=rank: float(getattr(r, "min_expected_value", 0.0)),
                        lambda v, r=rank: setattr(r, "min_expected_value", float(v)),
                        lo=0.0, hi=2.0,
                    )
                ctx.recommendation_applier = applier
                ctx.recommendation_gateway.set_applier(applier.apply)
        except Exception as exc:
            logger.warning("[SystemContext] RecommendationApplier init failed: {}", exc)

        logger.info(
            "[SystemContext] continuous-learning closure — adaptive_weights={} "
            "rec_applier={}",
            ctx.adaptive_weight_provider is not None,
            ctx.recommendation_applier is not None,
        )

        # ── Cross-instrument opportunity layer (GAP 1-5) ─────────────
        # Pure helpers built from the cross_instrument config. They are consumed
        # only at gated call sites, so when every cross_instrument flag is off the
        # system behaves identically. The queue + proactive scanner are built on
        # the event-driven system (they need runtime callables).
        ci_cfg = getattr(config, "cross_instrument", None)
        try:
            from brain.cross_instrument_ranker import CrossInstrumentRanker as _XRank
            from brain.opportunity_sizer import OpportunityQualitySizer as _QSizer
            from management.position_displacer import PositionDisplacer as _Displacer

            ctx.cross_instrument_ranker = _XRank(
                spread_pips_lookup=None,  # bound at runtime by the bootstrap
                win_rate_lookup=None,
                spread_ev_penalty_per_pip=float(
                    getattr(ci_cfg, "spread_ev_penalty_per_pip", 0.02) if ci_cfg else 0.02
                ),
                winrate_ev_weight=float(
                    getattr(ci_cfg, "winrate_ev_weight", 0.5) if ci_cfg else 0.5
                ),
            )
            ctx.opportunity_quality_sizer = _QSizer(
                enabled=bool(getattr(ci_cfg, "quality_sizing_enabled", False) if ci_cfg else False),
                max_boost=float(getattr(ci_cfg, "quality_sizing_max_boost", 1.3) if ci_cfg else 1.3),
                min_cut=float(getattr(ci_cfg, "quality_sizing_min_cut", 0.7) if ci_cfg else 0.7),
                ev_ref=float(getattr(ci_cfg, "quality_sizing_ev_ref", 1.0) if ci_cfg else 1.0),
            )
            ctx.position_displacer = _Displacer(
                enabled=bool(getattr(ci_cfg, "displacement_enabled", False) if ci_cfg else False),
                ev_margin=float(getattr(ci_cfg, "displacement_ev_margin", 0.5) if ci_cfg else 0.5),
                min_profit_protect=float(
                    getattr(ci_cfg, "displacement_min_profit_protect", 1.0) if ci_cfg else 1.0
                ),
                max_per_cycle=int(getattr(ci_cfg, "displacement_max_per_cycle", 1) if ci_cfg else 1),
                cooldown_seconds=float(
                    getattr(ci_cfg, "displacement_cooldown_seconds", 300.0) if ci_cfg else 300.0
                ),
            )
            logger.info(
                "[SystemContext] cross-instrument layer — quality_sizing={} "
                "displacement={} (queue/scanner on event-driven system)",
                ctx.opportunity_quality_sizer.enabled,
                ctx.position_displacer.enabled,
            )
        except Exception as exc:
            logger.warning("[SystemContext] cross-instrument layer init failed: {}", exc)

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
            data_repo_url = (
                str(getattr(db_cfg, "data_repo_url", "") or "") or None
                if db_cfg else None
            )
            auto_sync = bool(
                getattr(db_cfg, "auto_sync_data_repo", True) if db_cfg else True
            )
            # Refuse to enable auto-sync unless the data dir is a valid dedicated
            # data repo (own .git, origin → the data repo, never the source). An
            # invalid topology disables sync entirely rather than risk the source
            # engine repo.
            if auto_sync:
                try:
                    from runtime_paths import data_dir as _data_dir
                    from scripts.git_identity import data_repo_ready

                    ready, reason = data_repo_ready(
                        _data_dir(), data_repo_url=data_repo_url
                    )
                    if not ready:
                        logger.critical(
                            "[SystemContext] auto-sync DISABLED — data repo "
                            "identity invalid: {}",
                            reason,
                        )
                        auto_sync = False
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[SystemContext] data-repo identity check failed, "
                        "disabling auto-sync: {}", exc,
                    )
                    auto_sync = False
            ctx.daily_maintenance = _DailyMaint(
                data_dir=str(
                    getattr(db_cfg, "data_dir", "data") if db_cfg else "data"
                ),
                auto_sync_data_repo=auto_sync,
                sync_branch=str(
                    getattr(db_cfg, "sync_branch", "main") if db_cfg else "main"
                ),
                sync_orphan_branch=bool(
                    getattr(db_cfg, "sync_orphan_branch", True) if db_cfg else True
                ),
                sync_exclude_patterns=list(
                    getattr(db_cfg, "exclude_patterns", ["*.csv"]) if db_cfg else ["*.csv"]
                ),
                sync_interval_hours=float(
                    getattr(db_cfg, "interval_hours", 1) if db_cfg else 1
                ),
                vacuum_size_threshold_mb=float(
                    getattr(db_cfg, "vacuum_size_threshold_mb", 50.0)
                    if db_cfg else 50.0
                ),
                compact_repo_size_mb=float(
                    getattr(db_cfg, "compact_repo_size_mb", 500.0)
                    if db_cfg else 500.0
                ),
                max_rss_mb=float(
                    getattr(db_cfg, "max_rss_mb", 2048.0) if db_cfg else 2048.0
                ),
                data_repo_url=data_repo_url,
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
            # Event-driven retrain: a committed regime flip notifies the
            # optimiser to retrain out-of-band. ml_adapter is built earlier, so
            # its hook is available now; None-safe when the optimiser is absent.
            _regime_cb = (
                ctx.ml_adapter.notify_regime_change
                if ctx.ml_adapter is not None else None
            )
            ctx.regime_detector = _RegimeDetL7(
                lookback_bars=getattr(rd_cfg, "lookback_bars", 50) if rd_cfg else 50,
                hysteresis_bars=getattr(rd_cfg, "hysteresis_bars", 5) if rd_cfg else 5,
                on_regime_change=_regime_cb,
            )
        except Exception as exc:
            logger.warning("[SystemContext] RegimeDetector init failed: {}", exc)

        # ── BehaviorDiscoveryEngine (L6) ───────────────────────────
        try:
            from adaptive.behavior_discovery import BehaviorDiscoveryEngine as _BehavDisc
            ctx.behavior_discovery = _BehavDisc()
        except Exception as exc:
            logger.warning("[SystemContext] BehaviorDiscoveryEngine init failed: {}", exc)

        # Signal discovery / parameter evolution / virtual-module engines
        # RETIRED - severed from the live path; the Cognitive Brain decides.

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
