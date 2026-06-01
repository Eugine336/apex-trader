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
    ):
        self.max_stall_candles = max_stall_candles
        self.partial_close_ratio = partial_close_ratio
        self.breakeven_buffer_pips = breakeven_buffer_pips
        self.default_pip_value = default_pip_value
        self.tp_adjust_enabled = tp_adjust_enabled
        self.trailing = StructureTrailingStop()
        self.partial_calc = PartialCloseCalculator()
        self._trades: dict[str, ManagedTrade] = {}

    @staticmethod
    def _is_long(direction: str) -> bool:
        """Treat BUY/LONG as long, SELL/SHORT as short."""
        return direction.upper() in ("LONG", "BUY")

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
    ) -> ManagedTrade:
        if trade.status in TERMINAL_STATUSES:
            return trade

        trade.current_price = current_price
        trade.candles_since_entry += 1

        self._update_pnl(trade)
        self._update_extremes(trade)

        if self._check_stop_loss(trade):
            return trade
        if self._check_tp1(trade):
            pass
        if trade.partial_closed and not trade.breakeven_active:
            self._activate_breakeven(trade)
        if trade.breakeven_active and current_df_m5 is not None:
            self._update_trailing(trade, current_df_m5)
        if self.tp_adjust_enabled and trade.partial_closed and current_df_m5 is not None:
            self._adjust_tp2(trade, current_df_m5)
        if self._check_tp2(trade):
            return trade
        if current_df_m5 is not None and self._check_structure_exit(trade, current_df_m5):
            return trade
        if self._check_stall(trade):
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
    ) -> ManagedTrade:
        trade.current_price = close_price
        trade.status = status
        trade.close_time = datetime.now(timezone.utc)
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

    def _check_stop_loss(self, trade: ManagedTrade) -> bool:
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
            self.close_trade(trade, reason, trade.current_price, TradeStatus.STOPPED)
        return hit

    def _check_tp1(self, trade: ManagedTrade) -> bool:
        if trade.partial_closed:
            return False
        is_long = self._is_long(trade.direction)
        hit = (
            (is_long and trade.current_price >= trade.tp1)
            or (not is_long and trade.current_price <= trade.tp1)
        )
        if hit:
            lots_close, lots_remain = self.partial_calc.calculate_partial(
                trade.remaining_size_lots, self.partial_close_ratio,
            )
            trade.remaining_size_lots = lots_remain
            trade.partial_closed = True
            trade.tp1_hit_time = datetime.now(timezone.utc)
            trade.status = TradeStatus.TP1_HIT
            logger.info(
                f"TP1 HIT: {trade.pair} — closed {self.partial_close_ratio:.0%} "
                f"({lots_close} lots) at {trade.tp1}, +{trade.pnl_pips:.1f} pips"
            )
        return hit

    def _activate_breakeven(self, trade: ManagedTrade) -> None:
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

    def _check_tp2(self, trade: ManagedTrade) -> bool:
        is_long = self._is_long(trade.direction)
        hit = (
            (is_long and trade.current_price >= trade.tp2)
            or (not is_long and trade.current_price <= trade.tp2)
        )
        if hit:
            self.close_trade(trade, "TP2 hit", trade.current_price, TradeStatus.CLOSED)
        return hit

    def _check_structure_exit(
        self, trade: ManagedTrade, df_m5: pd.DataFrame,
    ) -> bool:
        """Exit if M5 structure shifts against the trade direction."""
        if trade.partial_closed:
            return False
        stall_minutes = (datetime.now(timezone.utc) - trade.entry_time).total_seconds() / 60
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
            )
            return True
        if not is_long and analysis.last_event in (StructureEvent.CHOCH_BULLISH, StructureEvent.BOS_BULLISH):
            self.close_trade(
                trade,
                f"Structure exit — bullish shift after {stall_minutes:.0f}min",
                trade.current_price,
                TradeStatus.TIME_EXIT,
            )
            return True
        return False

    def _adjust_tp2(
        self, trade: ManagedTrade, df_m5: pd.DataFrame,
    ) -> None:
        """Extend or tighten TP2 based on M5 structure when trailing is active."""
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

        """Time-based stall exit — scales with entry timeframe."""
        if trade.partial_closed:
            return False
        stall_limits = {"M1": 30, "M5": 60, "M15": 90, "H1": 180, "H4": 360}
        stall_limit = stall_limits.get(trade.entry_timeframe, 75)
        stall_minutes = (datetime.now(timezone.utc) - trade.entry_time).total_seconds() / 60
        if stall_minutes > stall_limit and abs(trade.pnl_pips) < 5.0:
            self.close_trade(
                trade,
                f"Stall exit — {stall_minutes:.0f}min (limit {stall_limit}), {trade.pnl_pips:.1f}pip",
                trade.current_price,
                TradeStatus.TIME_EXIT,
            )
            return True
        return False