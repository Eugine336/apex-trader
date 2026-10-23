"""Market facts and evidence records for Apex's Gold-only observation layer.

There is intentionally no trade candidate, entry direction, stop/target, rank or
strategy-voting model in this package. A pattern record contains only what was
observed on completed bars; the existing CognitiveBrain interprets the facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import math
from typing import Mapping, Sequence


class Venue(str, Enum):
    MT5 = "MT5"
    DERIV = "DERIV"


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _finite_positive(value: float, label: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")


@dataclass(frozen=True, slots=True)
class Candle:
    """One completed candle whose timestamp is its close time."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        _aware(self.timestamp, "Candle.timestamp")
        for name in ("open", "high", "low", "close"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Candle.{name} must be finite and positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("Candle.high violates OHLC ordering")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("Candle.low violates OHLC ordering")
        if not math.isfinite(float(self.volume)) or self.volume < 0:
            raise ValueError("Candle.volume must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class EventWindow:
    """Verified pre-event range and event window supplied by a calendar adapter."""
    name: str
    starts_at: datetime
    ends_at: datetime
    pre_event_high: float
    pre_event_low: float
    impact: str = "HIGH"

    def __post_init__(self) -> None:
        _aware(self.starts_at, "EventWindow.starts_at")
        _aware(self.ends_at, "EventWindow.ends_at")
        if self.ends_at <= self.starts_at:
            raise ValueError("EventWindow ends_at must be after starts_at")
        _finite_positive(self.pre_event_high, "pre_event_high")
        _finite_positive(self.pre_event_low, "pre_event_low")
        if self.pre_event_high <= self.pre_event_low:
            raise ValueError("EventWindow pre-event high must exceed low")

    def is_active(self, at: datetime) -> bool:
        _aware(at, "event comparison time")
        return self.starts_at <= at <= self.ends_at


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """One coherent quote and completed-candle read from one broker venue."""
    symbol: str
    venue: str | Venue
    observed_at: datetime
    bid: float
    ask: float
    candles: Mapping[str, Sequence[Candle]]
    event_window: EventWindow | None = None
    market_open: bool = True
    data_quality_ok: bool = True

    def __post_init__(self) -> None:
        _aware(self.observed_at, "MarketSnapshot.observed_at")
        if not self.symbol or not self.symbol.strip():
            raise ValueError("MarketSnapshot.symbol is required")
        _finite_positive(float(self.bid), "MarketSnapshot.bid")
        _finite_positive(float(self.ask), "MarketSnapshot.ask")
        if self.ask < self.bid:
            raise ValueError("ask cannot be less than bid")
        venue = self.venue.value if isinstance(self.venue, Venue) else str(self.venue).upper()
        object.__setattr__(self, "venue", venue)
        normalized: dict[str, tuple[Candle, ...]] = {}
        for timeframe, raw_bars in self.candles.items():
            tf = str(timeframe).upper().strip()
            if not tf:
                raise ValueError("Candle timeframe cannot be empty")
            bars = tuple(raw_bars)
            if any(not isinstance(bar, Candle) for bar in bars):
                raise TypeError(f"{tf} candles must contain Candle objects")
            if any(bar.timestamp > self.observed_at for bar in bars):
                raise ValueError(f"{tf} contains a candle from after the snapshot time")
            if any(bars[i].timestamp >= bars[i + 1].timestamp for i in range(len(bars) - 1)):
                raise ValueError(f"{tf} candles must be sorted and timestamps unique")
            normalized[tf] = bars
        object.__setattr__(self, "candles", normalized)
        if self.event_window is not None and self.event_window.ends_at < self.observed_at - timedelta(days=2):
            raise ValueError("EventWindow is stale relative to the market snapshot")

    @property
    def mid(self) -> float:
        return (float(self.bid) + float(self.ask)) / 2.0

    @property
    def spread(self) -> float:
        return float(self.ask) - float(self.bid)

    def bars(self, timeframe: str) -> tuple[Candle, ...]:
        return tuple(self.candles.get(str(timeframe).upper(), ()))

    def latest_bar(self, timeframe: str = "M5") -> Candle | None:
        bars = self.bars(timeframe)
        return bars[-1] if bars else None


@dataclass(frozen=True, slots=True)
class PatternObservation:
    """Factual detector output: no trade direction, order geometry or vote score."""
    symbol: str
    venue: str
    pattern_id: str
    timeframe: str
    observed_at: datetime
    source_bar_time: datetime
    facts: tuple[str, ...]
    measurements: Mapping[str, float | int | str | bool]

    def __post_init__(self) -> None:
        _aware(self.observed_at, "PatternObservation.observed_at")
        _aware(self.source_bar_time, "PatternObservation.source_bar_time")
        if not self.symbol or not self.pattern_id or not self.timeframe:
            raise ValueError("PatternObservation requires symbol, pattern_id and timeframe")
        facts = tuple(str(fact).strip()[:240] for fact in self.facts if str(fact).strip())
        if not facts:
            raise ValueError("PatternObservation requires at least one factual observation")
        object.__setattr__(self, "facts", facts[:12])
        safe: dict[str, float | int | str | bool] = {}
        for key, value in dict(self.measurements).items():
            name = str(key).strip()[:80]
            if not name:
                continue
            if isinstance(value, bool):
                safe[name] = value
            elif isinstance(value, (int, float)):
                number = float(value)
                if math.isfinite(number):
                    safe[name] = number
            elif isinstance(value, str):
                safe[name] = value[:240]
        object.__setattr__(self, "measurements", safe)
