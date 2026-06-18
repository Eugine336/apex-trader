"""APEX TRADER — Entry Plane data models (Phase 7).

Frozen dataclasses for entry zones, gate results, pending entries,
and configuration.  Follows the WorldModel / Intent pattern from
Phases 1 and 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ZoneType(str, Enum):
    FVG_OB_OVERLAP = "FVG_OB_OVERLAP"
    FVG_MIDPOINT = "FVG_MIDPOINT"
    OB_MIDPOINT = "OB_MIDPOINT"


class EntryState(str, Enum):
    WATCHING = "WATCHING"
    ZONE_TOUCHED = "ZONE_TOUCHED"
    CONFIRMING_M1 = "CONFIRMING_M1"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class EntryZone:
    """A price zone where an entry is valid, extracted from WorldModel."""

    symbol: str
    direction: str  # "LONG" or "SHORT"
    zone_type: ZoneType
    top: float
    bottom: float
    midpoint: float
    invalidation_level: float
    conviction: int  # score from scanner
    created_at: datetime
    expires_at: datetime
    timeframe: str
    has_sweep: bool = False


@dataclass(frozen=True)
class GateResult:
    """Outcome of a single validation gate."""

    passed: bool
    gate_name: str
    reason: str


@dataclass(frozen=True)
class PendingEntry:
    """Tracks an entry from zone-touch through M1 confirmation."""

    symbol: str
    direction: str
    zone: EntryZone
    touch_price: float
    touch_time: datetime
    state: EntryState
    m1_candles_seen: int = 0
    confirmed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None


@dataclass
class EntryConfig:
    """Tunable parameters for the entry plane."""

    zone_proximity_pips: float = 3.0
    m1_confirmation_timeout_candles: int = 5
    m1_min_bars: int = 10
    zone_expiry_seconds: float = 900.0  # 15 minutes
    max_concurrent_pending: int = 5
    max_spread_multiplier: float = 3.0
    min_risk_reward: float = 1.5
    min_entry_score: int = 65
    coalesce_hz: float = 15.0
