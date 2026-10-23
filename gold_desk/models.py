"""Immutable domain objects for the Gold trading desk.

Contract:
- Candle.timestamp is the *close time* of a completed candle.
- Forming candles must not be passed to a strategy.
- All datetimes must be timezone-aware.
- Prices are broker quote prices; pip/tick assumptions belong in the broker adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import hashlib
import math
from typing import Mapping, Sequence


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


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
    """Pre-event range and verified event window supplied by a calendar adapter.

    This model does not guess release times or event outcomes. The data adapter
    must provide the real event window and the range measured before release.
    """
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
    """One coherent market read consumed by all strategy families."""
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
            # Old event metadata is almost always an adapter bug; do not silently
            # treat stale news as a current catalyst.
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
class TradeIdea:
    """A research candidate, not an executable broker order."""
    symbol: str
    venue: str
    direction: Direction
    strategy_id: str
    entry: float
    stop: float
    target: float
    created_at: datetime
    expires_at: datetime
    source_bar_time: datetime
    atr: float
    quality_score: float
    rationale: str
    evidence: tuple[str, ...] = ()
    supporting_strategies: tuple[str, ...] = ()
    risk_budget_pct: float = 0.25

    def __post_init__(self) -> None:
        _aware(self.created_at, "TradeIdea.created_at")
        _aware(self.expires_at, "TradeIdea.expires_at")
        _aware(self.source_bar_time, "TradeIdea.source_bar_time")
        for name in ("entry", "stop", "target", "atr"):
            _finite_positive(float(getattr(self, name)), f"TradeIdea.{name}")
        if self.expires_at <= self.created_at:
            raise ValueError("TradeIdea must expire after creation")
        if not math.isfinite(self.quality_score) or not 0 <= self.quality_score <= 100:
            raise ValueError("quality_score must be in [0, 100]")
        if not math.isfinite(self.risk_budget_pct) or self.risk_budget_pct <= 0:
            raise ValueError("risk_budget_pct must be finite and positive")
        if self.direction is Direction.LONG:
            if not self.stop < self.entry < self.target:
                raise ValueError("LONG idea requires stop < entry < target")
        elif self.direction is Direction.SHORT:
            if not self.target < self.entry < self.stop:
                raise ValueError("SHORT idea requires target < entry < stop")

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_distance(self) -> float:
        return abs(self.target - self.entry)

    @property
    def reward_to_risk(self) -> float:
        return self.reward_distance / self.risk_distance if self.risk_distance > 0 else 0.0

    @property
    def signal_key(self) -> str:
        """Stable per-bar deduplication key; deliberately independent of tick price."""
        raw = "|".join((
            self.symbol.upper(), self.venue.upper(), self.direction.value,
            self.source_bar_time.isoformat(),
        ))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
