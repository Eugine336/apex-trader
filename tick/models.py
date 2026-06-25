"""APEX TRADER — Tick data models (Phase 2).

Frozen dataclasses for tick events and candle-close events.
These are the currency of the event-driven architecture: the Tick Router
emits ``Tick`` objects; the ``CandleCloseDetector`` derives ``CandleClose``
events from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Tick:
    """A single price update from a broker.

    Immutable so it can be safely shared across threads without copying.
    ``source`` identifies the originating broker ("mt5" or "deriv").
    """

    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    source: str

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def epoch(self) -> float:
        return self.timestamp.timestamp()


@dataclass(frozen=True)
class CandleClose:
    """Signals that a candle has closed on a given (symbol, timeframe).

    Emitted by ``CandleCloseDetector`` when a tick's timestamp crosses a
    candle boundary.  Consumers (Phase 3) use this to trigger analysis.

    ``last_tick`` is ``None`` when the close was fired by the detector's
    watchdog timer (no tick arrived to cross the boundary) rather than by
    the tick stream.
    """

    symbol: str
    timeframe: str
    close_time: datetime
    last_tick: Optional[Tick] = None
