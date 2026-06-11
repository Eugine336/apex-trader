"""
Trade context — everything the decision engine knows about one trade.
Rich, uncompressed. Every dimension the analysis layer produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeContext:
    """Full context for an open-trade management decision."""

    # ── Position identity ────────────────────────────────────────────────
    symbol: str = ""
    order_id: str = ""
    direction: str = ""           # "BUY" or "SELL"
    entry_type: str = ""          # "APEX_ENTRY", "ORPHAN_ADOPTED", etc.

    # ── Price state ──────────────────────────────────────────────────────
    entry_price: float = 0.0
    current_price: float = 0.0
    current_sl: float = 0.0
    pnl_pips: float = 0.0
    pnl_dollars: float = 0.0
    hold_minutes: float = 0.0

    # ── Position lifecycle ───────────────────────────────────────────────
    at_breakeven: bool = False
    tp1_hit: bool = False
    trailing: bool = False
    partial_closed: bool = False
    lots: float = 0.0
    original_risk_pips: float = 0.0

    # ── Scanner re-score ─────────────────────────────────────────────────
    scan_score: int = 0
    scan_direction: str = ""      # "LONG", "SHORT", "NEUTRAL"

    # ── Multi-timeframe structure ────────────────────────────────────────
    d1_trend: str = "UNKNOWN"
    d1_confidence: float = 0.0
    d1_event: str = "NONE"
    d1_swing_high: Optional[float] = None
    d1_swing_low: Optional[float] = None

    h4_trend: str = "UNKNOWN"
    h4_confidence: float = 0.0
    h4_event: str = "NONE"
    h4_swing_high: Optional[float] = None
    h4_swing_low: Optional[float] = None

    h1_trend: str = "UNKNOWN"
    h1_confidence: float = 0.0
    h1_event: str = "NONE"
    h1_swing_high: Optional[float] = None
    h1_swing_low: Optional[float] = None
    h1_last_candle_bearish: Optional[bool] = None
    h1_last_candle_doji: bool = False

    m1_trend: str = "UNKNOWN"
    m1_confidence: float = 0.0
    m1_event: str = "NONE"
    m1_aligned_count: int = 0     # 0-5 candles aligned

    # ── Score history ────────────────────────────────────────────────────
    score_history: list[int] = field(default_factory=list)

    # ── Portfolio context ────────────────────────────────────────────────
    open_trade_count: int = 0
    max_open_trades: int = 5
    portfolio_heat_pct: float = 0.0

    # ── Session / News ───────────────────────────────────────────────────
    session_name: str = "UNKNOWN"
    session_tradeable: bool = True
    minutes_to_high_impact_news: float = 999.0
    news_impact: str = "NONE"

    # ── Pressure (from _compute_in_trade_context_pressure) ───────────────
    context_pressure: int = 0
    opposing_boost: int = 0
    pressure_details: list[str] = field(default_factory=list)

    # ── Confluences from scan ────────────────────────────────────────────
    confluences: list[str] = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.direction.upper() in ("BUY", "LONG")

    @property
    def is_adopted(self) -> bool:
        return self.entry_type == "ORPHAN_ADOPTED"

    @property
    def profit_r(self) -> float:
        if self.original_risk_pips < 1e-8:
            return 0.0
        return self.pnl_pips / self.original_risk_pips


@dataclass
class EntryContext:
    """Full context for an entry decision — rich, uncompressed."""

    # ── Signal identity ──────────────────────────────────────────────────
    symbol: str = ""
    direction: str = ""           # "LONG" or "SHORT"
    scan_score: int = 0
    scan_direction: str = ""

    # ── Entry zone quality ───────────────────────────────────────────────
    entry_type: str = ""          # "FVG_MIDPOINT", "OB_MIDPOINT", etc.
    entry_price: float = 0.0
    stop_loss: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    risk_reward_1: float = 0.0
    risk_reward_2: float = 0.0
    risk_pips: float = 0.0
    entry_mode: str = "PENDING"   # from EntryEngine
    micro_confirmation: str = ""

    # ── Position sizing inputs ───────────────────────────────────────────
    base_lots: float = 0.0
    account_balance: float = 0.0
    risk_pct: float = 0.0

    # ── Multi-timeframe structure ────────────────────────────────────────
    d1_trend: str = "UNKNOWN"
    d1_confidence: float = 0.0
    d1_event: str = "NONE"

    h4_trend: str = "UNKNOWN"
    h4_confidence: float = 0.0
    h4_event: str = "NONE"

    h1_trend: str = "UNKNOWN"
    h1_confidence: float = 0.0
    h1_event: str = "NONE"

    m1_trend: str = "UNKNOWN"
    m1_confidence: float = 0.0
    m1_event: str = "NONE"
    m1_aligned_count: int = 0

    # ── Session / News ───────────────────────────────────────────────────
    session_name: str = "UNKNOWN"
    session_tradeable: bool = True
    minutes_to_high_impact_news: float = 999.0
    news_impact: str = "NONE"

    # ── Portfolio context ────────────────────────────────────────────────
    open_trade_count: int = 0
    max_open_trades: int = 5
    portfolio_heat_pct: float = 0.0
    current_spread: float = 0.0
    typical_spread: float = 0.0

    # ── Regime / adaptive ────────────────────────────────────────────────
    regime: str = ""
    ev_estimate: float = 0.0
    pair_multiplier: float = 1.0

    # ── Confluences from scan ────────────────────────────────────────────
    confluences: list[str] = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.direction.upper() in ("BUY", "LONG")
