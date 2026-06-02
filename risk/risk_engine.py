"""
APEX TRADER — Risk Engine
The ultimate authority on whether a trade is allowed.
Every entry signal must pass through this gate.
The account's survival is non-negotiable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from brain.correlation_engine import CorrelationEngine, OpenTrade
from brain.drawdown_guard import DrawdownGuard, DrawdownMode
from config import (
    INSTRUMENT_REGISTRY,
    AppConfig,
    RiskConfig,
)
from risk.daily_tracker import PnLTracker
from risk.position_sizer import PositionSizer
from risk.spread_monitor import SpreadMonitor
from platform_context import PlatformContext, build_context_for_symbol
from adaptive.ev_estimator import EVEstimator


@dataclass
class RiskAssessment:
    approved: bool
    risk_pct: float
    position_size_lots: float
    max_loss_dollars: float
    checks: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    risk_mode: str = "NORMAL"
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    open_trade_count: int = 0
    total_exposure_pct: float = 0.0
    stake_usd: float = 0.0          # Deriv only; 0.0 for MT5
    sizing_mode: str = "lots"       # "lots" | "stake"
    ev_estimate: float = 0.0
    ev_confidence: str = "unknown"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AccountSnapshot:
    balance: float
    equity: float
    margin_used: float
    margin_free: float
    open_trades: int
    daily_pnl: float
    daily_pnl_pct: float
    weekly_pnl: float
    weekly_pnl_pct: float
    monthly_pnl: float
    monthly_pnl_pct: float
    max_daily_drawdown_remaining: float
    max_weekly_drawdown_remaining: float
    risk_mode: str
    timestamp: datetime


class RiskEngine:
    """
    Central risk authority — consolidates DrawdownGuard, CorrelationEngine,
    PositionSizer, PnLTracker, and SpreadMonitor into a single gate.
    No trade enters the market without clearance from this engine.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        starting_balance: float = 10_000.0,
    ):
        cfg = config or AppConfig()
        self.risk_cfg: RiskConfig = cfg.risk
        self.balance = starting_balance

        self.drawdown_guard = DrawdownGuard(base_risk_pct=self.risk_cfg.risk_per_trade_pct / 100.0)
        self.correlation_engine = CorrelationEngine(
            max_single_currency_exposure=self.risk_cfg.max_correlated_trades * 0.02,
            max_correlated_trades=self.risk_cfg.max_correlated_trades,
            allow_intentional_hedge=cfg.risk.allow_intentional_hedge,
        )
        self.position_sizer = PositionSizer()
        self.pnl_tracker = PnLTracker(starting_balance=starting_balance)
        self.spread_monitor = SpreadMonitor(
            max_multiplier=self.risk_cfg.max_spread_multiplier,
        )
        self.ev_estimator = EVEstimator()

        logger.info(
            f"RiskEngine initialized — balance=${starting_balance:,.2f}, "
            f"risk={self.risk_cfg.risk_per_trade_pct}%, "
            f"max_daily_dd={self.risk_cfg.max_daily_drawdown_pct}%"
        )

    def assess(
        self,
        pair: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        open_trades: list[dict[str, Any]] | None = None,
        account_balance: float | None = None,
        current_spread_pips: float | None = None,
        context: Optional[PlatformContext] = None,
        score: int = 0,
        trade_history: list[dict] | None = None,
        regime: str = "",
        session: str = "",
    ) -> RiskAssessment:
        now = datetime.now(timezone.utc)
        balance = account_balance or self.balance
        trades = open_trades or []
        checks: list[str] = []
        rejections: list[str] = []

        dd_status = self.drawdown_guard.get_status(now)
        mode = dd_status.mode
        risk_pct_decimal = dd_status.current_risk_pct

        if mode == DrawdownMode.FROZEN.value:
            rejections.append("Daily loss limit reached — trading frozen until tomorrow")
            logger.warning(f"[RiskEngine] REJECTED {pair}: FROZEN mode active")
            return self._build_assessment(
                False, 0.0, 0.0, 0.0, checks, rejections, mode,
                dd_status, len(trades), now,
            )
        checks.append(f"Drawdown mode: {mode} (risk={risk_pct_decimal:.2%})")

        pnl_snap = self.pnl_tracker.get_snapshot(account_balance=balance, timestamp=now)
        daily_loss_limit = self.risk_cfg.max_daily_drawdown_pct
        if self.pnl_tracker.is_daily_limit_hit(daily_loss_limit, balance, timestamp=now):
            self.drawdown_guard.mode = DrawdownMode.FROZEN
            rejections.append(
                f"Daily P&L {pnl_snap.daily_total_pct:.2f}% breached "
                f"-{daily_loss_limit}% limit — FROZEN"
            )
            logger.warning(f"[RiskEngine] {pair}: daily limit hit → FROZEN")
            return self._build_assessment(
                False, 0.0, 0.0, 0.0, checks, rejections,
                DrawdownMode.FROZEN.value, dd_status, len(trades), now,
            )
        checks.append(f"Daily P&L: {pnl_snap.daily_total_pct:.2f}% (limit: -{daily_loss_limit}%)")

        if self.pnl_tracker.is_weekly_limit_hit(8.0, balance, timestamp=now):
            if self.drawdown_guard.mode not in {DrawdownMode.RECOVERY, DrawdownMode.FROZEN}:
                self.drawdown_guard.mode = DrawdownMode.RECOVERY
                risk_pct_decimal = self.drawdown_guard.risk_map[DrawdownMode.RECOVERY]
                mode = DrawdownMode.RECOVERY.value
            checks.append("Weekly loss > 8% — RECOVERY mode engaged")
        else:
            checks.append(f"Weekly P&L: {pnl_snap.weekly_total_pct:.2f}%")

        if len(trades) >= self.risk_cfg.max_open_trades:
            rejections.append(
                f"Maximum {self.risk_cfg.max_open_trades} trades already open "
                f"({len(trades)} active)"
            )
            logger.warning(f"[RiskEngine] REJECTED {pair}: max trades reached")
            return self._build_assessment(
                False, 0.0, 0.0, 0.0, checks, rejections, mode,
                dd_status, len(trades), now,
            )
        checks.append(f"Open trades: {len(trades)}/{self.risk_cfg.max_open_trades}")

        corr_trades = self._to_open_trades(trades)
        can_open, corr_reason = self.correlation_engine.can_open_trade(
            pair, direction, corr_trades, risk_pct=risk_pct_decimal,
        )
        if not can_open:
            rejections.append(corr_reason)
            logger.warning(f"[RiskEngine] REJECTED {pair}: {corr_reason}")
            return self._build_assessment(
                False, 0.0, 0.0, 0.0, checks, rejections, mode,
                dd_status, len(trades), now,
            )
        checks.append("Correlation/exposure check passed")

        for t in trades:
            t_pair = t.get("pair", "").upper() if isinstance(t, dict) else getattr(t, "pair", "")
            t_dir = t.get("direction", "").upper() if isinstance(t, dict) else getattr(t, "direction", "")
            if t_pair == pair.upper() and t_dir == direction.upper():
                rejections.append(f"Already have {direction.upper()} on {pair.upper()}")
                logger.warning(f"[RiskEngine] REJECTED {pair}: duplicate trade")
                return self._build_assessment(
                    False, 0.0, 0.0, 0.0, checks, rejections, mode,
                    dd_status, len(trades), now,
                )
        checks.append("No duplicate pair+direction")

        ev_est = None
        if trade_history:
            ev_est = self.ev_estimator.estimate(pair, regime, session, trade_history)
            checks.append(
                f"EV estimate: {ev_est.expected_value:+.4f} "
                f"(WR={ev_est.win_rate:.0%}, n={ev_est.sample_size}, "
                f"confidence={ev_est.confidence}, source={ev_est.source})"
            )
            if (
                ev_est.expected_value < self.risk_cfg.ev_threshold
                and ev_est.confidence in ("high", "medium")
            ):
                rejections.append(
                    f"Negative EV: {ev_est.expected_value:+.4f} "
                    f"({ev_est.confidence} confidence, {ev_est.sample_size} trades) — "
                    f"below threshold {self.risk_cfg.ev_threshold}"
                )
                logger.warning(f"[RiskEngine] REJECTED {pair}: negative EV ({ev_est.expected_value:+.4f})")
                return self._build_assessment(
                    False, 0.0, 0.0, 0.0, checks, rejections, mode,
                    dd_status, len(trades), now,
                    ev_estimate=ev_est.expected_value,
                    ev_confidence=ev_est.confidence,
                )

        if score > 0:
            hwm_state = {
                "is_at_peak": dd_status.drawdown_from_peak_pct <= 0,
                "drawdown_from_peak_pct": dd_status.drawdown_from_peak_pct,
            }
            risk_pct_decimal = self._scale_risk_by_score(
                risk_pct_decimal, score, hwm_state,
            )
            checks.append(f"Risk scaled by score {score}: {risk_pct_decimal:.3%}")

        # Build context if not supplied — auto-detect from instrument registry
        if context is None:
            context = build_context_for_symbol(pair)

        info = INSTRUMENT_REGISTRY.get(pair.upper())
        pip_size = info.pip_size if info else 0.0001
        pip_value = info.pip_value_per_lot if info else 10.0

        if context.uses_stake:
            # Deriv path: max_loss = stake = risk_amount; no lots
            size_result = self.position_sizer.calculate_stake(
                account_balance=balance,
                risk_pct=risk_pct_decimal,
                entry_price=entry_price,
                stop_loss=stop_loss,
            )
            checks.append(
                f"[{context.platform}/{context.broker}] Stake sized: "
                f"${size_result.stake_usd:.2f} (stake-based), max_loss=${size_result.max_loss:.2f}"
            )
        else:
            # MT5 path: size in lots
            size_result = self.position_sizer.calculate(
                account_balance=balance,
                risk_pct=risk_pct_decimal,
                entry_price=entry_price,
                stop_loss=stop_loss,
                pip_size=pip_size,
                pip_value_per_lot=pip_value,
                context=context,
            )
            checks.append(
                f"[{context.platform}/{context.broker}] Position sized: "
                f"{size_result.lots} lots, max_loss=${size_result.max_loss:.2f}"
            )

        if size_result.sizing_mode.endswith("_skip_micro"):
            rejections.append("Account too small for this trade — position size below minimum")
            logger.warning(f"[RiskEngine] REJECTED {pair}: micro account skip ({size_result.sizing_mode})")
            return self._build_assessment(
                False, 0.0, 0.0, 0.0, checks, rejections, mode,
                dd_status, len(trades), now,
            )

        # Daily budget enforcement — reduce size if approaching daily limit
        remaining_daily = (daily_loss_limit / 100.0 * balance) + pnl_snap.daily_total
        if remaining_daily <= 0:
            rejections.append("No daily loss budget remaining")
            return self._build_assessment(
                False, 0.0, 0.0, 0.0, checks, rejections, mode,
                dd_status, len(trades), now,
            )
        if remaining_daily < size_result.max_loss:
            reduced_risk = remaining_daily / balance
            if context.uses_stake:
                size_result = self.position_sizer.calculate_stake(
                    account_balance=balance,
                    risk_pct=reduced_risk,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                )
            else:
                size_result = self.position_sizer.calculate(
                    account_balance=balance,
                    risk_pct=reduced_risk,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    pip_size=pip_size,
                    pip_value_per_lot=pip_value,
                    context=context,
                )
            checks.append(f"Size reduced to fit daily limit — {reduced_risk:.3%} risk")

        risk_pips = size_result.risk_pips
        if risk_pips > 0:
            tp_distance = risk_pips * self.risk_cfg.min_risk_reward
            checks.append(f"Min R:R {self.risk_cfg.min_risk_reward}:1 requires {tp_distance:.5g} pip target")

        if current_spread_pips is not None:
            safe, spread_reason = self.spread_monitor.is_spread_safe(
                pair, current_spread_pips,
            )
            if not safe:
                rejections.append(spread_reason)
                logger.warning(f"[RiskEngine] REJECTED {pair}: {spread_reason}")
                return self._build_assessment(
                    False, 0.0, 0.0, 0.0, checks, rejections, mode,
                    dd_status, len(trades), now,
                )
            checks.append(f"Spread safe: {current_spread_pips:.1f} pips")

        exposure_map = self.correlation_engine.calculate_exposure(
            corr_trades + [OpenTrade(pair=pair.upper(), direction=direction.upper(), risk_pct=risk_pct_decimal)]
        )

        if context.uses_stake:
            logger.info(
                f"[RiskEngine] APPROVED {direction.upper()} {pair.upper()} — "
                f"stake=${size_result.stake_usd:.2f}, risk={risk_pct_decimal:.2%}, "
                f"platform={context.platform}/{context.broker}, mode={mode}"
            )
        else:
            logger.info(
                f"[RiskEngine] APPROVED {direction.upper()} {pair.upper()} — "
                f"{size_result.lots} lots, risk={risk_pct_decimal:.2%}, "
                f"platform={context.platform}/{context.broker}, mode={mode}"
            )

        return self._build_assessment(
            True, risk_pct_decimal, size_result.lots, size_result.max_loss,
            checks, rejections, mode, dd_status, len(trades), now,
            exposure_pct=exposure_map.max_single_currency_exposure,
            stake_usd=size_result.stake_usd,
            sizing_mode=size_result.sizing_mode,
            ev_estimate=ev_est.expected_value if ev_est else 0.0,
            ev_confidence=ev_est.confidence if ev_est else "unknown",
        )

    def record_trade_result(
        self,
        pnl_dollars: float,
        pnl_pips: float,
        pair: str,
        direction: str,
        timestamp: datetime | None = None,
    ) -> None:
        timestamp = timestamp or datetime.now(timezone.utc)
        is_win = pnl_dollars > 0
        pnl_pct = pnl_dollars / self.balance if self.balance else 0.0

        self.pnl_tracker.record(pnl_dollars, is_win, timestamp)
        self.drawdown_guard.register_trade_result(pnl_pct, timestamp)
        self.balance += pnl_dollars

        status = self.drawdown_guard.get_status(timestamp)
        result_tag = "WIN" if is_win else "LOSS"
        logger.info(
            f"[RiskEngine] Trade result: {result_tag} {direction} {pair} "
            f"${pnl_dollars:+.2f} ({pnl_pips:+.1f} pips) — "
            f"mode={status.mode}, balance=${self.balance:,.2f}"
        )

    def get_account_snapshot(
        self,
        open_trades: list[dict[str, Any]] | None = None,
        account_balance: float | None = None,
    ) -> AccountSnapshot:
        now = datetime.now(timezone.utc)
        balance = account_balance or self.balance
        trades = open_trades or []

        pnl_snap = self.pnl_tracker.get_snapshot(account_balance=balance, timestamp=now)
        dd_status = self.drawdown_guard.get_status(now)

        daily_limit_dollars = self.risk_cfg.max_daily_drawdown_pct / 100.0 * balance
        weekly_limit_dollars = 0.08 * balance

        return AccountSnapshot(
            balance=round(balance, 2),
            equity=round(balance + pnl_snap.daily_unrealized, 2),
            margin_used=0.0,
            margin_free=balance,
            open_trades=len(trades),
            daily_pnl=pnl_snap.daily_realized,
            daily_pnl_pct=pnl_snap.daily_total_pct,
            weekly_pnl=pnl_snap.weekly_realized,
            weekly_pnl_pct=pnl_snap.weekly_total_pct,
            monthly_pnl=pnl_snap.monthly_realized,
            monthly_pnl_pct=pnl_snap.monthly_total_pct,
            max_daily_drawdown_remaining=round(
                daily_limit_dollars + pnl_snap.daily_realized, 2,
            ),
            max_weekly_drawdown_remaining=round(
                weekly_limit_dollars + pnl_snap.weekly_realized, 2,
            ),
            risk_mode=dd_status.mode,
            timestamp=now,
        )

    def reset_daily(self) -> None:
        now = datetime.now(timezone.utc)
        self.pnl_tracker.reset_daily(now)

        if self.drawdown_guard.mode == DrawdownMode.FROZEN:
            self.drawdown_guard.mode = DrawdownMode.CAUTION
            logger.info("[RiskEngine] NEW DAY: Was FROZEN → now CAUTION")
        else:
            logger.info(f"[RiskEngine] NEW DAY: Daily P&L reset. Mode: {self.drawdown_guard.mode.value}")

    def reset_weekly(self) -> None:
        now = datetime.now(timezone.utc)
        self.pnl_tracker.reset_weekly(now)
        logger.info("[RiskEngine] NEW WEEK: Weekly P&L reset")

    def _scale_risk_by_score(
        self, base_risk_pct: float, score: int, hwm_state: dict,
    ) -> float:
        if score >= 92:
            factor = 1.0
        elif score >= 88:
            factor = 0.85
        elif score >= 85:
            factor = 0.7
        else:
            factor = 0.5

        scaled = base_risk_pct * factor

        if hwm_state.get("is_at_peak") and score >= 90:
            scaled *= 1.1
        dd_pct = hwm_state.get("drawdown_from_peak_pct", 0)
        if dd_pct > 0.10:
            scaled *= 0.85

        scaled = min(scaled, 0.025)
        return round(scaled, 6)

    def _build_assessment(
        self,
        approved: bool,
        risk_pct: float,
        lots: float,
        max_loss: float,
        checks: list[str],
        rejections: list[str],
        mode: str,
        dd_status: Any,
        open_count: int,
        now: datetime,
        exposure_pct: float = 0.0,
        stake_usd: float = 0.0,
        sizing_mode: str = "lots",
        ev_estimate: float = 0.0,
        ev_confidence: str = "unknown",
    ) -> RiskAssessment:
        pnl_snap = self.pnl_tracker.get_snapshot(account_balance=self.balance, timestamp=now)
        return RiskAssessment(
            approved=approved,
            risk_pct=risk_pct,
            position_size_lots=lots,
            max_loss_dollars=max_loss,
            checks=checks,
            rejections=rejections,
            risk_mode=mode,
            daily_pnl_pct=pnl_snap.daily_total_pct,
            weekly_pnl_pct=pnl_snap.weekly_total_pct,
            open_trade_count=open_count,
            total_exposure_pct=round(exposure_pct * 100, 2),
            stake_usd=stake_usd,
            sizing_mode=sizing_mode,
            ev_estimate=ev_estimate,
            ev_confidence=ev_confidence,
            timestamp=now,
        )

    @staticmethod
    def _to_open_trades(trades: list[dict[str, Any]]) -> list[OpenTrade]:
        result: list[OpenTrade] = []
        for t in trades:
            if isinstance(t, dict):
                result.append(OpenTrade(
                    pair=t.get("pair", "").upper(),
                    direction=t.get("direction", "LONG").upper(),
                    risk_pct=float(t.get("risk_pct", 0.02)),
                ))
            elif isinstance(t, OpenTrade):
                result.append(t)
            else:
                result.append(OpenTrade(
                    pair=getattr(t, "pair", ""),
                    direction=getattr(t, "direction", "LONG"),
                    risk_pct=getattr(t, "risk_pct", 0.02),
                ))
        return result
