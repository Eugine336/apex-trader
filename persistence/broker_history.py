"""
APEX TRADER — Broker closed-trade history ingest (#10).

Pulls the account's broker-side closed-trade history (MT5 deals) and writes each
completed round-trip into the event store for reporting / equity reconstruction
/ audit. Idempotent by position_id.

IMPORTANT: this data is deliberately NOT fed into the adaptive learners. External
/ pre-bot trades carry none of the confluence / regime / session labels those
learners require, so feeding them would pollute the score/pair/regime models
(the same reason rejected-setup shadows are kept out of the learners). It is an
accounting / visibility source only.

Leaf module — standard library + loguru only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger

BROKER_HISTORY_IMPORT = "BROKER_HISTORY_IMPORT"

# MT5 deal-entry flags (deal.entry)
_ENTRY_OUT = 1
_ENTRY_OUT_BY = 3
# MT5 deal type (deal.type)
_DEAL_TYPE_SELL = 1


def group_into_roundtrips(deals: list[dict]) -> list[dict]:
    """Group raw broker deals into completed round-trip trades by position_id.

    A round-trip is emitted only once it has a closing (OUT) deal; still-open
    positions are skipped — the live reconciler owns those. Net P&L sums the
    gross profit plus commission, swap and fees across all deals in the
    position. Pure / deterministic / testable.
    """
    by_pos: dict[Any, list[dict]] = {}
    for d in deals:
        pid = d.get("position_id") or d.get("order") or d.get("ticket")
        if pid:
            by_pos.setdefault(pid, []).append(d)

    roundtrips: list[dict] = []
    for pid, group in by_pos.items():
        group.sort(key=lambda x: x.get("time", 0))
        has_close = any(d.get("entry") in (_ENTRY_OUT, _ENTRY_OUT_BY) for d in group)
        if not has_close:
            continue  # position still open

        ins = [d for d in group if d.get("entry") not in (_ENTRY_OUT, _ENTRY_OUT_BY)]
        outs = [d for d in group if d.get("entry") in (_ENTRY_OUT, _ENTRY_OUT_BY)]
        entry_deal = ins[0] if ins else group[0]
        exit_deal = outs[-1] if outs else group[-1]

        gross = sum(float(d.get("profit", 0.0) or 0.0) for d in group)
        commission = sum(float(d.get("commission", 0.0) or 0.0) for d in group)
        swap = sum(float(d.get("swap", 0.0) or 0.0) for d in group)
        fee = sum(float(d.get("fee", 0.0) or 0.0) for d in group)

        roundtrips.append({
            "position_id": pid,
            "symbol": entry_deal.get("symbol") or exit_deal.get("symbol") or "",
            "direction": "SELL" if entry_deal.get("type") == _DEAL_TYPE_SELL else "BUY",
            "volume": float(entry_deal.get("volume", 0.0) or 0.0),
            "open_time": int(entry_deal.get("time", 0) or 0),
            "close_time": int(exit_deal.get("time", 0) or 0),
            "open_price": float(entry_deal.get("price", 0.0) or 0.0),
            "close_price": float(exit_deal.get("price", 0.0) or 0.0),
            "gross_profit": round(gross, 2),
            "commission": round(commission, 2),
            "swap": round(swap, 2),
            "fee": round(fee, 2),
            "net_pnl": round(gross + commission + swap + fee, 2),
            "magic": entry_deal.get("magic", 0),
        })
    return roundtrips


def import_broker_history(
    connector,
    event_store,
    account_key: str = "",
    lookback_days: int = 365,
    now: Optional[datetime] = None,
) -> int:
    """Backfill the account's closed-trade history into the event store.

    Idempotent: each round-trip is keyed by ``brokerhist-<account>-<position_id>``
    via the event correlation_id, and already-imported ids are skipped, so it is
    safe to run on every startup. Returns the number of NEW trades imported.
    """
    if connector is None or event_store is None:
        return 0
    if not hasattr(connector, "get_deal_history"):
        return 0

    now = now or datetime.now(timezone.utc)
    frm = now - timedelta(days=max(1, lookback_days))
    try:
        deals = connector.get_deal_history(frm, now)
    except Exception as exc:
        logger.warning("[broker_history] deal fetch failed: {}", exc)
        return 0

    roundtrips = group_into_roundtrips(deals or [])
    if not roundtrips:
        return 0

    already: set[str] = set()
    try:
        existing = event_store.query(event_type=BROKER_HISTORY_IMPORT, limit=1_000_000)
        for e in existing:
            cid = e.get("correlation_id")
            if cid:
                already.add(cid)
    except Exception as exc:
        logger.debug("[broker_history] existing-import scan failed (continuing): {}", exc)

    imported = 0
    for rt in roundtrips:
        cid = f"brokerhist-{account_key}-{rt['position_id']}"
        if cid in already:
            continue
        try:
            event_store.emit(
                event_type=BROKER_HISTORY_IMPORT,
                severity="INFO",
                symbol=rt["symbol"],
                correlation_id=cid,
                source_module="broker_history",
                payload={**rt, "account": account_key, "source": "broker_history"},
            )
            imported += 1
        except Exception as exc:
            logger.debug("[broker_history] emit failed for {}: {}", cid, exc)

    if imported:
        logger.info(
            "[broker_history] imported {} broker closed-trade(s) for account '{}' into the event store",
            imported, account_key or "default",
        )
    return imported
