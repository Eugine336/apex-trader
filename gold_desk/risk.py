"""Fail-closed preflight gate for Gold opportunities.

This is a second line of defence, not a replacement for Apex's existing
RiskEngine, portfolio risk checks, execution RiskGate, or broker-side controls.
It intentionally defaults to denying live trading.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from gold_desk.coordinator import GoldOnlyGuard
from gold_desk.models import Direction, MarketSnapshot, TradeIdea


@dataclass(frozen=True, slots=True)
class RiskState:
    equity: float
    peak_equity: float
    day_start_equity: float
    open_risk_pct: float = 0.0
    open_positions: int = 0
    manually_halted: bool = False

    def __post_init__(self) -> None:
        for name in ("equity", "peak_equity", "day_start_equity"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.open_risk_pct < 0 or not math.isfinite(self.open_risk_pct):
            raise ValueError("open_risk_pct must be finite and non-negative")
        if self.open_positions < 0:
            raise ValueError("open_positions must be non-negative")

    @property
    def daily_drawdown_pct(self) -> float:
        return max(0.0, (self.day_start_equity - self.equity) / self.day_start_equity * 100.0)

    @property
    def peak_drawdown_pct(self) -> float:
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity * 100.0)


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class GoldRiskConfig:
    max_risk_per_trade_pct: float = 0.25
    max_total_open_risk_pct: float = 0.75
    max_daily_drawdown_pct: float = 2.0
    max_peak_drawdown_pct: float = 6.0
    max_open_positions: int = 1
    min_reward_to_risk: float = 1.20
    max_spread_to_atr: float = 0.20
    max_quote_age_seconds: float = 3.0
    max_entry_drift_atr: float = 0.25


class GoldExecutionGate:
    """Authorize a candidate for subsequent execution checks; never sends orders."""

    def __init__(
        self,
        config: GoldRiskConfig | None = None,
        gold_guard: GoldOnlyGuard | None = None,
    ) -> None:
        self.config = config or GoldRiskConfig()
        self.gold_guard = gold_guard or GoldOnlyGuard()

    def authorize(
        self,
        idea: TradeIdea,
        snapshot: MarketSnapshot,
        state: RiskState,
        *,
        live_armed: bool = False,
        now: datetime | None = None,
    ) -> GateDecision:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            return GateDecision(False, "gate clock must be timezone-aware")
        if not live_armed:
            return GateDecision(False, "live execution is not explicitly armed; shadow-only")
        if self.gold_guard.canonical_symbol(snapshot.symbol) != "XAUUSD":
            return GateDecision(False, "hard Gold-only invariant rejected snapshot symbol")
        if self.gold_guard.canonical_symbol(idea.symbol) != "XAUUSD":
            return GateDecision(False, "hard Gold-only invariant rejected idea symbol")
        if str(snapshot.venue).upper() not in {"MT5", "DERIV"}:
            return GateDecision(False, "venue is not explicitly selected")
        if str(snapshot.venue).upper() != str(idea.venue).upper():
            return GateDecision(False, "candidate venue does not match current market venue")
        if state.manually_halted:
            return GateDecision(False, "manual kill switch is active")
        if not snapshot.market_open or not snapshot.data_quality_ok:
            return GateDecision(False, "market closed or data quality invalid")
        quote_age = (now - snapshot.observed_at).total_seconds()
        if quote_age < -1.0 or quote_age > self.config.max_quote_age_seconds:
            return GateDecision(False, f"quote is stale or from the future ({quote_age:.2f}s)")
        if idea.expires_at <= now:
            return GateDecision(False, "candidate expired")
        if idea.atr <= 0 or snapshot.spread / idea.atr > self.config.max_spread_to_atr:
            return GateDecision(False, "spread exceeds ATR-based execution limit")
        if abs(snapshot.mid - idea.entry) / idea.atr > self.config.max_entry_drift_atr:
            return GateDecision(False, "price drifted too far from candidate entry")
        if idea.direction is Direction.LONG and not idea.stop < snapshot.mid < idea.target:
            return GateDecision(False, "long thesis is already invalidated or target reached")
        if idea.direction is Direction.SHORT and not idea.target < snapshot.mid < idea.stop:
            return GateDecision(False, "short thesis is already invalidated or target reached")
        if idea.reward_to_risk < self.config.min_reward_to_risk:
            return GateDecision(False, "reward/risk below minimum")
        if idea.risk_budget_pct > self.config.max_risk_per_trade_pct:
            return GateDecision(False, "per-trade risk budget exceeds policy")
        if state.open_risk_pct + idea.risk_budget_pct > self.config.max_total_open_risk_pct:
            return GateDecision(False, "aggregate open-risk cap would be exceeded")
        if state.daily_drawdown_pct >= self.config.max_daily_drawdown_pct:
            return GateDecision(False, "daily drawdown limit reached")
        if state.peak_drawdown_pct >= self.config.max_peak_drawdown_pct:
            return GateDecision(False, "peak-equity drawdown limit reached")
        if state.open_positions >= self.config.max_open_positions:
            return GateDecision(False, "open-position limit reached")
        return GateDecision(True, "preflight passed; existing portfolio/execution gates must still approve")
