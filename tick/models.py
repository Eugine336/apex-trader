"""APEX TRADER — Tick data models (Phase 2).

Frozen dataclasses for tick events and candle-close events.
These are the currency of the event-driven architecture: the Tick Router
emits ``Tick`` objects; the ``CandleCloseDetector`` derives ``CandleClose``
events from them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


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
    # Set only by a broker adapter that preserves and validates the provider's
    # original quote timestamp. Receipt/analysis timestamps are not source time.
    source_timestamp_verified: bool = False
    quote_kind: str = "unknown"
    spread_unit: str = "unknown"
    raw_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        timestamp = self.timestamp
        tzinfo = getattr(timestamp, "tzinfo", None)
        try:
            aware = tzinfo is not None and timestamp.utcoffset() is not None
        except (AttributeError, TypeError, ValueError, OverflowError):
            aware = False
        if aware:
            object.__setattr__(self, "timestamp", timestamp.astimezone(timezone.utc))
        object.__setattr__(
            self,
            "source_timestamp_verified",
            self.source_timestamp_verified is True and aware,
        )
        quote_kind = str(self.quote_kind or "unknown").strip().lower()
        if quote_kind not in {"bid_ask", "mid_only"}:
            quote_kind = "unknown"
        object.__setattr__(self, "quote_kind", quote_kind)
        spread_unit = str(self.spread_unit or "unknown").strip().lower()
        if spread_unit not in {"pips", "ticks", "points", "unknown"}:
            spread_unit = "unknown"
        object.__setattr__(self, "spread_unit", spread_unit)
        object.__setattr__(
            self,
            "raw_fields",
            dict(self.raw_fields) if isinstance(self.raw_fields, dict) else {},
        )

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
