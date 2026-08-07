"""
Trade Planner data models.

`TradePlanContext` carries every advisor's analysis — scanner, decision
engine, RL, adaptive/ML, portfolio, timing and market state — without any
verdict.  The planner consumes it and produces a `TradePlan`: a complete,
structured, self-explaining trade plan rather than a yes/no answer.

Leaf module — depends only on the standard library so it can be imported
from anywhere without circular-import risk.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TradePlanContext:
    """Everything the planner knows about a potential trade.

    Populated from all advisor layers.  No advisor produces a verdict here —
    they each contribute their analysis and the planner reasons over the whole
    picture.
    """

    # ── Instrument ───────────────────────────────────────────────────────
    symbol: str = ""
    pip_size: float = 0.0001
    spread_pips: float = 0.0
    atr_pips: float = 0.0
    current_price: float = 0.0

    # ── Scanner ──────────────────────────────────────────────────────────
    direction: str = ""              # "BUY"/"LONG" or "SELL"/"SHORT"
    scanner_score: float = 0.0       # 0–100
    zone_type: str = ""              # "ORDER_BLOCK", "FVG", etc.
    zone_quality: float = 0.0        # 0–1
    zone_entry_price: float = 0.0    # planned entry (zone midpoint)

    # ── Layered-decision quality (carried from the scanner) ──────────────
    # OQ/EQ gate READY in the scanner; carrying them through lets the planner
    # reason over market-condition quality (OQ) and entry geometry (EQ)
    # instead of being blind to the signals that qualified the trade.
    # scan_timestamp lets the planner judge how stale the scanner_score is.
    oq: float = 0.0                  # Opportunity Quality (0–10)
    eq: float = 0.0                  # Entry Quality (0–10)
    scan_timestamp: float = 0.0      # epoch seconds when the scan was performed

    # ── Decision Engine (sub-scores, not a verdict) ──────────────────────
    de_htf_score: float = 0.0
    de_structure_score: float = 0.0
    de_momentum_score: float = 0.0
    de_volume_score: float = 0.0
    de_session_score: float = 0.0
    de_spread_score: float = 0.0
    de_confidence: float = 0.0       # overall 0–1
    de_tf_alignment: float = 0.0     # signed −1..+1 directional read

    # ── Situation Engine raw dimensions (not collapsed to conviction) ────
    # The decision engine compresses these into a single conviction float;
    # carrying the raw dimensions lets the planner reason about *why*
    # conviction is high or low (e.g. strong structure but weak momentum).
    sa_tf_alignment: float = 0.0         # signed −1..+1
    sa_momentum: float = 0.0             # signed −1..+1
    sa_structure_integrity: float = 0.0  # 0..1
    sa_read_confidence: float = 0.0      # 0..1

    # ── RL Model ─────────────────────────────────────────────────────────
    rl_action: int = 0               # 0=HOLD, 1=BUY, 2=SELL, 3=CLOSE
    rl_confidence: float = 0.0
    rl_expected_r: float = 0.0
    rl_value_estimate: float = 0.0
    rl_stage: int = 1

    # ── Adaptive / ML ────────────────────────────────────────────────────
    pair_multiplier: float = 1.0
    ev_estimate: float = 0.0
    pair_win_rate: float = 0.5
    session_win_rate: float = 0.5

    # ── Regime-adaptive trade shaping (RegimeLearner; neutral until confident) ──
    # Applied inside the planner: regime_tp_mult scales the TP R-multiples,
    # regime_sl_buffer_pips is an additive pip delta on the ATR stop ONLY
    # (applied pre-sizing so risk stays correct), and regime_runner_pct
    # (None = use planner default) sets how much of the position runs past TP1.
    regime_tp_mult: float = 1.0
    regime_sl_buffer_pips: float = 0.0
    regime_runner_pct: Optional[float] = None

    # ── Proposed levels (from entry engine) ──────────────────────────────
    proposed_sl_price: float = 0.0
    proposed_sl_pips: float = 0.0
    proposed_tp1_price: float = 0.0
    proposed_tp2_price: float = 0.0
    risk_reward_1: float = 0.0
    risk_reward_2: float = 0.0
    structure_sl_available: bool = False
    micro_confirmation: str = ""
    brain_entry_mode: str = "PENDING"  # MARKET/PENDING from entry engine

    # ── Portfolio state ──────────────────────────────────────────────────
    open_positions: int = 0
    correlated_exposure: float = 0.0  # 0–1
    portfolio_heat_pct: float = 0.0   # current total open risk as % of equity
    daily_pnl_r: float = 0.0
    max_positions: int = 5
    # Lightweight book of open positions as (symbol, direction) tuples — used
    # by the Portfolio Governor for currency/sector/correlation checks. Not
    # serialised in to_dict (kept out of the journal payload).
    open_position_book: list = field(default_factory=list)

    # ── Timing ───────────────────────────────────────────────────────────
    session: str = "UNKNOWN"
    day_of_week: int = 0             # 0=Monday
    minutes_to_session_change: int = 999
    is_news_window: bool = False
    minutes_to_news: float = 999.0

    # ── Risk context ─────────────────────────────────────────────────────
    account_balance: float = 0.0
    base_risk_pct: float = 0.5
    current_drawdown_pct: float = 0.0

    # ── Situation label (from SituationEngine, for journaling) ───────────
    situation_label: str = "UNKNOWN"

    @property
    def is_long(self) -> bool:
        return self.direction.upper() in ("BUY", "LONG")

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "scanner_score": round(self.scanner_score, 2),
            "oq": round(self.oq, 2),
            "eq": round(self.eq, 2),
            "zone_type": self.zone_type,
            "zone_quality": round(self.zone_quality, 3),
            "atr_pips": round(self.atr_pips, 2),
            "spread_pips": round(self.spread_pips, 2),
            "de_confidence": round(self.de_confidence, 3),
            "de_tf_alignment": round(self.de_tf_alignment, 3),
            "sa_tf_alignment": round(self.sa_tf_alignment, 3),
            "sa_momentum": round(self.sa_momentum, 3),
            "sa_structure_integrity": round(self.sa_structure_integrity, 3),
            "sa_read_confidence": round(self.sa_read_confidence, 3),
            "rl_action": self.rl_action,
            "rl_confidence": round(self.rl_confidence, 3),
            "rl_expected_r": round(self.rl_expected_r, 3),
            "rl_stage": self.rl_stage,
            "pair_multiplier": round(self.pair_multiplier, 3),
            "ev_estimate": round(self.ev_estimate, 4),
            "pair_win_rate": round(self.pair_win_rate, 3),
            "session_win_rate": round(self.session_win_rate, 3),
            "risk_reward_2": round(self.risk_reward_2, 2),
            "proposed_sl_pips": round(self.proposed_sl_pips, 2),
            "structure_sl_available": self.structure_sl_available,
            "open_positions": self.open_positions,
            "correlated_exposure": round(self.correlated_exposure, 3),
            "portfolio_heat_pct": round(self.portfolio_heat_pct, 3),
            "daily_pnl_r": round(self.daily_pnl_r, 3),
            "session": self.session,
            "day_of_week": self.day_of_week,
            "minutes_to_session_change": self.minutes_to_session_change,
            "is_news_window": self.is_news_window,
            "minutes_to_news": round(self.minutes_to_news, 1),
            "current_drawdown_pct": round(self.current_drawdown_pct, 3),
            "situation_label": self.situation_label,
        }


@dataclass
class TradePlan:
    """A complete, structured trade plan — the planner's rich verdict."""

    # ── Decision ─────────────────────────────────────────────────────────
    action: str = "SKIP"             # "ENTER", "WAIT", "SKIP"
    direction: str = ""              # "BUY", "SELL"

    # ── Entry ────────────────────────────────────────────────────────────
    entry_mode: str = "MARKET"       # "MARKET", "LIMIT", "STOP"
    entry_price: Optional[float] = None
    wait_reason: Optional[str] = None
    wait_until_minutes: Optional[int] = None

    # ── Stop Loss ────────────────────────────────────────────────────────
    sl_strategy: str = "structure"   # "structure", "atr", "swing", "fixed_rr"
    sl_price: float = 0.0
    sl_pips: float = 0.0

    # ── Take Profit ──────────────────────────────────────────────────────
    tp_strategy: str = "partial_trail"  # "fixed_rr","structure","trail_only","partial_trail"
    tp1_price: Optional[float] = None
    tp1_rr: float = 0.0
    tp2_price: Optional[float] = None
    tp2_rr: Optional[float] = None
    runner_pct: float = 0.0          # fraction left as runner (0–1)

    # ── Sizing ───────────────────────────────────────────────────────────
    risk_pct: float = 0.0
    size_reasoning: str = ""

    # ── Management ───────────────────────────────────────────────────────
    be_trigger_r: float = 0.5
    trail_strategy: str = "swing"    # "swing", "atr", "none"
    trail_activation_r: float = 1.0
    scale_in_allowed: bool = False

    # ── Meta ─────────────────────────────────────────────────────────────
    confidence: float = 0.0          # 0–1
    reasoning: str = ""
    advisor_agreement: float = 0.0   # 0–1
    governor_blocked_by: Optional[str] = None  # which governor check blocked, if any
    # #24 — portfolio-governor accumulated-risk dimmer. When the governor runs in
    # graded mode it no longer hard-blocks an analytical concentration limit
    # (currency / sector / correlated); it allows the trade carrying this bounded
    # [floor, 1.0] multiplier, which the orchestrator folds into size so the
    # near-/over-limit concentration sizes DOWN instead of opening at full size.
    # 1.0 = clear of every analytical limit (or governor not graded).
    governor_risk_multiplier: float = 1.0
    # Phase 9 gate softening: bounded [gate_floor, 1.0] quality multiplier set
    # when the planner's conviction floor was softened (orchestrator live) — the
    # setup flows as ENTER carrying this factor instead of SKIP, and the
    # orchestrator folds it into graded size. 1.0 = conviction gate passed clean.
    gate_quality_multiplier: float = 1.0
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=_now_iso)

    @property
    def should_enter(self) -> bool:
        return self.action == "ENTER"

    @property
    def is_market(self) -> bool:
        return self.entry_mode == "MARKET"

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "direction": self.direction,
            "entry_mode": self.entry_mode,
            "entry_price": self.entry_price,
            "wait_reason": self.wait_reason,
            "wait_until_minutes": self.wait_until_minutes,
            "sl_strategy": self.sl_strategy,
            "sl_price": self.sl_price,
            "sl_pips": round(self.sl_pips, 2),
            "tp_strategy": self.tp_strategy,
            "tp1_price": self.tp1_price,
            "tp1_rr": round(self.tp1_rr, 2),
            "tp2_price": self.tp2_price,
            "tp2_rr": round(self.tp2_rr, 2) if self.tp2_rr is not None else None,
            "runner_pct": round(self.runner_pct, 2),
            "risk_pct": round(self.risk_pct, 4),
            "size_reasoning": self.size_reasoning,
            "be_trigger_r": round(self.be_trigger_r, 2),
            "trail_strategy": self.trail_strategy,
            "trail_activation_r": round(self.trail_activation_r, 2),
            "scale_in_allowed": self.scale_in_allowed,
            "confidence": round(self.confidence, 3),
            "advisor_agreement": round(self.advisor_agreement, 3),
            "governor_blocked_by": self.governor_blocked_by,
            "gate_quality_multiplier": round(self.gate_quality_multiplier, 4),
            "reasoning": self.reasoning,
        }
