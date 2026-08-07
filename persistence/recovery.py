"""Event-log startup recovery / reconciliation (Phase 3).

On startup, fold the replayable event log (seq-ordered, see ``event_store`` and
``EventReplayer``) into the set of order_ids the log believes are still open
(entries minus closes), then reconcile that against the broker's live open
positions.

Strictly **read-only / observability**: it logs (and optionally emits) the
discrepancies so a crash window can be noticed — positions the log thinks are
open that the broker no longer shows (likely closed during downtime / a missed
close event), and broker positions with no entry record in the log (orphans).
It never opens or closes anything; remediation is left to a human/operator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Set

from loguru import logger

from persistence import domain_events as DE
from persistence.event_replayer import EventReplayer

# Lifecycle signals.  An order_id is "open" once an entry event is seen and
# "closed" once a close event is seen, folded in seq order.
_OPEN_TYPES = frozenset({DE.ORDER_FILLED, DE.TRADE_OPEN})
_CLOSE_TYPES = frozenset({DE.TRADE_CLOSE, DE.TRADE_CLOSE_DERIVED})


def _order_id(event: Dict[str, Any]) -> str:
    """Extract the order_id from an event row's JSON payload (falling back to a
    top-level key for robustness)."""
    payload: Dict[str, Any] = {}
    pj = event.get("payload_json")
    if isinstance(pj, str) and pj:
        try:
            loaded = json.loads(pj)
            if isinstance(loaded, dict):
                payload = loaded
        except (TypeError, ValueError):
            payload = {}
    elif isinstance(pj, dict):
        payload = pj
    return str(payload.get("order_id") or event.get("order_id") or "")


class PositionLifecycleProjection:
    """Folds lifecycle events (in ``seq`` order) into the open-position set.

    ORDER_FILLED / TRADE_OPEN open an order_id; TRADE_CLOSE[_DERIVED] close it.
    Idempotent: repeated opens for the same id collapse, and a close for an
    unknown id is a no-op.
    """

    def __init__(self) -> None:
        self._open: Dict[str, Dict[str, Any]] = {}

    def apply(self, event: Dict[str, Any]) -> None:
        et = event.get("event_type")
        is_open = et in _OPEN_TYPES
        is_close = et in _CLOSE_TYPES
        if not (is_open or is_close):
            return
        oid = _order_id(event)
        if not oid:
            return
        if is_open:
            self._open[oid] = {
                "symbol": event.get("symbol", ""),
                "seq": event.get("seq"),
            }
        else:
            self._open.pop(oid, None)

    def open_order_ids(self) -> Set[str]:
        return set(self._open)

    def details(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._open)


@dataclass
class ReconciliationReport:
    """Outcome of reconciling the event-log open set against the broker."""

    log_open: Set[str]
    broker_open: Set[str]
    missing_from_broker: Set[str]  # log says open, broker doesn't show
    orphan_at_broker: Set[str]  # broker shows, no entry in the log
    store_degraded: bool = False  # backing event DB is corrupt/unwritable

    @property
    def is_clean(self) -> bool:
        return not self.missing_from_broker and not self.orphan_at_broker


def reconcile(
    log_open: Iterable[str],
    broker_open: Iterable[str],
) -> ReconciliationReport:
    lo = {str(x) for x in log_open if x}
    bo = {str(x) for x in broker_open if x}
    return ReconciliationReport(
        log_open=lo,
        broker_open=bo,
        missing_from_broker=lo - bo,
        orphan_at_broker=bo - lo,
    )


def build_log_open_set(
    store: Any,
    *,
    consumer: str = "startup_recovery",
    checkpoint_db: Optional[str] = None,
) -> Set[str]:
    """Full-fold the event log into the order_ids it believes are open.

    Drives the replayable log via ``EventReplayer`` reset to ``seq`` 0 so each
    call reconstructs the *current* open set (a one-shot reconstruction, not a
    resume-from-checkpoint).
    """
    proj = PositionLifecycleProjection()
    replayer = EventReplayer(store, consumer=consumer, checkpoint_db=checkpoint_db)
    try:
        replayer.reset(0)
        replayer.drain(proj.apply)
    finally:
        replayer.close()
    return proj.open_order_ids()


def run_startup_recovery(
    store: Any,
    broker_order_ids: Iterable[str],
    *,
    checkpoint_db: Optional[str] = None,
    emit: Optional[Callable[["ReconciliationReport"], None]] = None,
) -> ReconciliationReport:
    """Reconcile the event-log open set against live broker positions.

    Read-only: logs discrepancies (and optionally calls *emit* with the report
    for telemetry); never opens or closes positions.
    """
    log_open = build_log_open_set(store, checkpoint_db=checkpoint_db)
    report = reconcile(log_open, broker_order_ids)
    # If the event DB is corrupt, the folded "open" set is unreliable: a
    # malformed DB yields zero log-open and would otherwise report "clean".
    # Surface it loudly so the caller can degrade safety rather than trust an
    # empty reconciliation.
    store_degraded = bool(getattr(store, "is_degraded", False))
    report.store_degraded = store_degraded
    if store_degraded:
        logger.critical(
            "[recovery] event store is DEGRADED (corrupt/unwritable DB) — "
            "reconciliation against the event log is UNRELIABLE; {} broker "
            "position(s) seen. Verify open positions manually.",
            len(report.broker_open),
        )
    if report.is_clean:
        logger.info(
            "[recovery] event-log reconciliation clean — {} open position(s) "
            "match the log",
            len(report.broker_open),
        )
    else:
        logger.warning(
            "[recovery] event-log reconciliation MISMATCH | "
            "log-open-not-at-broker={} | broker-orphans-not-in-log={}",
            sorted(report.missing_from_broker),
            sorted(report.orphan_at_broker),
        )
        if emit is not None:
            try:
                emit(report)
            except Exception as exc:
                logger.debug("[recovery] emit failed: {}", exc)
    return report
