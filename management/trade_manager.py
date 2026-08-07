"""
APEX TRADER — Trade Manager
The hands that protect every winner and cut every loser.
Once a trade is open, this module owns it completely.

I got in clean. Now I protect this winner.
50% off at TP1, stop to breakeven, trail the rest by structure.
If price stalls — I'm out. If the setup forms again — I'm back in.
No ego. Pure management.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import pandas as pd
from loguru import logger

from brain.structure_engine import StructureAnalysis, StructureEngine, StructureEvent
from config import get_pip_size, get_instrument
from management.trailing_stop import StructureTrailingStop
from management.partial_close import PartialCloseCalculator


class TradeStatus(Enum):
    OPEN = "OPEN"
    TP1_HIT = "TP1_HIT"
    BREAKEVEN = "BREAKEVEN"
    TRAILING = "TRAILING"
    CLOSED = "CLOSED"
    STOPPED = "STOPPED"
    TIME_EXIT = "TIME_EXIT"


TERMINAL_STATUSES = {TradeStatus.CLOSED, TradeStatus.STOPPED, TradeStatus.TIME_EXIT}


@dataclass
class EntrySignal:
    """Represents the output of the trigger engine (Phase 3)."""
    pair: str
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    risk_reward_1: float
    risk_reward_2: float
    position_size_lots: float
    score: int
    confluences: list[str] = field(default_factory=list)
    entry_zone: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    entry_timeframe: str = "M5"
    # Execution platform ("mt5" | "deriv"). Deriv uses all-or-nothing stake
    # contracts, so lot-based partial math must be skipped for it. Defaults to
    # "mt5" for backward compatibility with existing call sites.
    platform: str = "mt5"
    # Trade Planner per-trade management overrides — primitive values only so
    # the management package stays free of any planning import. ``None`` means
    # "use the TradeManager's global default" (backward compatible).
    plan_be_trigger_r: Optional[float] = None
    plan_trail_activation_r: Optional[float] = None
    plan_trail_strategy: Optional[str] = None
    plan_partial_ratio: Optional[float] = None
    # Layered-decision quality at entry (OQ/EQ). Carried so management can
    # measure how far conditions have decayed since entry (P3). ``None`` when
    # the trade was not opened through the layered-decision path.
    entry_oq: Optional[float] = None
    entry_eq: Optional[float] = None


@dataclass
class ManagedTrade:
    trade_id: str
    pair: str
    direction: str
    entry_price: float
    current_price: float
    stop_loss: float
    original_stop_loss: float
    tp1: float
    tp2: float
    original_tp2: float
    position_size_lots: float
    remaining_size_lots: float
    status: TradeStatus
    pnl_pips: float
    pnl_dollars: float
    entry_time: datetime
    tp1_hit_time: Optional[datetime]
    close_time: Optional[datetime]
    close_reason: Optional[str]
    candles_since_entry: int
    highest_price_since_entry: float
    lowest_price_since_entry: float
    trailing_stop: Optional[float]
    breakeven_active: bool
    partial_closed: bool
    re_entry_eligible: bool
    score: int
    pip_size: float
    pip_value_per_lot: float
    confluences: list[str] = field(default_factory=list)
    entry_zone: str = ""
    entry_timeframe: str = "M5"
    tp3: Optional[float] = None
    original_tp3: Optional[float] = None
    tp3_hit: bool = False
    # Execution platform ("mt5" | "deriv"). Used to guard against running
    # lot-based partial math on Deriv stake contracts.
    platform: str = "mt5"
    # Trade Planner per-trade management overrides — ``None`` means the trade
    # was not opened by the planner (or the field was unset) and the manager's
    # global config value is used instead.
    plan_be_trigger_r: Optional[float] = None
    plan_trail_activation_r: Optional[float] = None
    plan_trail_strategy: Optional[str] = None
    plan_partial_ratio: Optional[float] = None
    # Layered-decision quality at entry (OQ/EQ) — carried so management can
    # measure decay since entry (P3). ``None`` = not opened via layered path.
    entry_oq: Optional[float] = None
    entry_eq: Optional[float] = None
    # Latest strategic (D1/H4/H1/M1) structure read recorded by the decision
    # engine (P4). The mechanical M5 structure-exit consults these so the two
    # management brains stay coherent. ``None`` = the strategic brain has not
    # spoken for this trade yet.
    strategic_structure_integrity: Optional[float] = None
    strategic_tf_alignment: Optional[float] = None
    strategic_assessment_time: Optional[datetime] = None


class TradeManager:
    """
    Manages every open trade from entry to exit.
    Called on every new candle/tick to check stages:
    1. Stop Loss Hit?
    2. TP1 → Partial Close
    3. Move to Breakeven
    4. Structure Trailing Stop
    5. TP2 → Full Close
    6. Time-Based Exit (stall detection)
    """

    def __init__(
        self,
        max_stall_candles: int = 15,
        partial_close_ratio: float = 0.5,
        breakeven_buffer_pips: float = 2.0,
        default_pip_value: float = 10.0,
        tp_adjust_enabled: bool = False,
        tp3_ladder_enabled: bool = False,
        tp3_r_multiple: float = 4.0,
        tp3_close_ratio: float = 0.5,
        breakeven_min_profit_r: float = 0.5,
        trailing_swing_lookback: int = 12,
        strategic_structure_intact_threshold: float = 0.6,
        strategic_structure_max_age_seconds: float = 600.0,
        structure_exit_tf_alignment_enabled: bool = False,
        structure_exit_tf_alignment_defer: float = 0.5,
        heat_trail_tighten_enabled: bool = False,
        heat_trail_factor_defensive: float = 0.7,
        heat_trail_factor_reducing: float = 0.5,
        heat_trail_factor_emergency: float = 0.5,
    ):
        self.max_stall_candles = max_stall_candles
        self.partial_close_ratio = partial_close_ratio
        self.breakeven_buffer_pips = breakeven_buffer_pips
        self.default_pip_value = default_pip_value
        self.tp_adjust_enabled = tp_adjust_enabled
        self.tp3_ladder_enabled = tp3_ladder_enabled
        self.tp3_r_multiple = tp3_r_multiple
        self.tp3_close_ratio = tp3_close_ratio
        # P12: require this much profit (in R) before BE activates, so a normal
        # post-TP1 retest doesn't immediately stop the runner at breakeven.
        self.breakeven_min_profit_r = breakeven_min_profit_r
        # P4: the M5 structure-exit defers to the strategic brain when its
        # (richer, multi-timeframe) structure read is fresh and intact, and
        # fires with higher confidence when the strategic read says broken.
        self.strategic_structure_intact_threshold = strategic_structure_intact_threshold
        self.strategic_structure_max_age_seconds = max(0.0, strategic_structure_max_age_seconds)
        # #32: the M5 structure-exit fires a full close on the first counter-
        # direction CHoCH/BOS and never consults the strategic tf_alignment it
        # already carries. When enabled, a fresh strategic tf_alignment that
        # still strongly supports the trade direction DEFERS that mechanical
        # exit (the higher-timeframe trend says the break is noise, not a
        # reversal). Off by default → legacy binary structure exit.
        self.structure_exit_tf_alignment_enabled = bool(structure_exit_tf_alignment_enabled)
        self.structure_exit_tf_alignment_defer = max(0.0, min(1.0, structure_exit_tf_alignment_defer))
        # P8: heat-aware trail tightening. When the portfolio-heat state machine
        # is in DEFENSIVE/REDUCING/EMERGENCY, multiply the structure-trail buffer
        # by the matching factor so a post-BE runner locks gains faster in a
        # stressed book. Never widens (factors are clamped to (0, 1]) and only
        # tightens in the profit direction (calculate_trail enforces this).
        self.heat_trail_tighten_enabled = heat_trail_tighten_enabled
        self.heat_trail_factor_defensive = heat_trail_factor_defensive
        self.heat_trail_factor_reducing = heat_trail_factor_reducing
        self.heat_trail_factor_emergency = heat_trail_factor_emergency
        self.trailing = StructureTrailingStop(swing_lookback=trailing_swing_lookback)
        self.partial_calc = PartialCloseCalculator()
        self._trades: dict[str, ManagedTrade] = {}
        # Persistent M5 structure analyzer + per-frame memo. _check_structure_exit
        # and _adjust_tp2 both run StructureEngine(swing_lookback=3).analyze() on
        # the same M5 frame; reusing one engine and caching the analysis by frame
        # identity removes the repeated construction + recomputation per update.
        self._m5_structure_engine = StructureEngine(swing_lookback=3)
        self._m5_analysis_cache: Optional[tuple] = None

    def _analyze_m5_structure(self, df_m5: pd.DataFrame) -> StructureAnalysis:
        """Analyze an M5 frame once, memoized by frame identity within a cycle."""
        cached = self._m5_analysis_cache
        if cached is not None and cached[0] is df_m5:
            return cached[1]
        analysis = self._m5_structure_engine.analyze(df_m5)
        self._m5_analysis_cache = (df_m5, analysis)
        return analysis

    @staticmethod
    def _is_long(direction: str) -> bool:
        """Treat BUY/LONG as long, SELL/SHORT as short. Fail loud on unknown."""
        normalized = direction.strip().upper()
        if normalized in ("LONG", "BUY"):
            return True
        if normalized in ("SHORT", "SELL"):
            return False
        logger.error("Unknown trade direction '{}' — cannot classify", direction)
        raise ValueError(f"Unknown trade direction: {direction!r}")

    # ------------------------------------------------------------------
    # Plan-aware parameter resolution
    #
    # When a trade was opened by the Trade Planner it carries per-trade
    # management overrides. These resolvers return the plan value when present
    # and finite, otherwise the manager's global config default — so positions
    # without a plan behave exactly as before.
    # ------------------------------------------------------------------

    def _eff_partial_ratio(self, trade: ManagedTrade) -> float:
        val = trade.plan_partial_ratio
        if val is not None and 0.0 < val <= 1.0:
            return val
        return self.partial_close_ratio

    def _eff_be_trigger_r(self, trade: ManagedTrade) -> float:
        val = trade.plan_be_trigger_r
        if val is not None and val >= 0.0:
            return val
        return self.breakeven_min_profit_r

    def _should_trail(self, trade: ManagedTrade, pnl_r: float) -> bool:
        """Whether trailing should run this tick given the trade's plan."""
        if trade.plan_trail_strategy == "none":
            return False
        activation = trade.plan_trail_activation_r
        if activation is not None and pnl_r < activation:
            return False
        return True

    def _heat_trail_factor(self, portfolio_heat_state: Optional[str]) -> float:
        """Trail-buffer multiplier for the current portfolio-heat state.

        Returns 1.0 (normal width) when tightening is disabled, no state is
        supplied, or the state is NORMAL/unknown. Fail-safe: any unexpected
        value falls back to the normal width rather than a tighter/looser stop.
        """
        if not self.heat_trail_tighten_enabled or not portfolio_heat_state:
            return 1.0
        try:
            state = str(portfolio_heat_state).strip().upper()
            if state == "DEFENSIVE":
                return self.heat_trail_factor_defensive
            if state == "REDUCING":
                return self.heat_trail_factor_reducing
            if state == "EMERGENCY":
                return self.heat_trail_factor_emergency
        except Exception:
            return 1.0
        return 1.0

    # ------------------------------------------------------------------
    # Open
    # ------------------------------------------------------------------

    def open_trade(self, signal: EntrySignal) -> ManagedTrade:
        trade_id = uuid.uuid4().hex[:12]
        pip_size = get_pip_size(signal.pair)
        # Resolve pip_value from instrument registry — each instrument declares its own
        try:
            pip_value_per_lot = get_instrument(signal.pair).pip_value_per_lot
        except KeyError:
            pip_value_per_lot = self.default_pip_value

        trade = ManagedTrade(
            trade_id=trade_id,
            pair=signal.pair,
            direction=signal.direction,
            entry_price=signal.entry_price,
            current_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            original_stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            original_tp2=signal.tp2,
            position_size_lots=signal.position_size_lots,
            remaining_size_lots=signal.position_size_lots,
            status=TradeStatus.OPEN,
            pnl_pips=0.0,
            pnl_dollars=0.0,
            entry_time=signal.timestamp,
            tp1_hit_time=None,
            close_time=None,
            close_reason=None,
            candles_since_entry=0,
            highest_price_since_entry=signal.entry_price,
            lowest_price_since_entry=signal.entry_price,
            trailing_stop=None,
            breakeven_active=False,
            partial_closed=False,
            re_entry_eligible=False,
            score=signal.score,
            pip_size=pip_size,
            pip_value_per_lot=pip_value_per_lot,
            confluences=list(signal.confluences),
            entry_zone=signal.entry_zone,
            entry_timeframe=getattr(signal, "entry_timeframe", "M5"),
            platform=getattr(signal, "platform", "mt5"),
        )

        if self.tp3_ladder_enabled:
            risk_distance = abs(signal.entry_price - signal.stop_loss)
            is_long = self._is_long(signal.direction)
            candidate = (
                signal.entry_price + self.tp3_r_multiple * risk_distance
                if is_long
                else signal.entry_price - self.tp3_r_multiple * risk_distance
            )
            beyond_tp2 = (
                (candidate > signal.tp2) if is_long else (candidate < signal.tp2)
            )
            if beyond_tp2:
                trade.tp3 = candidate
                trade.original_tp3 = candidate

        # Carry any Trade Planner per-trade management overrides onto the trade.
        # Positions without a plan leave these as None and use global config.
        trade.plan_be_trigger_r = getattr(signal, "plan_be_trigger_r", None)
        trade.plan_trail_activation_r = getattr(signal, "plan_trail_activation_r", None)
        trade.plan_trail_strategy = getattr(signal, "plan_trail_strategy", None)
        trade.plan_partial_ratio = getattr(signal, "plan_partial_ratio", None)
        # Carry the entry-time layered-decision quality (OQ/EQ) so management
        # can measure decay since entry (P3).
        trade.entry_oq = getattr(signal, "entry_oq", None)
        trade.entry_eq = getattr(signal, "entry_eq", None)
        if any(
            v is not None
            for v in (
                trade.plan_be_trigger_r,
                trade.plan_trail_activation_r,
                trade.plan_trail_strategy,
                trade.plan_partial_ratio,
            )
        ):
            logger.debug(
                "[TradeManager] plan-aware management for {} — "
                "BE@{}R trail={}@{}R partial={}",
                signal.pair,
                trade.plan_be_trigger_r,
                trade.plan_trail_strategy,
                trade.plan_trail_activation_r,
                trade.plan_partial_ratio,
            )

        self._trades[trade_id] = trade
        logger.info(
            f"TRADE OPENED: {signal.pair} {signal.direction} @ {signal.entry_price}, "
            f"SL: {signal.stop_loss}, TP1: {signal.tp1}, TP2: {signal.tp2}, "
            f"Size: {signal.position_size_lots}"
        )
        return trade

    # ------------------------------------------------------------------
    # Main update loop — called on every new candle / tick
    # ------------------------------------------------------------------

    def update(
        self,
        trade: ManagedTrade,
        current_price: float,
        current_df_m5: Optional[pd.DataFrame] = None,
        bar_time: Optional[datetime] = None,
        portfolio_heat_state: Optional[str] = None,
    ) -> ManagedTrade:
        if trade.status in TERMINAL_STATUSES:
            return trade

        trade.current_price = current_price
        trade.candles_since_entry += 1

        self._update_pnl(trade)
        self._update_extremes(trade)

        if self._check_stop_loss(trade, bar_time=bar_time):
            return trade
        if self._check_tp1(trade, bar_time=bar_time):
            pass
        # P12: don't snap to breakeven on the first profitable tick after TP1 —
        # require a minimum profit in R so the runner survives a normal retest.
        # A Trade Plan may override the global R threshold per trade.
        risk_pips = abs(trade.entry_price - trade.original_stop_loss) / trade.pip_size
        pnl_r = (trade.pnl_pips / risk_pips) if risk_pips > 1e-9 else 0.0
        if trade.partial_closed and not trade.breakeven_active:
            if pnl_r >= self._eff_be_trigger_r(trade):
                self._activate_breakeven(trade)
        if (
            trade.breakeven_active
            and current_df_m5 is not None
            and self._should_trail(trade, pnl_r)
        ):
            self._update_trailing(
                trade, current_df_m5,
                trail_factor=self._heat_trail_factor(portfolio_heat_state),
            )
        if self.tp_adjust_enabled and trade.partial_closed and current_df_m5 is not None:
            self._adjust_tp2(trade, current_df_m5)
        if self._check_tp3(trade):
            pass
        if self._check_tp2(trade, bar_time=bar_time):
            return trade
        if current_df_m5 is not None and self._check_structure_exit(trade, current_df_m5, bar_time=bar_time):
            return trade
        if self._check_stall(trade, bar_time=bar_time):
            return trade

        return trade

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def close_trade(
        self,
        trade: ManagedTrade,
        reason: str,
        close_price: float,
        status: TradeStatus = TradeStatus.CLOSED,
        bar_time: Optional[datetime] = None,
    ) -> ManagedTrade:
        trade.current_price = close_price
        trade.status = status
        trade.close_time = bar_time or datetime.now(timezone.utc)
        trade.close_reason = reason
        self._update_pnl(trade)
        # This only marks the IN-MEMORY trade closed — it is the manager's exit
        # DECISION, not a broker confirmation. The trading loop decides whether
        # to send it to the broker, and may DEFER a discretionary (stall/
        # structure) exit when the strategic engine says HOLD. The authoritative,
        # broker-CONFIRMED "TRADE CLOSED" is logged by the loop's
        # _record_closed_trade. Keep this at DEBUG so an internal or deferred
        # exit never looks like a real broker close.
        logger.debug(
            f"[manager exit decision] {trade.pair} {trade.direction} — {reason} "
            f"— {trade.pnl_pips:+.1f} pips ({trade.pnl_dollars:+.2f}$)"
        )
        return trade

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_open_trades(self) -> list[ManagedTrade]:
        return [t for t in self._trades.values() if t.status not in TERMINAL_STATUSES]

    def get_all_trades(self) -> list[ManagedTrade]:
        return list(self._trades.values())

    def get_trade(self, trade_id: str) -> Optional[ManagedTrade]:
        return self._trades.get(trade_id)

    def get_trade_summary(self, trade: ManagedTrade) -> dict:
        return {
            "trade_id": trade.trade_id,
            "pair": trade.pair,
            "direction": trade.direction,
            "entry_price": trade.entry_price,
            "current_price": trade.current_price,
            "stop_loss": trade.stop_loss,
            "tp1": trade.tp1,
            "tp2": trade.tp2,
            "status": trade.status.value,
            "pnl_pips": round(trade.pnl_pips, 1),
            "pnl_dollars": round(trade.pnl_dollars, 2),
            "remaining_lots": trade.remaining_size_lots,
            "breakeven_active": trade.breakeven_active,
            "partial_closed": trade.partial_closed,
            "candles_in_trade": trade.candles_since_entry,
            "score": trade.score,
        }

    # ------------------------------------------------------------------
    # Internal stages
    # ------------------------------------------------------------------

    def _update_pnl(self, trade: ManagedTrade) -> None:
        if trade.pip_size <= 0:
            logger.warning(
                "[TradeManager] {} pip_size {} ≤ 0 — skipping P&L update (cannot compute)",
                trade.pair, trade.pip_size,
            )
            return
        if self._is_long(trade.direction):
            raw_pips = (trade.current_price - trade.entry_price) / trade.pip_size
        else:
            raw_pips = (trade.entry_price - trade.current_price) / trade.pip_size
        trade.pnl_pips = round(raw_pips, 1)
        trade.pnl_dollars = round(
            raw_pips * trade.pip_value_per_lot * trade.remaining_size_lots, 2
        )

    def _update_extremes(self, trade: ManagedTrade) -> None:
        if trade.current_price > trade.highest_price_since_entry:
            trade.highest_price_since_entry = trade.current_price
        if trade.current_price < trade.lowest_price_since_entry:
            trade.lowest_price_since_entry = trade.current_price

    def _check_stop_loss(self, trade: ManagedTrade, bar_time: Optional[datetime] = None) -> bool:
        is_long = self._is_long(trade.direction)
        hit = (
            (is_long and trade.current_price <= trade.stop_loss)
            or (not is_long and trade.current_price >= trade.stop_loss)
        )
        if hit:
            reason = "Stop loss hit"
            if trade.breakeven_active:
                reason = "Stopped at breakeven"
                trade.re_entry_eligible = True
            self.close_trade(trade, reason, trade.current_price, TradeStatus.STOPPED, bar_time=bar_time)
        return hit

    def _check_tp1(self, trade: ManagedTrade, bar_time: Optional[datetime] = None) -> bool:
        if trade.partial_closed:
            return False
        if trade.tp1 is None or trade.tp1 <= 0:
            return False
        is_long = self._is_long(trade.direction)
        hit = (
            (is_long and trade.current_price >= trade.tp1)
            or (not is_long and trade.current_price <= trade.tp1)
        )
        if hit:
            # Hardening: Deriv stake contracts are all-or-nothing — there is no
            # lot-based partial. The main loop intercepts TP1 (via the
            # partial_closed flag) and handles the stake reduction with a
            # close+reopen. Flag the hit so that intercept fires, but skip the
            # lot-based partial math which is meaningless for stake positions.
            if getattr(trade, "platform", "mt5") == "deriv":
                trade.partial_closed = True
                trade.tp1_hit_time = bar_time or datetime.now(timezone.utc)
                trade.status = TradeStatus.TP1_HIT
                _log = logger.debug if str(trade.trade_id).startswith("shadow_") else logger.info
                _log(
                    f"TP1 HIT: {trade.pair} (deriv stake) — flagged for "
                    f"close+reopen, +{trade.pnl_pips:.1f} pips"
                )
                return hit
            partial_ratio = self._eff_partial_ratio(trade)
            lots_close, lots_remain = self.partial_calc.calculate_partial(
                trade.remaining_size_lots, partial_ratio,
            )
            trade.remaining_size_lots = lots_remain
            trade.partial_closed = True
            trade.tp1_hit_time = bar_time or datetime.now(timezone.utc)
            trade.status = TradeStatus.TP1_HIT
            _log = logger.debug if str(trade.trade_id).startswith("shadow_") else logger.info
            _log(
                f"TP1 HIT: {trade.pair} — closed {partial_ratio:.0%} "
                f"({lots_close} lots) at {trade.tp1}, +{trade.pnl_pips:.1f} pips"
            )
        return hit

    def _activate_breakeven(self, trade: ManagedTrade) -> None:
        if trade.pnl_pips <= 0:
            return
        direction = "LONG" if self._is_long(trade.direction) else "SHORT"
        be_level = self.partial_calc.calculate_breakeven_level(
            trade.entry_price,
            direction,
            self.breakeven_buffer_pips,
            trade.pip_size,
        )
        trade.stop_loss = be_level
        trade.breakeven_active = True
        trade.status = TradeStatus.BREAKEVEN
        _log = logger.debug if str(trade.trade_id).startswith("shadow_") else logger.info
        _log(f"BREAKEVEN SET: {trade.pair} — SL moved to {be_level}")

    def _update_trailing(
        self, trade: ManagedTrade, df_m5: pd.DataFrame, trail_factor: float = 1.0,
    ) -> None:
        direction = "LONG" if self._is_long(trade.direction) else "SHORT"
        # P8: shrink the structure buffer when the book is hot so the runner's
        # stop sits closer to structure (tighter). Fail-safe: a bad/zero factor
        # reverts to the normal buffer. calculate_trail still only returns an
        # improving stop, so the never-worsen-SL invariant always holds.
        buffer_pips = None
        try:
            if trail_factor and 0.0 < trail_factor < 1.0:
                buffer_pips = self.trailing.buffer_pips * trail_factor
        except Exception:
            buffer_pips = None
        new_sl = self.trailing.calculate_trail(
            direction, trade.stop_loss, df_m5, trade.pip_size,
            buffer_pips=buffer_pips,
        )
        if new_sl is not None:
            trade.stop_loss = new_sl
            trade.trailing_stop = new_sl
            trade.status = TradeStatus.TRAILING
            tightened = " (heat-tightened)" if buffer_pips is not None else ""
            logger.info(
                f"TRAILING: {trade.pair} — SL moved to {new_sl} (structure-based){tightened}"
            )

    def _check_tp3(self, trade: ManagedTrade) -> bool:
        if not self.tp3_ladder_enabled:
            return False
        if trade.tp3 is None or trade.tp3 <= 0 or trade.tp3_hit or not trade.partial_closed:
            return False
        is_long = self._is_long(trade.direction)
        hit = (
            (is_long and trade.current_price >= trade.tp3)
            or (not is_long and trade.current_price <= trade.tp3)
        )
        if hit:
            tp3_lots = round(trade.remaining_size_lots * self.tp3_close_ratio, 2)
            tp3_lots = max(0.01, tp3_lots)
            remainder = round(trade.remaining_size_lots - tp3_lots, 2)
            remainder = max(0.0, remainder)
            trade.remaining_size_lots = remainder
            trade.tp3_hit = True
            logger.info(
                "TP3 HIT: {} — closed {:.0%} of runner ({} lots) at {:.5f}",
                trade.pair, self.tp3_close_ratio, tp3_lots, trade.tp3,
            )
        return hit

    def _check_tp2(self, trade: ManagedTrade, bar_time: Optional[datetime] = None) -> bool:
        if trade.tp2 is None or trade.tp2 <= 0:
            return False
        is_long = self._is_long(trade.direction)
        hit = (
            (is_long and trade.current_price >= trade.tp2)
            or (not is_long and trade.current_price <= trade.tp2)
        )
        if hit:
            self.close_trade(trade, "TP2 hit", trade.current_price, TradeStatus.CLOSED, bar_time=bar_time)
        return hit

    def _check_structure_exit(
        self, trade: ManagedTrade, df_m5: pd.DataFrame,
        bar_time: Optional[datetime] = None,
    ) -> bool:
        """Exit if M5 structure shifts against the trade direction.

        P4: this M5-only read is reconciled with the strategic brain. When the
        strategic (D1/H4/H1/M1) structure read is fresh and intact, defer this
        independent exit — the richer brain says the structure still holds. When
        the strategic read is fresh and says structure is broken, the exit fires
        with higher confidence. A stale/absent strategic read falls back to the
        M5 analysis below (unchanged behaviour).
        """
        if trade.partial_closed:
            return False
        now = bar_time or datetime.now(timezone.utc)
        strat = self._fresh_strategic_assessment(trade, now)
        if strat is not None and strat[0] >= self.strategic_structure_intact_threshold:
            logger.debug(
                "[structure-exit] {} deferred — strategic structure intact "
                "({:.2f} ≥ {:.2f})",
                trade.pair, strat[0], self.strategic_structure_intact_threshold,
            )
            return False
        stall_minutes = (now - trade.entry_time).total_seconds() / 60
        if stall_minutes < 30:
            return False
        if len(df_m5) < 10:
            return False
        analysis = self._analyze_m5_structure(df_m5)
        is_long = self._is_long(trade.direction)
        strat_confirms_break = (
            strat is not None
            and strat[0] < self.strategic_structure_intact_threshold
        )
        confirm = " (strategic structure confirms break)" if strat_confirms_break else ""
        # #32: consult the carried strategic tf_alignment. We only reach here
        # when the strategic structure read is NOT intact (an intact read already
        # deferred above) — but a fresh tf_alignment can still strongly support
        # the trade direction, meaning the higher-timeframe trend treats this M5
        # break as a pullback, not a reversal. In that case defer the full close
        # (the trade keeps running under its existing stop) instead of ignoring
        # the alignment the engine already recorded.
        if self.structure_exit_tf_alignment_enabled and strat is not None:
            tf_align = strat[1]
            supports = (
                tf_align >= self.structure_exit_tf_alignment_defer
                if is_long
                else tf_align <= -self.structure_exit_tf_alignment_defer
            )
            if supports:
                logger.debug(
                    "[structure-exit] {} deferred — strategic tf_alignment "
                    "{:+.2f} still supports {} (|·| ≥ {:.2f})",
                    trade.pair, tf_align, trade.direction,
                    self.structure_exit_tf_alignment_defer,
                )
                return False
        if is_long and analysis.last_event in (StructureEvent.CHOCH_BEARISH, StructureEvent.BOS_BEARISH):
            self.close_trade(
                trade,
                f"Structure exit — bearish shift after {stall_minutes:.0f}min{confirm}",
                trade.current_price,
                TradeStatus.TIME_EXIT,
                bar_time=bar_time,
            )
            return True
        if not is_long and analysis.last_event in (StructureEvent.CHOCH_BULLISH, StructureEvent.BOS_BULLISH):
            self.close_trade(
                trade,
                f"Structure exit — bullish shift after {stall_minutes:.0f}min{confirm}",
                trade.current_price,
                TradeStatus.TIME_EXIT,
                bar_time=bar_time,
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Strategic-structure sync (P4)
    # ------------------------------------------------------------------

    def record_strategic_assessment(
        self,
        trade: ManagedTrade,
        structure_integrity: float,
        tf_alignment: float,
        assessed_at: Optional[datetime] = None,
    ) -> None:
        """Store the strategic engine's latest structure read on the trade.

        Called by the trading loop after the (slower, multi-timeframe) decision
        engine runs, so the (faster, M5-only) mechanical structure-exit can
        defer to it while it is fresh.
        """
        trade.strategic_structure_integrity = structure_integrity
        trade.strategic_tf_alignment = tf_alignment
        trade.strategic_assessment_time = assessed_at or datetime.now(timezone.utc)

    def _fresh_strategic_assessment(
        self, trade: ManagedTrade, now: datetime,
    ) -> Optional[tuple[float, float]]:
        """Return ``(structure_integrity, tf_alignment)`` when the strategic
        read is present and not older than ``strategic_structure_max_age_seconds``;
        otherwise ``None`` (the mechanical M5 read then governs the exit)."""
        integrity = trade.strategic_structure_integrity
        ts = trade.strategic_assessment_time
        if integrity is None or ts is None:
            return None
        try:
            age = (now - ts).total_seconds()
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self.strategic_structure_max_age_seconds:
            return None
        return (integrity, trade.strategic_tf_alignment or 0.0)


    def _adjust_tp2(
        self, trade: ManagedTrade, df_m5: pd.DataFrame,
    ) -> None:
        """Extend or tighten TP2 based on M5 structure when trailing is active."""
        if trade.tp2 is None or trade.tp2 <= 0:
            return
        if len(df_m5) < 10:
            return
        is_long = self._is_long(trade.direction)
        analysis = self._analyze_m5_structure(df_m5)
        continuation_events = (StructureEvent.BOS_BULLISH,) if is_long else (StructureEvent.BOS_BEARISH,)
        counter_events = (StructureEvent.CHOCH_BEARISH,) if is_long else (StructureEvent.CHOCH_BULLISH,)
        if analysis.last_event in continuation_events:
            risk_distance = abs(trade.entry_price - trade.original_stop_loss)
            new_tp2 = trade.tp2 + risk_distance if is_long else trade.tp2 - risk_distance
            if (is_long and new_tp2 > trade.tp2) or (not is_long and new_tp2 < trade.tp2):
                trade.tp2 = new_tp2
                logger.info(
                    "TP2 EXTENDED: {} — new TP2 {:.5f} (continuation BOS)",
                    trade.pair, new_tp2,
                )
        elif analysis.last_event in counter_events and trade.partial_closed:
            halfway = (trade.entry_price + trade.tp2) / 2
            if is_long and trade.current_price > halfway:
                trade.tp2 = trade.current_price + (trade.current_price - trade.entry_price) * 0.3
            elif not is_long and trade.current_price < halfway:
                trade.tp2 = trade.current_price - (trade.entry_price - trade.current_price) * 0.3
            logger.info(
                "TP2 TIGHTENED: {} — new TP2 {:.5f} (counter structure)",
                trade.pair, trade.tp2,
            )

    def _check_stall(self, trade: ManagedTrade, bar_time: Optional[datetime] = None) -> bool:
        """Time-based stall exit — scales with entry timeframe."""
        if trade.partial_closed:
            return False
        stall_limits = {"M1": 30, "M5": 60, "M15": 90, "H1": 180, "H4": 360}
        stall_limit = stall_limits.get(trade.entry_timeframe, 75)
        now = bar_time or datetime.now(timezone.utc)
        stall_minutes = (now - trade.entry_time).total_seconds() / 60
        # Instrument-aware "flat" threshold: a fixed 5-pip band is meaningless
        # across instruments (5 pips on Gold ≈ $0.05; on an index it's noise).
        # Scale it to a fraction of THIS trade's own stop distance so the band
        # is proportional to the instrument's volatility/pricing.
        risk_pips = (
            abs(trade.entry_price - trade.original_stop_loss) / trade.pip_size
            if trade.pip_size > 0 else 0.0
        )
        flat_threshold = 0.15 * risk_pips if risk_pips > 0 else 5.0
        if stall_minutes > stall_limit and abs(trade.pnl_pips) < flat_threshold:
            self.close_trade(
                trade,
                f"Stall exit — {stall_minutes:.0f}min (limit {stall_limit}), {trade.pnl_pips:.1f}pip",
                trade.current_price,
                TradeStatus.TIME_EXIT,
                bar_time=bar_time,
            )
            return True
        return False
