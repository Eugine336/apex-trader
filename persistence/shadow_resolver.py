"""
APEX TRADER — Shadow Resolver (Phase 4)

Replays the REAL TradeManager bar-by-bar over forward price to determine
counterfactual outcomes for rejected/skipped setups.

Design constraints (binding):
  - Uses the production TradeManager.update() — the real management path
  - NO look-ahead: never feeds a bar at or before entry timestamp
  - bar_time seam ensures time-based management logic is faithful
  - Resolution granularity (M1/M5) is stamped on every outcome, never hidden
  - Resolver failure CANNOT crash the trading loop (best-effort)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import pandas as pd
from loguru import logger

from config import get_instrument
from management.trade_manager import (
    ManagedTrade,
    TradeManager,
    TradeStatus,
    TERMINAL_STATUSES,
)
from persistence.shadow_store import (
    ShadowContract,
    ShadowResolution,
    ShadowStore,
)

from runtime_paths import data_dir as _data_dir  # noqa: E402

_DATA_DIR = _data_dir()


def _load_forward_bars(
    symbol: str, after_ms: int, timeframe: str = "M5",
) -> Tuple[Optional[pd.DataFrame], str]:
    """Load bars from CSV strictly after the given timestamp.

    Returns (df, granularity) where granularity records the actual TF used.
    Falls back from M5 → M1 if M5 CSV doesn't exist.
    """
    for tf in [timeframe, "M5", "M1"]:
        csv_path = _DATA_DIR / f"{symbol}_{tf}.csv"
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path)
            if "time" not in df.columns:
                continue
            df["time"] = pd.to_datetime(df["time"], utc=True)
            after_dt = datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)
            df = df[df["time"] > after_dt].reset_index(drop=True)
            if df.empty:
                continue
            return df, tf
        except Exception as exc:
            logger.debug("[ShadowResolver] failed to load CSV {}: {}", csv_path, exc)
            continue
    return None, "NONE"


def _build_managed_trade(contract: ShadowContract) -> ManagedTrade:
    """Construct a ManagedTrade from a shadow contract for replay."""
    try:
        pip_value_per_lot = get_instrument(contract.symbol).pip_value_per_lot
    except (KeyError, AttributeError):
        pip_value_per_lot = 10.0

    entry_dt = datetime.fromtimestamp(
        contract.ts_utc_ms / 1000, tz=timezone.utc,
    )

    return ManagedTrade(
        trade_id=f"shadow_{contract.contract_id}",
        pair=contract.symbol,
        direction=contract.direction,
        entry_price=contract.entry_price,
        current_price=contract.entry_price,
        stop_loss=contract.stop_loss,
        original_stop_loss=contract.stop_loss,
        tp1=contract.tp1,
        tp2=contract.tp2,
        original_tp2=contract.tp2,
        position_size_lots=contract.position_size,
        remaining_size_lots=contract.position_size,
        status=TradeStatus.OPEN,
        pnl_pips=0.0,
        pnl_dollars=0.0,
        entry_time=entry_dt,
        tp1_hit_time=None,
        close_time=None,
        close_reason=None,
        candles_since_entry=0,
        highest_price_since_entry=contract.entry_price,
        lowest_price_since_entry=contract.entry_price,
        trailing_stop=None,
        breakeven_active=False,
        partial_closed=False,
        re_entry_eligible=False,
        score=contract.score,
        pip_size=contract.pip_size,
        pip_value_per_lot=pip_value_per_lot,
        entry_timeframe=contract.entry_timeframe,
        tp3=contract.tp3,
        original_tp3=contract.tp3,
    )


def _determine_intrabar_price(
    bar: pd.Series, trade: ManagedTrade,
) -> float:
    """Determine the effective price for this bar.

    Checks if bar extremes would trigger SL or TP before using close.
    For ambiguous bars (both SL and TP hit), uses conservative assumption
    (SL wins, since we can't determine intra-bar sequence from OHLC).
    """
    is_long = trade.direction.upper() in ("LONG", "BUY")
    sl = trade.stop_loss

    sl_hit = (
        (is_long and bar["low"] <= sl)
        or (not is_long and bar["high"] >= sl)
    )

    tp_level = trade.tp1 if not trade.partial_closed else trade.tp2
    tp_hit = False
    if tp_level and tp_level > 0:
        tp_hit = (
            (is_long and bar["high"] >= tp_level)
            or (not is_long and bar["low"] <= tp_level)
        )

    if sl_hit and tp_hit:
        return sl
    if sl_hit:
        return sl
    if tp_hit:
        return tp_level

    return float(bar["close"])


def _build_m5_window(
    bars_df: pd.DataFrame, current_idx: int, window: int = 50,
) -> pd.DataFrame:
    """Build the trailing M5 DataFrame window the TradeManager expects."""
    start = max(0, current_idx - window + 1)
    return bars_df.iloc[start : current_idx + 1].reset_index(drop=True)


def _classify_outcome(trade: ManagedTrade) -> str:
    """Map terminal TradeStatus + close_reason to a shadow outcome label."""
    reason = (trade.close_reason or "").lower()

    if trade.status == TradeStatus.STOPPED:
        if trade.breakeven_active or "breakeven" in reason:
            return "BREAKEVEN"
        return "LOSS"

    if "tp2" in reason:
        return "WIN"
    if "tp1" in reason:
        return "PARTIAL"
    if "structure" in reason:
        if trade.partial_closed:
            return "PARTIAL"
        return "LOSS"
    if "stall" in reason:
        if trade.pnl_pips > 0:
            return "BREAKEVEN"
        return "LOSS"

    if trade.pnl_pips > 0:
        return "WIN"
    return "LOSS"


def _compute_r_multiple(trade: ManagedTrade) -> float:
    """R-multiple = realised PnL / initial risk distance."""
    risk_distance = abs(trade.entry_price - trade.original_stop_loss)
    if risk_distance == 0:
        return 0.0
    if trade.direction.upper() in ("LONG", "BUY"):
        return (trade.current_price - trade.entry_price) / risk_distance
    else:
        return (trade.entry_price - trade.current_price) / risk_distance


def resolve_contract(
    contract: ShadowContract,
    bars_df: pd.DataFrame,
    granularity: str,
    manager: Optional[TradeManager] = None,
) -> ShadowResolution:
    """Replay TradeManager.update() bar-by-bar over forward price.

    Returns a ShadowResolution with the counterfactual outcome.
    """
    if manager is None:
        manager = TradeManager()

    trade = _build_managed_trade(contract)
    manager._trades[trade.trade_id] = trade

    bars_replayed = 0

    for idx in range(len(bars_df)):
        bar = bars_df.iloc[idx]
        bar_time = bar["time"]
        if not isinstance(bar_time, datetime):
            bar_time = pd.Timestamp(bar_time).to_pydatetime()
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)

        effective_price = _determine_intrabar_price(bar, trade)
        m5_window = _build_m5_window(bars_df, idx)

        trade = manager.update(
            trade,
            current_price=effective_price,
            current_df_m5=m5_window,
            bar_time=bar_time,
        )
        bars_replayed += 1

        if trade.status in TERMINAL_STATUSES:
            outcome = _classify_outcome(trade)
            r_mult = _compute_r_multiple(trade)
            res_ts = int(bar_time.timestamp() * 1000)

            return ShadowResolution(
                outcome=outcome,
                r_multiple=round(r_mult, 4),
                exit_reason=trade.close_reason or "unknown",
                exit_price=trade.current_price,
                resolution_ts=res_ts,
                resolution_granularity=granularity,
                bars_replayed=bars_replayed,
                resolver_meta={
                    "final_status": trade.status.value,
                    "pnl_pips": round(trade.pnl_pips, 2),
                    "partial_closed": trade.partial_closed,
                    "breakeven_active": trade.breakeven_active,
                },
            )

    last_bar = bars_df.iloc[-1]
    last_time = last_bar["time"]
    if not isinstance(last_time, datetime):
        last_time = pd.Timestamp(last_time).to_pydatetime()
    if last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=timezone.utc)

    r_mult = _compute_r_multiple(trade)
    return ShadowResolution(
        outcome="EXPIRED",
        r_multiple=round(r_mult, 4),
        exit_reason="data_exhausted",
        exit_price=trade.current_price,
        resolution_ts=int(last_time.timestamp() * 1000),
        resolution_granularity=granularity,
        bars_replayed=bars_replayed,
        resolver_meta={
            "final_status": trade.status.value,
            "pnl_pips": round(trade.pnl_pips, 2),
            "partial_closed": trade.partial_closed,
            "breakeven_active": trade.breakeven_active,
            "reason": "no_management_or_tp_sl_triggered_within_available_data",
        },
    )


def run_resolver(
    store: Optional[ShadowStore] = None,
    batch_size: int = 50,
) -> Dict[str, int]:
    """Process pending shadow contracts. Returns counts by outcome.

    Best-effort: individual contract failures are logged, never raised.
    """
    if store is None:
        store = ShadowStore()

    pending = store.get_pending(limit=batch_size)
    counts: Dict[str, int] = {}

    for contract in pending:
        try:
            bars_df, granularity = _load_forward_bars(
                contract.symbol, contract.ts_utc_ms,
            )
            if bars_df is None or bars_df.empty:
                store.mark_expired(contract.contract_id, 0)
                counts["NO_DATA"] = counts.get("NO_DATA", 0) + 1
                continue

            resolution = resolve_contract(contract, bars_df, granularity)
            if resolution.outcome == "EXPIRED":
                store.mark_expired(contract.contract_id, resolution.bars_replayed)
            else:
                store.resolve_contract(contract.contract_id, resolution)

            counts[resolution.outcome] = counts.get(resolution.outcome, 0) + 1

        except Exception as exc:
            logger.debug("[ShadowResolver] contract {} failed: {}", contract.contract_id, exc)
            counts["ERROR"] = counts.get("ERROR", 0) + 1

    return counts
