"""APEX TRADER — Position Worker (Phase 4).

Pure evaluator: given a frozen ``PositionSnapshot``, a ``Tick``, and an
optional ``WorldModel``, runs ALL management checks and returns a list of
``Intent`` objects.  **Never makes broker calls.**  The Action Executor
(Phase 6) is responsible for sending intents to the broker.

The checks mirror the three management layers in ``main_loop.py``:

1. **Tick-level** — TradeManager's update() logic: SL hit, TP1 partial,
   breakeven, trailing, TP2/TP3 full close, structure exit, stall exit.

2. **Exit checks** — invalidation, conviction collapse, HTF candle close,
   dynamic SL tightening, absolute profit protection, news exit, session
   close, spread deterioration, opportunity cost.

3. **Strategic** — placeholder hooks for SituationEngine/DecisionEngine/
   Orchestrator results (populated by Phase 7+).

Thread-safe by construction: no mutable shared state, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from execution.intents import Intent, IntentType
from execution.position_snapshot import PositionSnapshot
from config import is_always_open, is_session_gated


@dataclass
class WorkerConfig:
    """Configuration for PositionWorker management checks.

    Mirrors the config values used across TradeManager, ExitChecksMixin,
    and RiskHeatMarginMixin.  Callers build this from ``AppConfig`` once
    per cycle.
    """

    # ── TradeManager params ───────────────────────────────────────────
    breakeven_buffer_pips: float = 2.0
    breakeven_min_profit_r: float = 0.5
    partial_close_ratio: float = 0.5
    tp3_ladder_enabled: bool = False
    tp3_close_ratio: float = 0.5
    tp_adjust_enabled: bool = False

    # ── Trailing ─────────────────────────────────────────────────────
    heat_trail_tighten_enabled: bool = False
    heat_trail_factor_defensive: float = 0.7
    heat_trail_factor_reducing: float = 0.5
    heat_trail_factor_emergency: float = 0.5

    # ── Stall exit ───────────────────────────────────────────────────
    stall_limits: dict = field(default_factory=lambda: {
        "M1": 30, "M5": 60, "M15": 90, "H1": 180, "H4": 360,
    })
    stall_default_limit: int = 75

    # ── Structure exit ───────────────────────────────────────────────
    strategic_structure_intact_threshold: float = 0.6
    strategic_structure_max_age_seconds: float = 600.0
    structure_exit_tf_alignment_enabled: bool = False
    structure_exit_tf_alignment_defer: float = 0.5

    # ── Invalidation check ───────────────────────────────────────────
    invalidation_score_threshold: int = 40
    opposing_signal_threshold: int = 75

    # ── Conviction collapse ──────────────────────────────────────────
    conviction_decline_cycles: int = 4
    conviction_decline_min_drop: int = 3
    conviction_profit_hold_pips: float = 20.0

    # ── Dynamic SL tightening ────────────────────────────────────────
    dynamic_sl_tightening_enabled: bool = True
    dynamic_sl_tighten_at_r: float = 2.0
    dynamic_sl_tighten_ratio: float = 0.5

    # ── Absolute profit protection ───────────────────────────────────
    absolute_profit_protection_enabled: bool = True
    absolute_profit_pips: float = 50.0
    absolute_profit_usd: float = 100.0

    # ── Session close ────────────────────────────────────────────────
    session_close_enabled: bool = True
    index_close_buffer_minutes: int = 30
    dead_zone_management: bool = True

    # ── Weekend protection ───────────────────────────────────────────
    # Pre-weekend de-risk for session-gated (FX) and index positions. Mirrors
    # AppConfig.risk.weekend_protection_*; synthetics (24/7) are never touched.
    weekend_protection_enabled: bool = True
    weekend_protection_mode: str = "derisk"   # "close" | "derisk" | "reduce"
    weekend_close_buffer_minutes: int = 15
    friday_close_hour_utc: int = 21
    weekend_reduce_fraction: float = 0.5

    # ── Spread deterioration ─────────────────────────────────────────
    spread_monitor_enabled: bool = True
    spread_deterioration_multiplier: float = 3.0

    # ── Opportunity cost ─────────────────────────────────────────────
    opportunity_cost_exit_mode: str = "off"
    opportunity_cost_min_hold_minutes: float = 60.0
    opportunity_cost_max_pnl_pips: float = 5.0
    opportunity_cost_score_margin: int = 15

    # ── Portfolio state ──────────────────────────────────────────────
    portfolio_heat_state: str = "NORMAL"
    max_open_trades: int = 5


@dataclass(frozen=True)
class ScanContext:
    """Optional scan data passed to workers for strategic checks.

    When scan data is unavailable (e.g. between candle closes), workers
    skip scan-dependent checks gracefully.
    """

    direction: str = ""
    score: int = 0
    opposing_score_boost: int = 0


@dataclass(frozen=True)
class MarketContext:
    """Optional per-symbol market context for advanced checks."""

    typical_spread: Optional[float] = None
    current_spread: Optional[float] = None
    h1_last_closed_open: Optional[float] = None
    h1_last_closed_close: Optional[float] = None
    h1_last_closed_high: Optional[float] = None
    h1_last_closed_low: Optional[float] = None
    h1_last_closed_time: Optional[datetime] = None
    last_seen_h1_close: Optional[datetime] = None
    blocked_candidate: Optional[dict] = None


class PositionWorker:
    """Evaluates a single position and emits management Intents.

    Stateless per call — all state comes from the frozen ``PositionSnapshot``
    and the ``WorkerConfig``.  Safe to run in a ``ThreadPoolExecutor``.

    Usage::

        worker = PositionWorker(config)
        intents = worker.evaluate(snapshot, now)
    """

    def __init__(self, config: Optional[WorkerConfig] = None) -> None:
        self.cfg = config or WorkerConfig()

    def evaluate(
        self,
        snap: PositionSnapshot,
        now: Optional[datetime] = None,
        scan: Optional[ScanContext] = None,
        market: Optional[MarketContext] = None,
    ) -> list[Intent]:
        """Run all management checks and return a list of Intents.

        Returns an empty list if no action is needed.  Multiple intents
        can be returned (e.g. MODIFY_SL + PARTIAL_CLOSE); the Intent
        Aggregator (Phase 5) resolves conflicts.
        """
        now = now or datetime.now(timezone.utc)
        intents: list[Intent] = []

        if snap.trade_status in ("CLOSED", "STOPPED", "TIME_EXIT"):
            return intents

        # ── Layer 1: Tick-level checks (TradeManager.update logic) ────
        self._check_stop_loss(snap, intents)
        self._check_tp1(snap, intents)
        self._check_breakeven(snap, intents)
        self._check_tp3(snap, intents)
        self._check_tp2(snap, intents)
        self._check_stall(snap, now, intents)

        # ── Layer 2: Exit checks (ExitChecksMixin logic) ─────────────
        self._check_absolute_profit_protection(snap, intents)
        self._check_dynamic_sl_tightening(snap, intents)
        self._check_weekend_protection(snap, now, intents)

        if scan is not None:
            self._check_invalidation(snap, scan, intents)
            self._check_conviction_collapse(snap, intents)

        if market is not None:
            self._check_htf_candle_close(snap, market, intents)
            self._check_spread_deterioration(snap, market, intents)
            self._check_session_close(snap, now, intents)
            self._check_opportunity_cost(snap, now, market, intents)

        return intents

    # ──────────────────────────────────────────────────────────────────
    # Layer 1: Tick-level checks (mirrors TradeManager.update)
    # ──────────────────────────────────────────────────────────────────

    def _check_stop_loss(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if snap.sl <= 0:
            return
        if snap.is_long:
            hit = snap.current_price <= snap.sl
        else:
            hit = snap.current_price >= snap.sl
        if hit:
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="stop_loss",
                reason=f"SL hit @ {snap.current_price:.5f} (SL={snap.sl:.5f})",
            ))

    def _check_tp1(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if snap.partial_closed or snap.tp1 <= 0:
            return
        if snap.is_long:
            hit = snap.current_price >= snap.tp1
        else:
            hit = snap.current_price <= snap.tp1
        if hit:
            if snap.platform == "deriv":
                out.append(Intent.close(
                    symbol=snap.symbol,
                    ticket=snap.order_id,
                    source="tp1_deriv",
                    reason=f"TP1 hit (deriv stake close+reopen) @ {snap.tp1:.5f}",
                ))
            else:
                ratio = snap.plan_partial_ratio or self.cfg.partial_close_ratio
                out.append(Intent.partial_close(
                    symbol=snap.symbol,
                    ticket=snap.order_id,
                    fraction=ratio,
                    source="tp1_partial",
                    reason=f"TP1 hit @ {snap.tp1:.5f}, partial {ratio:.0%}",
                ))

    def _check_breakeven(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if not snap.partial_closed or snap.at_breakeven:
            return
        trigger_r = snap.plan_be_trigger_r or self.cfg.breakeven_min_profit_r
        if snap.pnl_r < trigger_r:
            return
        if snap.pnl_pips <= 0:
            return
        direction = "LONG" if snap.is_long else "SHORT"
        be_level = self._breakeven_level(
            snap.entry_price, direction, self.cfg.breakeven_buffer_pips, snap.pip_size,
        )
        if snap.is_long and be_level <= snap.sl:
            return
        if not snap.is_long and be_level >= snap.sl:
            return
        out.append(Intent.modify_sl(
            symbol=snap.symbol,
            ticket=snap.order_id,
            new_sl=be_level,
            source="breakeven",
            reason=f"BE activation @ {be_level:.5f} ({snap.pnl_r:.1f}R)",
        ))

    def _check_tp3(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if not self.cfg.tp3_ladder_enabled:
            return
        if snap.tp3 is None or snap.tp3 <= 0 or snap.tp3_hit or not snap.partial_closed:
            return
        if snap.is_long:
            hit = snap.current_price >= snap.tp3
        else:
            hit = snap.current_price <= snap.tp3
        if hit:
            out.append(Intent.partial_close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                fraction=self.cfg.tp3_close_ratio,
                source="tp3_ladder",
                reason=f"TP3 hit @ {snap.tp3:.5f}",
            ))

    def _check_tp2(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if snap.tp2 is None or snap.tp2 <= 0:
            return
        if snap.is_long:
            hit = snap.current_price >= snap.tp2
        else:
            hit = snap.current_price <= snap.tp2
        if hit:
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="tp2_target",
                reason=f"TP2 hit @ {snap.tp2:.5f}",
            ))

    def _check_stall(
        self, snap: PositionSnapshot, now: datetime, out: list[Intent],
    ) -> None:
        if snap.partial_closed:
            return
        stall_limit = self.cfg.stall_limits.get(
            snap.entry_timeframe, self.cfg.stall_default_limit,
        )
        stall_minutes = (now - snap.open_time).total_seconds() / 60
        risk_pips = snap.risk_pips
        flat_threshold = 0.15 * risk_pips if risk_pips > 0 else 5.0
        if stall_minutes > stall_limit and abs(snap.pnl_pips) < flat_threshold:
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="stall_exit",
                reason=(
                    f"Stall exit — {stall_minutes:.0f}min (limit {stall_limit}), "
                    f"{snap.pnl_pips:.1f}pip"
                ),
            ))

    # ──────────────────────────────────────────────────────────────────
    # Layer 2: Exit checks (mirrors ExitChecksMixin)
    # ──────────────────────────────────────────────────────────────────

    def _check_absolute_profit_protection(
        self, snap: PositionSnapshot, out: list[Intent],
    ) -> None:
        if not self.cfg.absolute_profit_protection_enabled:
            return
        if snap.at_breakeven:
            return
        if snap.pnl_pips < self.cfg.absolute_profit_pips:
            return
        profit_usd = snap.pnl_pips * snap.pip_value_per_lot * snap.lots
        if profit_usd < self.cfg.absolute_profit_usd:
            return
        be_level = self._breakeven_level(
            snap.entry_price,
            "LONG" if snap.is_long else "SHORT",
            self.cfg.breakeven_buffer_pips,
            snap.pip_size,
        )
        is_improvement = (
            (snap.is_long and be_level > snap.sl)
            or (not snap.is_long and be_level < snap.sl)
        )
        if not is_improvement:
            return
        out.append(Intent.modify_sl(
            symbol=snap.symbol,
            ticket=snap.order_id,
            new_sl=be_level,
            source="absolute_profit_protection",
            reason=(
                f"Profit lock — +{snap.pnl_pips:.1f}pip / "
                f"${profit_usd:.2f} | SL→BE {be_level:.5f}"
            ),
        ))

    def _check_dynamic_sl_tightening(
        self, snap: PositionSnapshot, out: list[Intent],
    ) -> None:
        if not self.cfg.dynamic_sl_tightening_enabled:
            return
        if not snap.at_breakeven:
            return
        original_risk = abs(snap.entry_price - snap.sl_original)
        if original_risk < 1e-8:
            return
        if snap.is_long:
            profit_r = (snap.current_price - snap.entry_price) / original_risk
        else:
            profit_r = (snap.entry_price - snap.current_price) / original_risk
        if profit_r < self.cfg.dynamic_sl_tighten_at_r:
            return
        tighten_distance = original_risk * self.cfg.dynamic_sl_tighten_ratio
        if snap.is_long:
            new_sl = snap.current_price - tighten_distance
            if new_sl <= snap.sl:
                return
        else:
            new_sl = snap.current_price + tighten_distance
            if new_sl >= snap.sl:
                return
        new_sl = round(new_sl, 5)
        out.append(Intent.modify_sl(
            symbol=snap.symbol,
            ticket=snap.order_id,
            new_sl=new_sl,
            source="dynamic_sl_tighten",
            reason=f"Dynamic SL tighten — {snap.sl:.5f} → {new_sl:.5f} ({profit_r:.1f}R)",
        ))

    def _check_invalidation(
        self, snap: PositionSnapshot, scan: ScanContext, out: list[Intent],
    ) -> None:
        if scan.score < self.cfg.invalidation_score_threshold:
            if snap.pnl_pips <= 0:
                out.append(Intent.close(
                    symbol=snap.symbol,
                    ticket=snap.order_id,
                    source="invalidation_low_score",
                    reason=f"Low score ({scan.score}) with negative P&L",
                ))
                return

        opposing = (
            (snap.is_long and scan.direction == "SHORT")
            or (not snap.is_long and scan.direction == "LONG")
        )
        effective_score = min(100, int(scan.score + max(0, scan.opposing_score_boost)))
        if opposing and effective_score >= self.cfg.opposing_signal_threshold:
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="invalidation_opposing",
                reason=(
                    f"Opposing signal ({scan.direction}@{effective_score}) "
                    f"vs {snap.direction}"
                ),
            ))

    def _check_conviction_collapse(
        self, snap: PositionSnapshot, out: list[Intent],
    ) -> None:
        n = self.cfg.conviction_decline_cycles
        scores = snap.score_history
        if len(scores) < n:
            return
        recent = scores[-n:]
        is_declining = all(
            recent[i] - recent[i + 1] >= self.cfg.conviction_decline_min_drop
            for i in range(len(recent) - 1)
        )
        if not is_declining:
            return
        if snap.pnl_pips > self.cfg.conviction_profit_hold_pips:
            return
        out.append(Intent.close(
            symbol=snap.symbol,
            ticket=snap.order_id,
            source="conviction_collapse",
            reason=f"Conviction collapse — scores {recent[0]}→{recent[-1]}",
        ))

    def _check_htf_candle_close(
        self,
        snap: PositionSnapshot,
        market: MarketContext,
        out: list[Intent],
    ) -> None:
        if market.h1_last_closed_open is None or market.h1_last_closed_close is None:
            return
        if market.h1_last_closed_time is None:
            return
        if (
            market.last_seen_h1_close is not None
            and market.h1_last_closed_time <= market.last_seen_h1_close
        ):
            return
        candle_open = market.h1_last_closed_open
        candle_close = market.h1_last_closed_close
        if candle_open == 0 or candle_close == 0:
            return
        candle_body = abs(candle_close - candle_open)
        candle_range = (
            (market.h1_last_closed_high or candle_close)
            - (market.h1_last_closed_low or candle_open)
        )
        if candle_range > 0 and (candle_body / candle_range) < 0.3:
            return
        candle_bearish = candle_close < candle_open
        candle_bullish = candle_close > candle_open
        opposing_close = (
            (snap.is_long and candle_bearish)
            or (not snap.is_long and candle_bullish)
        )
        if not opposing_close:
            return
        if snap.pnl_pips > 30:
            return
        direction_str = "BEARISH" if candle_bearish else "BULLISH"
        out.append(Intent.close(
            symbol=snap.symbol,
            ticket=snap.order_id,
            source="htf_candle_close",
            reason=f"H1 {direction_str} candle close against {snap.direction}",
        ))

    def _check_spread_deterioration(
        self,
        snap: PositionSnapshot,
        market: MarketContext,
        out: list[Intent],
    ) -> None:
        if not self.cfg.spread_monitor_enabled:
            return
        if market.typical_spread is None or market.typical_spread < 1e-8:
            return
        if market.current_spread is None:
            return
        spread_ratio = market.current_spread / market.typical_spread
        if spread_ratio < self.cfg.spread_deterioration_multiplier:
            return
        if snap.at_breakeven:
            return
        be_level = self._breakeven_level(
            snap.entry_price,
            "LONG" if snap.is_long else "SHORT",
            self.cfg.breakeven_buffer_pips,
            snap.pip_size,
        )
        is_improvement = (
            (snap.is_long and be_level > snap.sl)
            or (not snap.is_long and be_level < snap.sl)
        )
        if not is_improvement:
            return
        out.append(Intent.modify_sl(
            symbol=snap.symbol,
            ticket=snap.order_id,
            new_sl=be_level,
            source="spread_deterioration",
            reason=f"Spread {spread_ratio:.1f}× normal — SL→BE {be_level:.5f}",
        ))

    def _check_session_close(
        self,
        snap: PositionSnapshot,
        now: datetime,
        out: list[Intent],
    ) -> None:
        if not self.cfg.session_close_enabled:
            return
        utc_hour = now.hour
        utc_minute = now.minute
        index_close_windows = {
            "HK50": (8, 0),
            "JP225": (6, 30),
            "AUS200": (6, 0),
            "GER40": (20, 0),
            "FRA40": (20, 0),
            "UK100": (16, 30),
            "US30": (21, 0),
            "US100": (21, 0),
            "US500": (21, 0),
        }
        close_time = index_close_windows.get(snap.symbol)
        if close_time:
            close_h, close_m = close_time
            close_total = close_h * 60 + close_m
            now_total = utc_hour * 60 + utc_minute
            diff = close_total - now_total
            if 0 < diff <= self.cfg.index_close_buffer_minutes:
                out.append(Intent.close(
                    symbol=snap.symbol,
                    ticket=snap.order_id,
                    source="session_close",
                    reason=f"Exchange closes in {diff}min",
                ))
                return

        if self.cfg.dead_zone_management:
            if "USD" in snap.symbol or "JPY" in snap.symbol:
                in_dead_zone = utc_hour == 0 or (utc_hour == 1 and utc_minute <= 59)
                if in_dead_zone and not snap.at_breakeven:
                    be_level = self._breakeven_level(
                        snap.entry_price,
                        "LONG" if snap.is_long else "SHORT",
                        self.cfg.breakeven_buffer_pips,
                        snap.pip_size,
                    )
                    out.append(Intent.modify_sl(
                        symbol=snap.symbol,
                        ticket=snap.order_id,
                        new_sl=be_level,
                        source="dead_zone_protection",
                        reason=f"Dead zone — SL→BE {be_level:.5f}",
                    ))

    def _check_weekend_protection(
        self,
        snap: PositionSnapshot,
        now: datetime,
        out: list[Intent],
    ) -> None:
        """Pre-weekend protection for session-gated (FX) and index positions.

        Within ``weekend_close_buffer_minutes`` of the Friday FX close, apply
        the configured action so positions are not carried over the weekend
        gap unmanaged.  24/7 synthetics are never touched.

        Modes:
            "close"  — flatten the position
            "derisk" — move the SL to breakeven (if not already there)
            "reduce" — partially close to cut weekend exposure
        """
        if not self.cfg.weekend_protection_enabled:
            return
        # Never touch 24/7 synthetics — they trade through the weekend.
        if is_always_open(snap.symbol):
            return
        if now.weekday() != 4:  # Friday only
            return
        close_total = self.cfg.friday_close_hour_utc * 60
        now_total = now.hour * 60 + now.minute
        diff = close_total - now_total
        if not (0 < diff <= self.cfg.weekend_close_buffer_minutes):
            return

        mode = self.cfg.weekend_protection_mode
        if mode == "close":
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="weekend_protection",
                reason=f"Weekend protection — FX closes in {diff}min (close)",
            ))
        elif mode == "reduce":
            if not snap.partial_closed:
                out.append(Intent.partial_close(
                    symbol=snap.symbol,
                    ticket=snap.order_id,
                    fraction=self.cfg.weekend_reduce_fraction,
                    source="weekend_protection",
                    reason=f"Weekend protection — FX closes in {diff}min (reduce)",
                ))
        else:  # "derisk" — move SL to breakeven
            if not snap.at_breakeven and snap.pip_size > 0:
                be_level = self._breakeven_level(
                    snap.entry_price,
                    "LONG" if snap.is_long else "SHORT",
                    self.cfg.breakeven_buffer_pips,
                    snap.pip_size,
                )
                out.append(Intent.modify_sl(
                    symbol=snap.symbol,
                    ticket=snap.order_id,
                    new_sl=be_level,
                    source="weekend_protection",
                    reason=f"Weekend protection — FX closes in {diff}min (SL→BE {be_level:.5f})",
                ))

    def _check_opportunity_cost(
        self,
        snap: PositionSnapshot,
        now: datetime,
        market: MarketContext,
        out: list[Intent],
    ) -> None:
        if self.cfg.opportunity_cost_exit_mode == "off":
            return
        if market.blocked_candidate is None:
            return
        hold_minutes = (now - snap.open_time).total_seconds() / 60
        if hold_minutes < self.cfg.opportunity_cost_min_hold_minutes:
            return
        if snap.pnl_pips > self.cfg.opportunity_cost_max_pnl_pips:
            return
        if snap.partial_closed:
            return
        blocked = market.blocked_candidate
        score_delta = blocked.get("score", 0) - snap.score
        if score_delta < self.cfg.opportunity_cost_score_margin:
            return
        if self.cfg.opportunity_cost_exit_mode == "active":
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="opportunity_cost",
                reason=(
                    f"Opportunity cost — blocked {blocked.get('pair', '?')} "
                    f"(score={blocked.get('score', 0)}, delta=+{score_delta})"
                ),
            ))

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _breakeven_level(
        entry_price: float,
        direction: str,
        buffer_pips: float,
        pip_size: float,
    ) -> float:
        if direction == "LONG":
            return round(entry_price + buffer_pips * pip_size, 5)
        return round(entry_price - buffer_pips * pip_size, 5)
