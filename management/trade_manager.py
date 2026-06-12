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

from brain.structure_engine import StructureEngine, StructureEvent
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
    # Trade Planner per-trade management overrides — primitive values only so
    # the management package stays free of any planning import. ``None`` means
    # "use the TradeManager's global default" (backward compatible).
    plan_be_trigger_r: Optional[float] = None
    plan_trail_activation_r: Optional[float] = None
    plan_trail_strategy: Optional[str] = None
    plan_partial_ratio: Optional[float] = None


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
    # Trade Planner per-trade management overrides — ``None`` means the trade
    # was not opened by the planner (or the field was unset) and the manager's
    # global config value is used instead.
    plan_be_trigger_r: Optional[float] = None
    plan_trail_activation_r: Optional[float] = None
    plan_trail_strategy: Optional[str] = None
    plan_partial_ratio: Optional[float] = None


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
        self.trailing = StructureTrailingStop(swing_lookback=trailing_swing_lookback)
        self.partial_calc = PartialCloseCalculator()
        self._trades: dict[str, ManagedTrade] = {}

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
            self._update_trailing(trade, current_df_m5)
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
        logger.info(
            f"TRADE CLOSED: {trade.pair} {trade.direction} — {reason} "
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
            partial_ratio = self._eff_partial_ratio(trade)
            lots_close, lots_remain = self.partial_calc.calculate_partial(
                trade.remaining_size_lots, partial_ratio,
            )
            trade.remaining_size_lots = lots_remain
            trade.partial_closed = True
            trade.tp1_hit_time = bar_time or datetime.now(timezone.utc)
            trade.status = TradeStatus.TP1_HIT
            logger.info(
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
        logger.info(f"BREAKEVEN SET: {trade.pair} — SL moved to {be_level}")

    def _update_trailing(
        self, trade: ManagedTrade, df_m5: pd.DataFrame,
    ) -> None:
        direction = "LONG" if self._is_long(trade.direction) else "SHORT"
        new_sl = self.trailing.calculate_trail(
            direction, trade.stop_loss, df_m5, trade.pip_size,
        )
        if new_sl is not None:
            trade.stop_loss = new_sl
            trade.trailing_stop = new_sl
            trade.status = TradeStatus.TRAILING
            logger.info(
                f"TRAILING: {trade.pair} — SL moved to {new_sl} (structure-based)"
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
        """Exit if M5 structure shifts against the trade direction."""
        if trade.partial_closed:
            return False
        now = bar_time or datetime.now(timezone.utc)
        stall_minutes = (now - trade.entry_time).total_seconds() / 60
        if stall_minutes < 30:
            return False
        if len(df_m5) < 10:
            return False
        struct = StructureEngine(swing_lookback=3)
        analysis = struct.analyze(df_m5)
        is_long = self._is_long(trade.direction)
        if is_long and analysis.last_event in (StructureEvent.CHOCH_BEARISH, StructureEvent.BOS_BEARISH):
            self.close_trade(
                trade,
                f"Structure exit — bearish shift after {stall_minutes:.0f}min",
                trade.current_price,
                TradeStatus.TIME_EXIT,
                bar_time=bar_time,
            )
            return True
        if not is_long and analysis.last_event in (StructureEvent.CHOCH_BULLISH, StructureEvent.BOS_BULLISH):
            self.close_trade(
                trade,
                f"Structure exit — bullish shift after {stall_minutes:.0f}min",
                trade.current_price,
                TradeStatus.TIME_EXIT,
                bar_time=bar_time,
            )
            return True
        return False

    def _adjust_tp2(
        self, trade: ManagedTrade, df_m5: pd.DataFrame,
    ) -> None:
        """Extend or tighten TP2 based on M5 structure when trailing is active."""
        if trade.tp2 is None or trade.tp2 <= 0:
            return
        if len(df_m5) < 10:
            return
        is_long = self._is_long(trade.direction)
        struct = StructureEngine(swing_lookback=3)
        analysis = struct.analyze(df_m5)
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
        if stall_minutes > stall_limit and abs(trade.pnl_pips) < 5.0:
            self.close_trade(
                trade,
                f"Stall exit — {stall_minutes:.0f}min (limit {stall_limit}), {trade.pnl_pips:.1f}pip",
                trade.current_price,
                TradeStatus.TIME_EXIT,
                bar_time=bar_time,
            )
            return True
        return False
