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
from brain.smoothing import piecewise_linear
from config import (
    INSTRUMENT_REGISTRY,
    AppConfig,
    RiskConfig,
)
from risk.daily_tracker import PnLTracker
from risk.position_sizer import PositionSizer
from risk.spread_monitor import SpreadMonitor
from risk import risk_accumulation as ra
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
    # #24 — accumulated near-/over-limit analytical risk dimensions, reported
    # additively alongside the (unchanged) hard physics rejections. ``risk_score``
    # (0..1) is the closeness of the nearest analytical limit and
    # ``risk_near_breaches`` names the dimensions at/over their limit — surfaced
    # for the trace / dashboard so a trade taken with little headroom is visible.
    risk_score: float = 0.0
    risk_near_breaches: list[str] = field(default_factory=list)
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
        starting_balance: float | None = None,
    ):
        cfg = config or AppConfig()
        self.risk_cfg: RiskConfig = cfg.risk
        self.balance = (
            starting_balance
            if starting_balance is not None
            else self.risk_cfg.backtest_starting_balance_usd
        )
        # Pending broker balance awaiting a second corroborating read before an
        # implausibly large single-cycle swing is trusted (see reconcile_balance).
        self._balance_resync_candidate: float | None = None
        # Per-platform balances, tracked INDEPENDENTLY. MT5 and Deriv balances
        # must never collide or substitute for one another: a disconnected leg
        # retains its last value here so the pooled balance does not oscillate.
        # Each platform carries its own swing-corroboration candidate.
        self._platform_balances: dict[str, float] = {}
        self._platform_resync_candidate: dict[str, float] = {}

        self.drawdown_guard = DrawdownGuard(
            base_risk_pct=self.risk_cfg.risk_per_trade_pct / 100.0,
            rolling_window_days=self.risk_cfg.drawdown_rolling_window_days,
        )
        self.correlation_engine = CorrelationEngine(
            max_single_currency_exposure=self.risk_cfg.max_correlated_trades * 0.02,
            max_correlated_trades=self.risk_cfg.max_correlated_trades,
            allow_intentional_hedge=cfg.risk.allow_intentional_hedge,
        )
        self.position_sizer = PositionSizer(
            micro_account_threshold_usd=self.risk_cfg.micro_account_threshold_usd,
            deriv_min_stake_usd=self.risk_cfg.deriv_min_stake_usd,
            max_risk_pct_per_trade=self.risk_cfg.max_risk_pct_per_trade,
            engine_cap=self._RISK_PCT_CAP,
            allow_min_lot_over_risk=getattr(
                self.risk_cfg, "allow_min_lot_over_risk", False
            ),
        )
        self.pnl_tracker = PnLTracker(starting_balance=self.balance)
        self.spread_monitor = SpreadMonitor(
            max_multiplier=self.risk_cfg.max_spread_multiplier,
        )
        self.ev_estimator = EVEstimator()

        logger.info(
            f"RiskEngine initialized — balance=${self.balance:,.2f}, "
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
        conviction: float | None = None,
        portfolio_heat_pct: float = 0.0,
        strategy_allocation: float = 1.0,
    ) -> RiskAssessment:
        """DEPRECATED for the live entry path — superseded by the organisational
        split. The event-driven entry handler now runs the binary safety vetoes
        (FROZEN / daily-loss / max-trades / correlation / duplicate / EV / spread)
        as discrete Compliance-style gates and sizes through ``PortfolioDivision``
        (``portfolio/division.py``), which also enforces the remaining
        daily-loss budget this chain owned. ``assess`` is retained for the
        backtest engine, offline tooling, and tests — do not reintroduce it on
        the live path.
        """
        now = datetime.now(timezone.utc)
        # A passed balance of exactly 0.0 is a real state (margin call) and must
        # NOT silently fall back to the stale internal balance — only None does.
        balance = account_balance if account_balance is not None else self.balance
        if balance is None or balance <= 0:
            logger.warning(
                f"[RiskEngine] REJECTED {pair}: account balance unavailable "
                f"or non-positive ({balance!r}) — refusing to size"
            )
            return self._build_assessment(
                False, 0.0, 0.0, 0.0,
                [], ["Account balance unavailable or non-positive — refusing to size"],
                "NORMAL", self.drawdown_guard.get_status(now), 0, now,
            )
        # Per-account balance drives SIZING; total portfolio equity drives the
        # GLOBAL daily/weekly drawdown backstop (pooled P&L ÷ total equity), so
        # the backstop is no longer denominated by whichever account traded last.
        total_equity = self.balance if (self.balance and self.balance > 0) else balance
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

        pnl_snap = self.pnl_tracker.get_snapshot(account_balance=total_equity, timestamp=now)
        daily_loss_limit = self.risk_cfg.max_daily_drawdown_pct
        if self.pnl_tracker.is_daily_limit_hit(daily_loss_limit, total_equity, timestamp=now):
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

        if self.pnl_tracker.is_weekly_limit_hit(self.risk_cfg.max_weekly_drawdown_pct, total_equity, timestamp=now):
            if self.drawdown_guard.mode not in {DrawdownMode.RECOVERY, DrawdownMode.FROZEN}:
                self.drawdown_guard.mode = DrawdownMode.RECOVERY
                risk_pct_decimal = self.drawdown_guard.risk_map[DrawdownMode.RECOVERY]
                mode = DrawdownMode.RECOVERY.value
            checks.append(
                f"Weekly loss > {self.risk_cfg.max_weekly_drawdown_pct}% — RECOVERY mode engaged"
            )
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

        # ── Position sizing chain (single auditable path) ────────────────
        # P3+P7 (PIPELINE_INTEGRATION_MAP §7.4/§7.7): fresh DecisionEngine
        # conviction — when supplied — replaces the stale scanner score as the
        # primary sizing input. All sizing factors are consolidated into one
        # auditable, purely de-risking chain (each factor clamped to [0,1]) and
        # logged so any trade's risk % can be fully reconstructed from the log.
        if conviction is not None or score > 0:
            hwm_state = {
                "is_at_peak": dd_status.drawdown_from_peak_pct <= 0,
                "drawdown_from_peak_pct": dd_status.drawdown_from_peak_pct,
            }
            risk_pct_decimal = self.compute_position_size_risk(
                base_risk_pct=risk_pct_decimal,
                conviction=conviction,
                score=score,
                hwm_state=hwm_state,
                portfolio_heat_pct=portfolio_heat_pct,
                strategy_allocation=strategy_allocation,
            )
            _src = f"conviction {conviction:.2f}" if conviction is not None else f"score {score}"
            checks.append(f"Risk scaled by {_src}: {risk_pct_decimal:.3%}")

        # Build context if not supplied — auto-detect from instrument registry
        if context is None:
            context = build_context_for_symbol(pair)

        info = INSTRUMENT_REGISTRY.get(pair.upper())
        if info is None and not context.uses_stake:
            rejections.append(
                f"Unknown instrument {pair} — refusing to size on assumed pip economics"
            )
            logger.warning(f"[RiskEngine] REJECTED {pair}: not in INSTRUMENT_REGISTRY")
            return self._build_assessment(
                False, 0.0, 0.0, 0.0, checks, rejections, mode,
                dd_status, len(trades), now,
            )
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
                symbol=pair,
            )
            checks.append(
                f"[{context.platform}/{context.broker}] Position sized: "
                f"{size_result.lots} lots, max_loss=${size_result.max_loss:.2f}"
            )

        if "skip" in size_result.sizing_mode:
            rejections.append(f"Position sizing rejected ({size_result.sizing_mode})")
            logger.warning(f"[RiskEngine] REJECTED {pair}: {size_result.sizing_mode}")
            return self._build_assessment(
                False, 0.0, 0.0, 0.0, checks, rejections, mode,
                dd_status, len(trades), now,
            )

        # Daily budget enforcement — reduce size if approaching daily limit.
        # Budget is the GLOBAL remaining loss room (total equity); the resulting
        # reduced risk is then applied to THIS account's sizing balance.
        remaining_daily = (daily_loss_limit / 100.0 * total_equity) + pnl_snap.daily_total
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
                    symbol=pair,
                )
            checks.append(f"Size reduced to fit daily limit — {reduced_risk:.3%} risk")

        # Re-verify after sizing: the MIN_LOT floor can push max_loss back above
        # the remaining daily budget even after the reduction above. Reject the
        # trade rather than silently breaching the daily loss limit.
        if remaining_daily > 0 and size_result.max_loss > remaining_daily:
            rejections.append(
                f"Sized max_loss ${size_result.max_loss:.2f} exceeds remaining "
                f"daily budget ${remaining_daily:.2f} (min-lot floor)"
            )
            logger.warning(
                f"[RiskEngine] REJECTED {pair}: min-lot max_loss "
                f"${size_result.max_loss:.2f} > remaining daily ${remaining_daily:.2f}"
            )
            return self._build_assessment(
                False, 0.0, 0.0, 0.0, checks, rejections, mode,
                dd_status, len(trades), now,
            )

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
            risk_dimensions=self._risk_dimensions(
                open_count=len(trades),
                exposure_pct=exposure_map.max_single_currency_exposure,
                ev_est=ev_est,
                current_spread_pips=current_spread_pips,
                pair=pair,
            ),
        )

    def _risk_dimensions(
        self,
        *,
        open_count: int,
        exposure_pct: float,
        ev_est: Any,
        current_spread_pips: float | None,
        pair: str,
    ) -> "ra.RiskScore":
        """Measure every analytical risk dimension for reporting (#24).

        Purely informational — the hard physics/safety rejections above are
        unchanged. This accumulates the open-count, currency-exposure, EV and
        spread proximities so a near-limit (but approved) trade is visible on the
        trace / dashboard instead of every dimension but the first being lost.
        """
        dims: list[ra.RiskDimension] = []
        try:
            if self.risk_cfg.max_open_trades > 0:
                dims.append(ra.dimension(
                    "open_trades", open_count, self.risk_cfg.max_open_trades,
                    f"{open_count}/{self.risk_cfg.max_open_trades} open",
                ))
            max_cur = self.correlation_engine.max_single_currency_exposure
            if max_cur and max_cur > 0:
                dims.append(ra.dimension(
                    "currency_exposure", exposure_pct, max_cur,
                    f"exposure {exposure_pct:.2%} vs {max_cur:.2%}",
                ))
            if ev_est is not None and ev_est.confidence in ("high", "medium"):
                # EV above the threshold is safe; closeness to (or below) it is
                # risk. Offset both sides by the same constant so a negative
                # threshold still yields a sensible positive ratio.
                offset = 1.0
                dims.append(ra.dimension(
                    "expected_value",
                    offset + self.risk_cfg.ev_threshold,
                    max(1e-6, offset + ev_est.expected_value),
                    f"EV {ev_est.expected_value:+.4f} vs threshold {self.risk_cfg.ev_threshold:+.4f}",
                    lower_is_riskier=True,
                ))
            if current_spread_pips is not None:
                typical = self.spread_monitor.typical_spread(pair) if hasattr(self.spread_monitor, "typical_spread") else 0.0
                if typical and typical > 0:
                    dims.append(ra.dimension(
                        "spread", current_spread_pips,
                        typical * self.risk_cfg.max_spread_multiplier,
                        f"spread {current_spread_pips:.1f} vs cap "
                        f"{typical * self.risk_cfg.max_spread_multiplier:.1f}",
                    ))
        except Exception as exc:  # noqa: BLE001
            # Optional risk dimensions failed to build — proceed with whatever
            # dimensions were collected, but make the degradation visible.
            logger.warning("[RiskEngine] optional risk dimension build skipped: {}", exc)
        return ra.accumulate(dims)

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
        balance = account_balance if account_balance is not None else self.balance
        trades = open_trades or []

        pnl_snap = self.pnl_tracker.get_snapshot(account_balance=balance, timestamp=now)
        dd_status = self.drawdown_guard.get_status(now)

        daily_limit_dollars = self.risk_cfg.max_daily_drawdown_pct / 100.0 * balance
        weekly_limit_dollars = self.risk_cfg.max_weekly_drawdown_pct / 100.0 * balance

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

    def get_state(self) -> dict:
        """Read-only snapshot for the dashboard (Risk panel).

        Surfaces live RiskEngine state — balance, daily P&L, drawdown, risk
        mode and daily-loss-limit usage — drawn from the DrawdownGuard and
        PnLTracker.  Read-only and self-contained; never mutates and (being
        called from the dashboard) tolerates partial state gracefully.
        """
        now = datetime.now(timezone.utc)
        status = self.drawdown_guard.get_status(now)
        snap = self.pnl_tracker.get_snapshot(account_balance=self.balance, timestamp=now)

        # daily_total_pct is in percent (e.g. -2.4); drawdown-from-peak + the
        # guard's daily_pnl_pct are fractions (e.g. -0.024).
        daily_pnl_pct = float(snap.daily_total_pct)
        daily_limit_pct = float(self.risk_cfg.max_daily_drawdown_pct)  # e.g. 3.0
        daily_loss_pct = abs(daily_pnl_pct) if daily_pnl_pct < 0 else 0.0
        daily_loss_used_pct = (
            round(min(100.0, daily_loss_pct / daily_limit_pct * 100.0), 2)
            if daily_limit_pct > 0 else 0.0
        )

        rolling_dd_pct = round(float(status.drawdown_from_peak_pct) * 100.0, 3)
        normal_risk = self.drawdown_guard.risk_map.get(DrawdownMode.NORMAL, 0.0) or 0.0
        sizing_factor = (
            round(float(status.current_risk_pct) / normal_risk, 4)
            if normal_risk > 0 else 1.0
        )
        # Best-effort peak equity in dollars from the rolling drawdown fraction.
        dd_frac = float(status.drawdown_from_peak_pct)
        peak_equity = (
            round(self.balance / (1.0 - dd_frac), 2)
            if 0.0 < dd_frac < 1.0 else round(self.balance, 2)
        )
        is_frozen = status.mode == DrawdownMode.FROZEN.value

        return {
            "enabled": True,
            "source": "live",
            "state": status.mode,                 # legacy key consumed by the panel
            "risk_mode": status.mode,
            "balance": round(self.balance, 2),
            "current_equity": round(self.balance + float(snap.daily_unrealized), 2),
            "peak_equity": peak_equity,
            "daily_pnl_dollars": round(float(snap.daily_realized), 2),
            "daily_pnl_pct": round(daily_pnl_pct, 3),
            "daily_drawdown_pct": round(daily_loss_pct, 3),
            "rolling_drawdown_pct": rolling_dd_pct,
            "daily_loss_limit_pct": round(daily_limit_pct, 3),
            "daily_loss_used_pct": daily_loss_used_pct,
            "current_risk_pct": round(float(status.current_risk_pct) * 100.0, 4),
            "sizing_factor": sizing_factor,
            "should_flatten": bool(is_frozen),
            "consecutive_losses": int(status.consecutive_losses),
            "consecutive_wins": int(status.consecutive_wins),
            "limits": {
                "max_daily_drawdown_pct": daily_limit_pct,
                "max_weekly_drawdown_pct": float(self.risk_cfg.max_weekly_drawdown_pct),
            },
            "risk_events": [],      # RiskEngine keeps no event list; panel degrades
            "correlations": [],     # correlation surfaced elsewhere (Portfolio)
        }

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

    def to_state(self) -> dict:
        """Serialise daily risk state (balance + drawdown guard + P&L tallies)
        so a mid-day restart doesn't reset the loss budget / FROZEN mode / daily
        loss tracking to fresh."""
        return {
            "balance": self.balance,
            "drawdown_guard": self.drawdown_guard.to_state(),
            "pnl_tracker": self.pnl_tracker.to_state(),
        }

    def restore_state(self, state: dict) -> None:
        """Restore balance + drawdown guard from a persisted payload."""
        try:
            bal = float(state.get("balance", 0.0) or 0.0)
            if bal > 0:
                self.balance = bal
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[RiskEngine] corrupt persisted balance ignored — balance starts "
                "fresh this session: {}", exc,
            )
        dg = state.get("drawdown_guard")
        if dg:
            try:
                self.drawdown_guard.restore_state(dg)
            except Exception as exc:
                logger.warning("[RiskEngine] drawdown guard restore failed: {}", exc)
        pnl_state = state.get("pnl_tracker")
        if pnl_state:
            try:
                self.pnl_tracker.restore_state(pnl_state)
            except Exception as exc:
                logger.warning("[RiskEngine] pnl tracker restore failed: {}", exc)

    def reconcile_balance(
        self,
        broker_balance: float | None,
        divergence_warn_pct: float = 1.0,
        max_jump_pct: float = 50.0,
    ) -> None:
        """Sync the internal balance to broker truth.

        ``self.balance`` is advanced by record_trade_result() between cycles and
        can drift from the broker (commissions, swaps, slippage, manual trades).
        This pulls it back to the authoritative broker balance each call and
        logs a warning when the pre-sync divergence exceeds the threshold so a
        persistent desync is visible. A broker balance of exactly 0.0 is a valid
        state and is applied; only None/negative is ignored.

        A single-cycle swing larger than ``max_jump_pct`` is treated as suspect
        (e.g. a reconnecting platform leg momentarily reporting only its own
        standalone balance instead of the pooled multi-platform figure) and is
        NOT applied until a second consecutive read corroborates it, so a
        one-cycle fluke cannot silently corrupt downstream sizing / drawdown
        state.
        """
        if broker_balance is None or broker_balance < 0:
            return
        broker_balance = float(broker_balance)
        prev = self.balance
        if prev and prev > 0 and broker_balance > 0:
            divergence_pct = abs(prev - broker_balance) / broker_balance * 100.0
            if divergence_pct >= max_jump_pct:
                candidate = self._balance_resync_candidate
                corroborated = (
                    candidate is not None
                    and candidate > 0
                    and abs(candidate - broker_balance) / broker_balance * 100.0
                    < max_jump_pct
                )
                if not corroborated:
                    self._balance_resync_candidate = broker_balance
                    logger.warning(
                        "[RiskEngine] implausible balance swing {:.2f}% rejected — "
                        "internal ${:,.2f} vs broker ${:,.2f}; awaiting a second "
                        "corroborating read before resyncing",
                        divergence_pct, prev, broker_balance,
                    )
                    return
                logger.warning(
                    "[RiskEngine] balance swing {:.2f}% corroborated over two reads "
                    "— syncing to broker ${:,.2f}",
                    divergence_pct, broker_balance,
                )
            elif divergence_pct >= divergence_warn_pct:
                logger.warning(
                    "[RiskEngine] balance desync {:.2f}% — internal ${:,.2f} vs "
                    "broker ${:,.2f}; syncing to broker truth",
                    divergence_pct, prev, broker_balance,
                )
        self._balance_resync_candidate = None
        self.balance = broker_balance

    def reconcile_platform_balances(
        self,
        platform_balances: dict[str, float] | None,
        max_jump_pct: float = 50.0,
    ) -> None:
        """Sync per-platform balances independently, then pool the total.

        Each platform's balance is reconciled against its OWN previous value —
        never against another platform's — so MT5 (real) and Deriv (demo)
        balances can never collide or substitute for one another. A platform
        absent from this read retains its last-known balance rather than
        dropping out of the pooled total. The implausible-swing guard is
        applied per platform: a single-cycle jump beyond ``max_jump_pct`` for a
        given platform must be corroborated by a second consecutive read for
        that same platform before it is applied.

        ``self.balance`` is set to the sum of the (independently maintained)
        per-platform balances, keeping the pooled figure stable even while one
        leg reconnects.
        """
        if not platform_balances:
            return
        for platform, raw in platform_balances.items():
            if raw is None:
                continue
            try:
                bal = float(raw)
            except (TypeError, ValueError):
                continue
            if bal < 0:
                continue
            prev = self._platform_balances.get(platform)
            if prev and prev > 0 and bal > 0:
                divergence_pct = abs(prev - bal) / bal * 100.0
                if divergence_pct >= max_jump_pct:
                    candidate = self._platform_resync_candidate.get(platform)
                    corroborated = (
                        candidate is not None
                        and candidate > 0
                        and abs(candidate - bal) / bal * 100.0 < max_jump_pct
                    )
                    if not corroborated:
                        self._platform_resync_candidate[platform] = bal
                        logger.warning(
                            "[RiskEngine] {} balance swing {:.2f}% rejected — "
                            "internal ${:,.2f} vs broker ${:,.2f}; awaiting a "
                            "second corroborating read before resyncing",
                            platform, divergence_pct, prev, bal,
                        )
                        continue
                    logger.warning(
                        "[RiskEngine] {} balance swing {:.2f}% corroborated over "
                        "two reads — syncing to broker ${:,.2f}",
                        platform, divergence_pct, bal,
                    )
            self._platform_resync_candidate.pop(platform, None)
            self._platform_balances[platform] = bal
        pooled = sum(self._platform_balances.values())
        if pooled > 0:
            self.balance = pooled

    def get_platform_balance(self, platform: str) -> float | None:
        """Last-known independent balance for a single platform, if tracked."""
        return self._platform_balances.get(platform)

    # Hard ceiling on per-trade risk regardless of any scaling factor.
    _RISK_PCT_CAP = 0.025

    def compute_position_size_risk(
        self,
        base_risk_pct: float,
        conviction: float | None,
        score: int,
        hwm_state: dict,
        portfolio_heat_pct: float = 0.0,
        strategy_allocation: float = 1.0,
    ) -> float:
        """Single auditable sizing chain (P7).

        Returns the per-trade risk % after applying every sizing factor. The
        chain is purely multiplicative and de-risking only — each factor is
        clamped to [0,1] so no factor can ever inflate risk above ``base``.
        Every factor is logged with its input and the running total so a
        trade's sizing can be fully reconstructed from the logs.

        When ``conviction`` is supplied (Decision Engine active, the default
        live path) it drives sizing via fresh data. When it is ``None`` the
        engine falls back to the legacy stale-score scaler for backward
        compatibility (e.g. Decision Engine disabled).

        ``strategy_allocation`` (L5.5a) is the Capital Allocation Engine's
        per-strategy sizing multiplier — the trade's execution-style share of
        the book, clamped to [0,1]. Defaults to 1.0 (no-op) so the chain is
        unchanged when the allocator is absent / disabled / cold.
        """
        if conviction is None:
            # Legacy path — preserve exact historical behaviour.
            return self._scale_risk_by_score_DEPRECATED(base_risk_pct, score, hwm_state)

        dd_pct = hwm_state.get("drawdown_from_peak_pct", 0.0) or 0.0

        conviction_scale = self._scale_by_conviction(conviction)
        heat_scale = self._scale_by_portfolio_heat(portfolio_heat_pct)
        drawdown_scale = self._scale_by_drawdown(dd_pct)
        allocation_scale = max(0.0, min(1.0, float(strategy_allocation)))

        size = base_risk_pct
        size *= allocation_scale
        size *= conviction_scale
        size *= heat_scale
        size *= drawdown_scale

        capped = min(size, self._RISK_PCT_CAP)
        final = round(capped, 6)

        logger.info(
            "[RiskEngine] Sizing chain: base={:.4%} "
            "× allocation({:.2f})={:.2f} "
            "× conviction({:.2f})={:.2f} "
            "× heat({:.2f}%)={:.2f} "
            "× drawdown({:.2%})={:.2f} "
            "= {:.4%}{}",
            base_risk_pct, float(strategy_allocation), allocation_scale,
            conviction, conviction_scale,
            portfolio_heat_pct, heat_scale,
            dd_pct, drawdown_scale,
            final, " (capped)" if capped < size else "",
        )
        return final

    @staticmethod
    def _scale_by_conviction(conviction: float) -> float:
        """Fresh-conviction sizing factor (P3) — 0–1 conviction → [0,1] factor.

        #30 — a continuous monotonic curve anchored on the old 0.85/0.88/0.92
        tier values instead of a step function, so 0.879 and 0.851 size
        *differently* (0.845 vs 0.705) rather than collapsing onto 0.7. De-risking
        only — never amplifies above 1.0.
        """
        c = max(0.0, min(1.0, conviction))
        return round(piecewise_linear(
            c,
            ((0.0, 0.5), (0.85, 0.7), (0.88, 0.85), (0.92, 1.0), (1.0, 1.0)),
        ), 4)

    @staticmethod
    def _scale_by_portfolio_heat(heat_pct: float) -> float:
        """Portfolio-heat sizing factor — de-risk smoothly as live heat climbs
        toward the block threshold. Heat is expressed in percent of equity.

        #30 — continuous curve anchored on the old 1.0/1.5/2.0% tier edges so a
        heat of 1.49% and 1.51% no longer jump from 0.85 to 0.7.
        """
        h = max(0.0, heat_pct)
        return round(piecewise_linear(
            h,
            ((1.0, 1.0), (1.5, 0.85), (2.0, 0.7), (3.0, 0.5)),
        ), 4)

    @staticmethod
    def _scale_by_drawdown(dd_pct: float) -> float:
        """Drawdown-from-peak sizing factor — de-risk deeper into drawdown.
        ``dd_pct`` is a fraction (0.10 == 10% below the high-water mark).

        #30 — continuous curve anchored on the old 5/10/15% tier edges so the
        factor transitions smoothly instead of stepping at the boundaries.
        """
        d = max(0.0, dd_pct)
        return round(piecewise_linear(
            d,
            ((0.05, 1.0), (0.10, 0.85), (0.15, 0.7), (0.25, 0.5)),
        ), 4)

    def _scale_risk_by_score_DEPRECATED(
        self, base_risk_pct: float, score: int, hwm_state: dict,
    ) -> float:
        """DEPRECATED — replaced by conviction-based sizing via
        ``compute_position_size_risk`` / ``_scale_by_conviction`` (P3).

        Retained for the legacy path (Decision Engine disabled) where no fresh
        conviction is available, and so the change is traceable. Uses the stale
        scanner ``score`` computed at scan time T, not entry time T+N.
        """
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

        scaled = min(scaled, self._RISK_PCT_CAP)
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
        risk_dimensions: "ra.RiskScore | None" = None,
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
            risk_score=(round(risk_dimensions.score, 4) if risk_dimensions is not None else 0.0),
            risk_near_breaches=(
                (risk_dimensions.near_breaches + risk_dimensions.breaches)
                if risk_dimensions is not None else []
            ),
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
