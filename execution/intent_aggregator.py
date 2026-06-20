"""APEX TRADER — Intent Aggregator (Phase 5).

Collects ``Intent`` objects emitted by multiple ``PositionWorker`` instances
running in parallel, deduplicates redundant modifications, resolves conflicts
when several checks target the same position, and outputs a clean,
priority-sorted list for the Action Executor (Phase 6).

Thread-safe: ``submit()`` may be called concurrently from worker threads.
``flush()`` drains the buffer and returns a resolved batch.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from execution.intents import Intent, IntentType


@dataclass
class AggregatorConfig:
    """Tuning knobs for the Intent Aggregator."""

    sl_dedup_threshold_pips: float = 0.5
    max_partial_close_fraction: float = 1.0


@dataclass
class _PositionInfo:
    """Per-position context registered before flushing."""

    direction: str
    current_sl: float
    pip_size: float


class IntentAggregator:
    """Collects, deduplicates, and conflict-resolves Intents.

    Lifecycle per evaluation cycle::

        agg = IntentAggregator(config)

        # Workers register position context, then submit intents
        agg.register_position("T1", "LONG", current_sl=1.099, pip_size=0.0001)
        agg.submit(worker.evaluate(snap))

        # After all workers complete
        actions = agg.flush()
        for intent in actions:
            executor.execute(intent)

    Conflict-resolution rules
    -------------------------
    For intents targeting the **same** ``position_ticket``:

    * **CLOSE beats everything** — any CLOSE intent discards all MODIFY_SL,
      MODIFY_TP, and PARTIAL_CLOSE intents for that position.  When multiple
      CLOSE intents exist, the earliest (by timestamp) is kept for
      deterministic behaviour.

    * **Tightest SL wins** — among MODIFY_SL intents, the most protective
      value is kept (highest for LONG, lowest for SHORT).  Falls back to
      latest timestamp when direction is unknown.

    * **Latest TP wins** — among MODIFY_TP intents, the most recent by
      timestamp is kept.

    * **PARTIAL_CLOSE stacks** — fractions are summed and capped at
      ``max_partial_close_fraction`` (default 1.0).  When the total reaches
      1.0 the partials are collapsed into a single CLOSE intent.

    SL deduplication
    ----------------
    A MODIFY_SL intent whose ``new_sl`` is within
    ``sl_dedup_threshold_pips`` of the position's *current* broker SL is
    discarded to avoid unnecessary broker traffic.
    """

    def __init__(self, config: Optional[AggregatorConfig] = None) -> None:
        self._config = config or AggregatorConfig()
        self._lock = threading.Lock()
        self._buffer: list[Intent] = []
        self._position_info: dict[str, _PositionInfo] = {}

    # ── Public API ────────────────────────────────────────────────────

    def register_position(
        self,
        ticket: str,
        direction: str,
        current_sl: float,
        pip_size: float,
    ) -> None:
        """Register position context needed for SL dedup and tightest-SL.

        Should be called once per position per evaluation cycle, *before*
        the worker's intents are submitted.
        """
        with self._lock:
            self._position_info[ticket] = _PositionInfo(
                direction=direction,
                current_sl=current_sl,
                pip_size=pip_size,
            )

    def submit(self, intents: list[Intent]) -> None:
        """Thread-safe: accept intents from a worker evaluation."""
        if not intents:
            return
        with self._lock:
            self._buffer.extend(intents)

    def flush(self) -> list[Intent]:
        """Drain buffer and return deduplicated, conflict-resolved intents.

        The returned list is sorted by priority descending (CLOSE first).
        After this call the internal buffer is empty and ready for the next
        evaluation cycle.
        """
        with self._lock:
            buffer = list(self._buffer)
            self._buffer.clear()
            info = dict(self._position_info)
            self._position_info.clear()

        if not buffer:
            return []

        opens = [i for i in buffer if i.intent_type == IntentType.OPEN]
        managed = [i for i in buffer if i.intent_type != IntentType.OPEN]

        by_position: dict[str, list[Intent]] = defaultdict(list)
        for intent in managed:
            by_position[intent.position_ticket].append(intent)

        result: list[Intent] = []
        for ticket, pos_intents in by_position.items():
            result.extend(
                self._resolve_position(ticket, pos_intents, info.get(ticket)),
            )

        result.extend(self._resolve_opens(opens))

        result.sort(key=lambda i: i.priority, reverse=True)
        return result

    # ── Internal resolution ───────────────────────────────────────────

    def _resolve_position(
        self,
        ticket: str,
        intents: list[Intent],
        info: Optional[_PositionInfo],
    ) -> list[Intent]:
        closes = [i for i in intents if i.intent_type == IntentType.CLOSE]
        if closes:
            return [min(closes, key=lambda i: i.timestamp)]

        resolved: list[Intent] = []

        sl_intents = [i for i in intents if i.intent_type == IntentType.MODIFY_SL]
        if sl_intents:
            best = self._pick_tightest_sl(sl_intents, info)
            if best is not None and not self._should_skip_sl(best, info):
                resolved.append(best)

        tp_intents = [i for i in intents if i.intent_type == IntentType.MODIFY_TP]
        if tp_intents:
            resolved.append(max(tp_intents, key=lambda i: i.timestamp))

        partial_intents = [
            i for i in intents if i.intent_type == IntentType.PARTIAL_CLOSE
        ]
        if partial_intents:
            resolved.extend(self._resolve_partials(ticket, partial_intents))

        return resolved

    def _pick_tightest_sl(
        self,
        intents: list[Intent],
        info: Optional[_PositionInfo],
    ) -> Optional[Intent]:
        if not intents:
            return None
        if len(intents) == 1:
            return intents[0]
        if info is None:
            return max(intents, key=lambda i: i.timestamp)
        is_long = info.direction.upper() in ("BUY", "LONG")
        if is_long:
            return max(intents, key=lambda i: i.new_sl if i.new_sl is not None else 0.0)
        return min(
            intents,
            key=lambda i: i.new_sl if i.new_sl is not None else float("inf"),
        )

    def _should_skip_sl(
        self, intent: Intent, info: Optional[_PositionInfo],
    ) -> bool:
        if info is None or info.current_sl <= 0 or info.pip_size <= 0:
            return False
        new_sl = intent.new_sl
        if new_sl is None:
            return True
        diff_pips = abs(new_sl - info.current_sl) / info.pip_size
        return diff_pips < self._config.sl_dedup_threshold_pips

    def _resolve_partials(
        self, ticket: str, intents: list[Intent],
    ) -> list[Intent]:
        cap = self._config.max_partial_close_fraction
        total = sum(i.close_fraction or 0.0 for i in intents)

        if total >= cap:
            sources = ", ".join(i.source for i in intents)
            return [
                Intent.close(
                    symbol=intents[0].symbol,
                    ticket=ticket,
                    source="aggregated_partial_close",
                    reason=f"Partial closes sum to {total:.0%} (>={cap:.0%}) — full close [{sources}]",
                    timestamp=intents[0].timestamp,
                ),
            ]
        return list(intents)

    def _resolve_opens(self, opens: list[Intent]) -> list[Intent]:
        """Deduplicate OPEN intents.

        Entries carry no position ticket, so they are keyed separately from
        management intents — by idempotency key when present, else by
        symbol+direction. Repeats of the same intended entry in one cycle
        collapse to the most recent, so a duplicate decision can never enqueue
        two orders for the same setup.
        """
        if not opens:
            return []
        by_key: dict[str, Intent] = {}
        for i in opens:
            key = i.idempotency_key or f"{i.symbol}|{(i.direction or '').upper()}"
            cur = by_key.get(key)
            if cur is None or i.timestamp > cur.timestamp:
                by_key[key] = i
        return list(by_key.values())


def should_skip_sl_update(
    current_sl: float,
    new_sl: float,
    pip_size: float,
    threshold_pips: float = 0.5,
) -> bool:
    """Return True if *new_sl* is within *threshold_pips* of *current_sl*.

    Standalone helper usable outside the aggregator (e.g. by the Action
    Executor for a final guard).
    """
    if pip_size <= 0:
        return False
    return abs(new_sl - current_sl) / pip_size < threshold_pips
