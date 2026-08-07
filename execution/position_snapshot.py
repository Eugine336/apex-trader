"""APEX TRADER — Position Snapshot (Phase 4).

A frozen, point-in-time copy of a ManagedPosition plus its associated
TradeManager ManagedTrade state.  Workers receive snapshots so they never
touch live mutable state — all broker interaction is deferred to the
Action Executor (Phase 6) via Intent objects.

Built from ``ManagedPosition`` + ``ManagedTrade`` each cycle; the snapshot
is discarded after evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def _pos_pnl(pos) -> float:
    """Broker-reported P&L, falling back to legacy attribute names."""
    v = getattr(pos, "pnl", None)
    if v is None:
        v = getattr(pos, "broker_pnl", None)
    return float(v) if v is not None else 0.0


def _pos_lots(pos) -> float:
    """Broker-reported lots, falling back to legacy ``broker_lots``."""
    v = getattr(pos, "lots", None)
    if not v:
        v = getattr(pos, "broker_lots", None)
    return float(v) if v else 0.0


def _pos_entry(pos) -> float:
    """Broker open price, falling back to legacy ``entry_price``."""
    v = getattr(pos, "open_price", None)
    if not v:
        v = getattr(pos, "entry_price", None)
    return float(v) if v else 0.0


def _pos_tp(pos) -> float:
    """Broker take-profit (single broker TP == tp1), falling back to ``tp1``."""
    v = getattr(pos, "tp", None)
    if not v:
        v = getattr(pos, "tp1", None)
    return float(v) if v else 0.0


@dataclass(frozen=True)
class PositionSnapshot:
    """Immutable point-in-time view of a single open position.

    Combines data from ``ManagedPosition`` (broker-level tracking) and
    ``ManagedTrade`` (management-level state: status, trailing, partials).
    """

    # ── Identity ──────────────────────────────────────────────────────
    order_id: str
    platform: str
    symbol: str
    direction: str

    # ── Entry ─────────────────────────────────────────────────────────
    entry_price: float
    lots: float
    remaining_lots: float
    open_time: datetime
    score: int

    # ── Levels ────────────────────────────────────────────────────────
    sl: float
    sl_original: float
    tp1: float
    tp2: float
    tp2_original: float
    tp3: Optional[float] = None

    # ── State flags ───────────────────────────────────────────────────
    tp1_hit: bool = False
    tp3_hit: bool = False
    at_breakeven: bool = False
    trailing: bool = False
    partial_closed: bool = False
    re_entry_eligible: bool = False
    # True while a worker-path SL move is in flight to the broker but not yet
    # confirmed. The optimistic mutation writes the new (unconfirmed) level into
    # the management state, so the synthetic stop-hit check must skip this cycle
    # — otherwise a broker-rejected SL ("Invalid stops") could trigger a phantom
    # stop-loss CLOSE before the rollback restores the real stop.
    sl_pending_confirmation: bool = False

    # ── Live data (from broker / last tick) ───────────────────────────
    current_price: float = 0.0
    broker_pnl: float = 0.0
    broker_lots: float = 0.0

    # ── Broker volume constraints ─────────────────────────────────────
    # Minimum tradeable lot size for this symbol. A position already at this
    # size cannot be partially closed (the executor would compute a zero close
    # volume), so TP1 must fall back to SL→breakeven protection instead.
    volume_min: float = 0.01

    # ── P&L (from TradeManager) ───────────────────────────────────────
    pnl_pips: float = 0.0
    pnl_dollars: float = 0.0
    pip_size: float = 0.0001
    pip_value_per_lot: float = 10.0

    # ── Timing ────────────────────────────────────────────────────────
    candles_since_entry: int = 0
    entry_timeframe: str = "M5"

    # ── Extremes ──────────────────────────────────────────────────────
    highest_since_entry: float = 0.0
    lowest_since_entry: float = 0.0

    # ── Trade status string (from TradeManager) ──────────────────────
    trade_status: str = "OPEN"
    tm_trade_id: str = ""

    # ── Trade Planner overrides ──────────────────────────────────────
    plan_be_trigger_r: Optional[float] = None
    plan_trail_activation_r: Optional[float] = None
    plan_trail_strategy: Optional[str] = None
    plan_partial_ratio: Optional[float] = None

    # ── Strategic assessment ─────────────────────────────────────────
    strategic_structure_integrity: Optional[float] = None
    strategic_tf_alignment: Optional[float] = None
    strategic_assessment_time: Optional[datetime] = None

    # ── Deriv-specific ───────────────────────────────────────────────
    stake_usd: float = 0.0
    multiplier: int = 100

    # ── Scores history (for conviction collapse) ─────────────────────
    score_history: tuple[int, ...] = ()

    # ── Confluences ──────────────────────────────────────────────────
    confluences: tuple[str, ...] = ()

    # ── Scale-in ─────────────────────────────────────────────────────
    scale_in_count: int = 0

    @property
    def is_long(self) -> bool:
        return self.direction.upper() in ("BUY", "LONG")

    @property
    def risk_pips(self) -> float:
        if self.pip_size <= 0:
            return 0.0
        return abs(self.entry_price - self.sl_original) / self.pip_size

    @property
    def pnl_r(self) -> float:
        rp = self.risk_pips
        if rp < 1e-9:
            return 0.0
        return self.pnl_pips / rp


def build_position_snapshot(
    pos,
    tm_trade=None,
    current_price: float = 0.0,
    score_history: tuple[int, ...] = (),
) -> PositionSnapshot:
    """Build a PositionSnapshot from a ManagedPosition + optional ManagedTrade.

    Accepts the live mutable objects and freezes their state. Safe to call
    from the trading loop thread — the returned snapshot is immutable.
    """
    tp1 = _pos_tp(pos)
    tp2 = getattr(pos, "tp2", 0.0) or 0.0
    sl = getattr(pos, "sl", 0.0) or 0.0

    sl_original = sl
    tp2_original = tp2
    remaining_lots = _pos_lots(pos)
    pnl_pips = 0.0
    pnl_dollars = 0.0
    pip_size = 0.0001
    pip_value_per_lot = 10.0
    candles_since_entry = 0
    entry_timeframe = "M5"
    highest = current_price
    lowest = current_price
    trade_status = "OPEN"
    tm_trade_id = getattr(pos, "tm_trade_id", "") or ""
    tp3 = None
    tp3_hit = False
    partial_closed = getattr(pos, "tp1_hit", False)
    plan_be_trigger_r = None
    plan_trail_activation_r = None
    plan_trail_strategy = None
    plan_partial_ratio = None
    strat_integrity = None
    strat_align = None
    strat_time = None

    if tm_trade is not None:
        sl_original = getattr(tm_trade, "original_stop_loss", sl)
        tp2_original = getattr(tm_trade, "original_tp2", tp2)
        remaining_lots = getattr(tm_trade, "remaining_size_lots", remaining_lots)
        pnl_pips = getattr(tm_trade, "pnl_pips", 0.0) or 0.0
        pnl_dollars = getattr(tm_trade, "pnl_dollars", 0.0) or 0.0
        pip_size = getattr(tm_trade, "pip_size", 0.0001) or 0.0001
        pip_value_per_lot = getattr(tm_trade, "pip_value_per_lot", 10.0) or 10.0
        candles_since_entry = getattr(tm_trade, "candles_since_entry", 0)
        entry_timeframe = getattr(tm_trade, "entry_timeframe", "M5")
        highest = getattr(tm_trade, "highest_price_since_entry", current_price)
        lowest = getattr(tm_trade, "lowest_price_since_entry", current_price)
        trade_status = getattr(tm_trade, "status", "OPEN")
        if hasattr(trade_status, "value"):
            trade_status = trade_status.value
        tp3 = getattr(tm_trade, "tp3", None)
        tp3_hit = getattr(tm_trade, "tp3_hit", False)
        partial_closed = getattr(tm_trade, "partial_closed", False)
        plan_be_trigger_r = getattr(tm_trade, "plan_be_trigger_r", None)
        plan_trail_activation_r = getattr(tm_trade, "plan_trail_activation_r", None)
        plan_trail_strategy = getattr(tm_trade, "plan_trail_strategy", None)
        plan_partial_ratio = getattr(tm_trade, "plan_partial_ratio", None)
        strat_integrity = getattr(tm_trade, "strategic_structure_integrity", None)
        strat_align = getattr(tm_trade, "strategic_tf_alignment", None)
        strat_time = getattr(tm_trade, "strategic_assessment_time", None)
        sl = getattr(tm_trade, "stop_loss", sl)
        tp1 = getattr(tm_trade, "tp1", tp1)
        tp2 = getattr(tm_trade, "tp2", tp2)

    return PositionSnapshot(
        order_id=getattr(pos, "order_id", ""),
        platform=getattr(pos, "platform", ""),
        symbol=getattr(pos, "symbol", ""),
        direction=getattr(pos, "direction", ""),
        entry_price=_pos_entry(pos),
        lots=_pos_lots(pos),
        remaining_lots=remaining_lots,
        open_time=getattr(pos, "open_time", datetime.now(timezone.utc)),
        score=getattr(pos, "score", 0),
        sl=sl,
        sl_original=sl_original,
        tp1=tp1,
        tp2=tp2,
        tp2_original=tp2_original,
        tp3=tp3,
        tp1_hit=getattr(pos, "tp1_hit", False),
        tp3_hit=tp3_hit,
        at_breakeven=getattr(pos, "at_breakeven", False),
        trailing=getattr(pos, "trailing", False),
        partial_closed=partial_closed,
        re_entry_eligible=getattr(pos, "re_entry_eligible", False),
        sl_pending_confirmation=getattr(tm_trade, "sl_pending_confirmation", False),
        current_price=current_price,
        broker_pnl=_pos_pnl(pos),
        broker_lots=_pos_lots(pos),
        volume_min=float(getattr(pos, "volume_min", 0.01) or 0.01),
        pnl_pips=pnl_pips,
        pnl_dollars=pnl_dollars,
        pip_size=pip_size,
        pip_value_per_lot=pip_value_per_lot,
        candles_since_entry=candles_since_entry,
        entry_timeframe=entry_timeframe,
        highest_since_entry=highest,
        lowest_since_entry=lowest,
        trade_status=trade_status,
        tm_trade_id=tm_trade_id,
        plan_be_trigger_r=plan_be_trigger_r,
        plan_trail_activation_r=plan_trail_activation_r,
        plan_trail_strategy=plan_trail_strategy,
        plan_partial_ratio=plan_partial_ratio,
        strategic_structure_integrity=strat_integrity,
        strategic_tf_alignment=strat_align,
        strategic_assessment_time=strat_time,
        stake_usd=getattr(pos, "stake_usd", 0.0),
        multiplier=getattr(pos, "multiplier", 100),
        score_history=tuple(score_history) if score_history else (),
        confluences=tuple(getattr(pos, "confluences", [])),
        scale_in_count=getattr(pos, "scale_in_count", 0),
    )
