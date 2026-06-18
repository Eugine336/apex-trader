"""APEX TRADER — System Context.

Shared subsystem container that provides the event-driven system with access
to the same risk, decision, and lifecycle subsystems that TradingLoop uses.

Phase 1 scope: risk layer only (DrawdownGuard, PortfolioRiskStateMachine,
PortfolioGovernor, AccountRiskManager, CorrelationEngine, RiskEngine,
RiskReporter).  Later phases will add decision intelligence, adaptive
learning, and ops subsystems.

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
    from config import AppConfig
    from governor.portfolio_governor import PortfolioGovernor
    from platforms.platform_manager import PlatformManager
    from risk.account_risk import AccountRiskManager
    from risk.portfolio_risk_state import PortfolioRiskStateMachine
    from risk.risk_engine import RiskEngine
    from risk.risk_reporter import RiskReporter


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
