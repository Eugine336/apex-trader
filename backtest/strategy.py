"""Backtest strategy interface.

The harness's job is *simulation fidelity*, not reinventing the brain. Signal
generation is therefore pluggable: a :class:`Strategy` receives a
:class:`BarContext` (only past/current data — never the future) and returns
:class:`Signal` objects. The real APEX brain can be wired in via this protocol
(or via the full-loop adapter); the built-in
:class:`MovingAverageCrossStrategy` is a small, deterministic default used to
self-test the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol, Sequence, runtime_checkable

from backtest.broker import SimulatedBroker
from backtest.data import Candle


@dataclass
class Signal:
    """A request to open a position. SL/TP are absolute prices.

    ``size`` is in lots. ``horizon`` and ``profile`` are free-form strategy
    fingerprints recorded with the trade so per-style breakdowns are possible.
    """

    pair: str
    direction: str  # "BUY" / "SELL"
    sl: float
    tp: float
    size: float = 0.01
    horizon: str = "SWING"
    profile: str = "default"
    reason: str = ""


@dataclass
class BarContext:
    """What a strategy sees on each step — strictly no future data."""

    pair: str
    now: datetime
    candle: Candle  # the just-closed bar
    history: Sequence[Candle]  # all bars up to and including ``candle``
    broker: SimulatedBroker
    open_positions: Sequence = field(default_factory=list)
    regime: str = "UNKNOWN"
    account_balance: float = 0.0


@runtime_checkable
class Strategy(Protocol):
    """Pluggable signal generator."""

    def on_bar(self, ctx: BarContext) -> list[Signal]:  # pragma: no cover - protocol
        ...


def _sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    window = values[-period:]
    return sum(window) / period


class MovingAverageCrossStrategy:
    """A minimal, deterministic SMA-crossover demo strategy.

    Goes long when the fast SMA crosses above the slow SMA, short on the
    opposite cross. SL/TP are placed a fixed pip distance from the close at a
    configured reward:risk. Deterministic and lookahead-free — it only ever
    reads ``ctx.history``. This exists to exercise the engine end-to-end, not as
    a production strategy.
    """

    def __init__(
        self,
        *,
        fast: int = 10,
        slow: int = 30,
        sl_pips: float = 20.0,
        rr: float = 2.0,
        size: float = 0.10,
        one_position_per_pair: bool = True,
    ) -> None:
        if fast >= slow:
            raise ValueError("fast period must be < slow period")
        self.fast = int(fast)
        self.slow = int(slow)
        self.sl_pips = float(sl_pips)
        self.rr = float(rr)
        self.size = float(size)
        self.one_position_per_pair = bool(one_position_per_pair)

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        if self.one_position_per_pair and any(
            getattr(p, "symbol", None) == ctx.pair for p in ctx.open_positions
        ):
            return []
        closes = [c.close for c in ctx.history]
        if len(closes) < self.slow + 1:
            return []
        fast_now = _sma(closes, self.fast)
        slow_now = _sma(closes, self.slow)
        fast_prev = _sma(closes[:-1], self.fast)
        slow_prev = _sma(closes[:-1], self.slow)
        if None in (fast_now, slow_now, fast_prev, slow_prev):
            return []

        from backtest.broker import _instrument_pip_size

        pip = _instrument_pip_size(ctx.pair)
        price = ctx.candle.close
        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up:
            sl = price - self.sl_pips * pip
            tp = price + self.sl_pips * self.rr * pip
            return [Signal(ctx.pair, "BUY", sl, tp, self.size, reason="sma_cross_up")]
        if crossed_down:
            sl = price + self.sl_pips * pip
            tp = price - self.sl_pips * self.rr * pip
            return [Signal(ctx.pair, "SELL", sl, tp, self.size, reason="sma_cross_down")]
        return []
