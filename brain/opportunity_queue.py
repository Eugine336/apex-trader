"""
APEX TRADER — Global Opportunity Queue (GAP 1)

When several instruments signal at once, entries currently reach the execution
pipeline in TICK-ARRIVAL order — a mediocre EURUSD setup that fires 50 ms before
a stellar GBPJPY setup gets capital first. This queue fixes that: it collects
every instrument's scored entry during a short window, ranks them against each
other (via :class:`brain.cross_instrument_ranker.CrossInstrumentRanker`), and
dispatches them best-EV-first.

Contract — DISABLED IS A PERFECT PASS-THROUGH:
  When ``enabled`` is False, :meth:`submit` calls ``dispatch`` synchronously, in
  the caller's thread, with the exact same arguments — byte-for-byte identical to
  the pre-queue direct call. No window, no thread, no reordering. The five
  cross-instrument layers are opt-in; the default system is unchanged.

When enabled, ``submit`` buffers the decision and arms a single drain timer for
``window_ms``. On drain (a daemon timer thread) the buffer is ranked and each
survivor is dispatched in rank order, with its cross-instrument rank stamped onto
the decision dict (``cross_rank`` / ``cross_rank_total`` / ``cross_adjusted_ev``)
so the quality sizer (GAP 3) can size by relative quality. Dispatch (broker I/O)
runs OUTSIDE the lock so a slow entry never blocks new submissions.

Leaf-ish: stdlib + loguru; the ranker is injected (duck-typed), so no import
cycle.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger


@dataclass
class QueuedOpportunity:
    """One instrument's scored entry awaiting cross-instrument dispatch."""

    symbol: str
    direction: str
    ev: float
    confidence: float
    coherence: float
    source: str
    decision: dict
    allocation: Any = None
    submitted_at: float = 0.0
    # Set by the cross-instrument ranker (GAP 2).
    adjusted_ev: float = 0.0

    @staticmethod
    def from_decision(decision: dict, allocation: Any = None, *, now: float = 0.0) -> "QueuedOpportunity":
        ev = float(decision.get("candidate_ev", 0.0) or 0.0)
        conf = float(decision.get("candidate_score", 0.0) or 0.0)
        if conf <= 0.0:
            # Fall back to conviction (0..100) as a confidence proxy.
            conf = float(decision.get("conviction", 0.0) or 0.0) / 100.0
        return QueuedOpportunity(
            symbol=str(decision.get("symbol", "") or ""),
            direction=str(decision.get("direction", "") or ""),
            ev=ev,
            confidence=conf,
            coherence=float(decision.get("coherence", 0.0) or 0.0),
            source=str(decision.get("source", "") or ""),
            decision=decision,
            allocation=allocation,
            submitted_at=now,
            adjusted_ev=ev,
        )


class GlobalOpportunityQueue:
    """Collect → rank → dispatch entry candidates across all instruments."""

    def __init__(
        self,
        *,
        dispatch: Callable[..., None],
        enabled: bool = False,
        window_ms: int = 1000,
        ranker: Optional[Any] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dispatch = dispatch
        self.enabled = bool(enabled)
        self._window_s = max(0.0, float(window_ms) / 1000.0)
        self._ranker = ranker
        self._clock = clock

        self._lock = threading.Lock()
        self._pending: list[QueuedOpportunity] = []
        self._timer: Optional[threading.Timer] = None
        self._running = True

        self._submitted = 0
        self._dispatched = 0
        self._windows = 0

    # ── Submission ────────────────────────────────────────────────────────

    def submit(self, decision: dict, allocation: Any = None) -> None:
        """Submit a passed entry decision.

        Disabled → dispatch immediately (identity pass-through). Enabled → buffer
        and arm the drain timer.
        """
        if not self.enabled or not self._running:
            self._dispatch_one(decision, allocation)
            return

        item = QueuedOpportunity.from_decision(
            decision, allocation, now=self._clock(),
        )
        with self._lock:
            self._pending.append(item)
            self._submitted += 1
            if self._timer is None:
                self._timer = threading.Timer(self._window_s, self._on_window_elapsed)
                self._timer.daemon = True
                self._timer.start()

    # ── Drain ─────────────────────────────────────────────────────────────

    def _on_window_elapsed(self) -> None:
        try:
            self.flush()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[opp-queue] drain failed: {}", exc)

    def flush(self) -> int:
        """Rank and dispatch every buffered candidate now. Returns dispatched count."""
        with self._lock:
            batch = self._pending
            self._pending = []
            self._timer = None
        if not batch:
            return 0

        self._windows += 1
        ranked = batch
        if self._ranker is not None:
            try:
                ranked = self._ranker.rank(batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[opp-queue] ranker failed, FIFO order: {}", exc)
                ranked = batch

        total = len(ranked)
        if total > 1:
            logger.info(
                "[opp-queue] dispatching {} cross-instrument candidate(s) best-first: {}",
                total,
                " > ".join(
                    f"{o.symbol}:{o.direction}({o.adjusted_ev:+.2f}R)" for o in ranked
                ),
            )
        dispatched = 0
        for idx, opp in enumerate(ranked):
            try:
                opp.decision["cross_rank"] = idx
                opp.decision["cross_rank_total"] = total
                opp.decision["cross_adjusted_ev"] = float(opp.adjusted_ev)
            except Exception:
                pass
            self._dispatch_one(opp.decision, opp.allocation)
            dispatched += 1
        return dispatched

    def _dispatch_one(self, decision: dict, allocation: Any) -> None:
        try:
            if allocation is not None:
                self._dispatch(decision, allocation)
            else:
                self._dispatch(decision)
            self._dispatched += 1
        except Exception:
            logger.exception("[opp-queue] dispatch failed for {}", decision.get("symbol"))

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Flush any pending candidates and stop accepting new windows."""
        with self._lock:
            t = self._timer
            self._timer = None
        if t is not None:
            t.cancel()
        try:
            self.flush()
        except Exception:
            logger.exception("[opp-queue] stop flush failed")
        self._running = False

    def stats(self) -> dict:
        with self._lock:
            pending = len(self._pending)
        return {
            "enabled": self.enabled,
            "window_ms": int(self._window_s * 1000),
            "submitted": self._submitted,
            "dispatched": self._dispatched,
            "windows": self._windows,
            "pending": pending,
        }


__all__ = ["GlobalOpportunityQueue", "QueuedOpportunity"]
