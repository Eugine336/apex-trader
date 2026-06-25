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
    # Opportunity-first context: a zone whose direction opposes the HTF bias
    # is NOT dropped — it is tagged so downstream departments can size it down
    # (counter-trend / reversal treatment) rather than blocking it outright.
    is_counter_trend: bool = False
    bias_direction: str = ""  # HTF bias at extraction time ("LONG"/"SHORT"/"")


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
    # ── Single structural R:R guardrail (opportunistic-trading rewire) ────
    # The MARKET decides the trade's reward:risk — targets come from structure
    # ahead (next FVG/OB/liquidity pool), not a hardcoded multiple. This is the
    # ONE guardrail that says "skip a setup where the nearest structural target
    # is closer than the stop" and is the single source of truth shared by the
    # entry gate's TP1/TP2 checks, the entry validator, and the broker's
    # post-adjustment check (which previously each had their own 1.0/1.5/1.8).
    min_structural_rr: float = 1.0
    min_entry_score: int = 85
    # Hard floor for the GateTuner-adjusted entry-score bar.  The tuner may
    # LOWER min_entry_score within a bounded envelope when its rejected setups
    # keep winning, but the live gate never drops the bar below this floor.
    watchlist_score: int = 70
    # Minimum signed HTF alignment for an entry. Alignment is +1 (HTF fully
    # supports the trade) → -1 (HTF fully opposes). A strongly counter-trend
    # setup (alignment below this floor) is rejected at the gate regardless of
    # zone geometry — those entries are immediately closed by management's
    # structure read, so taking them only burns spread. None disables the gate.
    min_htf_alignment: float = -0.5
    # Conviction multiplier applied to a counter-trend zone (its direction
    # opposes the resolved HTF bias). Pure zone geometry (FVG+OB=100, FVG=80,
    # OB=70) ignored whether the setup fought the trend, so a counter-trend
    # FVG+OB scored the same 100 as a with-trend one and sailed through the
    # score≥85 gate. At 0.70 a counter-trend FVG+OB becomes 70 (< 85 gate),
    # naturally filtering geometry-only counter-trend setups.
    counter_trend_conviction_mult: float = 0.70
    coalesce_hz: float = 15.0
