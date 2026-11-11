"""Broker-truth safety-floor evaluation for open positions.

The CognitiveBrain owns discretionary thesis and opportunity management.
This worker emits only independent capital/operational protection intents:
confirmed hard-stop handling, absolute-profit protection, spread deterioration,
session close and pre-weekend protection. It never derives market direction,
scores a position, chooses a strategy, or performs discretionary profit-taking.
It never calls a broker; the shared ActionExecutor owns dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from execution.intents import Intent, IntentType
from execution.position_snapshot import PositionSnapshot
from config import is_always_open


def sl_within_min_room(
    current_price: float,
    new_sl: float,
    pip_size: float,
    min_room_pips: float,
) -> bool:
    """True when an SL sits inside the broker's minimum modification room."""
    if pip_size <= 0 or current_price <= 0 or not new_sl or new_sl <= 0:
        return False
    if min_room_pips <= 0:
        return False
    return abs(current_price - new_sl) < (min_room_pips * pip_size)


@dataclass
class WorkerConfig:
    """Only deterministic capital and operational safety settings."""

    breakeven_buffer_pips: float = 2.0
    min_sl_modify_room_pips: float = 2.0

    absolute_profit_protection_enabled: bool = True
    absolute_profit_pips: float = 50.0
    absolute_profit_usd: float = 100.0

    session_close_enabled: bool = True
    index_close_buffer_minutes: int = 30
    dead_zone_management: bool = True

    weekend_protection_enabled: bool = True
    weekend_protection_mode: str = "derisk"
    weekend_close_buffer_minutes: int = 15
    friday_close_hour_utc: int = 21
    weekend_reduce_fraction: float = 0.5

    spread_monitor_enabled: bool = True
    spread_deterioration_multiplier: float = 3.0


@dataclass(frozen=True)
class MarketContext:
    """Non-directional execution conditions required by safety checks."""

    typical_spread: Optional[float] = None
    current_spread: Optional[float] = None


class PositionWorker:
    """Evaluate a broker position against the independent safety floor."""

    def __init__(self, config: Optional[WorkerConfig] = None) -> None:
        self.cfg = config or WorkerConfig()

    def evaluate(
        self,
        snap: PositionSnapshot,
        now: Optional[datetime] = None,
        market: Optional[MarketContext] = None,
    ) -> list[Intent]:
        """Return safety intents only; discretionary management belongs to Brain."""
        now = now or datetime.now(timezone.utc)
        intents: list[Intent] = []

        if snap.trade_status in ("CLOSED", "STOPPED", "TIME_EXIT"):
            return intents

        # Never evaluate against an optimistic, broker-unconfirmed stop level.
        if not snap.sl_pending_confirmation:
            self._check_stop_loss(snap, intents)

        self._check_absolute_profit_protection(snap, intents)
        self._check_weekend_protection(snap, now, intents)
        self._check_session_close(snap, now, intents)
        if market is not None:
            self._check_spread_deterioration(snap, market, intents)

        return self._drop_too_close_sl_moves(snap, intents)

    def _drop_too_close_sl_moves(
        self, snap: PositionSnapshot, intents: list[Intent],
    ) -> list[Intent]:
        """Drop MODIFY_SL intents whose level is within the broker minimum room.

        A breakeven / trailing / profit-protection move that lands within
        ``min_sl_modify_room_pips`` of the current price would be clamped by the
        broker connector to a forced level (pinning the stop near breakeven and
        choking a still-running trade). Defer it instead — it re-emits next cycle
        once price has cleared enough room. CLOSE / PARTIAL_CLOSE / MODIFY_TP are
        never touched, so protective exits are unaffected.
        """
        room = self.cfg.min_sl_modify_room_pips
        if room <= 0:
            return intents
        kept: list[Intent] = []
        for intent in intents:
            if intent.intent_type == IntentType.MODIFY_SL and sl_within_min_room(
                snap.current_price, intent.new_sl or 0.0, snap.pip_size, room,
            ):
                logger.debug(
                    "[pos-worker] deferring SL move for {} — {} @ {:.5f} within "
                    "{:.1f}pip of price {:.5f} (source={})",
                    snap.symbol, intent.intent_type.name, intent.new_sl or 0.0,
                    room, snap.current_price, getattr(intent, "source", ""),
                )
                continue
            kept.append(intent)
        return kept

    # ──────────────────────────────────────────────────────────────────
    # Independent broker-truth safety checks
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
                    if self._be_move_is_valid(snap, be_level):
                        out.append(Intent.modify_sl(
                            symbol=snap.symbol,
                            ticket=snap.order_id,
                            new_sl=be_level,
                            source="dead_zone_protection",
                            reason=f"Dead zone — SL→BE {be_level:.5f}",
                        ))
                    else:
                        logger.debug(
                            "[dead-zone] {} BE move skipped — not profitable enough "
                            "or SL would not improve (pnl={:.1f}p sl={:.5f} be={:.5f})",
                            snap.symbol, snap.pnl_pips, snap.sl, be_level,
                        )

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
                if self._be_move_is_valid(snap, be_level):
                    out.append(Intent.modify_sl(
                        symbol=snap.symbol,
                        ticket=snap.order_id,
                        new_sl=be_level,
                        source="weekend_protection",
                        reason=f"Weekend protection — FX closes in {diff}min (SL→BE {be_level:.5f})",
                    ))
                else:
                    logger.debug(
                        "[weekend] {} BE derisk skipped — not profitable enough "
                        "or SL would not improve (pnl={:.1f}p sl={:.5f} be={:.5f})",
                        snap.symbol, snap.pnl_pips, snap.sl, be_level,
                    )

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

    @staticmethod
    def _be_move_is_valid(snap: PositionSnapshot, be_level: float) -> bool:
        """Allow only profitable stop improvements; never loosen the broker SL."""
        if snap.pnl_pips <= 2.0:
            return False
        if snap.is_long:
            return be_level > snap.sl
        return be_level < snap.sl

