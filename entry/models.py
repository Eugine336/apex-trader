"""APEX TRADER — Entry Plane data models (Phase 7).

Frozen dataclasses for entry zones, gate results, pending entries,
and configuration.  Follows the WorldModel / Intent pattern from
Phases 1 and 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    # ── Single structural R:R guardrail (opportunistic-trading rewire) ────
    # The MARKET decides the trade's reward:risk — targets come from structure
    # ahead (next FVG/OB/liquidity pool), not a hardcoded multiple. This is the
    # ONE guardrail that says "skip a setup where the nearest structural target
    # is closer than the stop". It is the floor used by the entry gate's
    # TP1/TP2 checks; the trigger-engine entry validator and the broker's
    # post-adjustment check use ``RiskConfig.min_risk_reward``, which mirrors
    # this value (both default 1.0) so there is one effective number rather
    # than the old conflicting 1.0/1.5/1.8 floors.
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
    # Absolute permissive cap for the GateTuner-adjusted HTF-alignment floor.
    # The tuner may LOWER min_htf_alignment within a bounded envelope when its
    # rejected counter-HTF setups keep winning (the data proving HTF opposition
    # was not predictive for that instrument), but the live gate never drops the
    # floor below this cap, so a fully-opposed setup (alignment ≈ -1) is never
    # blindly admitted. Loosening-only: the tuner can never make the floor
    # STRICTER than the operator's configured min_htf_alignment.
    min_htf_alignment_floor: float = -1.0
    # Conviction multiplier applied to a counter-trend zone (its direction
    # opposes the resolved HTF bias). Pure zone geometry (FVG+OB=100, FVG=80,
    # OB=70) ignored whether the setup fought the trend, so a counter-trend
    # FVG+OB scored the same 100 as a with-trend one and sailed through the
    # score≥85 gate. At 0.70 a counter-trend FVG+OB becomes 70 (< 85 gate),
    # naturally filtering geometry-only counter-trend setups.
    counter_trend_conviction_mult: float = 0.70
    # ── Opportunistic direction verification (A1) ─────────────────────────
    # A zone's direction is mechanical (a bullish FVG ⇒ LONG). Before committing
    # the entry, momentum is consulted: when the live momentum read for the zone
    # direction is at/below ``-momentum_oppose_threshold`` the setup is fighting
    # the move. The flip to trade WITH the market is confirmed by live
    # tick_momentum (real-time, sub-candle) plus the M5 structural trend — the
    # opposite is taken only when ticks are genuinely moving that way AND the M5
    # trend does not oppose it; otherwise the entry is SKIPPED rather than taken
    # against momentum. 0.2 ≈ "clearly opposing" on the [-1, +1] scale.
    momentum_oppose_threshold: float = 0.20
    # Minimum tick_momentum (signed for the flip direction) to confirm a
    # direction flip. tick_momentum measures directional efficiency of the
    # last ~20 ticks in [-1, +1]; 0.30 requires a clear, clean directional
    # move — not just drift. Combined with the M5 structural trend check
    # so the flip has both real-time price confirmation AND structural backing.
    tick_momentum_flip_threshold: float = 0.30
    # ── Expected-Value gate (Phase 4 — Opportunity Engine) ────────────────
    # APEX is opportunistic: the MARKET decides the side. Instead of asking
    # "does the trade align with the HTF bias?" (a hard directional veto that
    # blocked every counter-trend idea), the gate asks "given the probabilistic
    # evidence and the zone's reward:risk, is the expected value positive
    # enough to take this trade?". EV = p_win × R:R − p_loss, where p_win /
    # p_loss come from the Phase 3 probabilistic bias (long/short probability)
    # mapped to the trade direction.
    #
    # Minimum EV in R-multiples to take a trade. 0.3R means the probability-
    # weighted outcome must exceed 0.3× the risk. Replaces the alignment floor
    # as the directional gate.
    min_entry_ev: float = 0.3
    # NOTE: there is deliberately NO counter-trend EV premium. APEX prefers no
    # direction — the EV gate decides purely on expected value. If a
    # counter-trend idea has lower probability-weighted EV it fails the bar on
    # its own merits; adding a directional surcharge would be a thumb on the
    # scale that contradicts the opportunistic philosophy.
    # When True, the EV gate replaces the alignment-floor gate as the entry
    # plane's directional check (the opportunistic default). Set False to revert
    # to the legacy ``min_htf_alignment`` floor gate.
    ev_gate_enabled: bool = True
    # Counter-trend conviction multiplier used WHEN the EV gate is enabled. The
    # legacy ``counter_trend_conviction_mult`` (0.70) was tuned to filter
    # geometry-only counter-trend setups at the score≥85 gate BEFORE any
    # directional EV check existed. With the EV gate now handling directional
    # risk explicitly, the conviction haircut is softened to 0.85 so a
    # counter-trend FVG+OB (100 × 0.85 = 85) can still clear the score gate and
    # reach the EV gate, which decides on its merits rather than blocking it on
    # geometry alone.
    counter_trend_conviction_mult_ev: float = 0.85
    coalesce_hz: float = 15.0
