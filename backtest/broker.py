"""SimulatedBroker — a faithful in-memory broker for backtesting.

Implements the exact :class:`platforms.base_connector.BaseConnector` interface
the live system speaks, so the real pipeline (and tests) can drive it with no
behavioural changes. Fills are simulated bar-by-bar against historical OHLC:

* **No lookahead** — ``get_ohlcv`` only returns candles at or before the current
  replay cursor; ``advance`` is what moves the cursor forward one bar.
* **Spread** — bid/ask are derived from the bar's close using the instrument's
  pip size (configurable spread in pips).
* **Slippage** — applied adversely on market fills and on stop-loss fills, drawn
  from a seeded RNG so runs are reproducible.
* **Pessimistic intrabar fills** — when a bar's range straddles both SL and TP,
  the stop is assumed to fill first (worst case).
* **P&L** — computed in account currency via the instrument's
  ``pip_value_per_lot`` so results line up with the live sizing model.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    PositionInfo,
    TickData,
)
from backtest.data import Candle

# Sensible fallbacks for symbols not in the instrument registry (e.g. ad-hoc
# synthetic test pairs). Real registered instruments use their declared values.
_DEFAULT_PIP_SIZE = 0.0001
_DEFAULT_PIP_VALUE = 10.0
_DEFAULT_SPREAD_PIPS = 1.0


def _instrument_pip_size(symbol: str) -> float:
    try:
        from config import get_pip_size

        return float(get_pip_size(symbol))
    except Exception:  # noqa: BLE001 — unknown/ad-hoc symbol → fallback
        return _DEFAULT_PIP_SIZE


def _instrument_pip_value(symbol: str) -> float:
    try:
        from config import get_instrument

        return float(get_instrument(symbol).pip_value_per_lot)
    except Exception:  # noqa: BLE001
        return _DEFAULT_PIP_VALUE


def _instrument_default_spread(symbol: str) -> Optional[float]:
    try:
        from config import get_instrument

        return float(get_instrument(symbol).typical_spread_pips)
    except Exception:  # noqa: BLE001
        return None


@dataclass
class SimulatedPosition:
    """A live position inside the simulator."""

    order_id: str
    symbol: str
    direction: str  # "BUY" / "SELL"
    lots: float
    open_price: float
    sl: float
    tp: float
    open_time: datetime
    comment: str = ""
    trail_distance_pips: float = 0.0  # optional broker-side trailing (off by default)
    current_price: float = 0.0
    _hwm: float = 0.0  # high-water mark for trailing (BUY) / low-water (SELL)

    def pip_size(self) -> float:
        return _instrument_pip_size(self.symbol)

    def unrealized_pnl(self, price: float) -> float:
        pips = (price - self.open_price) / self.pip_size()
        if self.direction == "SELL":
            pips = -pips
        return pips * _instrument_pip_value(self.symbol) * self.lots


@dataclass
class ClosedFill:
    """The record the broker hands back when a position closes."""

    order_id: str
    symbol: str
    direction: str
    lots: float
    open_price: float
    close_price: float
    open_time: datetime
    close_time: datetime
    pnl: float
    pnl_pips: float
    exit_reason: str  # "tp" | "sl" | "manual" | "trailing"
    comment: str = ""


class SimulatedBroker(BaseConnector):
    """In-memory broker driven by historical candles.

    Typical lifecycle::

        broker = SimulatedBroker(starting_balance=10_000)
        broker.load_data("EURUSD", {"M5": candles})
        for candle in candles:                      # runner-driven timeline
            fills = broker.advance("EURUSD", candle) # process SL/TP first
            broker.place_order("EURUSD", "BUY", 0.1, sl, tp)
    """

    def __init__(
        self,
        *,
        starting_balance: float = 10_000.0,
        spread_pips: float = 1.0,
        slippage_pips: float = 0.5,
        currency: str = "USD",
        leverage: int = 100,
        seed: int = 42,
    ) -> None:
        self.starting_balance = float(starting_balance)
        self.balance = float(starting_balance)
        self.spread_pips = float(spread_pips)
        self.slippage_pips = float(slippage_pips)
        self.currency = currency
        self.leverage = int(leverage)
        self._rng = random.Random(seed)
        self._connected = True

        # Per-symbol data + replay cursor (timestamp of the latest closed bar).
        self._data: dict[str, dict[str, list[Candle]]] = {}
        self._cursor: dict[str, datetime] = {}
        self._cur_price: dict[str, float] = {}

        self._positions: dict[str, SimulatedPosition] = {}
        self._closed_fills: list[ClosedFill] = []
        self._next_id = 1

    # ── Data wiring ──────────────────────────────────────────────────────

    def load_data(self, symbol: str, frames: dict[str, list[Candle]]) -> None:
        """Register {timeframe: [Candle, ...]} for a symbol."""
        self._data[symbol] = {
            tf: sorted(rows, key=lambda c: c.time) for tf, rows in frames.items()
        }
        # Seed current price from the first available bar so pre-advance reads
        # are sane; the cursor stays unset until the first advance().
        for rows in self._data[symbol].values():
            if rows:
                self._cur_price.setdefault(symbol, rows[0].open)
                break

    def advance(self, symbol: str, candle: Candle) -> list[ClosedFill]:
        """Move the replay cursor to ``candle`` and process intrabar fills.

        Returns the fills that closed on this bar. SL/TP are checked against the
        bar's high/low; when both are reachable, the stop fills first.
        """
        self._cursor[symbol] = candle.time
        self._cur_price[symbol] = candle.close
        fills: list[ClosedFill] = []
        for oid in list(self._positions.keys()):
            pos = self._positions.get(oid)
            if pos is None or pos.symbol != symbol:
                continue
            pos.current_price = candle.close
            # Check the stop/target against THIS bar using the SL set from prior
            # bars (no intrabar lookahead), then ratchet any trailing stop so it
            # only affects subsequent bars.
            fill = self._check_exit(pos, candle)
            if fill is not None:
                fills.append(fill)
            else:
                self._apply_trailing(pos, candle)
        return fills

    # ── Trailing + exit logic ────────────────────────────────────────────

    def _apply_trailing(self, pos: SimulatedPosition, candle: Candle) -> None:
        if pos.trail_distance_pips <= 0:
            return
        pip = pos.pip_size()
        dist = pos.trail_distance_pips * pip
        if pos.direction == "BUY":
            pos._hwm = max(pos._hwm or candle.high, candle.high)
            new_sl = pos._hwm - dist
            if new_sl > pos.sl:
                pos.sl = new_sl
        else:
            pos._hwm = min(pos._hwm or candle.low, candle.low)
            new_sl = pos._hwm + dist
            if pos.sl == 0 or new_sl < pos.sl:
                pos.sl = new_sl

    def _check_exit(self, pos: SimulatedPosition, candle: Candle) -> Optional[ClosedFill]:
        pip = pos.pip_size()
        slip = self.slippage_pips * pip
        hit_sl = False
        hit_tp = False
        if pos.direction == "BUY":
            hit_sl = pos.sl > 0 and candle.low <= pos.sl
            hit_tp = pos.tp > 0 and candle.high >= pos.tp
        else:  # SELL
            hit_sl = pos.sl > 0 and candle.high >= pos.sl
            hit_tp = pos.tp > 0 and candle.low <= pos.tp

        if not hit_sl and not hit_tp:
            return None

        # Pessimistic: stop fills first when the bar straddles both levels.
        if hit_sl:
            # Stop fills with adverse slippage (price gaps through the stop).
            price = pos.sl - slip if pos.direction == "BUY" else pos.sl + slip
            reason = "trailing" if pos.trail_distance_pips > 0 and self._is_trailed(pos) else "sl"
            return self._close(pos, price, candle.time, reason)
        # TP fills at the target (favourable; no adverse slippage applied).
        return self._close(pos, pos.tp, candle.time, "tp")

    @staticmethod
    def _is_trailed(pos: SimulatedPosition) -> bool:
        # A trailing exit is one where the SL has been ratcheted past entry.
        if pos.direction == "BUY":
            return pos.sl >= pos.open_price
        return pos.sl <= pos.open_price and pos.sl > 0

    def _close(
        self, pos: SimulatedPosition, price: float, when: datetime, reason: str
    ) -> ClosedFill:
        pnl = pos.unrealized_pnl(price)
        pips = (price - pos.open_price) / pos.pip_size()
        if pos.direction == "SELL":
            pips = -pips
        self.balance += pnl
        fill = ClosedFill(
            order_id=pos.order_id,
            symbol=pos.symbol,
            direction=pos.direction,
            lots=pos.lots,
            open_price=pos.open_price,
            close_price=price,
            open_time=pos.open_time,
            close_time=when,
            pnl=pnl,
            pnl_pips=pips,
            exit_reason=reason,
            comment=pos.comment,
        )
        self._closed_fills.append(fill)
        self._positions.pop(pos.order_id, None)
        return fill

    # ── BaseConnector interface ──────────────────────────────────────────

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_account_info(self) -> AccountInfo:
        equity = self.balance + sum(
            p.unrealized_pnl(self._cur_price.get(p.symbol, p.open_price))
            for p in self._positions.values()
        )
        return AccountInfo(
            balance=round(self.balance, 2),
            equity=round(equity, 2),
            margin=0.0,
            free_margin=round(equity, 2),
            margin_level=0.0,
            currency=self.currency,
            leverage=self.leverage,
            platform="backtest",
        )

    def _mid(self, symbol: str) -> float:
        return self._cur_price.get(symbol, 0.0)

    def get_price(self, symbol: str) -> TickData:
        return self.get_tick(symbol)

    def get_tick(self, symbol: str) -> TickData:
        mid = self._mid(symbol)
        pip = _instrument_pip_size(symbol)
        half = (self.spread_pips * pip) / 2.0
        when = self._cursor.get(symbol) or datetime.now(timezone.utc)
        return TickData(bid=mid - half, ask=mid + half, spread=self.spread_pips, time=when)

    def get_spread(self, symbol: str) -> float:
        return self.spread_pips

    def get_ohlcv(self, symbol: str, timeframe: str, count: int = 200, *, include_forming: bool = True) -> pd.DataFrame:
        frames = self._data.get(symbol, {})
        rows = frames.get(timeframe, [])
        cursor = self._cursor.get(symbol)
        if cursor is not None:
            # No lookahead: only bars at or before the current replay cursor.
            rows = [c for c in rows if c.time <= cursor]
        rows = rows[-count:] if count and count > 0 else rows
        if not rows:
            return pd.DataFrame(
                columns=["time", "open", "high", "low", "close", "volume"]
            )
        df = pd.DataFrame([c.as_row() for c in rows])
        df["time"] = pd.to_datetime(df["time"], utc=True)
        return df[["time", "open", "high", "low", "close", "volume"]]

    def place_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        idempotency_key: str = "",
    ):
        from platforms.base_connector import OrderResult

        direction = str(direction).upper()
        if direction not in ("BUY", "SELL"):
            return OrderResult(
                success=False, order_id="", fill_price=0.0, requested_price=0.0,
                slippage_pips=0.0, lots=lots, symbol=symbol, direction=direction,
                sl=sl, tp=tp, platform="backtest",
                error=f"invalid direction {direction!r}",
            )
        mid = self._mid(symbol)
        if mid <= 0:
            return OrderResult(
                success=False, order_id="", fill_price=0.0, requested_price=0.0,
                slippage_pips=0.0, lots=lots, symbol=symbol, direction=direction,
                sl=sl, tp=tp, platform="backtest",
                error="no market price (advance the broker first)",
            )
        pip = _instrument_pip_size(symbol)
        half = (self.spread_pips * pip) / 2.0
        # Cross the spread on entry, then apply adverse slippage.
        slip = self._rng.uniform(0.0, self.slippage_pips) * pip
        if direction == "BUY":
            fill = mid + half + slip
        else:
            fill = mid - half - slip
        slip_pips = abs(fill - mid) / pip
        oid = str(self._next_id)
        self._next_id += 1
        when = self._cursor.get(symbol) or datetime.now(timezone.utc)
        self._positions[oid] = SimulatedPosition(
            order_id=oid, symbol=symbol, direction=direction, lots=float(lots),
            open_price=fill, sl=float(sl), tp=float(tp), open_time=when,
            comment=comment, current_price=fill,
        )
        return OrderResult(
            success=True, order_id=oid, fill_price=fill, requested_price=mid,
            slippage_pips=slip_pips, lots=float(lots), symbol=symbol,
            direction=direction, sl=float(sl), tp=float(tp), platform="backtest",
        )

    def modify_order(
        self,
        order_id: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        pos = self._positions.get(str(order_id))
        if pos is None:
            return False
        if new_sl is not None:
            pos.sl = float(new_sl)
        if new_tp is not None:
            pos.tp = float(new_tp)
        return True

    def close_order(self, order_id: str, lots: Optional[float] = None) -> CloseResult:
        pos = self._positions.get(str(order_id))
        if pos is None:
            return CloseResult(
                success=False, order_id=str(order_id), close_price=0.0,
                lots_closed=0.0, pnl=0.0, platform="backtest",
                error="position not found",
            )
        mid = self._mid(pos.symbol)
        pip = _instrument_pip_size(pos.symbol)
        half = (self.spread_pips * pip) / 2.0
        # Closing crosses the spread the opposite way to the entry.
        price = mid - half if pos.direction == "BUY" else mid + half
        when = self._cursor.get(pos.symbol) or datetime.now(timezone.utc)
        # Partial close: shrink the live position, realise the closed slice.
        if lots is not None and 0 < float(lots) < pos.lots:
            slice_lots = float(lots)
            closed = SimulatedPosition(
                order_id=pos.order_id, symbol=pos.symbol, direction=pos.direction,
                lots=slice_lots, open_price=pos.open_price, sl=pos.sl, tp=pos.tp,
                open_time=pos.open_time, comment=pos.comment,
            )
            pnl = closed.unrealized_pnl(price)
            self.balance += pnl
            pos.lots -= slice_lots
            return CloseResult(
                success=True, order_id=pos.order_id, close_price=price,
                lots_closed=slice_lots, pnl=round(pnl, 2), platform="backtest",
            )
        fill = self._close(pos, price, when, "manual")
        return CloseResult(
            success=True, order_id=fill.order_id, close_price=price,
            lots_closed=fill.lots, pnl=round(fill.pnl, 2), platform="backtest",
        )

    def _to_position_info(self, pos: SimulatedPosition) -> PositionInfo:
        price = self._cur_price.get(pos.symbol, pos.open_price)
        return PositionInfo(
            order_id=pos.order_id, symbol=pos.symbol, direction=pos.direction,
            lots=pos.lots, open_price=pos.open_price, current_price=price,
            sl=pos.sl, tp=pos.tp, pnl=round(pos.unrealized_pnl(price), 2),
            swap=0.0, open_time=pos.open_time, platform="backtest",
        )

    def get_open_positions(self) -> list[PositionInfo]:
        return [self._to_position_info(p) for p in self._positions.values()]

    def get_position_info(self, order_id: str) -> Optional[PositionInfo]:
        pos = self._positions.get(str(order_id))
        return self._to_position_info(pos) if pos is not None else None

    def get_realized_pnl(self, order_id: str) -> Optional[float]:
        for fill in reversed(self._closed_fills):
            if fill.order_id == str(order_id):
                return round(fill.pnl, 2)
        return None

    # ── Backtest-only accessors ──────────────────────────────────────────

    @property
    def closed_fills(self) -> list[ClosedFill]:
        return list(self._closed_fills)

    @property
    def open_count(self) -> int:
        return len(self._positions)

    def force_close_all(self, when: Optional[datetime] = None) -> list[ClosedFill]:
        """Close every open position at the current market price (end of run)."""
        out: list[ClosedFill] = []
        for oid in list(self._positions.keys()):
            pos = self._positions[oid]
            mid = self._mid(pos.symbol)
            pip = _instrument_pip_size(pos.symbol)
            half = (self.spread_pips * pip) / 2.0
            price = mid - half if pos.direction == "BUY" else mid + half
            ts = when or self._cursor.get(pos.symbol) or datetime.now(timezone.utc)
            out.append(self._close(pos, price, ts, "manual"))
        return out
